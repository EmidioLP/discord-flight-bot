"""Testes dos parsers e do cliente HTTP, com a API mockada.

Os payloads seguem o schema documentado em https://geckoapi.com.br/docs para
os targets latamairlines.com:plp e voeazul.com.br:plp.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from src.fetch_prices import (
    GeckoAPIHTTPError,
    GeckoAPIParseError,
    GeckoAPITimeout,
    GeckoClient,
    build_azul_body,
    build_latam_body,
    fetch_azul,
    fetch_latam,
    parse_duration_to_minutes,
    parse_latam,
    parse_azul,
)

# --------------------------------------------------------------------------
# Payloads de exemplo
# --------------------------------------------------------------------------


def latam_item(origin, destination, departure, arrival, minutes, stops, amount):
    return {
        "recordType": "FLIGHT_OPTION",
        "position": {"flightOption": 1, "brand": 1},
        "route": {
            "originIata": origin,
            "destinationIata": destination,
            "departure": departure,
            "arrival": arrival,
        },
        "flight": {
            "flightCode": "LA1234",
            "durationMinutes": minutes,
            "stops": stops,
            "segments": [{"flightNumber": "1234"}],
        },
        "fare": {"brandId": "LIGHT", "brandText": "Light", "cabinLabel": "Economy"},
        "price": {
            "currency": "BRL",
            "amount": amount,
            "display": f"R$ {amount}",
            "total": amount,
        },
    }


LATAM_PAYLOAD = {
    "requestId": "req-1",
    "executionId": "exec-1",
    "data": {
        "source": "latamairlines.com",
        "type": "plp",
        "searchType": "ROUND_TRIP",
        "from": "BEL",
        "to": "NAT",
        "departureDate": "2026-12-27",
        "returnDate": "2027-01-05",
        "success": True,
        "totalResults": 4,
        "items": [
            latam_item("BEL", "NAT", "2026-12-27T08:15:00.000Z", "2026-12-27T12:40:00.000Z", 265, 1, 780.50),
            latam_item("BEL", "NAT", "2026-12-27T22:10:00.000Z", "2026-12-28T01:20:00.000Z", 190, 0, 940.00),
            latam_item("NAT", "BEL", "2027-01-05T14:00:00.000Z", "2027-01-05T17:05:00.000Z", 185, 0, 690.00),
            latam_item("NAT", "BEL", "2027-01-05T06:00:00.000Z", "2027-01-05T11:30:00.000Z", 330, 2, 615.25),
        ],
    },
}


def azul_journey(origin, destination, departure, arrival, duration, stops, amount):
    return {
        "position": 1,
        "id": "j1",
        "journeyKey": "key",
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "stopsCount": stops,
        "available": True,
        "duration": duration,
        "segments": [],
        "fares": [
            {
                "key": "f1",
                "productClass": {"code": "A", "category": "cat", "name": "Mais Azul"},
                "lowestFare": True,
                "total": {"currency": "BRL", "amount": amount},
            }
        ],
        "cheapestFare": {"key": "f1", "total": {"currency": "BRL", "amount": amount}},
    }


AZUL_PAYLOAD = {
    "requestId": "req-2",
    "executionId": "exec-2",
    "data": {
        "source": "voeazul.com.br",
        "type": "plp",
        "searchType": "ROUND_TRIP",
        "pricingMode": "cash",
        "from": "BEL",
        "to": "NAT",
        "currency": "BRL",
        "totalResults": 3,
        "trips": [
            {
                "position": 1,
                "origin": "BEL",
                "destination": "NAT",
                "date": "2026-12-27",
                "currency": "BRL",
                "journeys": [
                    azul_journey("BEL", "NAT", "2026-12-27T09:00:00", "2026-12-27T14:20:00", "PT5H20M", 1, 705.90),
                    azul_journey("BEL", "NAT", "2026-12-27T18:00:00", "2026-12-27T21:10:00", "PT3H10M", 0, 850.00),
                ],
            },
            {
                "position": 2,
                "origin": "NAT",
                "destination": "BEL",
                "date": "2027-01-05",
                "currency": "BRL",
                "journeys": [
                    azul_journey("NAT", "BEL", "2027-01-05T07:30:00", "2027-01-05T10:35:00", "PT3H05M", 0, 640.10),
                ],
            },
        ],
    },
}


# --------------------------------------------------------------------------
# Duracao
# --------------------------------------------------------------------------


class TestParseDuration:
    @pytest.mark.parametrize(
        "valor,esperado",
        [
            (265, 265),
            (265.0, 265),
            ("265", 265),
            ("PT5H20M", 320),
            ("PT3H", 180),
            ("PT45M", 45),
            ("P1DT2H30M", 1590),
            ("05:20", 320),
            ("05:20:00", 320),
            ("00:45", 45),
            (None, None),
            ("", None),
            ("mais ou menos 5 horas", None),
            (True, None),
        ],
    )
    def test_normaliza_para_minutos(self, valor, esperado):
        assert parse_duration_to_minutes(valor) == esperado


# --------------------------------------------------------------------------
# Corpos de request
# --------------------------------------------------------------------------


class TestRequestBodies:
    def test_latam_usa_target_e_type_separados(self, settings):
        body = build_latam_body(settings)
        assert body["target"] == "latamairlines.com"
        assert body["type"] == "plp"
        assert body["from"] == "BEL"
        assert body["to"] == "NAT"
        assert body["departureDate"] == "2026-12-27"
        assert body["returnDate"] == "2027-01-05"
        assert body["numAdults"] == 1

    def test_azul_manda_currency(self, settings):
        body = build_azul_body(settings)
        assert body["target"] == "voeazul.com.br"
        assert body["type"] == "plp"
        assert body["currency"] == "BRL"


# --------------------------------------------------------------------------
# Parser LATAM
# --------------------------------------------------------------------------


class TestParseLatam:
    def test_soma_o_mais_barato_de_cada_sentido(self, settings):
        offer = parse_latam(LATAM_PAYLOAD, settings)
        assert offer.airline == "LATAM"
        assert offer.price == pytest.approx(780.50 + 615.25)
        assert offer.currency == "BRL"

    def test_ida_vem_do_item_mais_barato_saindo_da_origem(self, settings):
        offer = parse_latam(LATAM_PAYLOAD, settings)
        assert offer.outbound.origin == "BEL"
        assert offer.outbound.duration_minutes == 265
        assert offer.outbound.stops == 1

    def test_volta_vem_do_item_mais_barato_saindo_do_destino(self, settings):
        offer = parse_latam(LATAM_PAYLOAD, settings)
        assert offer.inbound.origin == "NAT"
        assert offer.inbound.duration_minutes == 330
        assert offer.inbound.stops == 2

    def test_guarda_a_resposta_bruta(self, settings):
        assert parse_latam(LATAM_PAYLOAD, settings).raw_response is LATAM_PAYLOAD

    def test_validade_de_tarifa_fica_none(self, settings):
        """A GeckoAPI nao expoe validade; o campo precisa ficar explicitamente None."""
        assert parse_latam(LATAM_PAYLOAD, settings).fare_valid_until is None

    def test_so_ida_ainda_gera_oferta(self, settings):
        payload = {"data": {"success": True, "items": [LATAM_PAYLOAD["data"]["items"][0]]}}
        offer = parse_latam(payload, settings)
        assert offer.price == pytest.approx(780.50)
        assert offer.inbound is None

    def test_sem_data_levanta(self, settings):
        with pytest.raises(GeckoAPIParseError, match="sem o objeto 'data'"):
            parse_latam({"requestId": "x"}, settings)

    def test_items_vazio_levanta(self, settings):
        with pytest.raises(GeckoAPIParseError, match="vazio ou ausente"):
            parse_latam({"data": {"items": []}}, settings)

    def test_success_false_levanta(self, settings):
        with pytest.raises(GeckoAPIParseError, match="success=false"):
            parse_latam({"data": {"success": False, "items": [1]}}, settings)

    def test_nenhum_voo_da_origem_levanta(self, settings):
        payload = {"data": {"success": True, "items": [LATAM_PAYLOAD["data"]["items"][2]]}}
        with pytest.raises(GeckoAPIParseError, match="nenhum voo saindo de BEL"):
            parse_latam(payload, settings)

    def test_campos_nulos_nao_quebram(self, settings):
        """A doc avisa que campos podem vir null em producao."""
        item = latam_item("BEL", "NAT", None, None, None, None, 500.0)
        offer = parse_latam({"data": {"success": True, "items": [item]}}, settings)
        assert offer.price == 500.0
        assert offer.outbound.duration_minutes is None
        assert offer.outbound.stops is None
        assert offer.outbound.duration_label == "nao informado"
        assert offer.outbound.stops_label == "nao informado"


# --------------------------------------------------------------------------
# Parser Azul
# --------------------------------------------------------------------------


class TestParseAzul:
    def test_soma_a_journey_mais_barata_de_cada_trip(self, settings):
        offer = parse_azul(AZUL_PAYLOAD, settings)
        assert offer.airline == "AZUL"
        assert offer.price == pytest.approx(705.90 + 640.10)

    def test_converte_duracao_iso_para_minutos(self, settings):
        offer = parse_azul(AZUL_PAYLOAD, settings)
        assert offer.outbound.duration_minutes == 320
        assert offer.inbound.duration_minutes == 185

    def test_le_conexoes(self, settings):
        offer = parse_azul(AZUL_PAYLOAD, settings)
        assert offer.outbound.stops == 1
        assert offer.inbound.stops == 0
        assert offer.outbound.stops_label == "1 conexao"
        assert offer.inbound.stops_label == "direto"

    def test_ignora_journey_indisponivel(self, settings):
        payload = {
            "data": {
                "currency": "BRL",
                "trips": [
                    {
                        "origin": "BEL",
                        "destination": "NAT",
                        "journeys": [
                            {**azul_journey("BEL", "NAT", None, None, "PT3H", 0, 100.0), "available": False},
                            azul_journey("BEL", "NAT", None, None, "PT4H", 1, 500.0),
                        ],
                    }
                ],
            }
        }
        assert parse_azul(payload, settings).price == pytest.approx(500.0)

    def test_cai_para_fares_quando_nao_ha_cheapestFare(self, settings):
        journey = azul_journey("BEL", "NAT", None, None, "PT3H", 0, 400.0)
        del journey["cheapestFare"]
        journey["fares"].append(
            {"key": "f2", "total": {"currency": "BRL", "amount": 310.0}}
        )
        payload = {"data": {"currency": "BRL", "trips": [{"origin": "BEL", "journeys": [journey]}]}}
        assert parse_azul(payload, settings).price == pytest.approx(310.0)

    def test_usa_ordem_dos_trips_quando_a_origem_nao_bate(self, settings):
        payload = {
            "data": {
                "currency": "BRL",
                "trips": [
                    {"origin": "???", "journeys": [azul_journey("BEL", "NAT", None, None, "PT3H", 0, 300.0)]},
                ],
            }
        }
        assert parse_azul(payload, settings).price == pytest.approx(300.0)

    def test_trips_vazio_levanta_com_a_notificacao_da_api(self, settings):
        payload = {"data": {"trips": [], "notifications": [{"code": "E1", "message": "sem voos"}]}}
        with pytest.raises(GeckoAPIParseError, match="sem voos"):
            parse_azul(payload, settings)

    def test_sem_tarifa_disponivel_levanta(self, settings):
        payload = {"data": {"trips": [{"origin": "BEL", "journeys": []}]}}
        with pytest.raises(GeckoAPIParseError, match="nenhuma journey de ida"):
            parse_azul(payload, settings)

    def test_validade_de_tarifa_fica_none(self, settings):
        assert parse_azul(AZUL_PAYLOAD, settings).fare_valid_until is None


# --------------------------------------------------------------------------
# Cliente HTTP
# --------------------------------------------------------------------------


def fake_response(status_code=200, json_data=None, text=""):
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data if json_data is not None else {}
    return response


class TestGeckoClient:
    def test_envia_bearer_token(self, settings):
        session = Mock()
        session.post.return_value = fake_response(json_data=LATAM_PAYLOAD)
        client = GeckoClient("minha-chave", session=session)

        client.extract({"target": "x"}, "LATAM")

        headers = session.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer minha-chave"

    def test_sucesso_devolve_json(self, settings):
        session = Mock()
        session.post.return_value = fake_response(json_data=LATAM_PAYLOAD)
        client = GeckoClient("k", session=session)
        assert client.extract({}, "LATAM") == LATAM_PAYLOAD

    def test_debita_credito_por_tentativa(self, settings):
        session = Mock()
        session.post.return_value = fake_response(json_data=LATAM_PAYLOAD)
        gastos = []
        client = GeckoClient("k", session=session, credit_hook=gastos.append)

        client.extract({}, "LATAM")
        assert len(gastos) == 1

    def test_retry_em_500_debita_dois_creditos(self, settings):
        session = Mock()
        session.post.side_effect = [
            fake_response(status_code=500, text="boom"),
            fake_response(json_data=LATAM_PAYLOAD),
        ]
        gastos = []
        client = GeckoClient("k", max_retries=1, session=session, credit_hook=gastos.append)

        assert client.extract({}, "LATAM") == LATAM_PAYLOAD
        assert len(gastos) == 2

    def test_4xx_nao_faz_retry(self, settings):
        session = Mock()
        session.post.return_value = fake_response(status_code=401, text="unauthorized")
        client = GeckoClient("k", max_retries=3, session=session)

        with pytest.raises(GeckoAPIHTTPError) as excinfo:
            client.extract({}, "LATAM")
        assert excinfo.value.status_code == 401
        assert session.post.call_count == 1

    def test_timeout_esgota_as_tentativas(self, settings):
        session = Mock()
        session.post.side_effect = requests.Timeout("estourou")
        client = GeckoClient("k", max_retries=2, session=session)

        with pytest.raises(GeckoAPITimeout):
            client.extract({}, "LATAM")
        assert session.post.call_count == 3

    def test_erro_de_conexao_nao_debita_credito(self, settings):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("sem rede")
        gastos = []
        client = GeckoClient("k", max_retries=1, session=session, credit_hook=gastos.append)

        with pytest.raises(Exception):
            client.extract({}, "LATAM")
        assert gastos == []

    def test_budget_guard_bloqueia_antes_de_chamar(self, settings):
        session = Mock()
        client = GeckoClient("k", session=session, budget_guard=lambda: False)

        with pytest.raises(Exception, match="Sem creditos"):
            client.extract({}, "LATAM")
        session.post.assert_not_called()

    def test_json_invalido_levanta_parse_error(self, settings):
        response = fake_response(text="<html>erro</html>")
        response.json.side_effect = ValueError("nao e json")
        session = Mock()
        session.post.return_value = response
        client = GeckoClient("k", session=session)

        with pytest.raises(GeckoAPIParseError, match="nao e JSON valido"):
            client.extract({}, "LATAM")


class TestFetchers:
    def test_fetch_latam_ponta_a_ponta(self, settings):
        session = Mock()
        session.post.return_value = fake_response(json_data=LATAM_PAYLOAD)
        offer = fetch_latam(GeckoClient("k", session=session), settings)
        assert offer.airline == "LATAM"
        assert offer.price == pytest.approx(1395.75)

    def test_fetch_azul_ponta_a_ponta(self, settings):
        session = Mock()
        session.post.return_value = fake_response(json_data=AZUL_PAYLOAD)
        offer = fetch_azul(GeckoClient("k", session=session), settings)
        assert offer.airline == "AZUL"
        assert offer.price == pytest.approx(1346.0)
