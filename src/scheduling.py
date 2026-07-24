"""Decide se uma checagem deve rodar agora.

O GitHub Actions dispara em UTC e sem precisao (pode atrasar minutos), entao a
decisao final e tomada aqui, em horario de Belem, com o saldo real em maos.

Duas janelas:
  * manha  (05:00-09:00) - a checagem regular, a cada 2 dias;
  * noite  (20:00-23:00) - so na vespera do reset de creditos, para gastar o
                           que sobrou antes de virar po.

Tudo aqui e funcao pura recebendo `now`: nada de consultar o relogio por dentro,
para os testes conseguirem simular qualquer data sem mexer no sistema.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MODO_MANHA = "morning"
MODO_NOITE = "evening"
MODO_MANUAL = "manual"
MODOS = (MODO_MANHA, MODO_NOITE, MODO_MANUAL)


@dataclass(frozen=True)
class Decisao:
    """Resultado da decisao, com o motivo para o log."""

    rodar: bool
    motivo: str

    def __bool__(self) -> bool:
        return self.rodar


def parse_window(texto: str) -> tuple[time, time]:
    """Converte "05:00-09:00" em (time(5,0), time(9,0))."""
    try:
        inicio_txt, fim_txt = texto.split("-", 1)
        inicio = time.fromisoformat(inicio_txt.strip())
        fim = time.fromisoformat(fim_txt.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Janela invalida {texto!r}; use o formato HH:MM-HH:MM") from exc
    if inicio >= fim:
        raise ValueError(f"Janela invalida {texto!r}: o inicio precisa vir antes do fim")
    return inicio, fim


def is_within_window(agora: datetime, janela: str) -> bool:
    """O horario local esta dentro da janela?"""
    inicio, fim = parse_window(janela)
    return inicio <= agora.time() <= fim


def local_now(agora: datetime, timezone: str) -> datetime:
    """Converte para o fuso configurado.

    O runner do Actions roda em UTC; a janela e a data do reset so fazem sentido
    no fuso de quem viaja.
    """
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=ZoneInfo("UTC"))
    return agora.astimezone(ZoneInfo(timezone))


def reset_date(referencia: date, reset_day: int) -> date:
    """Proxima data em que os creditos resetam, a partir de `referencia`.

    A GeckoAPI nao informa isso (a resposta de /v1/me/credits tem so saldo e
    consumo), entao assumimos um dia fixo do mes, configuravel. O padrao e o
    dia 1, que e como o ledger local ja e chaveado.
    """
    dia = max(1, min(28, reset_day))  # 28 evita o buraco de fevereiro
    if referencia.day < dia:
        return referencia.replace(day=dia)
    if referencia.month == 12:
        return date(referencia.year + 1, 1, dia)
    return date(referencia.year, referencia.month + 1, dia)


def is_day_before_reset(agora: datetime, reset_day: int = 1) -> bool:
    """Hoje e a vespera do reset?

    E o unico dia em que sobra de credito vira desperdicio, entao e quando a
    janela da noite se justifica.
    """
    hoje = agora.date()
    return reset_date(hoje, reset_day) - hoje == timedelta(days=1)


def days_until_reset(agora: datetime, reset_day: int = 1) -> int:
    hoje = agora.date()
    return (reset_date(hoje, reset_day) - hoje).days


def last_day_of_month(referencia: date) -> int:
    return calendar.monthrange(referencia.year, referencia.month)[1]


def decide(
    modo: str,
    agora_utc: datetime,
    saldo: int | None,
    custo: int,
    timezone: str = "America/Belem",
    janela_manha: str = "05:00-09:00",
    janela_noite: str = "20:00-23:00",
    reset_day: int = 1,
    noite_habilitada: bool = True,
) -> Decisao:
    """Diz se a checagem deve rodar agora, e por que.

    `saldo` None significa que nao conseguimos consultar a GeckoAPI; nesse caso
    a decisao de orcamento fica com o ledger local, mais adiante no pipeline.
    """
    agora = local_now(agora_utc, timezone)
    carimbo = agora.strftime("%d/%m %H:%M")

    if modo == MODO_MANUAL:
        return Decisao(True, f"disparo manual ({carimbo})")

    if saldo is not None and saldo < custo:
        return Decisao(False, f"saldo insuficiente: {saldo} creditos, a checagem custa {custo}")

    if modo == MODO_MANHA:
        if not is_within_window(agora, janela_manha):
            return Decisao(False, f"{carimbo} esta fora da janela da manha ({janela_manha})")
        return Decisao(True, f"janela da manha ({janela_manha}), {carimbo}")

    if modo == MODO_NOITE:
        if not noite_habilitada:
            return Decisao(False, "janela da noite desabilitada")
        if not is_within_window(agora, janela_noite):
            return Decisao(False, f"{carimbo} esta fora da janela da noite ({janela_noite})")
        if not is_day_before_reset(agora, reset_day):
            faltam = days_until_reset(agora, reset_day)
            return Decisao(
                False,
                f"a janela da noite so vale na vespera do reset; faltam {faltam} dias",
            )
        return Decisao(
            True,
            f"vespera do reset com {saldo if saldo is not None else '?'} creditos sobrando; "
            f"gastando antes de expirar ({carimbo})",
        )

    return Decisao(False, f"modo desconhecido: {modo!r}")
