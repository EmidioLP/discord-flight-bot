"""Pipeline completo com a GeckoAPI e o Discord mockados.

Uma checagem = uma request ao KAYAK = uma linha no banco = uma mensagem.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import Mock, patch

import pytest
import requests

from src import storage
from src.compare import build_comparison
from src.discord_bot import build_embed
from src.main import run_check
from src.models import Leg, Offer

from test_fetch_prices import KAYAK_PAYLOAD

SALDO = {
    "userId": "user-1",
    "currentCredits": 100,
    "planId": "free",
    "creditsConsumed": {"last24Hours": 5, "last7Days": 5, "last30Days": 5},
}


def fake_post(url, json=None, **kwargs):
    resposta = Mock(status_code=200, text="ok")
    resposta.json.return_value = {} if "discord" in str(url) else KAYAK_PAYLOAD
    return resposta


def fake_get(url, **kwargs):
    """GET /v1/me/credits. Sem isto o run_check sai para a internet de verdade."""
    resposta = Mock(status_code=200, text="ok")
    resposta.json.return_value = SALDO
    return resposta


class TestRunCheck:
    def _rodar(self, settings, conn, enviados):
        def discord_post(url, json=None, **kwargs):
            enviados.append(json)
            return Mock(status_code=204, text="")

        with patch(
            "src.fetch_prices.requests.Session.post",
            side_effect=lambda *a, **k: fake_post(a[0] if a else k.get("url"), **k),
        ), patch(
            "src.fetch_prices.requests.Session.get",
            side_effect=lambda *a, **k: fake_get(a[0] if a else k.get("url"), **k),
        ), patch("src.discord_bot.requests.post", side_effect=discord_post):
            return run_check(settings, conn)

    def test_uma_checagem_gera_uma_linha_e_uma_mensagem(self, settings, conn):
        enviados = []
        assert self._rodar(settings, conn, enviados) == 1
        assert storage.count_checks(conn) == 1
        assert len(enviados) == 1

    def test_grava_a_companhia_e_o_vendedor_descobertos(self, settings, conn):
        self._rodar(settings, conn, [])
        row = conn.execute("SELECT * FROM price_checks").fetchone()
        assert row["airline"] == "GOL"
        assert row["provider"] == "123Milhas"
        assert row["provider_is_direct"] == 0
        assert row["price"] == pytest.approx(1180.00)
        assert row["seats_remaining"] == 2

    def test_gasta_cinco_creditos(self, settings, conn):
        """Uma request ao KAYAK, nao duas por companhia.

        O saldo mockado esta cheio (100), entao a reconciliacao nao lanca
        ajuste e o total do ledger e so o custo da request.
        """
        self._rodar(settings, conn, [])
        total = conn.execute("SELECT SUM(credits) AS t FROM credit_usage").fetchone()["t"]
        assert total == 5

    def test_embed_traz_companhia_e_vendedor(self, settings, conn):
        enviados = []
        self._rodar(settings, conn, enviados)
        embed = enviados[0]["embeds"][0]
        campos = {f["name"]: f["value"] for f in embed["fields"]}
        assert campos["Companhia aerea"] == "GOL"
        assert campos["Vendido por"] == "123Milhas (agencia)"
        assert "GOL" in embed["title"]

    def test_segunda_checagem_compara_com_a_primeira(self, settings, conn):
        self._rodar(settings, conn, [])
        enviados = []
        self._rodar(settings, conn, enviados)
        campos = {f["name"]: f["value"] for f in enviados[0]["embeds"][0]["fields"]}
        assert "Empatado" in campos["Menor preco historico"]

    def test_orcamento_esgotado_aborta_sem_chamar_a_api(self, settings, conn):
        conn.execute(
            "INSERT INTO credit_usage (year_month, credits, reason, recorded_at)"
            " VALUES (strftime('%Y-%m','now'), 100, 'mes cheio', 'x')"
        )
        conn.commit()
        enviados = []
        saldo_zerado = Mock(status_code=200, text="ok")
        saldo_zerado.json.return_value = {"currentCredits": 0, "planId": "free"}
        with patch("src.fetch_prices.requests.Session.post") as post_mock, patch(
            "src.fetch_prices.requests.Session.get", return_value=saldo_zerado
        ), patch(
            "src.discord_bot.requests.post",
            side_effect=lambda url, json=None, **k: (enviados.append(json), Mock(status_code=204, text=""))[1],
        ):
            assert run_check(settings, conn) == 0
        post_mock.assert_not_called()
        assert len(enviados) == 1, "avisa o estouro no Discord"

    def test_reconcilia_o_ledger_com_o_saldo_real(self, settings, conn):
        """Ledger inflado por retries nao pode recusar checagem que cabia."""
        conn.execute(
            "INSERT INTO credit_usage (year_month, credits, reason, recorded_at)"
            " VALUES (strftime('%Y-%m','now'), 90, 'inflado por retries', 'x')"
        )
        conn.commit()
        # A GeckoAPI diz que o saldo esta cheio: os 90 do ledger eram inflacao.
        assert self._rodar(settings, conn, []) == 1

    def test_saldo_indisponivel_nao_derruba_a_checagem(self, settings, conn):
        """Sem o saldo real, seguimos com o ledger local (que erra para cima)."""
        enviados = []

        def discord_post(url, json=None, **kwargs):
            enviados.append(json)
            return Mock(status_code=204, text="")

        with patch(
            "src.fetch_prices.requests.Session.post",
            side_effect=lambda *a, **k: fake_post(a[0] if a else k.get("url"), **k),
        ), patch(
            "src.fetch_prices.requests.Session.get", side_effect=requests.Timeout("sem saldo")
        ), patch("src.discord_bot.requests.post", side_effect=discord_post):
            assert run_check(settings, conn) == 1
        assert len(enviados) == 1


class TestEmbed:
    def _offer(self, **kwargs):
        base = dict(
            airline="GOL",
            price=1180.0,
            currency="BRL",
            outbound=Leg("BEL", "NAT", "2026-12-27T08:15:00", "2026-12-27T12:40:00", 265, 1),
            inbound=Leg("NAT", "BEL", "2027-01-05T14:00:00", "2027-01-05T17:05:00", 185, 0),
            provider="123Milhas",
            provider_is_direct=False,
            booking_url="https://exemplo/123",
            seats_remaining=2,
            total_options=1111,
        )
        base.update(kwargs)
        return Offer(**base)

    def _campos(self, embed):
        return {f["name"]: f["value"] for f in embed["fields"]}

    def test_titulo_tem_preco_e_companhia(self):
        embed = build_embed(self._offer(), build_comparison("GOL", 1180.0, None), "BEL -> NAT")
        assert "R$ 1.180,00" in embed["title"]
        assert "GOL" in embed["title"]

    def test_link_de_reserva_no_embed(self):
        embed = build_embed(self._offer(), build_comparison("GOL", 1180.0, None), "BEL -> NAT")
        assert embed["url"] == "https://exemplo/123"

    def test_sem_link_nao_cria_a_chave(self):
        offer = self._offer(booking_url=None)
        embed = build_embed(offer, build_comparison("GOL", 1180.0, None), "BEL -> NAT")
        assert "url" not in embed

    def test_poucos_assentos_viram_alerta(self):
        embed = build_embed(self._offer(seats_remaining=2), build_comparison("GOL", 1180.0, None), "x")
        assert "corre" in self._campos(embed)["Assentos restantes"]

    def test_muitos_assentos_nao_alertam(self):
        embed = build_embed(self._offer(seats_remaining=9), build_comparison("GOL", 1180.0, None), "x")
        assert "corre" not in self._campos(embed)["Assentos restantes"]

    def test_sem_assentos_nao_cria_o_campo(self):
        embed = build_embed(self._offer(seats_remaining=None), build_comparison("GOL", 1180.0, None), "x")
        assert "Assentos restantes" not in self._campos(embed)

    def test_total_de_opcoes_na_descricao(self):
        embed = build_embed(self._offer(), build_comparison("GOL", 1180.0, None), "BEL -> NAT")
        assert "1111 opcoes avaliadas" in embed["description"]

    def test_novo_minimo_muda_titulo_e_cor(self):
        embed = build_embed(self._offer(), build_comparison("GOL", 1180.0, 1400.0), "x")
        assert embed["title"].startswith("NOVO MENOR PRECO")
        assert embed["color"] == 0x2ECC71


class TestMigracaoDoBanco:
    """Bancos criados antes das colunas do KAYAK precisam ganhar as colunas."""

    def test_alter_table_adiciona_colunas_faltantes(self, tmp_path):
        caminho = tmp_path / "antigo.db"
        antigo = sqlite3.connect(str(caminho))
        antigo.executescript(
            """
            CREATE TABLE price_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL, airline TEXT NOT NULL,
                price REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'BRL',
                outbound_departure TEXT, outbound_arrival TEXT,
                outbound_duration_minutes INTEGER, outbound_stops INTEGER,
                inbound_departure TEXT, inbound_arrival TEXT,
                inbound_duration_minutes INTEGER, inbound_stops INTEGER,
                fare_valid_until TEXT, raw_response TEXT NOT NULL
            );
            """
        )
        antigo.execute(
            "INSERT INTO price_checks (checked_at, airline, price, currency, raw_response)"
            " VALUES ('2026-07-01T10:00:00+00:00', 'AZUL', 1346.0, 'BRL', '{}')"
        )
        antigo.commit()
        antigo.close()

        conn = storage.connect(caminho)
        colunas = {linha[1] for linha in conn.execute("PRAGMA table_info(price_checks)").fetchall()}
        assert {"provider", "provider_is_direct", "booking_url", "seats_remaining",
                "total_options"} <= colunas

        # O registro antigo sobrevive e continua contando no historico.
        assert storage.count_checks(conn) == 1
        conn.close()

    def test_migracao_e_idempotente(self, tmp_path):
        caminho = tmp_path / "novo.db"
        storage.connect(caminho).close()
        conn = storage.connect(caminho)  # roda o ALTER de novo
        assert storage.count_checks(conn) == 0
        conn.close()
