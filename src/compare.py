"""Comparacao do preco atual com o historico.

O menor preco historico nao e guardado em coluna: e sempre derivado com
MIN(price), para nao existir estado duplicado que possa dessincronizar.

A logica pura (`build_comparison`) e separada do acesso ao banco
(`historical_low`, `last_price`) justamente para ser testavel sem SQLite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import format_money
from .storage import DBConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Comparison:
    """Resultado da comparacao entre o preco atual e o historico."""

    airline: str
    current_price: float
    historical_low: float | None
    previous_price: float | None = None

    @property
    def is_first_check(self) -> bool:
        return self.historical_low is None

    @property
    def diff(self) -> float | None:
        """Diferenca para o menor preco historico. Negativo = mais barato."""
        if self.historical_low is None:
            return None
        return round(self.current_price - self.historical_low, 2)

    @property
    def pct_diff(self) -> float | None:
        """Variacao percentual sobre o menor preco historico."""
        if self.historical_low is None or self.historical_low == 0:
            return None
        return round((self.current_price - self.historical_low) / self.historical_low * 100, 2)

    @property
    def is_new_low(self) -> bool:
        """O preco atual bate (estritamente) o menor ja registrado."""
        return self.historical_low is not None and self.current_price < self.historical_low

    @property
    def is_tie(self) -> bool:
        return self.historical_low is not None and self.current_price == self.historical_low

    @property
    def diff_vs_previous(self) -> float | None:
        """Diferenca para a checagem imediatamente anterior."""
        if self.previous_price is None:
            return None
        return round(self.current_price - self.previous_price, 2)

    def summary(self, currency: str = "BRL") -> str:
        """Frase pronta para o embed do Discord."""
        if self.is_first_check:
            return "Primeira checagem registrada - virou a referencia."
        low = format_money(self.historical_low, currency)
        if self.is_new_low:
            return (
                f"Novo menor preco! {format_money(abs(self.diff), currency)} abaixo do "
                f"anterior ({low}), queda de {abs(self.pct_diff):.1f}%."
            )
        if self.is_tie:
            return f"Empatado com o menor preco historico ({low})."
        return (
            f"{format_money(self.diff, currency)} acima do menor historico ({low}), "
            f"alta de {self.pct_diff:.1f}%."
        )


def build_comparison(
    airline: str,
    current_price: float,
    historical_low: float | None,
    previous_price: float | None = None,
) -> Comparison:
    """Fabrica pura de Comparison, sem tocar no banco."""
    return Comparison(
        airline=airline,
        current_price=round(current_price, 2),
        historical_low=round(historical_low, 2) if historical_low is not None else None,
        previous_price=round(previous_price, 2) if previous_price is not None else None,
    )


def historical_low(conn: DBConnection, airline: str | None = None) -> float | None:
    """Menor preco ja registrado, ou None se nao houver.

    Sem `airline`, o minimo e global - que e o que interessa desde a mudanca
    para o KAYAK: a busca nao fixa companhia, entao comparar so contra o
    historico da companhia sorteada nesta checagem esconderia o preco real.
    Com `airline`, filtra (usado em relatorios).
    """
    if airline is None:
        row = conn.execute("SELECT MIN(price) AS low FROM price_checks").fetchone()
    else:
        row = conn.execute(
            "SELECT MIN(price) AS low FROM price_checks WHERE airline = ?", (airline,)
        ).fetchone()
    low = row["low"] if row is not None else None
    return float(low) if low is not None else None


def last_price(conn: DBConnection, airline: str | None = None) -> float | None:
    """Preco da checagem anterior mais recente."""
    if airline is None:
        row = conn.execute(
            "SELECT price FROM price_checks ORDER BY checked_at DESC, id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT price FROM price_checks
            WHERE airline = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (airline,),
        ).fetchone()
    return float(row["price"]) if row is not None else None


def compare_with_history(
    conn: DBConnection, airline: str, current_price: float, global_history: bool = True
) -> Comparison:
    """Compara o preco atual contra o historico ja gravado.

    Deve ser chamado ANTES de salvar a checagem atual, senao o proprio preco
    entra no MIN() e a comparacao sempre empata.

    `global_history=True` compara contra todas as checagens, independente da
    companhia; `airline` continua sendo gravado para identificar a oferta.
    """
    filtro = None if global_history else airline
    comparison = build_comparison(
        airline=airline,
        current_price=current_price,
        historical_low=historical_low(conn, filtro),
        previous_price=last_price(conn, filtro),
    )
    logger.info(
        "Comparacao | %s | atual=%.2f | minimo=%s | novo minimo=%s",
        airline,
        comparison.current_price,
        comparison.historical_low,
        comparison.is_new_low,
    )
    return comparison


def lowest_by_airline(conn: DBConnection) -> dict[str, float]:
    """Menor preco de cada companhia, para relatorios."""
    rows = conn.execute(
        "SELECT airline, MIN(price) AS low FROM price_checks GROUP BY airline"
    ).fetchall()
    return {row["airline"]: float(row["low"]) for row in rows if row["low"] is not None}
