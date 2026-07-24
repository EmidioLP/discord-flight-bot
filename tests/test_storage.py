"""Testes do storage, incluindo o adaptador de linha do libSQL.

O adaptador existe porque o cliente libSQL nao tem `row_factory` e devolve
tuplas puras, enquanto todo o codigo acessa coluna por nome (`row["price"]`).
Estes testes rodam contra o libSQL de verdade em arquivo local - mesmo cliente
que fala com o Turso, so que sem rede.
"""

from __future__ import annotations

import json

import pytest

from src import storage
from src.compare import compare_with_history, historical_low, lowest_by_airline
from src.credits_tracker import CreditsTracker
from src.models import Leg, Offer

libsql = pytest.importorskip("libsql", reason="extra [turso] nao instalado")


def make_offer(airline="LATAM", price=1395.75):
    return Offer(
        airline=airline,
        price=price,
        currency="BRL",
        outbound=Leg("BEL", "NAT", "2026-12-27T08:15:00Z", "2026-12-27T12:40:00Z", 265, 1),
        inbound=Leg("NAT", "BEL", "2027-01-05T14:00:00Z", "2027-01-05T17:05:00Z", 185, 0),
        raw_response={"data": {"items": [1, 2, 3]}},
        checked_at="2026-07-23T12:00:00+00:00",
    )


class TestRow:
    """O adaptador precisa cobrir os dois modos de acesso que o codigo usa."""

    def test_acesso_por_nome_e_por_indice(self):
        row = storage.Row((1, "LATAM", 1395.75), {"id": 0, "airline": 1, "price": 2})
        assert row["airline"] == "LATAM"
        assert row[1] == "LATAM"
        assert row["price"] == 1395.75

    def test_coluna_inexistente_da_erro_claro(self):
        row = storage.Row((1,), {"id": 0})
        with pytest.raises(KeyError, match="nao esta no resultado"):
            row["preco"]

    def test_keys_e_len(self):
        row = storage.Row((1, "LATAM"), {"id": 0, "airline": 1})
        assert row.keys() == ["id", "airline"]
        assert len(row) == 2


@pytest.fixture()
def libsql_conn(tmp_path):
    """Conexao libSQL local, exercitando o mesmo adaptador usado no Turso."""
    conn = storage._RemoteConnection(libsql.connect(str(tmp_path / "remote.db")))
    storage.init_db(conn)
    yield conn
    conn.close()


class TestLibsqlCompatibilidade:
    """O schema e as queries precisam funcionar identicos no libSQL."""

    def test_schema_aplica_sem_alteracao(self, libsql_conn):
        row = libsql_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        nomes = [r["name"] for r in row]
        assert "price_checks" in nomes
        assert "credit_usage" in nomes

    def test_init_db_e_idempotente(self, libsql_conn):
        storage.init_db(libsql_conn)
        storage.init_db(libsql_conn)
        assert storage.count_checks(libsql_conn) == 0

    def test_save_offer_devolve_lastrowid(self, libsql_conn):
        assert storage.save_offer(libsql_conn, make_offer()) == 1
        assert storage.save_offer(libsql_conn, make_offer()) == 2

    def test_round_trip_completo_dos_campos(self, libsql_conn):
        storage.save_offer(libsql_conn, make_offer())
        row = libsql_conn.execute("SELECT * FROM price_checks").fetchone()
        assert row["airline"] == "LATAM"
        assert row["price"] == 1395.75
        assert row["outbound_duration_minutes"] == 265
        assert row["outbound_stops"] == 1
        assert row["inbound_stops"] == 0
        assert row["fare_valid_until"] is None

    def test_raw_response_volta_como_json(self, libsql_conn):
        storage.save_offer(libsql_conn, make_offer())
        raw = storage.get_raw_response(libsql_conn, 1)
        assert raw == {"data": {"items": [1, 2, 3]}}

    def test_alias_de_agregacao_acessivel_por_nome(self, libsql_conn):
        """MIN(price) AS low precisa vir nomeado no description."""
        storage.save_offer(libsql_conn, make_offer(price=1500.0))
        storage.save_offer(libsql_conn, make_offer(price=1200.0))
        assert historical_low(libsql_conn, "LATAM") == 1200.0

    def test_group_by_com_alias(self, libsql_conn):
        storage.save_offer(libsql_conn, make_offer("LATAM", 1500.0))
        storage.save_offer(libsql_conn, make_offer("AZUL", 980.0))
        assert lowest_by_airline(libsql_conn) == {"LATAM": 1500.0, "AZUL": 980.0}

    def test_get_history_com_fetchall(self, libsql_conn):
        storage.save_offer(libsql_conn, make_offer("LATAM", 1500.0))
        storage.save_offer(libsql_conn, make_offer("AZUL", 980.0))
        rows = storage.get_history(libsql_conn)
        assert len(rows) == 2
        assert {r["airline"] for r in rows} == {"LATAM", "AZUL"}

    def test_comparacao_funciona_ponta_a_ponta(self, libsql_conn):
        storage.save_offer(libsql_conn, make_offer(price=1500.0))
        c = compare_with_history(libsql_conn, "LATAM", 1200.0)
        assert c.historical_low == 1500.0
        assert c.is_new_low is True

    def test_credits_tracker_funciona_no_libsql(self, libsql_conn):
        tracker = CreditsTracker(libsql_conn, monthly_budget=100)
        tracker.record(10, "checagem 1")
        tracker.record(10, "checagem 2")
        assert tracker.used_this_month() == 20
        assert tracker.remaining() == 80
        assert tracker.can_spend(80) is True
        assert tracker.can_spend(81) is False


class TestSelecaoDeConexao:
    def test_sem_remote_url_usa_sqlite_local(self, tmp_path):
        import sqlite3

        conn = storage.connect(tmp_path / "local.db")
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_com_remote_url_usa_o_adaptador(self, tmp_path):
        # Caminho de arquivo no lugar da URL: exercita o ramo remoto sem rede.
        conn = storage.connect(":memory:", remote_url=str(tmp_path / "r.db"))
        assert isinstance(conn, storage._RemoteConnection)
        conn.close()


class TestFailedExtractions:
    """O payload que o parser nao entendeu precisa sobreviver: o credito ja foi gasto."""

    def test_salva_e_recupera_o_payload(self, conn):
        payload = {"data": {"formato": "inesperado"}}
        rid = storage.save_failed_extraction(conn, "LATAM", "items ausente", payload)
        assert rid == 1
        assert storage.get_failed_extraction(conn, rid) == payload

    def test_recuperar_id_inexistente_devolve_none(self, conn):
        assert storage.get_failed_extraction(conn, 999) is None

    def test_nao_propaga_excecao_se_o_insert_falhar(self, conn):
        """Nunca pode virar um segundo ponto de quebra no pipeline."""
        conn.execute("DROP TABLE failed_extractions")
        conn.commit()
        assert storage.save_failed_extraction(conn, "AZUL", "erro", {"a": 1}) is None

    def test_serializa_valores_nao_json(self, conn):
        import datetime as dt

        rid = storage.save_failed_extraction(
            conn, "AZUL", "erro", {"quando": dt.datetime(2026, 7, 24)}
        )
        assert storage.get_failed_extraction(conn, rid)["quando"].startswith("2026-07-24")

    def test_funciona_no_libsql(self, libsql_conn):
        rid = storage.save_failed_extraction(libsql_conn, "LATAM", "erro", {"x": [1, 2]})
        assert storage.get_failed_extraction(libsql_conn, rid) == {"x": [1, 2]}
