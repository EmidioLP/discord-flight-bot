"""Testes da comparacao com o historico."""

from __future__ import annotations

from src import storage
from src.compare import (
    build_comparison,
    compare_with_history,
    historical_low,
    last_price,
    lowest_by_airline,
)
from src.models import Leg, Offer


def save(conn, airline, price, checked_at):
    storage.save_offer(
        conn,
        Offer(
            airline=airline,
            price=price,
            currency="BRL",
            outbound=Leg("BEL", "NAT", None, None, 265, 1),
            inbound=Leg("NAT", "BEL", None, None, 185, 0),
            raw_response={"stub": True},
            checked_at=checked_at,
        ),
    )


class TestBuildComparison:
    def test_primeira_checagem_nao_tem_historico(self):
        c = build_comparison("LATAM", 1200.0, historical_low=None)
        assert c.is_first_check is True
        assert c.diff is None
        assert c.pct_diff is None
        assert c.is_new_low is False
        assert "Primeira checagem" in c.summary()

    def test_preco_menor_e_novo_minimo(self):
        c = build_comparison("LATAM", 900.0, historical_low=1000.0)
        assert c.is_new_low is True
        assert c.diff == -100.0
        assert c.pct_diff == -10.0
        assert "Novo menor preco" in c.summary()

    def test_preco_maior(self):
        c = build_comparison("AZUL", 1100.0, historical_low=1000.0)
        assert c.is_new_low is False
        assert c.diff == 100.0
        assert c.pct_diff == 10.0
        assert "acima do menor historico" in c.summary()

    def test_preco_igual_empata_e_nao_e_novo_minimo(self):
        c = build_comparison("AZUL", 1000.0, historical_low=1000.0)
        assert c.is_new_low is False
        assert c.is_tie is True
        assert c.diff == 0.0
        assert c.pct_diff == 0.0
        assert "Empatado" in c.summary()

    def test_arredonda_para_dois_decimais(self):
        c = build_comparison("LATAM", 999.999, historical_low=1000.001)
        assert c.current_price == 1000.0
        assert c.historical_low == 1000.0

    def test_pct_diff_protegido_contra_divisao_por_zero(self):
        c = build_comparison("LATAM", 500.0, historical_low=0.0)
        assert c.pct_diff is None

    def test_diff_vs_previous_quando_sobe(self):
        c = build_comparison("LATAM", 1200.0, historical_low=1000.0, previous_price=1100.0)
        assert c.diff_vs_previous == 100.0

    def test_diff_vs_previous_quando_cai(self):
        c = build_comparison("LATAM", 1000.0, historical_low=1000.0, previous_price=1100.0)
        assert c.diff_vs_previous == -100.0

    def test_diff_vs_previous_ausente_sem_checagem_anterior(self):
        assert build_comparison("LATAM", 1000.0, None).diff_vs_previous is None

    def test_summary_formata_dinheiro_no_padrao_brasileiro(self):
        """Milhar com ponto e decimal com virgula, igual ao resto do embed."""
        c = build_comparison("LATAM", 1200.0, historical_low=1346.0)
        assert "R$ 1.346,00" in c.summary()
        assert "R$ 1,346.00" not in c.summary()

    def test_summary_respeita_outra_moeda(self):
        c = build_comparison("LATAM", 900.0, historical_low=1000.0)
        assert "USD 1.000,00" in c.summary("USD")


class TestHistoricalLow:
    def test_none_quando_nao_ha_registro(self, conn):
        assert historical_low(conn, "LATAM") is None

    def test_pega_o_menor_preco(self, conn):
        save(conn, "LATAM", 1500.0, "2026-07-01T10:00:00+00:00")
        save(conn, "LATAM", 1100.0, "2026-07-04T10:00:00+00:00")
        save(conn, "LATAM", 1300.0, "2026-07-07T10:00:00+00:00")
        assert historical_low(conn, "LATAM") == 1100.0

    def test_isola_por_companhia(self, conn):
        save(conn, "LATAM", 1500.0, "2026-07-01T10:00:00+00:00")
        save(conn, "AZUL", 900.0, "2026-07-01T10:05:00+00:00")
        assert historical_low(conn, "LATAM") == 1500.0
        assert historical_low(conn, "AZUL") == 900.0


class TestLastPrice:
    def test_none_sem_historico(self, conn):
        assert last_price(conn, "AZUL") is None

    def test_pega_a_checagem_mais_recente_e_nao_a_mais_barata(self, conn):
        save(conn, "AZUL", 800.0, "2026-07-01T10:00:00+00:00")
        save(conn, "AZUL", 1200.0, "2026-07-04T10:00:00+00:00")
        assert last_price(conn, "AZUL") == 1200.0


class TestCompareWithHistory:
    def test_primeira_checagem(self, conn):
        c = compare_with_history(conn, "LATAM", 1200.0)
        assert c.is_first_check is True

    def test_detecta_novo_minimo_contra_o_banco(self, conn):
        save(conn, "LATAM", 1500.0, "2026-07-01T10:00:00+00:00")
        save(conn, "LATAM", 1300.0, "2026-07-04T10:00:00+00:00")
        c = compare_with_history(conn, "LATAM", 1150.0)
        assert c.historical_low == 1300.0
        assert c.previous_price == 1300.0
        assert c.is_new_low is True
        assert c.diff == -150.0

    def test_nao_conta_o_proprio_preco_no_minimo(self, conn):
        """Comparar antes de salvar: o preco atual nao pode entrar no MIN()."""
        save(conn, "AZUL", 1000.0, "2026-07-01T10:00:00+00:00")
        c = compare_with_history(conn, "AZUL", 950.0)
        assert c.historical_low == 1000.0  # e nao 950
        assert c.is_new_low is True

    def test_companhias_nao_se_misturam(self, conn):
        save(conn, "AZUL", 700.0, "2026-07-01T10:00:00+00:00")
        c = compare_with_history(conn, "LATAM", 1200.0)
        assert c.is_first_check is True


class TestLowestByAirline:
    def test_vazio_sem_registros(self, conn):
        assert lowest_by_airline(conn) == {}

    def test_agrupa_por_companhia(self, conn):
        save(conn, "LATAM", 1500.0, "2026-07-01T10:00:00+00:00")
        save(conn, "LATAM", 1200.0, "2026-07-04T10:00:00+00:00")
        save(conn, "AZUL", 980.0, "2026-07-01T10:05:00+00:00")
        assert lowest_by_airline(conn) == {"LATAM": 1200.0, "AZUL": 980.0}
