"""Testes das janelas de execucao.

Tudo aqui e funcao pura recebendo `now`, entao da para simular qualquer data
sem mexer no relogio do sistema.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from src.scheduling import (
    MODO_MANHA,
    MODO_MANUAL,
    MODO_NOITE,
    days_until_reset,
    decide,
    is_day_before_reset,
    is_within_window,
    local_now,
    parse_window,
    reset_date,
)

UTC = timezone.utc


def utc(ano, mes, dia, hora, minuto=0):
    return datetime(ano, mes, dia, hora, minuto, tzinfo=UTC)


class TestParseWindow:
    def test_formato_valido(self):
        assert parse_window("05:00-09:00") == (time(5, 0), time(9, 0))

    def test_tolera_espacos(self):
        assert parse_window(" 20:00 - 23:00 ") == (time(20, 0), time(23, 0))

    @pytest.mark.parametrize("texto", ["", "05:00", "09:00-05:00", "abc-def", "5-9", "05:00-05:00"])
    def test_formato_invalido(self, texto):
        with pytest.raises(ValueError):
            parse_window(texto)


class TestLocalNow:
    def test_converte_utc_para_belem(self):
        """Belem e UTC-3 o ano inteiro, sem horario de verao."""
        assert local_now(utc(2026, 7, 24, 12), "America/Belem").hour == 9

    def test_naive_e_tratado_como_utc(self):
        naive = datetime(2026, 7, 24, 12, 0)
        assert local_now(naive, "America/Belem").hour == 9

    def test_virada_de_dia_para_tras(self):
        """01:00 UTC ainda e o dia anterior em Belem - critico para a vespera."""
        local = local_now(utc(2026, 8, 1, 1), "America/Belem")
        assert local.day == 31
        assert local.hour == 22


class TestIsWithinWindow:
    @pytest.mark.parametrize(
        "hora,esperado",
        [(4, False), (5, True), (7, True), (9, True), (10, False), (23, False)],
    )
    def test_janela_da_manha(self, hora, esperado):
        agora = datetime(2026, 7, 24, hora, 0)
        assert is_within_window(agora, "05:00-09:00") is esperado

    @pytest.mark.parametrize("hora,esperado", [(19, False), (20, True), (23, True), (0, False)])
    def test_janela_da_noite(self, hora, esperado):
        assert is_within_window(datetime(2026, 7, 24, hora, 0), "20:00-23:00") is esperado


class TestResetDate:
    def test_proximo_reset_no_mesmo_mes(self):
        assert reset_date(date(2026, 7, 20), reset_day=25) == date(2026, 7, 25)

    def test_proximo_reset_no_mes_seguinte(self):
        assert reset_date(date(2026, 7, 24), reset_day=1) == date(2026, 8, 1)

    def test_virada_de_ano(self):
        assert reset_date(date(2026, 12, 15), reset_day=1) == date(2027, 1, 1)

    def test_dia_alto_e_limitado_a_28(self):
        """Evita o buraco de fevereiro: dia 31 nao existe em todo mes."""
        assert reset_date(date(2026, 1, 30), reset_day=31) == date(2026, 2, 28)


class TestVesperaDoReset:
    def test_ultimo_dia_do_mes_e_vespera(self):
        assert is_day_before_reset(datetime(2026, 7, 31, 21), reset_day=1) is True

    def test_dia_anterior_nao_e(self):
        assert is_day_before_reset(datetime(2026, 7, 30, 21), reset_day=1) is False

    def test_funciona_em_fevereiro(self):
        assert is_day_before_reset(datetime(2027, 2, 28, 21), reset_day=1) is True

    def test_ultimo_dia_do_ano(self):
        assert is_day_before_reset(datetime(2026, 12, 31, 21), reset_day=1) is True

    def test_dias_restantes(self):
        assert days_until_reset(datetime(2026, 7, 24, 10), reset_day=1) == 8
        assert days_until_reset(datetime(2026, 7, 31, 10), reset_day=1) == 1


class TestDecideManha:
    def _decidir(self, hora_utc, saldo=100, dia=24):
        return decide(MODO_MANHA, utc(2026, 7, dia, hora_utc), saldo=saldo, custo=5)

    def test_dentro_da_janela_roda(self):
        # 09:00 UTC = 06:00 em Belem
        assert self._decidir(9).rodar is True

    def test_antes_das_5h_nao_roda(self):
        # 07:00 UTC = 04:00 em Belem
        d = self._decidir(7)
        assert d.rodar is False
        assert "fora da janela da manha" in d.motivo

    def test_depois_das_9h_nao_roda(self):
        # 14:00 UTC = 11:00 em Belem
        assert self._decidir(14).rodar is False

    def test_limite_exato_das_5h_roda(self):
        # 08:00 UTC = 05:00 em Belem
        assert self._decidir(8).rodar is True

    def test_limite_exato_das_9h_roda(self):
        # 12:00 UTC = 09:00 em Belem
        assert self._decidir(12).rodar is True

    def test_saldo_insuficiente_bloqueia(self):
        d = self._decidir(9, saldo=4)
        assert d.rodar is False
        assert "saldo insuficiente" in d.motivo

    def test_saldo_desconhecido_nao_bloqueia(self):
        """Sem o saldo real, quem decide e o ledger local mais adiante."""
        assert self._decidir(9, saldo=None).rodar is True


class TestDecideNoite:
    def _decidir(self, dia, hora_utc, saldo=40, habilitada=True):
        return decide(
            MODO_NOITE, utc(2026, 7, dia, hora_utc), saldo=saldo, custo=5,
            noite_habilitada=habilitada,
        )

    def test_vespera_do_reset_dentro_da_janela_roda(self):
        # 31/07 23:00 UTC = 31/07 20:00 em Belem, vespera do dia 1
        assert self._decidir(31, 23).rodar is True

    def test_fora_da_vespera_nao_roda(self):
        d = self._decidir(24, 23)
        assert d.rodar is False
        assert "vespera do reset" in d.motivo

    def test_fora_da_janela_nao_roda(self):
        # 31/07 12:00 UTC = 09:00 em Belem
        d = self._decidir(31, 12)
        assert d.rodar is False
        assert "fora da janela da noite" in d.motivo

    def test_saldo_esgotado_para_a_rajada(self):
        """'Ate que os creditos acabem': com menos que o custo, para."""
        d = self._decidir(31, 23, saldo=3)
        assert d.rodar is False
        assert "saldo insuficiente" in d.motivo

    def test_saldo_exato_ainda_roda(self):
        assert self._decidir(31, 23, saldo=5).rodar is True

    def test_desabilitada_nao_roda(self):
        d = self._decidir(31, 23, habilitada=False)
        assert d.rodar is False
        assert "desabilitada" in d.motivo

    def test_madrugada_utc_ainda_e_vespera_em_belem(self):
        """01/08 01:00 UTC = 31/07 22:00 em Belem: continua sendo a vespera."""
        d = decide(MODO_NOITE, utc(2026, 8, 1, 1), saldo=40, custo=5)
        assert d.rodar is True

    def test_madrugada_utc_apos_a_janela_nao_roda(self):
        """01/08 04:00 UTC = 01/08 01:00 em Belem: ja passou e ja resetou."""
        assert decide(MODO_NOITE, utc(2026, 8, 1, 4), saldo=40, custo=5).rodar is False


class TestDecideManual:
    def test_ignora_janela_e_horario(self):
        assert decide(MODO_MANUAL, utc(2026, 7, 24, 3), saldo=100, custo=5).rodar is True

    def test_ignora_ate_saldo(self):
        """Disparo manual e escolha consciente; o orcamento barra depois."""
        assert decide(MODO_MANUAL, utc(2026, 7, 24, 3), saldo=0, custo=5).rodar is True


class TestModoDesconhecido:
    def test_nao_roda(self):
        d = decide("madrugada", utc(2026, 7, 24, 3), saldo=100, custo=5)
        assert d.rodar is False
        assert "modo desconhecido" in d.motivo
