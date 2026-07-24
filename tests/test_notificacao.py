"""Uma checagem gera exatamente UMA mensagem: a companhia mais barata.

As duas companhias continuam sendo salvas no banco - o que mudou foi so a
notificacao. Estes testes cobrem a escolha do vencedor e o formato do embed.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src import storage
from src.compare import build_comparison
from src.discord_bot import build_embed
from src.main import AirlineResult, _notificar, run_check
from src.models import Leg, Offer

from test_fetch_prices import AZUL_PAYLOAD, LATAM_PAYLOAD


def make_offer(airline, price):
    return Offer(
        airline=airline,
        price=price,
        currency="BRL",
        outbound=Leg("BEL", "NAT", "2026-12-27T09:00:00", "2026-12-27T14:20:00", 320, 1),
        inbound=Leg("NAT", "BEL", "2027-01-05T07:30:00", "2027-01-05T10:35:00", 185, 0),
        raw_response={},
    )


def result(airline, price):
    return AirlineResult(
        airline=airline,
        offer=make_offer(airline, price),
        comparison=build_comparison(airline, price, historical_low=None),
    )


class TestEscolhaDoVencedor:
    def _capturar(self, sucessos, falhas):
        enviados = []
        settings = Mock(discord_webhook_url="https://d", route_label="BEL -> NAT")
        tracker = Mock(summary=Mock(return_value={"used": 10, "budget": 100, "year_month": "2026-07"}))
        with patch("src.main.notify_offer", side_effect=lambda **kw: enviados.append(kw)) as offer_mock, \
             patch("src.main.notify_error", side_effect=lambda *a, **k: enviados.append(a)) as err_mock:
            _notificar(settings, tracker, sucessos, falhas)
        return enviados, offer_mock, err_mock

    def test_envia_uma_unica_mensagem(self):
        enviados, _, _ = self._capturar([result("LATAM", 1512.0), result("AZUL", 1346.0)], [])
        assert len(enviados) == 1

    def test_escolhe_a_mais_barata(self):
        enviados, _, _ = self._capturar([result("LATAM", 1512.0), result("AZUL", 1346.0)], [])
        assert enviados[0]["offer"].airline == "AZUL"

    def test_a_ordem_da_lista_nao_importa(self):
        enviados, _, _ = self._capturar([result("AZUL", 1346.0), result("LATAM", 1512.0)], [])
        assert enviados[0]["offer"].airline == "AZUL"

    def test_a_perdedora_vai_como_alternativa(self):
        enviados, _, _ = self._capturar([result("LATAM", 1512.0), result("AZUL", 1346.0)], [])
        assert enviados[0]["alternatives"] == [("LATAM", 1512.0)]

    def test_uma_companhia_so_nao_tem_alternativa(self):
        enviados, _, _ = self._capturar([result("AZUL", 1346.0)], [("LATAM", "HTTP 504")])
        assert enviados[0]["alternatives"] == []
        assert enviados[0]["failures"] == [("LATAM", "HTTP 504")]

    def test_falha_vira_nota_e_nao_segunda_mensagem(self):
        enviados, offer_mock, err_mock = self._capturar(
            [result("AZUL", 1346.0)], [("LATAM", "HTTP 504")]
        )
        assert offer_mock.call_count == 1
        assert err_mock.call_count == 0

    def test_todas_falharam_manda_um_erro_so(self):
        enviados, offer_mock, err_mock = self._capturar(
            [], [("LATAM", "HTTP 504"), ("AZUL", "HTTP 500")]
        )
        assert offer_mock.call_count == 0
        assert err_mock.call_count == 1


class TestEmbedDaMaisBarata:
    def _embed(self, alternatives=None, failures=None):
        return build_embed(
            make_offer("AZUL", 1346.0),
            build_comparison("AZUL", 1346.0, historical_low=None),
            "BEL -> NAT",
            alternatives=alternatives,
            failures=failures,
        )

    def _campo(self, embed, nome):
        return next((f for f in embed["fields"] if f["name"] == nome), None)

    def test_mostra_a_outra_companhia_e_a_diferenca(self):
        embed = self._embed(alternatives=[("LATAM", 1512.0)])
        campo = self._campo(embed, "Outras companhias nesta checagem")
        assert "LATAM" in campo["value"]
        assert "R$ 1.512,00" in campo["value"]
        assert "R$ 166,00" in campo["value"]

    def test_descricao_indica_que_e_a_mais_barata(self):
        assert "Mais barata" in self._embed(alternatives=[("LATAM", 1512.0)])["description"]

    def test_sem_alternativa_nao_cria_o_campo(self):
        assert self._campo(self._embed(), "Outras companhias nesta checagem") is None

    def test_falha_aparece_no_embed(self):
        embed = self._embed(failures=[("LATAM", "HTTP 504: UPSTREAM_TIMEOUT")])
        campo = self._campo(embed, "Nao consegui consultar")
        assert "LATAM" in campo["value"]
        assert "504" in campo["value"]

    def test_duracao_aparece_formatada(self):
        embed = self._embed()
        assert "5h20min" in self._campo(embed, "Ida")["value"]


class TestFluxoCompleto:
    """run_check ponta a ponta com a API e o Discord mockados."""

    def test_salva_as_duas_e_notifica_uma(self, settings, conn):
        enviados = []

        def fake_post(url, json=None, **kwargs):
            resposta = Mock(status_code=200, text="ok")
            if "discord" in str(url):
                enviados.append(json)
                resposta.json.return_value = {}
            else:
                alvo = json.get("target", "")
                resposta.json.return_value = (
                    LATAM_PAYLOAD if alvo.startswith("latam") else AZUL_PAYLOAD
                )
            return resposta

        with patch("src.fetch_prices.requests.Session.post", side_effect=lambda *a, **k: fake_post(a[0] if a else k.get("url"), **k)), \
             patch("src.discord_bot.requests.post", side_effect=fake_post):
            sucessos = run_check(settings, conn)

        assert sucessos == 2
        assert storage.count_checks(conn, "LATAM") == 1, "LATAM tem que estar no banco"
        assert storage.count_checks(conn, "AZUL") == 1, "Azul tem que estar no banco"
        assert len(enviados) == 1, "mas so uma mensagem no Discord"

        embed = enviados[0]["embeds"][0]
        # Azul (1346.00) e mais barata que LATAM (1395.75) nos payloads de teste.
        assert "Azul" in embed["title"]
        campo = next(f for f in embed["fields"] if f["name"] == "Outras companhias nesta checagem")
        assert "LATAM" in campo["value"]
