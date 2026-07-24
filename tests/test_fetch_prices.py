"""Testes do parser do KAYAK e do cliente HTTP, com a API mockada.

Os payloads seguem o schema documentado em
https://geckoapi.com.br/docs/kayak-com-br-plp
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from src.fetch_prices import (
    GeckoAPIError,
    GeckoAPIHTTPError,
    GeckoAPIParseError,
    GeckoAPITimeout,
    GeckoClient,
    build_kayak_body,
    duration_from_timestamps,
    fetch_kayak,
    parse_duration_to_minutes,
    parse_kayak,
)

# --------------------------------------------------------------------------
# Payloads de exemplo
# --------------------------------------------------------------------------


def segment(origin, destination, departure, arrival, minutes, code, name, seats=9):
    return {
        "id": "s1",
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "durationMinutes": minutes,
        "airlineCode": code,
        "airlineName": name,
        "flightNumber": "1234",
        "cabinCode": "e",
        "cabinDisplay": "Economica",
        "seatsRemaining": seats,
    }


def leg(origin, destination, departure, arrival, minutes, stops, codes, segments):
    return {
        "id": "l1",
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "durationMinutes": minutes,
        "stops": stops,
        "airlineCodes": codes,
        "segments": segments,
    }


def booking(provider, amount, is_direct=True):
    return {
        "bookingId": "b1",
        "providerCode": provider[:3].upper(),
        "providerName": provider,
        "price": {"currency": "BRL", "amount": amount, "localizedPrice": f"R$ {amount}"},
        "totalPrice": {"currency": "BRL", "amount": amount, "localizedPrice": f"R$ {amount}"},
        "bookingUrl": f"https://exemplo/{provider}",
        "isDirect": is_direct,
    }


def item(amount, code="G3", name="GOL", stops=1, seats=9, cheapest=False, bookings=None):
    return {
        "position": 1,
        "tripId": "t1",
        "isCheapest": cheapest,
        "isBest": False,
        "price": {"currency": "BRL", "amount": amount, "localizedPrice": f"R$ {amount}"},
        "shareableUrl": "https://kayak.com.br/trip/1",
        "legs": [
            leg(
                "BEL", "NAT", "2026-12-27T08:15:00", "2026-12-27T12:40:00", 265, stops, [code],
                [segment("BEL", "NAT", "2026-12-27T08:15:00", "2026-12-27T12:40:00", 265,
                         code, name, seats)],
            ),
            leg(
                "NAT", "BEL", "2027-01-05T14:00:00", "2027-01-05T17:05:00", 185, 0, [code],
                [segment("NAT", "BEL", "2027-01-05T14:00:00", "2027-01-05T17:05:00", 185,
                         code, name, seats + 4)],
            ),
        ],
        "bookingOptions": bookings if bookings is not None else [booking(name, amount)],
    }


KAYAK_PAYLOAD = {
    "requestId": "req-1",
    "executionId": "exec-1",
    "data": {
        "source": "kayak.com.br",
        "type": "plp",
        "success": True,
        "status": "complete",
        "from": "BEL",
        "to": "NAT",
        "departureDate": "2026-12-27",
        "returnDate": "2027-01-05",
        "currency": "BRL",
        "totalResults": 1111,
        "cheapestPrice": {"currency": "BRL", "amount": 1180.00},
        "airlines": [
            {"code": "G3", "name": "GOL", "logoUrl": "x"},
            {"code": "AD", "name": "Azul", "logoUrl": "y"},
            {"code": "LA", "name": "LATAM", "logoUrl": "z"},
        ],
        "items": [
            item(1512.00, "LA", "LATAM", stops=1, seats=9),
            item(1180.00, "G3", "GOL", stops=1, seats=2, cheapest=True,
                 bookings=[booking("123Milhas", 1180.00, is_direct=False),
                           booking("GOL", 1290.00, is_direct=True)]),
            item(1346.00, "AD", "Azul", stops=0, seats=7),
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
            (265, 265), (265.0, 265), ("265", 265),
            ("PT5H20M", 320), ("PT3H", 180), ("PT45M", 45), ("P1DT2H30M", 1590),
            ("05:20", 320), ("05:20:00", 320), ("00:45", 45),
            (None, None), ("", None), ("mais ou menos 5 horas", None), (True, None),
        ],
    )
    def test_normaliza_para_minutos(self, valor, esperado):
        assert parse_duration_to_minutes(valor) == esperado


class TestDuracaoPelosHorarios:
    @pytest.mark.parametrize(
        "saida,chegada,esperado",
        [
            ("2026-12-27T09:00:00", "2026-12-27T14:20:00", 320),
            ("2026-12-27T09:00:00.000Z", "2026-12-27T12:40:00.000Z", 220),
            ("2026-12-27T22:10:00", "2026-12-28T01:20:00", 190),  # vira o dia
            ("2026-12-27T09:00:00-03:00", "2026-12-27T14:20:00-03:00", 320),
            (None, "2026-12-27T14:20:00", None),
            ("nao e data", "2026-12-27T14:20:00", None),
            ("2026-12-27T14:20:00", "2026-12-27T09:00:00", None),  # chegada antes
            ("2026-12-27T09:00:00", "2026-12-27T09:00:00", None),  # zero
        ],
    )
    def test_calcula(self, saida, chegada, esperado):
        assert duration_from_timestamps(saida, chegada) == esperado

    def test_fusos_incompativeis_nao_quebram(self):
        assert duration_from_timestamps("2026-12-27T09:00:00", "2026-12-27T14:20:00Z") is None

    def test_leg_sem_durationMinutes_usa_os_horarios(self, settings):
        payload = {"data": {"success": True, "items": [item(900.0)]}}
        payload["data"]["items"][0]["legs"][0]["durationMinutes"] = None
        assert parse_kayak(payload, settings).outbound.duration_minutes == 265


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


class TestRequestBody:
    def test_alvo_e_parametros(self, settings):
        body = build_kayak_body(settings)
        assert body["target"] == "kayak.com.br"
        assert body["type"] == "plp"
        assert body["from"] == "BEL"
        assert body["to"] == "NAT"
        assert body["departureDate"] == "2026-12-27"
        assert body["returnDate"] == "2027-01-05"
        assert body["currency"] == "BRL"
        assert body["lang"] == "pt-BR"

    def test_nao_filtra_companhia(self, settings):
        """A companhia vem na resposta; filtrar na busca esconderia a mais barata."""
        assert not any("airline" in chave.lower() for chave in build_kayak_body(settings))


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class TestParseKayak:
    def test_escolhe_a_viagem_mais_barata(self, settings):
        assert parse_kayak(KAYAK_PAYLOAD, settings).price == pytest.approx(1180.00)

    def test_identifica_a_companhia(self, settings):
        assert parse_kayak(KAYAK_PAYLOAD, settings).airline == "GOL"

    def test_identifica_o_vendedor_mais_barato(self, settings):
        offer = parse_kayak(KAYAK_PAYLOAD, settings)
        assert offer.provider == "123Milhas"
        assert offer.provider_is_direct is False
        assert offer.provider_label == "123Milhas (agencia)"

    def test_venda_direta_e_rotulada(self, settings):
        payload = {"data": {"success": True, "items": [
            item(900.0, bookings=[booking("Azul", 900.0, is_direct=True)])
        ]}}
        assert parse_kayak(payload, settings).provider_label == "Azul (venda direta)"

    def test_ida_e_volta_vem_dos_dois_legs(self, settings):
        offer = parse_kayak(KAYAK_PAYLOAD, settings)
        assert offer.outbound.origin == "BEL"
        assert offer.outbound.duration_minutes == 265
        assert offer.outbound.stops == 1
        assert offer.inbound.origin == "NAT"
        assert offer.inbound.duration_minutes == 185
        assert offer.inbound.stops == 0

    def test_assentos_restantes_pega_o_trecho_mais_apertado(self, settings):
        assert parse_kayak(KAYAK_PAYLOAD, settings).seats_remaining == 2

    def test_total_de_opcoes_avaliadas(self, settings):
        assert parse_kayak(KAYAK_PAYLOAD, settings).total_options == 1111

    def test_guarda_a_resposta_bruta(self, settings):
        assert parse_kayak(KAYAK_PAYLOAD, settings).raw_response is KAYAK_PAYLOAD

    def test_validade_de_tarifa_fica_none(self, settings):
        assert parse_kayak(KAYAK_PAYLOAD, settings).fare_valid_until is None

    def test_preco_menor_vence_a_flag_isCheapest(self, settings):
        """A flag e do ranking do KAYAK; o preco e o que voce paga."""
        payload = {"data": {"success": True, "items": [
            item(1500.0, cheapest=True),
            item(1100.0, "AD", "Azul"),
        ]}}
        assert parse_kayak(payload, settings).price == pytest.approx(1100.0)

    def test_voo_com_duas_companhias(self, settings):
        it = item(1000.0, "G3", "GOL")
        it["legs"][1]["segments"][0]["airlineName"] = "LATAM"
        assert parse_kayak({"data": {"success": True, "items": [it]}}, settings).airline == "GOL + LATAM"

    def test_usa_o_mapa_de_airlines_quando_o_segmento_so_tem_codigo(self, settings):
        it = item(1000.0, "AD", "Azul")
        for perna in it["legs"]:
            for seg in perna["segments"]:
                seg["airlineName"] = None
        payload = {"data": {"success": True, "airlines": [{"code": "AD", "name": "Azul"}],
                            "items": [it]}}
        assert parse_kayak(payload, settings).airline == "Azul"

    def test_so_ida_nao_quebra(self, settings):
        it = item(800.0)
        it["legs"] = it["legs"][:1]
        offer = parse_kayak({"data": {"success": True, "items": [it]}}, settings)
        assert offer.inbound is None
        assert offer.outbound is not None

    def test_sem_bookingOptions_cai_para_shareableUrl(self, settings):
        it = item(800.0, bookings=[])
        offer = parse_kayak({"data": {"success": True, "items": [it]}}, settings)
        assert offer.provider is None
        assert offer.provider_label == "vendedor nao informado"
        assert offer.booking_url == "https://kayak.com.br/trip/1"


class TestParseKayakErros:
    def test_sem_data(self, settings):
        payload = {"requestId": "x"}
        with pytest.raises(GeckoAPIParseError, match="sem o objeto 'data'") as exc:
            parse_kayak(payload, settings)
        assert exc.value.payload is payload

    def test_items_vazio(self, settings):
        payload = {"data": {"items": []}}
        with pytest.raises(GeckoAPIParseError, match="vazio ou ausente") as exc:
            parse_kayak(payload, settings)
        assert exc.value.payload is payload

    def test_success_false(self, settings):
        payload = {"data": {"success": False, "items": [item(100.0)]}}
        with pytest.raises(GeckoAPIParseError, match="success=false") as exc:
            parse_kayak(payload, settings)
        assert exc.value.payload is payload

    def test_nenhum_item_com_preco(self, settings):
        it = item(100.0)
        it["price"] = {}
        payload = {"data": {"success": True, "items": [it]}}
        with pytest.raises(GeckoAPIParseError, match="price.amount") as exc:
            parse_kayak(payload, settings)
        assert exc.value.payload is payload

    def test_item_sem_legs(self, settings):
        it = item(100.0)
        it["legs"] = []
        with pytest.raises(GeckoAPIParseError, match="nao tem legs"):
            parse_kayak({"data": {"success": True, "items": [it]}}, settings)

    def test_payload_e_opcional_na_excecao(self):
        assert GeckoAPIParseError("erro sem payload").payload is None


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
    def test_envia_bearer_token(self):
        session = Mock()
        session.post.return_value = fake_response(json_data=KAYAK_PAYLOAD)
        GeckoClient("minha-chave", session=session).extract({"target": "x"}, "KAYAK")
        assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer minha-chave"

    def test_sucesso_devolve_json(self):
        session = Mock()
        session.post.return_value = fake_response(json_data=KAYAK_PAYLOAD)
        assert GeckoClient("k", session=session).extract({}, "KAYAK") == KAYAK_PAYLOAD

    def test_debita_credito_por_tentativa(self):
        session = Mock()
        session.post.return_value = fake_response(json_data=KAYAK_PAYLOAD)
        gastos = []
        GeckoClient("k", session=session, credit_hook=gastos.append).extract({}, "KAYAK")
        assert len(gastos) == 1

    def test_4xx_nao_faz_retry(self):
        session = Mock()
        session.post.return_value = fake_response(status_code=401, text="unauthorized")
        client = GeckoClient("k", max_retries=3, session=session)
        with pytest.raises(GeckoAPIHTTPError) as exc:
            client.extract({}, "KAYAK")
        assert exc.value.status_code == 401
        assert session.post.call_count == 1

    def test_timeout_esgota_as_tentativas(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("estourou")
        client = GeckoClient("k", max_retries=2, retry_delay_seconds=0, session=session)
        with pytest.raises(GeckoAPITimeout):
            client.extract({}, "KAYAK")
        assert session.post.call_count == 3

    def test_erro_de_conexao_nao_debita_credito(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("sem rede")
        gastos = []
        client = GeckoClient("k", max_retries=1, retry_delay_seconds=0,
                             session=session, credit_hook=gastos.append)
        with pytest.raises(GeckoAPIError):
            client.extract({}, "KAYAK")
        assert gastos == []

    def test_budget_guard_bloqueia_antes_de_chamar(self):
        session = Mock()
        client = GeckoClient("k", session=session, budget_guard=lambda: False)
        with pytest.raises(GeckoAPIError, match="Sem creditos"):
            client.extract({}, "KAYAK")
        session.post.assert_not_called()

    def test_json_invalido_levanta_parse_error(self):
        response = fake_response(text="<html>erro</html>")
        response.json.side_effect = ValueError("nao e json")
        session = Mock()
        session.post.return_value = response
        with pytest.raises(GeckoAPIParseError, match="nao e JSON valido"):
            GeckoClient("k", session=session).extract({}, "KAYAK")


class TestRetryConscienteDeCusto:
    """Cada tentativa custa 5 creditos, entao a politica de retry e financeira."""

    def test_default_e_uma_retentativa(self):
        assert GeckoClient("k").max_retries == 1

    def test_pausa_entre_tentativas(self):
        session = Mock()
        session.post.side_effect = [
            fake_response(status_code=504, text="UPSTREAM_TIMEOUT"),
            fake_response(json_data=KAYAK_PAYLOAD),
        ]
        pausas = []
        client = GeckoClient("k", max_retries=1, retry_delay_seconds=60,
                             session=session, sleep_fn=pausas.append)
        client.extract({}, "KAYAK")
        assert pausas == [60], "retry imediato encontra a mesma lentidao e queima credito"

    def test_nao_pausa_antes_da_primeira_tentativa(self):
        session = Mock()
        session.post.return_value = fake_response(json_data=KAYAK_PAYLOAD)
        pausas = []
        GeckoClient("k", retry_delay_seconds=60, session=session,
                    sleep_fn=pausas.append).extract({}, "KAYAK")
        assert pausas == []

    def test_504_custa_no_maximo_duas_tentativas(self):
        session = Mock()
        session.post.return_value = fake_response(status_code=504, text="UPSTREAM_TIMEOUT")
        gastos = []
        client = GeckoClient("k", max_retries=1, retry_delay_seconds=0,
                             session=session, credit_hook=gastos.append)
        with pytest.raises(GeckoAPIHTTPError):
            client.extract({}, "KAYAK")
        assert len(gastos) == 2
        assert session.post.call_count == 2

    def test_budget_guard_corta_o_retry_no_meio(self):
        session = Mock()
        session.post.return_value = fake_response(status_code=504, text="timeout")
        chamadas = {"n": 0}

        def guard():
            chamadas["n"] += 1
            return chamadas["n"] <= 1

        client = GeckoClient("k", max_retries=3, retry_delay_seconds=0,
                             session=session, budget_guard=guard)
        with pytest.raises(GeckoAPIError, match="Sem creditos"):
            client.extract({}, "KAYAK")
        assert session.post.call_count == 1


class TestEstornoDe5xx:
    def test_504_debita_e_estorna(self):
        session = Mock()
        session.post.return_value = fake_response(status_code=504, text="UPSTREAM_TIMEOUT")
        debitos, estornos = [], []
        client = GeckoClient("k", max_retries=1, retry_delay_seconds=0, session=session,
                             credit_hook=debitos.append, refund_hook=estornos.append)
        with pytest.raises(GeckoAPIHTTPError):
            client.extract({}, "KAYAK")
        assert len(debitos) == 2
        assert len(estornos) == 2, "liquido zero: a GeckoAPI devolve o que nao entregou"

    def test_sucesso_nao_estorna(self):
        session = Mock()
        session.post.return_value = fake_response(json_data=KAYAK_PAYLOAD)
        debitos, estornos = [], []
        GeckoClient("k", session=session, credit_hook=debitos.append,
                    refund_hook=estornos.append).extract({}, "KAYAK")
        assert len(debitos) == 1
        assert estornos == []

    def test_4xx_nao_estorna(self):
        """Chave invalida e erro do cliente; nao ha promessa de estorno."""
        session = Mock()
        session.post.return_value = fake_response(status_code=401, text="unauthorized")
        estornos = []
        client = GeckoClient("k", session=session, refund_hook=estornos.append)
        with pytest.raises(GeckoAPIHTTPError):
            client.extract({}, "KAYAK")
        assert estornos == []

    def test_timeout_do_cliente_nao_estorna(self):
        """O request pode ter sido concluido do lado deles; erra para o lado seguro."""
        session = Mock()
        session.post.side_effect = requests.Timeout("estourou")
        debitos, estornos = [], []
        client = GeckoClient("k", max_retries=0, session=session,
                             credit_hook=debitos.append, refund_hook=estornos.append)
        with pytest.raises(GeckoAPITimeout):
            client.extract({}, "KAYAK")
        assert len(debitos) == 1
        assert estornos == []


class TestFetchKayak:
    def test_ponta_a_ponta(self, settings):
        session = Mock()
        session.post.return_value = fake_response(json_data=KAYAK_PAYLOAD)
        offer = fetch_kayak(GeckoClient("k", session=session), settings)
        assert offer.price == pytest.approx(1180.00)
        assert offer.airline == "GOL"
        assert offer.provider == "123Milhas"


class TestConsultaDeSaldo:
    """GET /v1/me/credits - endpoint de conta, nao despacha extracao."""

    SALDO = {
        "userId": "user-1",
        "currentCredits": 95,
        "planId": "free",
        "updatedAt": "2026-07-24T12:00:00.000Z",
        "creditsConsumed": {"last24Hours": 5, "last7Days": 15, "last30Days": 55},
    }

    def _client(self, resposta):
        session = Mock()
        session.get.return_value = resposta
        return GeckoClient("k", session=session), session

    def test_le_todos_os_campos(self):
        client, _ = self._client(fake_response(json_data=self.SALDO))
        saldo = client.get_credits()
        assert saldo.current_credits == 95
        assert saldo.plan_id == "free"
        assert saldo.last_24h == 5
        assert saldo.last_7d == 15
        assert saldo.last_30d == 55
        assert saldo.updated_at == "2026-07-24T12:00:00.000Z"

    def test_usa_get_no_endpoint_certo(self):
        client, session = self._client(fake_response(json_data=self.SALDO))
        client.get_credits()
        assert session.get.call_args.args[0] == "https://api.geckoapi.com.br/v1/me/credits"
        assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer k"

    def test_nao_debita_credito(self):
        """Endpoint de conta; se um dia se confirmar que cobra, plugamos o hook."""
        session = Mock()
        session.get.return_value = fake_response(json_data=self.SALDO)
        gastos = []
        GeckoClient("k", session=session, credit_hook=gastos.append).get_credits()
        assert gastos == []

    def test_campos_de_consumo_ausentes_nao_quebram(self):
        client, _ = self._client(fake_response(json_data={"currentCredits": 40}))
        saldo = client.get_credits()
        assert saldo.current_credits == 40
        assert saldo.last_24h is None
        assert saldo.plan_id is None

    def test_sem_currentCredits_levanta_com_payload(self):
        payload = {"userId": "x", "planId": "free"}
        client, _ = self._client(fake_response(json_data=payload))
        with pytest.raises(GeckoAPIParseError, match="currentCredits") as exc:
            client.get_credits()
        assert exc.value.payload == payload

    def test_401_levanta_http_error(self):
        client, _ = self._client(fake_response(status_code=401, text="unauthorized"))
        with pytest.raises(GeckoAPIHTTPError):
            client.get_credits()

    def test_timeout_levanta(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("estourou")
        with pytest.raises(GeckoAPITimeout):
            GeckoClient("k", session=session).get_credits()

    def test_nao_faz_retry(self):
        """Consultar saldo nao e caro, mas repetir a toa tambem nao ajuda."""
        session = Mock()
        session.get.side_effect = requests.Timeout("estourou")
        with pytest.raises(GeckoAPITimeout):
            GeckoClient("k", max_retries=3, session=session).get_credits()
        assert session.get.call_count == 1
