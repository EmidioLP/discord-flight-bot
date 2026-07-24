"""Fixtures compartilhadas."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src import storage
from src.config import Settings


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Banco em memoria com o schema real aplicado."""
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        geckoapi_key="chave-de-teste",
        discord_webhook_url="https://discord.com/api/webhooks/1/teste",
        origin="BEL",
        destination="NAT",
        departure_date="2026-12-27",
        return_date="2027-01-05",
        num_adults=1,
        num_children=0,
        num_infants=0,
        currency="BRL",
        monthly_credit_budget=100,
        credits_per_request=5,
        check_interval_days=4,
        db_path=tmp_path / "test.db",
        turso_database_url="",
        turso_auth_token="",
        http_timeout_seconds=10,
        http_max_retries=1,
        log_level="WARNING",
    )


class FakeClock:
    """Relogio controlavel, para simular virada de mes."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def set(self, year: int, month: int, day: int = 1) -> None:
        self.moment = datetime(year, month, day, tzinfo=timezone.utc)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc))
