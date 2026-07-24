"""Orquestrador do pipeline completo.

Fluxo de uma checagem:
  1. Verifica se ha creditos (uma request ao KAYAK = 5 creditos).
  2. Consulta o KAYAK sem filtrar companhia, faz o parse da viagem mais
     barata, compara com o historico, salva e envia um embed.
  3. Qualquer falha vira embed vermelho no Discord em vez de silencio.

Uso:
    python -m src.main once                  # checagem agora, sem janela
    python -m src.main once --mode morning   # so dentro da janela da manha
    python -m src.main once --mode evening   # so na vespera do reset, a noite
    python -m src.main schedule    # deixa rodando periodicamente (APScheduler)
    python -m src.main status      # historico local, sem tocar na rede
    python -m src.main credits     # saldo real na GeckoAPI e reconcilia o ledger
    python -m src.main dry-run     # testa o embed no Discord sem chamar a API
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import NamedTuple

from . import compare, scheduling, storage
from .config import SOURCE, ConfigError, Settings, load_settings, setup_logging
from .credits_tracker import CreditBudgetExceeded, CreditsTracker
from .discord_bot import DiscordError, notify_error, notify_offer
from .fetch_prices import GeckoAPIError, GeckoAPIParseError, GeckoClient, fetch_kayak
from .models import Leg, Offer
from .storage import DBConnection

logger = logging.getLogger("main")


class Resultado(NamedTuple):
    """Desfecho de uma execucao, para o codigo de saida refletir a realidade.

    Pular por estar fora da janela nao e erro. Consultar o preco e nao
    conseguir entregar a mensagem E erro: num bot cujo unico produto e a
    notificacao, silencio indistinguivel de sucesso e o pior desfecho.
    """

    executou: bool
    # "tudo que precisava ser entregue foi entregue". Um pulo de janela nao
    # tinha nada a entregar, entao conta como True.
    notificou: bool
    motivo: str = ""


def run_check(
    settings: Settings, conn: DBConnection, modo: str = scheduling.MODO_MANUAL
) -> Resultado:
    """Executa uma checagem.

    `modo` decide qual janela vale: `morning` e a checagem regular, `evening` so
    roda na vespera do reset de creditos, `manual` ignora janela.
    """
    tracker = CreditsTracker(conn, monthly_budget=settings.monthly_credit_budget)

    logger.info(
        "Iniciando checagem | %s | ida %s | volta %s",
        settings.route_label,
        settings.departure_date,
        settings.return_date,
    )
    logger.info("Creditos no mes (ledger local): %s", tracker.summary())

    # O saldo da GeckoAPI e a verdade; o ledger local conta por tentativa e
    # erra para cima. Reconciliar antes de decidir evita recusar uma checagem
    # que na verdade cabia.
    client = GeckoClient(
        api_key=settings.geckoapi_key,
        timeout=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        retry_delay_seconds=settings.http_retry_delay_seconds,
        credit_hook=lambda label: tracker.record(settings.credits_per_request, label),
        refund_hook=lambda label: tracker.refund(settings.credits_per_request, label),
    )
    saldo_atual: int | None = None
    try:
        saldo = client.get_credits()
        saldo_atual = saldo.current_credits
        tracker.reconcile(saldo_atual)
        storage.save_balance_observation(
            conn, saldo_atual, saldo.plan_id, datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    except GeckoAPIError as exc:
        # Sem o saldo real seguimos com o ledger local, que erra para o lado
        # seguro. Nao vale abortar a checagem por causa disso.
        logger.warning("Nao consegui consultar o saldo real (%s); usando o ledger local.", exc)

    # Gate de janela: o cron do Actions dispara em UTC e pode atrasar, entao a
    # decisao final e tomada aqui, em horario local e com o saldo em maos.
    decisao = scheduling.decide(
        modo=modo,
        agora_utc=datetime.now(timezone.utc),
        saldo=saldo_atual,
        custo=settings.credits_per_full_check,
        timezone=settings.timezone,
        janela_manha=settings.morning_window,
        janela_noite=settings.evening_window,
        reset_day=settings.credit_reset_day,
        noite_habilitada=settings.evening_burst_enabled,
    )
    if not decisao:
        # Sair calado de proposito: um "fora da janela" nao e erro e nao deve
        # virar mensagem no Discord a cada disparo do cron da noite.
        logger.info("Checagem nao executada: %s", decisao.motivo)
        # notificou=True porque nao havia nada a entregar: pular a janela e o
        # comportamento desejado, nao uma entrega que falhou.
        return Resultado(executou=False, notificou=True, motivo=decisao.motivo)
    logger.info("Executando: %s", decisao.motivo)

    try:
        tracker.ensure_budget(settings.credits_per_full_check, "checagem")
    except CreditBudgetExceeded as exc:
        logger.error("Checagem abortada: %s", exc)
        avisou = notify_error(settings.discord_webhook_url, "ORCAMENTO", exc, settings.route_label)
        return Resultado(executou=False, notificou=avisou, motivo=str(exc))

    # Teto por execucao, alem do teto mensal. Sem ele, uma sequencia de erros
    # 5xx com retry poderia consumir varias checagens do mes numa tacada so.
    usado_no_inicio = tracker.used_this_month()

    def budget_guard() -> bool:
        if not tracker.can_spend(settings.credits_per_request):
            logger.warning("Orcamento do mes esgotado.")
            return False
        gasto_neste_run = tracker.used_this_month() - usado_no_inicio
        if gasto_neste_run + settings.credits_per_request > settings.max_credits_per_run:
            logger.warning(
                "Teto desta execucao atingido (%s de %s creditos); abortando.",
                gasto_neste_run,
                settings.max_credits_per_run,
            )
            return False
        return True

    client.budget_guard = budget_guard

    try:
        offer = fetch_kayak(client, settings)
    except GeckoAPIParseError as exc:
        # O credito ja foi cobrado e a resposta veio: o payload e a unica coisa
        # aproveitavel que sobrou. Salvamos no banco e no log para corrigir o
        # parser depois sem precisar gastar credito de novo.
        logger.error("Parser nao entendeu a resposta do KAYAK: %s", exc)
        if exc.payload is not None:
            storage.save_failed_extraction(conn, SOURCE, str(exc), exc.payload)
            logger.error(
                "Resposta bruta (para corrigir o parser):\n%s",
                json.dumps(exc.payload, ensure_ascii=False)[:20000],
            )
        avisou = notify_error(settings.discord_webhook_url, SOURCE, exc, settings.route_label)
        return Resultado(executou=False, notificou=avisou, motivo=str(exc))
    except GeckoAPIError as exc:
        logger.error("Falha na consulta ao KAYAK: %s", exc)
        avisou = notify_error(settings.discord_webhook_url, SOURCE, exc, settings.route_label)
        return Resultado(executou=False, notificou=avisou, motivo=str(exc))

    # Compara ANTES de salvar, senao o preco atual entra no proprio MIN().
    comparison = compare.compare_with_history(conn, offer.airline, offer.price)
    storage.save_offer(conn, offer)

    logger.info(
        "Mais barata: %s por %.2f via %s",
        offer.airline,
        offer.price,
        offer.provider or "vendedor nao informado",
    )

    notificou = True
    motivo = ""
    try:
        notify_offer(
            webhook_url=settings.discord_webhook_url,
            offer=offer,
            comparison=comparison,
            route_label=settings.route_label,
            credits_summary=tracker.summary(),
        )
    except DiscordError as exc:
        # O dado ja esta salvo, entao a checagem nao se perde - mas a mensagem
        # e o produto do bot, e nao entregar precisa ficar visivel.
        logger.error("Nao consegui notificar no Discord: %s", exc)
        notificou = False
        motivo = str(exc)

    logger.info("Checagem finalizada | creditos: %s", tracker.summary())
    return Resultado(executou=True, notificou=notificou, motivo=motivo)


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------


def _open_db(settings: Settings):
    """Abre o banco conforme a config: Turso se houver URL, SQLite local senao."""
    logger.info("Banco: %s", settings.db_label)
    return storage.get_connection(
        settings.db_path,
        remote_url=settings.turso_database_url,
        auth_token=settings.turso_auth_token,
    )


def cmd_once(settings: Settings, modo: str = scheduling.MODO_MANUAL) -> int:
    with _open_db(settings) as conn:
        resultado = run_check(settings, conn, modo)

    if not resultado.notificou:
        # Vermelho no Actions de proposito: sem mensagem entregue, voce nao
        # teria como distinguir "pulou a janela" de "quebrou".
        logger.error("Nada foi entregue no Discord. Motivo: %s", resultado.motivo)
        return 1

    # Pular por estar fora da janela nao e falha: sai 0 para o cron da noite
    # nao pintar o Actions de vermelho todo dia.
    return 0


def cmd_schedule(settings: Settings) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone="America/Belem")

    def job() -> None:
        with _open_db(settings) as conn:
            run_check(settings, conn)

    scheduler.add_job(
        job,
        trigger=IntervalTrigger(days=settings.check_interval_days),
        id="flight_check",
        name=f"Checagem {settings.route_label}",
        max_instances=1,
        coalesce=True,  # se o processo ficou parado, roda uma vez so ao voltar
        misfire_grace_time=3600,
        next_run_time=datetime.now(timezone.utc),  # roda uma vez ja na subida
    )

    logger.info(
        "Scheduler ativo | a cada %s dias | %s creditos por checagem",
        settings.check_interval_days,
        settings.credits_per_full_check,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler encerrado pelo usuario.")
    return 0


def cmd_status(settings: Settings) -> int:
    """Relatorio: nao chama a GeckoAPI, nao gasta credito."""
    with _open_db(settings) as conn:
        tracker = CreditsTracker(conn, monthly_budget=settings.monthly_credit_budget)
        summary = tracker.summary()
        checagens_restantes = summary["remaining"] // settings.credits_per_full_check

        print(f"\nRota: {settings.route_label}")
        print(f"Ida: {settings.departure_date} | Volta: {settings.return_date}\n")
        print(
            f"Creditos em {summary['year_month']}: {summary['used']}/{summary['budget']} "
            f"(restam {summary['remaining']} = {checagens_restantes} checagens)\n"
        )

        total = storage.count_checks(conn)
        menor = compare.historical_low(conn)
        menor_texto = f"R$ {menor:,.2f}" if menor is not None else "sem registro"
        print(f"{total} checagens registradas | menor preco: {menor_texto}\n")

        por_companhia = compare.lowest_by_airline(conn)
        if por_companhia:
            print("Menor preco por companhia:")
            for airline, preco in sorted(por_companhia.items(), key=lambda kv: kv[1]):
                print(f"  {airline:<28} R$ {preco:>9,.2f}")
            print()

        dias_reset = storage.detect_reset_days(conn)
        print(f"Reset de creditos: dia {settings.credit_reset_day} (suposicao)")
        if dias_reset:
            print(f"  Saldo subiu, na pratica, no(s) dia(s): {dias_reset}")
            if settings.credit_reset_day not in dias_reset:
                print(f"  ATENCAO: ajuste CREDIT_RESET_DAY para {dias_reset[-1]}")
        else:
            print("  Nenhum reset observado ainda; o bot registra o saldo a cada checagem.")
        print()

        print("Ultimas checagens:")
        for row in storage.get_history(conn, limit=10):
            print(f"  {row['checked_at']} | R$ {row['price']:>9,.2f} | {row['airline']}")
        print()
    return 0


def cmd_dry_run(settings: Settings) -> int:
    """Envia um embed de exemplo ao Discord sem gastar creditos da GeckoAPI."""
    logger.info("Dry-run: gerando oferta ficticia e enviando ao Discord.")
    offer = Offer(
        airline="GOL Linhas Aereas",
        price=1234.56,
        currency="BRL",
        outbound=Leg("BEL", "NAT", "2026-12-27T08:15:00Z", "2026-12-27T12:40:00Z", 265, 1),
        inbound=Leg("NAT", "BEL", "2027-01-05T14:00:00Z", "2027-01-05T17:05:00Z", 185, 0),
        provider="123Milhas",
        provider_is_direct=False,
        booking_url="https://www.kayak.com.br",
        seats_remaining=2,
        total_options=1111,
        raw_response={"dry_run": True},
    )
    comparison = compare.build_comparison("GOL Linhas Aereas", 1234.56, historical_low=1400.00)
    try:
        notify_offer(settings.discord_webhook_url, offer, comparison, settings.route_label, None)
    except DiscordError as exc:
        logger.error("Dry-run falhou: %s", exc)
        return 1
    logger.info("Dry-run enviado com sucesso.")
    return 0


def cmd_credits(settings: Settings) -> int:
    """Consulta o saldo real na GeckoAPI e reconcilia o ledger local.

    Chama GET /v1/me/credits, que e endpoint de conta e nao despacha extracao.
    Se voce rodar duas vezes seguidas e o saldo cair, e porque ele cobra - e ai
    vale plugar o credit_hook em `GeckoClient.get_credits`.
    """
    client = GeckoClient(api_key=settings.geckoapi_key, timeout=settings.http_timeout_seconds)
    try:
        saldo = client.get_credits()
    except GeckoAPIError as exc:
        logger.error("Falha ao consultar o saldo: %s", exc)
        return 1

    print(f"\nSaldo atual:  {saldo.current_credits} creditos")
    if saldo.plan_id:
        print(f"Plano:        {saldo.plan_id}")
    print("\nConsumo recente (pela GeckoAPI):")
    for rotulo, valor in (
        ("ultimas 24h", saldo.last_24h),
        ("ultimos 7d ", saldo.last_7d),
        ("ultimos 30d", saldo.last_30d),
    ):
        print(f"  {rotulo}: {valor if valor is not None else 'nao informado'}")

    checagens = saldo.current_credits // settings.credits_per_full_check
    print(f"\nDa para {checagens} checagens com o saldo atual.\n")

    with _open_db(settings) as conn:
        tracker = CreditsTracker(conn, monthly_budget=settings.monthly_credit_budget)
        antes = tracker.used_this_month()
        tracker.reconcile(saldo.current_credits)
        depois = tracker.used_this_month()
        if antes != depois:
            print(f"Ledger local reconciliado: {antes} -> {depois} creditos usados no mes.\n")
        else:
            print("Ledger local ja batia com o saldo real.\n")
    return 0


COMMANDS = {
    "credits": cmd_credits,
    "once": cmd_once,
    "schedule": cmd_schedule,
    "status": cmd_status,
    "dry-run": cmd_dry_run,
}


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flight-bot", description="Monitor de precos de voos com aviso no Discord"
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="acao a executar")
    parser.add_argument(
        "--mode",
        choices=scheduling.MODOS,
        default=scheduling.MODO_MANUAL,
        help=(
            "janela de execucao do `once`: morning (checagem regular), "
            "evening (so na vespera do reset de creditos) ou manual (sem janela)"
        ),
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    args = parser.parse_args(argv)

    # `status` e o unico que roda sem segredos configurados.
    require_secrets = args.command != "status"

    try:
        settings = load_settings(require_secrets=require_secrets)
    except ConfigError as exc:
        setup_logging("INFO")
        logger.error("%s", exc)
        return 2

    setup_logging(args.log_level or settings.log_level)
    if args.command == "once":
        return cmd_once(settings, args.mode)
    return COMMANDS[args.command](settings)


if __name__ == "__main__":
    sys.exit(cli())
