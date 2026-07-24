"""Modelos de dominio compartilhados entre os modulos.

O parser das duas companhias normaliza para essas estruturas, entao storage,
compare e discord_bot nao precisam saber de qual API o dado veio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def format_money(value: float, currency: str = "BRL") -> str:
    """Formata valores no padrao brasileiro: R$ 1.234,56.

    Fica aqui (e nao no discord_bot) porque compare.py tambem monta texto com
    dinheiro; ter duas implementacoes ja causou divergencia de formato.
    """
    symbol = "R$" if currency.upper() == "BRL" else currency.upper()
    formatted = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{symbol} {formatted}"


@dataclass(frozen=True)
class Leg:
    """Um trecho da viagem (ida ou volta)."""

    origin: str
    destination: str
    departure: str | None  # ISO 8601
    arrival: str | None  # ISO 8601
    duration_minutes: int | None
    stops: int | None

    @property
    def duration_label(self) -> str:
        if self.duration_minutes is None:
            return "nao informado"
        hours, minutes = divmod(self.duration_minutes, 60)
        return f"{hours}h{minutes:02d}min" if hours else f"{minutes}min"

    @property
    def stops_label(self) -> str:
        if self.stops is None:
            return "nao informado"
        if self.stops == 0:
            return "direto"
        return f"{self.stops} conexao" if self.stops == 1 else f"{self.stops} conexoes"


@dataclass(frozen=True)
class Offer:
    """A oferta mais barata encontrada para uma companhia numa checagem."""

    airline: str
    price: float
    currency: str
    outbound: Leg | None
    inbound: Leg | None
    # As duas APIs da GeckoAPI nao expoem validade de tarifa; fica None ate que
    # exponham. Ver secao "Validade da tarifa" no README.
    fare_valid_until: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict, repr=False)
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def total_duration_minutes(self) -> int | None:
        parts = [
            leg.duration_minutes
            for leg in (self.outbound, self.inbound)
            if leg is not None and leg.duration_minutes is not None
        ]
        return sum(parts) if parts else None
