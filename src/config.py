"""Configuracao central, carregada do .env com defaults sensatos."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

LATAM = "LATAM"
AZUL = "AZUL"
AIRLINES = (LATAM, AZUL)


class ConfigError(RuntimeError):
    """Configuracao obrigatoria ausente ou invalida."""


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} precisa ser um inteiro, recebi {raw!r}") from exc


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise ConfigError(
            f"Variavel de ambiente {name} nao definida. "
            "Copie .env.example para .env e preencha."
        )
    return value.strip()


@dataclass(frozen=True)
class Settings:
    geckoapi_key: str
    discord_webhook_url: str

    origin: str
    destination: str
    departure_date: str
    return_date: str
    num_adults: int
    num_children: int
    num_infants: int
    currency: str

    monthly_credit_budget: int
    credits_per_request: int
    check_interval_days: int

    db_path: Path
    turso_database_url: str
    turso_auth_token: str
    http_timeout_seconds: int
    http_max_retries: int
    log_level: str

    @property
    def uses_remote_db(self) -> bool:
        """Com a URL do Turso preenchida o banco vai para a nuvem.

        E o que permite rodar no GitHub Actions, onde o disco e descartado no
        fim de cada execucao.
        """
        return bool(self.turso_database_url)

    @property
    def db_label(self) -> str:
        return "Turso (remoto)" if self.uses_remote_db else f"SQLite local ({self.db_path})"

    @property
    def credits_per_full_check(self) -> int:
        """Uma checagem completa = 1 request LATAM + 1 request Azul."""
        return self.credits_per_request * len(AIRLINES)

    @property
    def route_label(self) -> str:
        return f"{self.origin} -> {self.destination}"


def load_settings(*, require_secrets: bool = True) -> Settings:
    """Monta as Settings a partir do ambiente.

    require_secrets=False permite carregar em testes/inspecao sem .env.
    """
    db_path = Path(_get("DB_PATH", "data/prices.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Settings(
        geckoapi_key=_require("GECKOAPI_KEY") if require_secrets else _get("GECKOAPI_KEY", ""),
        discord_webhook_url=(
            _require("DISCORD_WEBHOOK_URL") if require_secrets else _get("DISCORD_WEBHOOK_URL", "")
        ),
        origin=_get("ORIGIN", "BEL").upper(),
        destination=_get("DESTINATION", "NAT").upper(),
        departure_date=_get("DEPARTURE_DATE", "2026-12-27"),
        return_date=_get("RETURN_DATE", "2027-01-05"),
        num_adults=_get_int("NUM_ADULTS", 1),
        num_children=_get_int("NUM_CHILDREN", 0),
        num_infants=_get_int("NUM_INFANTS", 0),
        currency=_get("CURRENCY", "BRL").upper(),
        monthly_credit_budget=_get_int("MONTHLY_CREDIT_BUDGET", 100),
        credits_per_request=_get_int("CREDITS_PER_REQUEST", 5),
        check_interval_days=_get_int("CHECK_INTERVAL_DAYS", 4),
        db_path=db_path,
        turso_database_url=_get("TURSO_DATABASE_URL", ""),
        turso_auth_token=_get("TURSO_AUTH_TOKEN", ""),
        http_timeout_seconds=_get_int("HTTP_TIMEOUT_SECONDS", 120),
        http_max_retries=_get_int("HTTP_MAX_RETRIES", 2),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
    )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # O apscheduler e o urllib3 sao barulhentos demais em INFO.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
