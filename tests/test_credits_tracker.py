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


class TestTetoPorExecucao:
    """Reproduz o guard de run_check: teto mensal E teto por execucao."""

    @staticmethod
    def _guard(tracker, usado_no_inicio, teto_run, custo=5):
        def guard():
            if not tracker.can_spend(custo):
                return False
            return (tracker.used_this_month() - usado_no_inicio) + custo <= teto_run

        return guard

    def test_corta_em_20_creditos_mesmo_com_orcamento_mensal_sobrando(self, conn, clock):
        tracker = make_tracker(conn, clock)
        guard = self._guard(tracker, tracker.used_this_month(), teto_run=20)

        gastos = 0
        while guard():
            tracker.record(5, "tentativa")
            gastos += 5
        assert gastos == 20
        assert tracker.remaining() == 80, "o teto mensal ainda tem folga"

    def test_teto_mensal_prevalece_quando_e_menor(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(90, "mes quase cheio")
        guard = self._guard(tracker, tracker.used_this_month(), teto_run=20)

        gastos = 0
        while guard():
            tracker.record(5, "tentativa")
            gastos += 5
        assert gastos == 10, "so cabiam 10 creditos ate o limite mensal"
        assert tracker.used_this_month() == 100

    def test_teto_conta_so_o_gasto_do_run_atual(self, conn, clock):
        """Gasto de execucoes anteriores no mesmo mes nao consome o teto do run."""
        tracker = make_tracker(conn, clock)
        tracker.record(40, "execucoes anteriores")
        guard = self._guard(tracker, tracker.used_this_month(), teto_run=20)

        gastos = 0
        while guard():
            tracker.record(5, "tentativa")
            gastos += 5
        assert gastos == 20


class TestEstorno:
    """A GeckoAPI devolve creditos de extracoes que ela nao concluiu (HTTP 504)."""

    def test_estorno_reduz_o_consumo(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(5, "LATAM tentativa 1")
        tracker.refund(5, "LATAM tentativa 1, HTTP 504")
        assert tracker.used_this_month() == 0
        assert tracker.remaining() == 100

    def test_ledger_continua_append_only(self, conn, clock):
        """O debito e o estorno coexistem: o historico mostra os dois."""
        tracker = make_tracker(conn, clock)
        tracker.record(5, "LATAM")
        tracker.refund(5, "HTTP 504")
        linhas = conn.execute(
            "SELECT credits, reason FROM credit_usage ORDER BY id"
        ).fetchall()
        assert [linha["credits"] for linha in linhas] == [5, -5]
        assert linhas[1]["reason"].startswith("estorno:")

    def test_cenario_real_uma_falha_e_um_sucesso(self, conn, clock):
        """LATAM falhou com 504 e foi estornada; Azul funcionou. Liquido: 5."""
        tracker = make_tracker(conn, clock)
        tracker.record(5, "LATAM tentativa 1")
        tracker.refund(5, "LATAM tentativa 1, HTTP 504")
        tracker.record(5, "LATAM tentativa 2")
        tracker.refund(5, "LATAM tentativa 2, HTTP 504")
        tracker.record(5, "Azul tentativa 1")
        assert tracker.used_this_month() == 5

    def test_estorno_negativo_e_rejeitado(self, conn, clock):
        with pytest.raises(ValueError):
            make_tracker(conn, clock).refund(-5, "invalido")

    def test_estorno_libera_orcamento_travado(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(100, "mes inteiro")
        assert tracker.can_spend(5) is False
        tracker.refund(10, "duas extracoes falhas")
        assert tracker.can_spend(5) is True


class TestReconciliacao:
    """GET /v1/me/credits e a verdade; o ledger local conta por tentativa."""

    def test_corrige_contagem_inflada(self, conn, clock):
        """Cenario real: 15 debitados na LATAM, mas a GeckoAPI so cobrou 5."""
        tracker = make_tracker(conn, clock)
        tracker.record(15, "LATAM com retry")
        assert tracker.used_this_month() == 15

        tracker.reconcile(saldo_real=95)  # 100 - 95 = 5 realmente gastos
        assert tracker.used_this_month() == 5
        assert tracker.remaining() == 95

    def test_corrige_contagem_a_menos(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(5, "uma checagem")
        tracker.reconcile(saldo_real=80)  # gastamos 20, nao 5
        assert tracker.used_this_month() == 20

    def test_sem_diferenca_nao_lanca_ajuste(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(20, "quatro checagens")
        linhas_antes = conn.execute("SELECT COUNT(*) FROM credit_usage").fetchone()[0]
        tracker.reconcile(saldo_real=80)
        linhas_depois = conn.execute("SELECT COUNT(*) FROM credit_usage").fetchone()[0]
        assert linhas_antes == linhas_depois

    def test_ajuste_fica_rastreavel_no_ledger(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(15, "LATAM com retry")
        tracker.reconcile(saldo_real=95)
        ultima = conn.execute(
            "SELECT credits, reason FROM credit_usage ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ultima["credits"] == -10
        assert "ajuste" in ultima["reason"]
        assert "saldo real 95" in ultima["reason"]

    def test_saldo_maior_que_o_orcamento_nao_vira_consumo_negativo(self, conn, clock):
        """Plano pago ou bonus: o orcamento mensal e teto nosso, nao da conta."""
        tracker = make_tracker(conn, clock)
        tracker.reconcile(saldo_real=500)
        assert tracker.used_this_month() == 0
        assert tracker.remaining() == 100

    def test_reconciliacao_libera_checagem_que_seria_recusada(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(100, "ledger inflado por retries")
        assert tracker.can_spend(5) is False

        tracker.reconcile(saldo_real=40)
        assert tracker.can_spend(5) is True

    def test_ajuste_pertence_ao_mes_corrente(self, conn, clock):
        tracker = make_tracker(conn, clock)
        tracker.record(50, "julho")
        clock.set(2026, 8, 2)
        tracker.reconcile(saldo_real=90)  # agosto: 10 gastos
        assert tracker.used_this_month() == 10
        total = conn.execute("SELECT SUM(credits) AS t FROM credit_usage").fetchone()["t"]
        assert total == 60, "o consumo de julho continua no ledger"
