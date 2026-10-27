"""E10 «Ver como»: SQL y binds de las tres funciones de `src.identidad.repo`, contra un cursor falso."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.identidad import repo


@pytest.fixture
def cursor():
    return MagicMock()


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


class TestFijarVerComo:
    def test_sql_y_binds_solo_sobre_sesion_viva_y_completa(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.fijar_ver_como(conn, "h1", "gerente_pais", "cli_a") is True
        sql, binds = cursor.execute.call_args[0]
        assert sql.startswith("UPDATE sesion SET ver_como_rol = :r, ver_como_cliente = :c, ver_como_desde = CAST(SYSTIMESTAMP AS TIMESTAMP)")
        assert "revocada = 0" in sql and "mfa_ok = 1" in sql and "expira_en > SYSTIMESTAMP" in sql
        assert binds == {"r": "gerente_pais", "c": "cli_a", "s": "h1"}
        conn.commit.assert_not_called()   # como el resto de la sección: commit lo hace quien llama

    def test_cliente_none_viaja_como_null(self, conn, cursor):
        cursor.rowcount = 1
        repo.fijar_ver_como(conn, "h1", "ceo", None)
        assert cursor.execute.call_args[0][1]["c"] is None

    def test_false_si_la_sesion_no_cumple(self, conn, cursor):
        cursor.rowcount = 0
        assert repo.fijar_ver_como(conn, "h1", "ceo", None) is False

    def test_cierra_el_cursor(self, conn, cursor):
        cursor.rowcount = 1
        repo.fijar_ver_como(conn, "h1", "ceo", None)
        cursor.close.assert_called_once()


class TestLimpiarVerComo:
    def test_pone_las_tres_columnas_en_null(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.limpiar_ver_como(conn, "h1") is True
        sql, binds = cursor.execute.call_args[0]
        assert "ver_como_rol = NULL" in sql and "ver_como_cliente = NULL" in sql and "ver_como_desde = NULL" in sql
        assert "ver_como_rol IS NOT NULL" in sql
        assert binds == {"s": "h1"}

    def test_idempotente_false_si_no_habia_nada(self, conn, cursor):
        cursor.rowcount = 0
        assert repo.limpiar_ver_como(conn, "h1") is False


class TestObtenerVerComo:
    def test_devuelve_la_tupla(self, conn, cursor):
        desde = datetime(2026, 10, 26, 12, 0)
        cursor.fetchone.return_value = ("gerente_pais", "cli_a", desde)
        assert repo.obtener_ver_como(conn, "h1", 30) == ("gerente_pais", "cli_a", desde)
        sql, binds = cursor.execute.call_args[0]
        assert "revocada = 0" in sql and "mfa_ok = 1" in sql and "expira_en > SYSTIMESTAMP" in sql
        assert "ver_como_rol IS NOT NULL" in sql
        assert "ver_como_desde > CAST(SYSTIMESTAMP AS TIMESTAMP) - NUMTODSINTERVAL(:ttl, 'MINUTE')" in sql
        assert binds == {"s": "h1", "ttl": 30}

    def test_cliente_puede_ser_none(self, conn, cursor):
        desde = datetime(2026, 10, 26, 12, 0)
        cursor.fetchone.return_value = ("ceo", None, desde)
        assert repo.obtener_ver_como(conn, "h1", 30) == ("ceo", None, desde)

    def test_none_si_no_hay_simulacion_vigente(self, conn, cursor):
        cursor.fetchone.return_value = None
        assert repo.obtener_ver_como(conn, "h1", 30) is None


def test_renovar_sesion_no_toca_la_simulacion(conn, cursor):
    """La simulación no se desliza: renovar no puede reiniciar `ver_como_desde`."""
    cursor.rowcount = 1
    repo.renovar_sesion(conn, "h1", 12, 7)
    assert "ver_como" not in cursor.execute.call_args[0][0]
