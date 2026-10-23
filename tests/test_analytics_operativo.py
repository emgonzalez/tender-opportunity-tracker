"""Tests para src.analytics.operativo — SQL y forma del dato, sin Oracle real."""
from unittest.mock import MagicMock

import pytest

from src.analytics import operativo


@pytest.fixture
def mock_cursor():
    return MagicMock()


@pytest.fixture
def mock_conn(mock_cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=mock_cursor)
    return conn


class TestEstadoUltimaCorrida:
    def test_sql_y_mapeo_ok(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = (1, 42, "v1", None, 125.0)
        r = operativo.estado_ultima_corrida(mock_conn)
        sql = mock_cursor.execute.call_args[0][0]
        assert "FROM   ejecucion" in sql and "ORDER  BY run_timestamp DESC" in sql
        assert "America/El_Salvador" in sql and "FETCH  FIRST 1" in sql
        assert r == {"estado": "OK", "hace_minutos": 125, "procesos_revisados": 42,
                     "version": "v1", "error_msg": None}
        mock_cursor.close.assert_called_once()

    def test_success_cero_es_error_y_minutos_negativos_se_acotan(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = (0, None, None, "falló", -3)
        r = operativo.estado_ultima_corrida(mock_conn)
        assert r["estado"] == "ERROR" and r["hace_minutos"] == 0 and r["procesos_revisados"] == 0

    def test_sin_filas(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = None
        r = operativo.estado_ultima_corrida(mock_conn)
        assert r["estado"] is None and r["hace_minutos"] is None
        mock_cursor.close.assert_called_once()


class TestGastoLlmHoy:
    def test_suma_y_porcentaje(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = (1.25, 7)
        r = operativo.gasto_llm_hoy(mock_conn, 5.0)
        sql = mock_cursor.execute.call_args[0][0]
        assert "SUM(costo_estimado_usd)" in sql and "FROM   llm_llamada" in sql
        assert "TRUNC(" in sql and "America/El_Salvador" in sql
        assert r == {"gasto_usd": 1.25, "tope_usd": 5.0, "pct": 25.0, "llamadas": 7}

    def test_sin_llamadas_suma_none(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = (None, 0)
        r = operativo.gasto_llm_hoy(mock_conn, 5.0)
        assert r["gasto_usd"] == 0.0 and r["pct"] == 0.0 and r["llamadas"] == 0

    def test_tope_cero_no_divide(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = (1.0, 1)
        assert operativo.gasto_llm_hoy(mock_conn, 0)["pct"] is None


class TestLatenciaYErroresLlm:
    def test_sql_binds_y_forma(self, mock_conn, mock_cursor):
        mock_cursor.fetchall.return_value = [("2026-10-22", 4, 800.4, 2100.6, 25.0)]
        r = operativo.latencia_y_errores_llm(mock_conn, dias=14)
        sql, binds = mock_cursor.execute.call_args[0]
        assert "PERCENTILE_CONT(0.5)" in sql and "PERCENTILE_CONT(0.95)" in sql
        assert "1 - AVG(ok)" in sql and "GROUP  BY TRUNC(ts)" in sql
        assert binds == {"dias": 14}
        assert r == [{"dia": "2026-10-22", "llamadas": 4, "p50_ms": 800, "p95_ms": 2101,
                      "tasa_error": 25.0}]
        mock_cursor.close.assert_called_once()

    def test_latencia_nula_y_vacio(self, mock_conn, mock_cursor):
        mock_cursor.fetchall.return_value = [("d", 2, None, None, None)]
        r = operativo.latencia_y_errores_llm(mock_conn)
        assert r[0]["p50_ms"] is None and r[0]["p95_ms"] is None and r[0]["tasa_error"] == 0.0
        mock_cursor.fetchall.return_value = []
        assert operativo.latencia_y_errores_llm(mock_conn) == []
