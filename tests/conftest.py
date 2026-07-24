"""Fixtures compartilhadas."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src import storage
from src.config import Settings


@pytest.fixture(autouse=True)
def _sem_rede_real(monkeypatch):
    """Nenhum teste pode sair para a internet.

    Sentinela no nivel do adaptador: mocks de `Session.post`/`Session.get`
    nunca chegam aqui, entao so uma chamada esquecida dispara. Foi exatamente
    isso que aconteceu quando `run_check` passou a consultar o saldo em
    GET /v1/me/credits e os testes so mockavam o POST - a suite comecou a
    bater na GeckoAPI de verdade, e o unico sintoma era ficar mais lenta.
    """

    def _bloquear(self, request, *args, **kwargs):
        raise RuntimeError(
            f"Teste tentou chamada de rede real: {request.method} {request.url}. "
            "Mocke a sessao (Session.post / Session.get) no teste."
        )

    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", _bloquear)


@pytest.fixture(autouse=True)
def _sem_sleep_real(monkeypatch):
    """Nenhum teste pode dormir de verdade.

    O retry do GeckoClient pausa 60s por padrao (retry imediato apos um
    UPSTREAM_TIMEOUT so queima credito). Sem este patch, um teste de retry
    trava a suite por minutos. Quem quiser inspecionar as pausas injeta o
    proprio `sleep_fn`.
    """
    monkeypatch.setattr("src.fetch_prices.time.sleep", lambda _s: None)


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
        http_retry_delay_seconds=0,
        max_credits_per_run=20,
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
