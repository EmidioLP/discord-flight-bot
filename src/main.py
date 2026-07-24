"""Orquestrador do pipeline completo.

Fluxo de uma checagem:
  1. Verifica se ha creditos para a checagem inteira (LATAM + Azul).
  2. Para cada companhia: consulta a GeckoAPI (creditos sao debitados por
     tentativa, nao por sucesso), faz o parse, compara com o historico,
     salva no banco e envia o embed.
  3. Falha de uma companhia nao impede a outra: cada uma e isolada.

Uso:
    python -m src.main once        # roda uma checagem agora
    python -m src.main schedule    # deixa rodando a cada 4 dias (APScheduler)
    python -m src.main status      # mostra creditos e historico, sem gastar nada
    python -m src.main dry-run     # testa o embed no Discord sem chamar a API
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Callable, NamedTuple

from . import compare, storage
from .storage import DBConnection
from .config import AIRLINES, ConfigError, Settings, load_settings, setup_logging
from .credits_tracker import CreditBudgetExceeded, CreditsTracker
from .discord_bot import DiscordError, notify_error, notify_offer
from .fetch_prices import FETCHERS, GeckoAPIError, GeckoAPIParseError, GeckoClient
from .models import Leg, Offer

logger = logging.getLogger("main")


class AirlineResult(NamedTuple):
    """Desfecho da consulta de uma companhia, antes de decidir o que notificar."""

    airline: str
    offer: Offer | None = None
    comparison: compare.Comparison | None = None
    error: str | None = None


def run_check(settings: Settings, conn: DBConnection) -> int:
    """Executa uma checagem completa. Devolve quantas companhias tiveram sucesso."""
    tracker = CreditsTracker(conn, monthly_budget=settings.monthly_credit_budget)

    logger.info(
        "Iniciando checagem | %s | ida %s | volta %s",
        settings.route_label,
        settings.departure_date,
        settings.return_date,
    )
    logger.info("Creditos no mes: %s", tracker.summary())

    try:
        tracker.ensure_budget(settings.credits_per_full_check, "checagem completa")
    except CreditBudgetExceeded as exc:
        logger.error("Checagem abortada: %s", exc)
        notify_error(settings.discord_webhook_url, "ORCAMENTO", exc, settings.route_label)
        return 0

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
                "Teto desta execucao atingido (%s de %s creditos); abortando o resto.",
                gasto_neste_run,
                settings.max_credits_per_run,
            )
            return False
        return True

    client = GeckoClient(
        api_key=settings.geckoapi_key,
        timeout=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        retry_delay_seconds=settings.http_retry_delay_seconds,
        credit_hook=lambda label: tracker.record(settings.credits_per_request, label),
        refund_hook=lambda label: tracker.refund(settings.credits_per_request, label),
        budget_guard=budget_guard,
    )

    resultados: list[AirlineResult] = []
    for airline in AIRLINES:
        try:
            resultados.append(
                _check_airline(settings, conn, tracker, client, airline, budget_guard)
            )
        except Exception as exc:  # noqa: BLE001 - uma companhia nao pode derrubar a outra
            logger.exception("Erro inesperado ao processar %s", airline)
            resultados.append(AirlineResult(airline=airline, error=f"{type(exc).__name__}: {exc}"))

    sucessos = [r for r in resultados if r.offer is not None]
    falhas = [(r.airline, r.error or "erro desconhecido") for r in resultados if r.offer is None]

    _notificar(settings, tracker, sucessos, falhas)

    logger.info(
        "Checagem finalizada | %s/%s companhias | creditos: %s",
        len(sucessos),
        len(AIRLINES),
        tracker.summary(),
    )
    return len(sucessos)


def _notificar(
    settings: Settings,
    tracker: CreditsTracker,
    sucessos: list[AirlineResult],
    falhas: list[tuple[str, str]],
) -> None:
    """Envia UMA mensagem por checagem: a companhia mais barata.

    As duas companhias continuam sendo salvas no banco - so a notificacao e
    que e enxuta. Falhas viram uma nota dentro do mesmo embed; so quando
    nenhuma companhia responde e que sai um embed de erro no lugar.
    """
    if not sucessos:
        logger.error("Nenhuma companhia respondeu; avisando o erro no Discord.")
        detalhe = "\n".join(f"{airline}: {erro}" for airline, erro in falhas)
        notify_error(
            settings.discord_webhook_url,
            "TODAS AS COMPANHIAS",
            RuntimeError(detalhe or "sem detalhes"),
            settings.route_label,
        )
        return

    vencedor = min(sucessos, key=lambda r: r.offer.price)  # type: ignore[union-attr]
    alternativas = [
        (r.airline, r.offer.price)  # type: ignore[union-attr]
        for r in sucessos
        if r is not vencedor
    ]
    logger.info(
        "Mais barata: %s por %.2f | alternativas: %s",
        vencedor.airline,
        vencedor.offer.price,  # type: ignore[union-attr]
        alternativas,
    )

    try:
        notify_offer(
            webhook_url=settings.discord_webhook_url,
            offer=vencedor.offer,
            comparison=vencedor.comparison,
            route_label=settings.route_label,
            credits_summary=tracker.summary(),
            alternatives=alternativas,
            failures=falhas,
        )
    except DiscordError as exc:
        # Os dados ja estao salvos; falhar o envio nao invalida a checagem.
        logger.error("Nao consegui notificar no Discord: %s", exc)


def _check_airline(
    settings: Settings,
    conn: DBConnection,
    tracker: CreditsTracker,
    client: GeckoClient,
    airline: str,
    budget_guard: Callable[[], bool],
) -> AirlineResult:
    """Consulta, compara e salva uma companhia. Nao notifica.

    A notificacao e decidida depois, em `_notificar`, que precisa ver as duas
    companhias para saber qual e a mais barata.
    """
    logger.info("--- %s ---", airline)

    if not budget_guard():
        logger.warning("Sem creditos disponiveis para consultar %s; pulando.", airline)
        return AirlineResult(airline=airline, error="sem creditos disponiveis")

    try:
        offer = FETCHERS[airline](client, settings)
    except GeckoAPIParseError as exc:
        # O credito ja foi cobrado e a resposta veio: o payload e a unica coisa
        # aproveitavel que sobrou. Salvamos no banco e no log para corrigir o
        # parser depois sem precisar gastar credito de novo.
        logger.error("Parser da %s nao entendeu a resposta: %s", airline, exc)
        if exc.payload is not None:
            storage.save_failed_extraction(conn, airline, str(exc), exc.payload)
            logger.error(
                "Resposta bruta da %s (para corrigir o parser):\n%s",
                airline,
                json.dumps(exc.payload, ensure_ascii=False)[:20000],
            )
        return AirlineResult(airline=airline, error=str(exc))
    except GeckoAPIError as exc:
        logger.error("Falha na consulta da %s: %s", airline, exc)
        return AirlineResult(airline=airline, error=str(exc))

    # Compara ANTES de salvar, senao o preco atual entra no proprio MIN().
    comparison = compare.compare_with_history(conn, airline, offer.price)
    storage.save_offer(conn, offer)

    return AirlineResult(airline=airline, offer=offer, comparison=comparison)


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


def cmd_once(settings: Settings) -> int:
    with _open_db(settings) as conn:
        successes = run_check(settings, conn)
    return 0 if successes else 1


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
        checks_left = summary["remaining"] // settings.credits_per_full_check

        print(f"\nRota: {settings.route_label}")
        print(f"Ida: {settings.departure_date} | Volta: {settings.return_date}\n")
        print(
            f"Creditos em {summary['year_month']}: {summary['used']}/{summary['budget']} "
            f"(restam {summary['remaining']} = {checks_left} checagens completas)\n"
        )

        lows = compare.lowest_by_airline(conn)
        for airline in AIRLINES:
            total = storage.count_checks(conn, airline)
            low = lows.get(airline)
            low_text = f"R$ {low:,.2f}" if low is not None else "sem registro"
            print(f"{airline:<6} | {total:>3} checagens | menor preco: {low_text}")

        print("\nUltimas checagens:")
        for row in storage.get_history(conn, limit=10):
            print(f"  {row['checked_at']} | {row['airline']:<6} | R$ {row['price']:,.2f}")
        print()
    return 0


def cmd_dry_run(settings: Settings) -> int:
    """Envia um embed de exemplo ao Discord sem gastar creditos da GeckoAPI."""
    logger.info("Dry-run: gerando oferta ficticia e enviando ao Discord.")
    offer = Offer(
        airline="LATAM",
        price=1234.56,
        currency="BRL",
        outbound=Leg("BEL", "NAT", "2026-12-27T08:15:00.000Z", "2026-12-27T12:40:00.000Z", 265, 1),
        inbound=Leg("NAT", "BEL", "2027-01-05T14:00:00.000Z", "2027-01-05T17:05:00.000Z", 185, 0),
        raw_response={"dry_run": True},
    )
    comparison = compare.build_comparison("LATAM", 1234.56, historical_low=1400.00)
    try:
        notify_offer(
            settings.discord_webhook_url, offer, comparison, settings.route_label, None
        )
    except DiscordError as exc:
        logger.error("Dry-run falhou: %s", exc)
        return 1
    logger.info("Dry-run enviado com sucesso.")
    return 0


COMMANDS = {
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
    return COMMANDS[args.command](settings)


if __name__ == "__main__":
    sys.exit(cli())
