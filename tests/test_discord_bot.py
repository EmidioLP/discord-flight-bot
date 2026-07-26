"""Testes do envio ao Discord, com foco em nao vazar o token do webhook.

A URL do webhook contem o token secreto. Uma exception do requests costuma
carregar a URL completa; se ela entrar na mensagem de erro ou no traceback
encadeado, vaza no log do Actions - que em repo publico e publico.
"""

from __future__ import annotations

import traceback
from unittest.mock import Mock

import pytest
import requests

from src.discord_bot import DiscordError, send_embed

# URL realista, com um "token" que NAO pode aparecer em lugar nenhum do erro.
WEBHOOK = "https://discord.com/api/webhooks/1530005749814792262/TOKEN_SUPER_SECRETO_abc123"
EMBED = {"title": "teste", "fields": []}


def _mensagem_e_traceback(exc: DiscordError) -> str:
    """Junta a mensagem e o traceback encadeado inteiro, como o log faria."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class TestNaoVazaOToken:
    def test_timeout_nao_expoe_a_url(self):
        session = Mock()
        session.post.side_effect = requests.Timeout(
            f"HTTPSConnectionPool: Read timed out. url={WEBHOOK}"
        )
        with pytest.raises(DiscordError) as exc:
            send_embed(WEBHOOK, EMBED, session=session)
        assert "TOKEN_SUPER_SECRETO" not in _mensagem_e_traceback(exc.value)

    def test_erro_de_conexao_nao_expoe_a_url(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError(
            f"Failed to establish a new connection to {WEBHOOK}"
        )
        with pytest.raises(DiscordError) as exc:
            send_embed(WEBHOOK, EMBED, session=session)
        texto = _mensagem_e_traceback(exc.value)
        assert "TOKEN_SUPER_SECRETO" not in texto
        assert WEBHOOK not in texto
        # Mas ainda diz QUE tipo de erro foi, para nao virar diagnostico cego.
        assert "ConnectionError" in str(exc.value)

    def test_chain_quebrada_para_nao_vazar_via_traceback(self):
        """logger.exception imprime o traceback encadeado; ele nao pode ter a URL."""
        session = Mock()
        session.post.side_effect = requests.ConnectionError(f"boom {WEBHOOK}")
        with pytest.raises(DiscordError) as exc:
            send_embed(WEBHOOK, EMBED, session=session)
        assert exc.value.__cause__ is None, "o encadeamento com a exception do requests foi cortado"


class TestSendEmbed:
    def test_sucesso_nao_levanta(self):
        session = Mock()
        session.post.return_value = Mock(status_code=204, text="")
        send_embed(WEBHOOK, EMBED, session=session)

    def test_http_de_erro_inclui_status_e_corpo(self):
        session = Mock()
        session.post.return_value = Mock(status_code=404, text="Unknown Webhook")
        with pytest.raises(DiscordError) as exc:
            send_embed(WEBHOOK, EMBED, session=session)
        assert "404" in str(exc.value)
        assert "Unknown Webhook" in str(exc.value)

    def test_corpo_de_erro_e_truncado(self):
        session = Mock()
        session.post.return_value = Mock(status_code=400, text="x" * 5000)
        with pytest.raises(DiscordError) as exc:
            send_embed(WEBHOOK, EMBED, session=session)
        assert len(str(exc.value)) < 400
