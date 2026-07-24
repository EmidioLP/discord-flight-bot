"""Controle de creditos consumidos no mes corrente.

A GeckoAPI cobra 5 creditos por request e o plano free da 100 creditos/mes.
Este modulo e a rede de protecao: mesmo que o scheduler dispare fora de hora,
ou que o processo seja executado na mao varias vezes, nada e gasto alem do
orcamento.

O "reset todo dia 1" nao precisa de rotina de limpeza: o ledger e chaveado por
YYYY-MM, entao virar o mes zera o consumo naturalmente.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from .storage import DBConnection

logger = logging.getLogger(__name__)


class CreditBudgetExceeded(RuntimeError):
    """Levantada quando a operacao estouraria o orcamento mensal."""


def current_year_month(now: datetime | None = None) -> str:
    """Chave do mes corrente no formato YYYY-MM."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


class CreditsTracker:
    """Ledger de creditos append-only sobre a tabela credit_usage.

    O clock e injetavel (`now_fn`) para os testes conseguirem simular virada
    de mes sem mexer no relogio do sistema.
    """

    def __init__(
        self,
        conn: DBConnection,
        monthly_budget: int = 100,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if monthly_budget < 0:
            raise ValueError("monthly_budget nao pode ser negativo")
        self.conn = conn
        self.monthly_budget = monthly_budget
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return self._now_fn()

    @property
    def year_month(self) -> str:
        return current_year_month(self._now())

    def used_this_month(self) -> int:
        """Total ja gasto no mes corrente."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(credits), 0) AS total FROM credit_usage WHERE year_month = ?",
            (self.year_month,),
        ).fetchone()
        # Indice em vez de nome: funciona igual no sqlite3.Row e no Row do libSQL.
        return int(row[0])

    def remaining(self) -> int:
        """Creditos disponiveis no mes (nunca negativo)."""
        return max(0, self.monthly_budget - self.used_this_month())

    def can_spend(self, credits: int) -> bool:
        """Ha saldo para gastar `credits` sem estourar o orcamento?"""
        if credits < 0:
            raise ValueError("credits nao pode ser negativo")
        return self.used_this_month() + credits <= self.monthly_budget

    def record(self, credits: int, reason: str = "") -> int:
        """Registra um gasto ja realizado. Devolve o total do mes apos o registro.

        Nao valida o orcamento de proposito: se a API foi chamada, o credito
        foi cobrado, e o ledger precisa refletir a realidade. A checagem
        preventiva e responsabilidade de `can_spend`/`ensure_budget`.
        """
        if credits < 0:
            raise ValueError("credits nao pode ser negativo")
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO credit_usage (year_month, credits, reason, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (current_year_month(now), credits, reason, now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        total = self.used_this_month()
        logger.info(
            "Creditos +%s (%s) | usados no mes: %s/%s",
            credits,
            reason or "sem motivo informado",
            total,
            self.monthly_budget,
        )
        return total

    def refund(self, credits: int, reason: str = "") -> int:
        """Registra um estorno como lancamento negativo no ledger.

        A GeckoAPI devolve os creditos de extracoes que ela mesma nao concluiu
        (por exemplo HTTP 504 / UPSTREAM_TIMEOUT). Sem lancar o estorno, o
        contador sobe sozinho e passa a barrar checagens que ainda cabiam no
        orcamento.

        E um lancamento negativo em vez de apagar o debito original: o ledger
        continua append-only e o historico mostra o que foi cobrado e o que
        voltou.
        """
        if credits < 0:
            raise ValueError("credits do estorno deve ser positivo")
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO credit_usage (year_month, credits, reason, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                current_year_month(now),
                -credits,
                f"estorno: {reason}" if reason else "estorno",
                now.isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        total = self.used_this_month()
        logger.info(
            "Estorno -%s (%s) | usados no mes: %s/%s",
            credits,
            reason or "sem motivo informado",
            total,
            self.monthly_budget,
        )
        return total

    def ensure_budget(self, credits: int, reason: str = "") -> None:
        """Levanta CreditBudgetExceeded se o gasto nao couber no orcamento."""
        if not self.can_spend(credits):
            used = self.used_this_month()
            raise CreditBudgetExceeded(
                f"Gasto de {credits} creditos ({reason or 'checagem'}) estouraria o "
                f"orcamento: {used}/{self.monthly_budget} usados em {self.year_month}, "
                f"restam {self.remaining()}."
            )

    def summary(self) -> dict[str, int | str]:
        """Resumo do mes, usado no rodape do embed e nos logs."""
        used = self.used_this_month()
        return {
            "year_month": self.year_month,
            "used": used,
            "budget": self.monthly_budget,
            "remaining": max(0, self.monthly_budget - used),
        }
