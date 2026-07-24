"""Cliente da GeckoAPI e parsers de LATAM e Azul.

Endpoint unico: POST https://api.geckoapi.com.br/v1/extract
Auth: header Authorization: Bearer <chave>
Custo: 5 creditos por request.

Os dois targets devolvem formatos bem diferentes (confirmado na doc oficial em
https://geckoapi.com.br/docs):

  latamairlines.com + type=plp -> data.items[]  (lista plana de opcoes)
  voeazul.com.br    + type=plp -> data.trips[].journeys[]  (aninhado por trecho)

Por isso cada um tem seu proprio parser, normalizando para models.Offer.
Todo campo e lido de forma defensiva: a doc avisa que "alguns campos podem
retornar null em producao", e a resposta bruta e sempre salva no banco.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable

import requests

from .config import AZUL, LATAM, Settings
from .models import Leg, Offer

logger = logging.getLogger(__name__)

API_URL = "https://api.geckoapi.com.br/v1/extract"

TARGET_LATAM = "latamairlines.com"
TARGET_AZUL = "voeazul.com.br"
EXTRACT_TYPE = "plp"


class GeckoAPIError(RuntimeError):
    """Falha generica ao falar com a GeckoAPI."""


class GeckoAPITimeout(GeckoAPIError):
    """A API nao respondeu dentro do timeout."""


class GeckoAPIHTTPError(GeckoAPIError):
    """A API respondeu com status de erro."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class GeckoAPIParseError(GeckoAPIError):
    """A resposta veio, mas nao tem o formato esperado.

    Carrega o payload junto: o credito ja foi cobrado, entao a resposta bruta
    e a unica coisa de valor que sobrou da chamada, e e o que permite corrigir
    o parser sem gastar credito de novo.
    """

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload


# --------------------------------------------------------------------------
# Helpers de normalizacao
# --------------------------------------------------------------------------

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)
_CLOCK_DURATION = re.compile(r"^(?P<hours>\d{1,3}):(?P<minutes>\d{2})(?::(?P<seconds>\d{2}))?$")


def parse_duration_to_minutes(value: Any) -> int | None:
    """Normaliza duracao para minutos.

    A LATAM manda `durationMinutes` (numero) e a Azul manda `duration` (string).
    A doc da Azul nao fixa o formato da string, entao aceitamos os tres que
    aparecem na pratica: ISO-8601 (PT2H10M), relogio (02:10 / 02:10:00) e
    numero puro.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None

    text = value.strip().upper()
    if not text:
        return None

    if text.isdigit():
        return int(text)

    match = _ISO_DURATION.match(text)
    if match and any(match.groupdict().values()):
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        return days * 24 * 60 + hours * 60 + minutes + seconds // 60

    match = _CLOCK_DURATION.match(text)
    if match:
        hours = int(match.group("hours"))
        minutes = int(match.group("minutes"))
        seconds = int(match.group("seconds") or 0)
        return hours * 60 + minutes + seconds // 60

    logger.warning("Nao consegui interpretar a duracao %r", value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _dig(payload: Any, *keys: str, default: Any = None) -> Any:
    """Acesso encadeado tolerante a None e a chaves ausentes."""
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


# --------------------------------------------------------------------------
# Cliente HTTP
# --------------------------------------------------------------------------


class GeckoClient:
    """Wrapper fino sobre requests, com retry consciente de creditos.

    Cada tentativa HTTP custa creditos, entao:
      * `credit_hook` e chamado logo apos cada request ser despachado, para o
        ledger registrar o gasto mesmo se a resposta falhar depois;
      * `budget_guard` e consultado antes de cada tentativa (inclusive antes
        de um retry) e aborta se nao houver saldo.
    """

    def __init__(
        self,
        api_key: str,
        timeout: int = 120,
        max_retries: int = 2,
        credit_hook: Callable[[str], None] | None = None,
        budget_guard: Callable[[], bool] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.credit_hook = credit_hook or (lambda _label: None)
        self.budget_guard = budget_guard or (lambda: True)
        self.session = session or requests.Session()

    def extract(self, body: dict[str, Any], label: str) -> dict[str, Any]:
        """Executa POST /v1/extract e devolve o JSON, com retry em falha transitoria."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 2):
            if not self.budget_guard():
                raise GeckoAPIError(
                    f"Sem creditos para a tentativa {attempt} de {label}; abortando."
                )

            logger.info("GeckoAPI | %s | tentativa %s | body=%s", label, attempt, body)
            try:
                response = self.session.post(
                    API_URL, json=body, headers=headers, timeout=self.timeout
                )
            except requests.Timeout as exc:
                # O request saiu, entao o credito provavelmente foi cobrado.
                self.credit_hook(f"{label} (tentativa {attempt}, timeout)")
                last_error = GeckoAPITimeout(f"Timeout de {self.timeout}s em {label}: {exc}")
                logger.warning("GeckoAPI | %s | timeout na tentativa %s", label, attempt)
                continue
            except requests.RequestException as exc:
                # Falha de conexao: nao houve request completo, nao cobra credito.
                last_error = GeckoAPIError(f"Erro de conexao em {label}: {exc}")
                logger.warning("GeckoAPI | %s | erro de conexao: %s", label, exc)
                continue

            self.credit_hook(f"{label} (tentativa {attempt}, HTTP {response.status_code})")

            if response.status_code >= 500 or response.status_code == 429:
                last_error = GeckoAPIHTTPError(response.status_code, response.text)
                logger.warning(
                    "GeckoAPI | %s | HTTP %s na tentativa %s",
                    label,
                    response.status_code,
                    attempt,
                )
                continue

            if response.status_code >= 400:
                # 4xx nao adianta repetir (chave invalida, parametro errado, sem credito).
                raise GeckoAPIHTTPError(response.status_code, response.text)

            try:
                payload = response.json()
            except ValueError as exc:
                raise GeckoAPIParseError(
                    f"{label}: resposta nao e JSON valido ({response.text[:200]!r})"
                ) from exc

            if not isinstance(payload, dict):
                raise GeckoAPIParseError(f"{label}: esperava objeto JSON, veio {type(payload)}")

            logger.info("GeckoAPI | %s | resposta OK na tentativa %s", label, attempt)
            return payload

        raise last_error or GeckoAPIError(f"{label}: falhou sem erro registrado")


# --------------------------------------------------------------------------
# Montagem dos corpos de request
# --------------------------------------------------------------------------


def build_latam_body(settings: Settings) -> dict[str, Any]:
    return {
        "target": TARGET_LATAM,
        "type": EXTRACT_TYPE,
        "from": settings.origin,
        "to": settings.destination,
        "departureDate": settings.departure_date,
        "returnDate": settings.return_date,
        "numAdults": settings.num_adults,
        "numChildren": settings.num_children,
        "numInfants": settings.num_infants,
    }


def build_azul_body(settings: Settings) -> dict[str, Any]:
    return {
        "target": TARGET_AZUL,
        "type": EXTRACT_TYPE,
        "from": settings.origin,
        "to": settings.destination,
        "departureDate": settings.departure_date,
        "returnDate": settings.return_date,
        "numAdults": settings.num_adults,
        "numChildren": settings.num_children,
        "numInfants": settings.num_infants,
        "currency": settings.currency,
    }


# --------------------------------------------------------------------------
# Parser LATAM: data.items[]
# --------------------------------------------------------------------------


def _latam_item_price(item: dict[str, Any]) -> float | None:
    price = item.get("price") or {}
    return _as_float(price.get("amount")) or _as_float(price.get("total"))


def _latam_item_to_leg(item: dict[str, Any]) -> Leg:
    route = item.get("route") or {}
    flight = item.get("flight") or {}
    return Leg(
        origin=str(route.get("originIata") or ""),
        destination=str(route.get("destinationIata") or ""),
        departure=route.get("departure"),
        arrival=route.get("arrival"),
        duration_minutes=parse_duration_to_minutes(flight.get("durationMinutes")),
        stops=_as_int(flight.get("stops")),
    )


def _cheapest_latam_item(
    items: Iterable[dict[str, Any]], origin: str
) -> tuple[dict[str, Any] | None, float | None]:
    best_item: dict[str, Any] | None = None
    best_price: float | None = None
    for item in items:
        if str(_dig(item, "route", "originIata") or "").upper() != origin.upper():
            continue
        price = _latam_item_price(item)
        if price is None:
            continue
        if best_price is None or price < best_price:
            best_item, best_price = item, price
    return best_item, best_price


def parse_latam(payload: dict[str, Any], settings: Settings) -> Offer:
    """Extrai a combinacao mais barata de ida + volta da resposta da LATAM.

    A LATAM devolve uma lista plana em `data.items[]`, misturando os dois
    sentidos; separamos por `route.originIata` e somamos o mais barato de cada
    lado. Se a API so devolver um sentido, o preco e usado como esta e a volta
    fica None (a resposta bruta no banco permite reprocessar depois).
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GeckoAPIParseError("LATAM: resposta sem o objeto 'data'", payload)

    if data.get("success") is False:
        raise GeckoAPIParseError("LATAM: a API marcou a extracao como success=false", payload)

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise GeckoAPIParseError("LATAM: 'data.items' vazio ou ausente", payload)

    outbound_item, outbound_price = _cheapest_latam_item(items, settings.origin)
    inbound_item, inbound_price = _cheapest_latam_item(items, settings.destination)

    if outbound_item is None:
        raise GeckoAPIParseError(
            f"LATAM: nenhum voo saindo de {settings.origin} em {len(items)} itens",
            payload,
        )

    outbound = _latam_item_to_leg(outbound_item)
    inbound = _latam_item_to_leg(inbound_item) if inbound_item else None

    if inbound_price is None:
        logger.warning(
            "LATAM: nenhum trecho de volta saindo de %s; usando so o preco da ida",
            settings.destination,
        )
        total = outbound_price or 0.0
    else:
        total = (outbound_price or 0.0) + inbound_price

    if total <= 0:
        raise GeckoAPIParseError("LATAM: nao encontrei preco valido nos itens", payload)

    currency = str(_dig(outbound_item, "price", "currency") or settings.currency)

    return Offer(
        airline=LATAM,
        price=round(total, 2),
        currency=currency,
        outbound=outbound,
        inbound=inbound,
        fare_valid_until=None,  # nao exposto pela GeckoAPI
        raw_response=payload,
    )


# --------------------------------------------------------------------------
# Parser Azul: data.trips[].journeys[]
# --------------------------------------------------------------------------


def _azul_journey_price(journey: dict[str, Any]) -> float | None:
    cheapest = _as_float(_dig(journey, "cheapestFare", "total", "amount"))
    if cheapest is not None:
        return cheapest
    fares = journey.get("fares")
    if not isinstance(fares, list):
        return None
    prices = [
        price
        for fare in fares
        if isinstance(fare, dict) and (price := _as_float(_dig(fare, "total", "amount"))) is not None
    ]
    return min(prices) if prices else None


def _azul_journey_to_leg(journey: dict[str, Any]) -> Leg:
    return Leg(
        origin=str(journey.get("origin") or ""),
        destination=str(journey.get("destination") or ""),
        departure=journey.get("departure"),
        arrival=journey.get("arrival"),
        duration_minutes=parse_duration_to_minutes(journey.get("duration")),
        stops=_as_int(journey.get("stopsCount")),
    )


def _cheapest_azul_journey(
    trip: dict[str, Any],
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    journeys = trip.get("journeys")
    if not isinstance(journeys, list):
        return None, None, None

    best: dict[str, Any] | None = None
    best_price: float | None = None
    currency: str | None = None
    for journey in journeys:
        if not isinstance(journey, dict):
            continue
        if journey.get("available") is False:
            continue
        price = _azul_journey_price(journey)
        if price is None:
            continue
        if best_price is None or price < best_price:
            best, best_price = journey, price
            currency = _dig(journey, "cheapestFare", "total", "currency") or trip.get("currency")
    return best, best_price, currency


def _find_azul_trip(trips: list[Any], origin: str) -> dict[str, Any] | None:
    for trip in trips:
        if isinstance(trip, dict) and str(trip.get("origin") or "").upper() == origin.upper():
            return trip
    return None


def parse_azul(payload: dict[str, Any], settings: Settings) -> Offer:
    """Extrai a combinacao mais barata de ida + volta da resposta da Azul.

    A Azul ja separa os sentidos em `data.trips[]` (um trip por trecho), entao
    localizamos cada trip pela origem e pegamos a journey mais barata de cada.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GeckoAPIParseError("Azul: resposta sem o objeto 'data'", payload)

    trips = data.get("trips")
    if not isinstance(trips, list) or not trips:
        notifications = data.get("notifications") or []
        detail = "; ".join(
            str(n.get("message")) for n in notifications if isinstance(n, dict) and n.get("message")
        )
        raise GeckoAPIParseError(
            f"Azul: 'data.trips' vazio ou ausente. {detail}".strip(), payload
        )

    outbound_trip = _find_azul_trip(trips, settings.origin)
    inbound_trip = _find_azul_trip(trips, settings.destination)

    if outbound_trip is None:
        # Fallback: se a API nao rotulou a origem como esperado, usa a ordem.
        logger.warning("Azul: nao achei trip saindo de %s; usando trips[0]", settings.origin)
        outbound_trip = trips[0] if isinstance(trips[0], dict) else None
        inbound_trip = trips[1] if len(trips) > 1 and isinstance(trips[1], dict) else None

    if outbound_trip is None:
        raise GeckoAPIParseError("Azul: nao consegui identificar o trecho de ida", payload)

    outbound_journey, outbound_price, currency = _cheapest_azul_journey(outbound_trip)
    if outbound_journey is None or outbound_price is None:
        raise GeckoAPIParseError("Azul: nenhuma journey de ida com tarifa disponivel", payload)

    inbound_journey = inbound_price = None
    if inbound_trip is not None:
        inbound_journey, inbound_price, _ = _cheapest_azul_journey(inbound_trip)

    if inbound_price is None:
        logger.warning("Azul: sem trecho de volta com tarifa; usando so o preco da ida")
        total = outbound_price
    else:
        total = outbound_price + inbound_price

    return Offer(
        airline=AZUL,
        price=round(total, 2),
        currency=str(currency or data.get("currency") or settings.currency),
        outbound=_azul_journey_to_leg(outbound_journey),
        inbound=_azul_journey_to_leg(inbound_journey) if inbound_journey else None,
        fare_valid_until=None,  # nao exposto pela GeckoAPI
        raw_response=payload,
    )


# --------------------------------------------------------------------------
# API publica
# --------------------------------------------------------------------------


def fetch_latam(client: GeckoClient, settings: Settings) -> Offer:
    payload = client.extract(build_latam_body(settings), label="LATAM")
    return parse_latam(payload, settings)


def fetch_azul(client: GeckoClient, settings: Settings) -> Offer:
    payload = client.extract(build_azul_body(settings), label="Azul")
    return parse_azul(payload, settings)


FETCHERS: dict[str, Callable[[GeckoClient, Settings], Offer]] = {
    LATAM: fetch_latam,
    AZUL: fetch_azul,
}
