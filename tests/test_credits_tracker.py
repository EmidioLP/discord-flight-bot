"""Testes do controle de creditos."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.credits_tracker import CreditBudgetExceeded, CreditsTracker, current_year_month


def make_tracker(conn, clock, budget=100):
    return CreditsTracker(conn, monthly_budget=budget, now_fn=clock)


class TestCurrentYearMonth:
    def test_formata_como_ano_mes(self):
        assert current_year_month(datetime(2026, 12, 27, tzinfo=timezone.utc)) == "2026-12"

    def test_mes_com_um_digito_recebe_zero_a_esquerda(self):
        assert current_year_month(datetime(2027, 1, 5, tzinfo=timezone.utc)) == "2027-01"


class TestSaldo:
    def test_comeca_zerado(self, conn, clock):
        tracker = make_tracker(conn, clock)
        assert tracker.used_this_month() == 0
        assert tracker.remaining() == 100

    def test_record_acumula(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(5, "latam")
        tracker.record(5, "azul")
        assert tracker.used_this_month() == 10
        assert tracker.remaining() == 90

    def test_record_devolve_total_do_mes(self, conn, clock):
        tracker = make_tracker(conn, clock)
        assert tracker.record(5, "latam") == 5
        assert tracker.record(5, "azul") == 10

    def test_remaining_nunca_fica_negativo(self, conn, clock):
        tracker = make_tracker(conn, clock, budget=10)
        tracker.record(25, "estouro fora do scheduler")
        assert tracker.used_this_month() == 25
        assert tracker.remaining() == 0

    def test_credito_negativo_e_rejeitado(self, conn, clock):
        tracker = make_tracker(conn, clock)
        with pytest.raises(ValueError):
            tracker.record(-5, "invalido")

    def test_orcamento_negativo_e_rejeitado(self, conn, clock):
        with pytest.raises(ValueError):
            CreditsTracker(conn, monthly_budget=-1, now_fn=clock)


class TestCanSpend:
    def test_permite_gasto_dentro_do_orcamento(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(90, "9 checagens")
        assert tracker.can_spend(10) is True

    def test_bloqueia_gasto_que_estoura(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(95, "quase no limite")
        assert tracker.can_spend(10) is False
        assert tracker.can_spend(5) is True

    def test_limite_exato_e_permitido(self, conn, clock):
        """100 creditos = exatamente 10 checagens completas; a decima tem que passar."""
        tracker = make_tracker(conn, clock)
        for i in range(9):
            tracker.record(10, f"checagem {i + 1}")
        assert tracker.used_this_month() == 90
        assert tracker.can_spend(10) is True
        tracker.record(10, "checagem 10")
        assert tracker.can_spend(10) is False

    def test_gasto_zero_sempre_cabe(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(100, "orcamento inteiro")
        assert tracker.can_spend(0) is True


class TestEnsureBudget:
    def test_nao_levanta_quando_ha_saldo(self, conn, clock):
        make_tracker(conn, clock).ensure_budget(10, "checagem")

    def test_levanta_quando_estoura(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(95, "quase la")
        with pytest.raises(CreditBudgetExceeded) as excinfo:
            tracker.ensure_budget(10, "checagem completa")
        mensagem = str(excinfo.value)
        assert "95/100" in mensagem
        assert "2026-07" in mensagem


class TestViradaDeMes:
    def test_consumo_zera_no_dia_primeiro(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(100, "mes cheio de julho")
        assert tracker.used_this_month() == 100
        assert tracker.can_spend(10) is False

        clock.set(2026, 8, 1)
        assert tracker.used_this_month() == 0
        assert tracker.remaining() == 100
        assert tracker.can_spend(10) is True

    def test_historico_do_mes_anterior_continua_no_ledger(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(40, "julho")
        clock.set(2026, 8, 3)
        tracker.record(10, "agosto")

        total = conn.execute("SELECT SUM(credits) AS t FROM credit_usage").fetchone()["t"]
        assert total == 50
        assert tracker.used_this_month() == 10

    def test_virada_de_ano(self, conn, clock):
        clock.set(2026, 12, 28)
        tracker = make_tracker(conn, clock)
        tracker.record(100, "dezembro")
        assert tracker.remaining() == 0

        clock.set(2027, 1, 1)
        assert tracker.remaining() == 100


class TestSummary:
    def test_traz_os_quatro_campos(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(30, "tres checagens")
        assert tracker.summary() == {
            "year_month": "2026-07",
            "used": 30,
            "budget": 100,
            "remaining": 70,
        }

    def test_remaining_do_summary_nao_fica_negativo(self, conn, clock):
        tracker = make_tracker(conn, clock, budget=10)
        tracker.record(50, "estouro")
        assert tracker.summary()["remaining"] == 0
