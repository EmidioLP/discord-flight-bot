"""Cliente da GeckoAPI e parser do KAYAK.

Endpoint unico: POST https://api.geckoapi.com.br/v1/extract
Auth: header Authorization: Bearer <chave>
Custo: 5 creditos por request.

Fonte: `kayak.com.br` + `type=plp` (schema em https://geckoapi.com.br/docs).

Por que metabusca em vez de consultar cada companhia: uma request cobre todas
as companhias e agencias, custa metade de LATAM+Azul separados, e o schema ja
entrega estruturado o que antes eu tinha que adivinhar:

  * `items[].legs[]`            -> ida e volta no mesmo item, sem separar por
                                   aeroporto de origem
  * `legs[].durationMinutes`    -> numero, sem adivinhar formato de string
  * `items[].price.amount`      -> preco total da viagem, sem somar trechos
  * `items[].isCheapest`        -> o proprio KAYAK marca a mais barata
  * `segments[].airlineName`    -> nome da companhia pronto
  * `bookingOptions[]`          -> quem vende (companhia ou agencia)
  * `segments[].seatsRemaining` -> assentos restantes

A doc nao expoe validade de tarifa em nenhum target; `seatsRemaining` e o dado
de urgencia mais proximo disso.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import requests

from .config import Settings
from .models import Leg, Offer

logger = logging.getLogger(__name__)

API_BASE = "https://api.geckoapi.com.br/v1"
API_URL = f"{API_BASE}/extract"
CREDITS_URL = f"{API_BASE}/me/credits"

TARGET_KAYAK = "kayak.com.br"
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


@dataclass(frozen=True)
class CreditBalance:
    """Resposta de GET /v1/me/credits: o saldo real da conta.

    E a fonte de verdade sobre creditos. O ledger local continua existindo como
    trilha de auditoria e como protecao quando este endpoint nao responde, mas
    quem manda no go/no-go e este numero.
    """

    current_credits: int
    plan_id: str | None = None
    last_24h: int | None = None
    last_7d: int | None = None
    last_30d: int | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------
# Helpers de normalizacao
# --------------------------------------------------------------------------

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)
_CLOCK_DURATION = re.compile(r"^(?P<hours>\d{1,3}):(?P<minutes>\d{2})(?::(?P<seconds>\d{2}))?$")


def parse_duration_to_minutes(value: Any) -> int | None:
    """Normaliza duracao para minutos.

    O KAYAK manda `durationMinutes` numerico, mas o campo pode vir null; os
    formatos de string ficam aceitos porque custam pouco e ja nos salvaram uma
    vez quando a Azul mandou um formato nao documentado.
    """
    if value is None or isinstance(value, bool):
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


def duration_from_timestamps(departure: Any, arrival: Any) -> int | None:
    """Calcula a duracao em minutos a partir da saida e da chegada.

    Rede de seguranca para quando `durationMinutes` vem null: os horarios ja
    estao ali, e a subtracao e mais confiavel do que deixar o campo vazio.
    """
    if not departure or not arrival:
        return None
    try:
        inicio = datetime.fromisoformat(str(departure).replace("Z", "+00:00"))
        fim = datetime.fromisoformat(str(arrival).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Horarios em formato inesperado: %r -> %r", departure, arrival)
        return None

    # Misturar naive com aware levanta TypeError na subtracao.
    if (inicio.tzinfo is None) != (fim.tzinfo is None):
        logger.warning("Saida e chegada com fusos incompativeis: %r / %r", departure, arrival)
        return None

    minutos = int((fim - inicio).total_seconds() // 60)
    if minutos <= 0:
        logger.warning("Duracao nao positiva (%s min): %r -> %r", minutos, departure, arrival)
        return None
    return minutos


def _resolve_duration(raw: Any, departure: Any, arrival: Any) -> int | None:
    """Usa a duracao informada; se nao der para interpretar, calcula pelos horarios."""
    minutos = parse_duration_to_minutes(raw)
    if minutos is not None:
        return minutos
    calculado = duration_from_timestamps(departure, arrival)
    if calculado is not None:
        logger.info("durationMinutes ausente; calculei %s min pelos horarios", calculado)
    return calculado


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
      * `refund_hook` lanca o estorno em 5xx, que a GeckoAPI devolve;
      * `budget_guard` e consultado antes de cada tentativa (inclusive antes
        de um retry) e aborta se nao houver saldo.
    """

    def __init__(
        self,
        api_key: str,
        timeout: int = 120,
        max_retries: int = 1,
        retry_delay_seconds: int = 60,
        credit_hook: Callable[[str], None] | None = None,
        refund_hook: Callable[[str], None] | None = None,
        budget_guard: Callable[[], bool] | None = None,
        session: requests.Session | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_delay_seconds = max(0, retry_delay_seconds)
        self.credit_hook = credit_hook or (lambda _label: None)
        self.refund_hook = refund_hook or (lambda _label: None)
        self.budget_guard = budget_guard or (lambda: True)
        self.session = session or requests.Session()
        self._sleep = sleep_fn or time.sleep

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get_credits(self) -> CreditBalance:
        """Consulta GET /v1/me/credits: o saldo real da conta.

        Sem retry e sem `credit_hook`: e endpoint de conta, nao despacha
        extracao. Se um dia se confirmar que cobra, e so plugar o hook aqui.
        """
        try:
            response = self.session.get(
                CREDITS_URL, headers=self._headers(), timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise GeckoAPITimeout(f"Timeout ao consultar saldo: {exc}") from exc
        except requests.RequestException as exc:
            raise GeckoAPIError(f"Erro de conexao ao consultar saldo: {exc}") from exc

        if response.status_code >= 400:
            raise GeckoAPIHTTPError(response.status_code, response.text)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeckoAPIParseError(
                f"Saldo: resposta nao e JSON valido ({response.text[:200]!r})"
            ) from exc

        if not isinstance(payload, dict):
            raise GeckoAPIParseError(f"Saldo: esperava objeto JSON, veio {type(payload)}")

        saldo = _as_int(payload.get("currentCredits"))
        if saldo is None:
            raise GeckoAPIParseError(
                f"Saldo: 'currentCredits' ausente ou nao numerico em {list(payload)}",
                payload,
            )

        consumido = payload.get("creditsConsumed") or {}
        balance = CreditBalance(
            current_credits=saldo,
            plan_id=str(payload.get("planId") or "") or None,
            last_24h=_as_int(consumido.get("last24Hours")),
            last_7d=_as_int(consumido.get("last7Days")),
            last_30d=_as_int(consumido.get("last30Days")),
            updated_at=str(payload.get("updatedAt") or "") or None,
            raw=payload,
        )
        logger.info(
            "Saldo GeckoAPI | %s creditos | plano %s | 30d: %s",
            balance.current_credits,
            balance.plan_id,
            balance.last_30d,
        )
        return balance

    def extract(self, body: dict[str, Any], label: str) -> dict[str, Any]:
        """Executa POST /v1/extract e devolve o JSON, com retry em falha transitoria."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error: Exception | None = None
        total_tentativas = self.max_retries + 1

        for attempt in range(1, total_tentativas + 1):
            if not self.budget_guard():
                raise GeckoAPIError(
                    f"Sem creditos para a tentativa {attempt} de {label}; abortando."
                )

            if attempt > 1 and self.retry_delay_seconds:
                # A GeckoAPI raspa o site de origem. Um UPSTREAM_TIMEOUT diz que
                # o site estava lento; repetir no segundo seguinte encontra a
                # mesma lentidao e queima mais 5 creditos. A pausa e barata: o
                # job do Actions tem 6h de teto.
                logger.info(
                    "GeckoAPI | %s | aguardando %ss antes da tentativa %s",
                    label,
                    self.retry_delay_seconds,
                    attempt,
                )
                self._sleep(self.retry_delay_seconds)

            logger.info(
                "GeckoAPI | %s | tentativa %s/%s | body=%s", label, attempt, total_tentativas, body
            )
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
                # A GeckoAPI estorna extracoes que ela mesma nao concluiu, entao
                # lancamos o credito de volta. Debitamos primeiro e estornamos
                # depois de proposito: se o processo morrer no meio, o ledger
                # erra para o lado seguro (contando a mais, nunca a menos).
                self.refund_hook(f"{label} (tentativa {attempt}, HTTP {response.status_code})")
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
# Request
# --------------------------------------------------------------------------


def build_kayak_body(settings: Settings) -> dict[str, Any]:
    """Busca so pela rota e datas: a companhia vem na resposta, nao no filtro."""
    return {
        "target": TARGET_KAYAK,
        "type": EXTRACT_TYPE,
        "from": settings.origin,
        "to": settings.destination,
        "departureDate": settings.departure_date,
        "returnDate": settings.return_date,
        "numAdults": settings.num_adults,
        "numChildren": settings.num_children,
        "numInfants": settings.num_infants,
        "lang": "pt-BR",
        "currency": settings.currency,
    }


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _airline_names(item: dict[str, Any], codigo_para_nome: dict[str, str]) -> str:
    """Nome das companhias que operam a viagem.

    Preferimos `segments[].airlineName`, que ja vem pronto. O mapa de
    `data.airlines[]` cobre o caso de o segmento trazer so o codigo. Voos com
    ida numa companhia e volta em outra viram "GOL + LATAM".
    """
    nomes: list[str] = []
    for leg in item.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        for segment in leg.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            nome = segment.get("airlineName") or codigo_para_nome.get(
                str(segment.get("airlineCode") or "")
            )
            if nome and nome not in nomes:
                nomes.append(str(nome))

    if nomes:
        return " + ".join(nomes)

    # Ultimo recurso: os codigos no proprio leg.
    codigos: list[str] = []
    for leg in item.get("legs") or []:
        if isinstance(leg, dict):
            for codigo in leg.get("airlineCodes") or []:
                nome = codigo_para_nome.get(str(codigo), str(codigo))
                if nome not in codigos:
                    codigos.append(nome)
    return " + ".join(codigos) if codigos else "companhia nao informada"


def _cheapest_booking_option(item: dict[str, Any]) -> tuple[str | None, bool | None, str | None]:
    """Vendedor mais barato: (nome, e_venda_direta, url).

    `isDirect` distingue compra no site da companhia de compra por agencia
    (123Milhas, MaxMilhas, Decolar). Nao filtramos por isso - o embed mostra
    quem vende e voce decide.
    """
    opcoes = item.get("bookingOptions")
    if not isinstance(opcoes, list):
        return None, None, None

    melhor: dict[str, Any] | None = None
    melhor_preco: float | None = None
    for opcao in opcoes:
        if not isinstance(opcao, dict):
            continue
        preco = _as_float(_dig(opcao, "totalPrice", "amount")) or _as_float(
            _dig(opcao, "price", "amount")
        )
        if preco is None:
            continue
        if melhor_preco is None or preco < melhor_preco:
            melhor, melhor_preco = opcao, preco

    if melhor is None:
        return None, None, None
    nome = melhor.get("providerName") or melhor.get("providerCode")
    url = melhor.get("bookingUrl") or melhor.get("universalLinkUrl")
    direto = melhor.get("isDirect")
    return (str(nome) if nome else None, bool(direto) if direto is not None else None, url)


def _seats_remaining(item: dict[str, Any]) -> int | None:
    """Menor numero de assentos restantes entre os segmentos.

    O gargalo da viagem inteira e o trecho com menos lugares. E o dado de
    urgencia mais proximo de "validade da tarifa", que nenhum target expoe.
    """
    valores: list[int] = []
    for leg in item.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        for segment in leg.get("segments") or []:
            if isinstance(segment, dict):
                assentos = _as_int(segment.get("seatsRemaining"))
                if assentos is not None and assentos > 0:
                    valores.append(assentos)
    return min(valores) if valores else None


def _leg_to_model(leg: dict[str, Any]) -> Leg:
    departure = leg.get("departure")
    arrival = leg.get("arrival")
    return Leg(
        origin=str(leg.get("origin") or ""),
        destination=str(leg.get("destination") or ""),
        departure=departure,
        arrival=arrival,
        duration_minutes=_resolve_duration(leg.get("durationMinutes"), departure, arrival),
        stops=_as_int(leg.get("stops")),
    )


def _item_price(item: dict[str, Any]) -> float | None:
    return _as_float(_dig(item, "price", "amount"))


def parse_kayak(payload: dict[str, Any], settings: Settings) -> Offer:
    """Extrai a viagem mais barata da resposta do KAYAK.

    O KAYAK ja marca a mais barata em `isCheapest`; conferimos contra o menor
    `price.amount` e ficamos com o menor dos dois, porque a flag e do ranking
    deles e o preco e o que voce paga.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GeckoAPIParseError("KAYAK: resposta sem o objeto 'data'", payload)

    if data.get("success") is False:
        raise GeckoAPIParseError("KAYAK: a API marcou a extracao como success=false", payload)

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise GeckoAPIParseError("KAYAK: 'data.items' vazio ou ausente", payload)

    candidatos = [
        (item, preco)
        for item in items
        if isinstance(item, dict) and (preco := _item_price(item)) is not None
    ]
    if not candidatos:
        raise GeckoAPIParseError(
            f"KAYAK: nenhum dos {len(items)} itens tem price.amount", payload
        )

    marcado = next((item for item, _ in candidatos if item.get("isCheapest")), None)
    escolhido, preco = min(candidatos, key=lambda par: par[1])
    if marcado is not None:
        preco_marcado = _item_price(marcado)
        if preco_marcado is not None and preco_marcado < preco:
            escolhido, preco = marcado, preco_marcado
        elif preco_marcado is not None and preco_marcado > preco:
            logger.info(
                "isCheapest apontava %.2f, mas achei %.2f mais barato; usando o menor",
                preco_marcado,
                preco,
            )

    codigo_para_nome = {
        str(a.get("code")): str(a.get("name"))
        for a in (data.get("airlines") or [])
        if isinstance(a, dict) and a.get("code") and a.get("name")
    }

    legs = [leg for leg in (escolhido.get("legs") or []) if isinstance(leg, dict)]
    if not legs:
        raise GeckoAPIParseError("KAYAK: item escolhido nao tem legs", payload)

    outbound = _leg_to_model(legs[0])
    inbound = _leg_to_model(legs[1]) if len(legs) > 1 else None
    if inbound is None:
        logger.warning("KAYAK devolveu so um leg; a volta ficara vazia no embed")

    provider, provider_is_direct, booking_url = _cheapest_booking_option(escolhido)

    return Offer(
        airline=_airline_names(escolhido, codigo_para_nome),
        price=round(preco, 2),
        currency=str(_dig(escolhido, "price", "currency") or settings.currency),
        outbound=outbound,
        inbound=inbound,
        provider=provider,
        provider_is_direct=provider_is_direct,
        booking_url=booking_url or escolhido.get("shareableUrl"),
        seats_remaining=_seats_remaining(escolhido),
        fare_valid_until=None,  # nenhum target da GeckoAPI expoe validade
        raw_response=payload,
        total_options=_as_int(data.get("totalResults")) or len(items),
    )


def fetch_kayak(client: GeckoClient, settings: Settings) -> Offer:
    payload = client.extract(build_kayak_body(settings), label="KAYAK")
    return parse_kayak(payload, settings)
