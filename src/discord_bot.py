"""Formatacao e envio das notificacoes via webhook do Discord.

Webhook em vez de bot completo: nao ha comandos a atender, so notificacao de
mao unica, entao nao precisamos manter um gateway websocket aberto.

Uma checagem gera exatamente um embed: a viagem mais barata que o KAYAK achou,
com a companhia e o vendedor identificados na propria mensagem.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

from .compare import Comparison
from .models import Leg, Offer, format_money

logger = logging.getLogger(__name__)

COLOR_NEW_LOW = 0x2ECC71  # verde
COLOR_TIE = 0x3498DB  # azul
COLOR_HIGHER = 0xE67E22  # laranja
COLOR_NEUTRAL = 0x95A5A6  # cinza
COLOR_ERROR = 0xE74C3C  # vermelho

# Poucos assentos restantes viram destaque no embed.
SEATS_ALERT_THRESHOLD = 3


class DiscordError(RuntimeError):
    """Falha ao enviar a mensagem para o Discord."""


def _format_datetime(iso_value: str | None) -> str:
    """Converte ISO 8601 para 'dd/mm/aaaa as HH:MM'."""
    if not iso_value:
        return "nao informado"
    try:
        moment = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Data em formato inesperado: %r", iso_value)
        return str(iso_value)
    return moment.strftime("%d/%m/%Y as %H:%M")


def _leg_block(leg: Leg | None) -> str:
    if leg is None:
        return "_Nao retornado pela API_"
    route = f"{leg.origin} -> {leg.destination}" if leg.origin and leg.destination else ""
    lines = [
        f"**Saida:** {_format_datetime(leg.departure)}",
        f"**Chegada:** {_format_datetime(leg.arrival)}",
        f"**Duracao:** {leg.duration_label}",
        f"**Conexoes:** {leg.stops_label}",
    ]
    if route:
        lines.insert(0, f"**Trecho:** {route}")
    return "\n".join(lines)


def _pick_color(comparison: Comparison) -> int:
    if comparison.is_first_check:
        return COLOR_NEUTRAL
    if comparison.is_new_low:
        return COLOR_NEW_LOW
    if comparison.is_tie:
        return COLOR_TIE
    return COLOR_HIGHER


def build_embed(
    offer: Offer,
    comparison: Comparison,
    route_label: str,
    credits_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Monta o embed da viagem mais barata da checagem."""
    title_prefix = "NOVO MENOR PRECO - " if comparison.is_new_low else ""

    historical = (
        format_money(comparison.historical_low, offer.currency)
        if comparison.historical_low is not None
        else "sem historico ainda"
    )

    fields: list[dict[str, Any]] = [
        {
            "name": "Preco total (ida + volta)",
            "value": f"## {format_money(offer.price, offer.currency)}",
            "inline": False,
        },
        {"name": "Companhia aerea", "value": offer.airline, "inline": True},
        {"name": "Vendido por", "value": offer.provider_label, "inline": True},
        {
            "name": "Menor preco historico",
            "value": f"{historical}\n{comparison.summary(offer.currency)}",
            "inline": False,
        },
        {"name": "Ida", "value": _leg_block(offer.outbound), "inline": True},
        {"name": "Volta", "value": _leg_block(offer.inbound), "inline": True},
    ]

    if offer.total_duration_minutes is not None:
        hours, minutes = divmod(offer.total_duration_minutes, 60)
        fields.append(
            {"name": "Tempo total em voo", "value": f"{hours}h{minutes:02d}min", "inline": False}
        )

    if offer.seats_remaining is not None:
        plural = "assento" if offer.seats_remaining == 1 else "assentos"
        alerta = " **- corre!**" if offer.seats_remaining <= SEATS_ALERT_THRESHOLD else ""
        fields.append(
            {
                "name": "Assentos restantes",
                "value": f"{offer.seats_remaining} {plural} no trecho mais apertado{alerta}",
                "inline": False,
            }
        )

    fields.append(
        {
            "name": "Validade da tarifa",
            "value": (
                offer.fare_valid_until
                if offer.fare_valid_until
                else "_Nenhum target da GeckoAPI expoe validade de tarifa. O preco vale "
                "para o momento da consulta e costuma mudar em horas._"
            ),
            "inline": False,
        }
    )

    if comparison.diff_vs_previous is not None:
        if comparison.diff_vs_previous == 0:
            texto = "Sem mudanca desde a checagem anterior."
        else:
            direcao = "subiu" if comparison.diff_vs_previous > 0 else "caiu"
            valor = format_money(abs(comparison.diff_vs_previous), offer.currency)
            texto = f"{direcao} {valor} desde a ultima checagem"
        fields.append({"name": "Desde a ultima checagem", "value": texto, "inline": False})

    descricao = f"Viagem mais barata encontrada | {route_label}"
    if offer.total_options:
        descricao += f" | {offer.total_options} opcoes avaliadas"

    footer = f"KAYAK | consultado em {_format_datetime(offer.checked_at)}"
    if credits_summary:
        footer += (
            f" | creditos {credits_summary['used']}/{credits_summary['budget']} "
            f"em {credits_summary['year_month']}"
        )

    embed: dict[str, Any] = {
        "title": f"{title_prefix}{format_money(offer.price, offer.currency)} - {offer.airline}",
        "description": descricao,
        "color": _pick_color(comparison),
        "fields": fields,
        "footer": {"text": footer[:2048]},
        "timestamp": offer.checked_at,
    }
    if offer.booking_url:
        embed["url"] = offer.booking_url
    return embed


def build_error_embed(source: str, error: Exception, route_label: str) -> dict[str, Any]:
    """Embed de falha, para voce saber que a checagem quebrou em vez de silenciar."""
    return {
        "title": f"Falha ao consultar {source}",
        "description": f"{route_label}\n```\n{type(error).__name__}: {error}\n```"[:4000],
        "color": COLOR_ERROR,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def send_embed(
    webhook_url: str,
    embed: dict[str, Any],
    timeout: int = 30,
    session: requests.Session | None = None,
) -> None:
    """Envia um embed ao webhook. Levanta DiscordError em qualquer falha."""
    http = session or requests
    payload = {"username": "Monitor de Voos", "embeds": [embed]}
    try:
        response = http.post(webhook_url, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise DiscordError(f"Timeout de {timeout}s ao enviar para o Discord") from exc
    except requests.RequestException as exc:
        raise DiscordError(f"Erro de conexao com o Discord: {exc}") from exc

    if response.status_code >= 400:
        raise DiscordError(f"Discord respondeu HTTP {response.status_code}: {response.text[:300]}")

    logger.info("Embed enviado ao Discord | %s", embed.get("title"))


def notify_offer(
    webhook_url: str,
    offer: Offer,
    comparison: Comparison,
    route_label: str,
    credits_summary: dict[str, Any] | None = None,
    timeout: int = 30,
) -> None:
    send_embed(webhook_url, build_embed(offer, comparison, route_label, credits_summary), timeout)


def notify_error(
    webhook_url: str,
    source: str,
    error: Exception,
    route_label: str,
    timeout: int = 30,
) -> bool:
    """Notifica falha. Devolve False se nem o aviso conseguiu sair.

    Nao propaga excecao para nao mascarar o erro original, mas o chamador
    precisa saber que a mensagem nao chegou - senao a falha vira silencio.
    """
    try:
        send_embed(webhook_url, build_error_embed(source, error, route_label), timeout)
        return True
    except DiscordError:
        logger.exception("Nao consegui nem avisar o erro no Discord (%s)", source)
        return False
