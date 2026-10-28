"""Tests de `pais.relevancia_estricta` (E11.1) y del día de corrida — cursor falso, sin Oracle.

El SQL lo ejercita contra Oracle real `tests/integration/test_oracle_local.py`; aquí se fija su
estructura (MAX por día, mismos días en numerador y denominador, definiciones de
`src.memoria.relevancia`) y el tratamiento en Python (None sin revisados, cotas, redondeo).
"""
import re
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.analytics import pais
from src.memoria import relevancia


@pytest.fixture
def cursor():
    c = MagicMock()
    c.fetchone = MagicMock(return_value=(0, 0, 0))
    return c


@pytest.fixture
def conn(cursor):
    cn = MagicMock()
    cn.cursor = MagicMock(return_value=cursor)
    return cn


def _sql(cursor):
    return cursor.execute.call_args[0][0]


def _cte(sql, nombre, siguiente=None):
    cuerpo = sql.split(f"{nombre} AS (")[1]
    return cuerpo.split(siguiente)[0] if siguiente else cuerpo


# ── resultado ───────────────────────────────────────────────────────────────

def test_cifras_reales_a_30_dias(conn, cursor):
    cursor.fetchone.return_value = (1139, 187, 85)
    r = pais.relevancia_estricta(conn, "multicomputos", 30)
    assert r == {"revisados": 1139, "relevantes": 187, "aptos": 85, "pct_aptos": 7.5, "pct_relevantes": 16.4,
                 "filtrado_por_marca": False}


def test_cifras_de_un_dia(conn, cursor):
    cursor.fetchone.return_value = (79, 15, 8)
    r = pais.relevancia_estricta(conn, "multicomputos", 1)
    assert (r["aptos"], r["pct_aptos"], r["pct_relevantes"]) == (8, 10.1, 19.0)


@pytest.mark.parametrize("fila", [(0, 0, 0), (None, None, None), None])
def test_sin_revisados_pct_none_sin_dividir_por_cero(conn, cursor, fila):
    cursor.fetchone.return_value = fila
    r = pais.relevancia_estricta(conn, "cli", 30)
    assert r == {"revisados": 0, "relevantes": 0, "aptos": 0, "pct_aptos": None, "pct_relevantes": None,
                 "filtrado_por_marca": False}


def test_cotas_aptos_le_relevantes_le_revisados(conn, cursor):
    # Defensa en Python además de la de SQL: nunca un cociente > 100 ni aptos > relevantes.
    cursor.fetchone.return_value = (10, 25, 40)
    r = pais.relevancia_estricta(conn, "cli", 30)
    assert r["aptos"] <= r["relevantes"] <= r["revisados"] == 10
    assert r["pct_relevantes"] == 100.0 and r["pct_aptos"] == 100.0


def test_decimal_de_oracle(conn, cursor):
    from decimal import Decimal
    cursor.fetchone.return_value = (Decimal(3), Decimal(1), Decimal(1))
    r = pais.relevancia_estricta(conn, "cli", 7)
    assert r["pct_aptos"] == 33.3 and isinstance(r["revisados"], int)


# ── SQL ─────────────────────────────────────────────────────────────────────

def test_binds_con_nombre_y_una_sola_consulta(conn, cursor):
    pais.relevancia_estricta(conn, "CLI-X", 14)
    assert cursor.execute.call_count == 1
    sql, binds = cursor.execute.call_args[0]
    assert binds == {"cliente_id": "CLI-X", "dias": 14}
    assert "CLI-X" not in sql and ":cliente_id" in sql and ":dias" in sql
    cursor.close.assert_called_once()


def test_revisados_max_por_dia_de_corridas_ok(conn, cursor):
    """Corridas repetidas el mismo día: cuenta el MAX del día, no la suma."""
    pais.relevancia_estricta(conn, "cli", 30)
    corridas = _cte(_sql(cursor), "corridas", "), clasificados AS")
    assert "FROM ejecucion e" in corridas and "e.success = 1" in corridas
    assert "MAX(e.procesos_revisados)" in corridas and "SUM(" not in corridas
    assert "GROUP BY TRUNC(e.run_timestamp)" in corridas
    # Día de El Salvador, como operativo.py: no depende de la zona de sesión de la ADB.
    assert "AT TIME ZONE 'America/El_Salvador'" in corridas
    # Días sin revisión real no aportan denominador (ni, por el JOIN, numerador).
    assert "HAVING MAX(e.procesos_revisados) > 0" in corridas


def test_numerador_usa_las_definiciones_unicas(conn, cursor):
    pais.relevancia_estricta(conn, "cli", 30)
    clasificados = _cte(_sql(cursor), "clasificados")
    assert f"SUM(CASE WHEN {relevancia.SQL_ES_RELEVANTE} THEN 1 ELSE 0 END)" in clasificados
    assert f"SUM(CASE WHEN {relevancia.SQL_ES_APTO} THEN 1 ELSE 0 END)" in clasificados
    assert f"GROUP BY {relevancia.SQL_DIA_DE_CORRIDA}" in clasificados
    assert "cp.cliente_id = :cliente_id" in clasificados


def test_numerador_y_denominador_sobre_los_mismos_dias(conn, cursor):
    """Un día con relevantes pero sin corrida OK no suma al numerador (la base es `corridas`,
    con LEFT JOIN a los clasificados), y por día se acota aptos ≤ relevantes ≤ revisados."""
    pais.relevancia_estricta(conn, "cli", 30)
    final = _sql(cursor).split("clasificados AS (")[1].split("GROUP BY")[-1]
    assert re.search(r"FROM corridas c\s+LEFT JOIN clasificados k ON k\.dia = c\.dia", final)
    assert "LEAST(NVL(k.relevantes, 0), c.revisados)" in final
    assert "LEAST(NVL(k.aptos, 0), NVL(k.relevantes, 0), c.revisados)" in final


# ── día de corrida ──────────────────────────────────────────────────────────

def _dia_de_corrida(fecha_pub_utc: datetime):
    """Evalúa `SQL_DIA_DE_CORRIDA` en Python: TRUNC(fecha + INTERVAL 'N' HOUR)."""
    m = re.fullmatch(r"TRUNC\(p\.fecha_pub_utc \+ INTERVAL '(\d+)' HOUR\)", relevancia.SQL_DIA_DE_CORRIDA)
    assert m, relevancia.SQL_DIA_DE_CORRIDA
    return (fecha_pub_utc + timedelta(hours=int(m.group(1)))).date()


def test_borde_de_las_13_utc():
    """La corrida de las 07:00 SV (13:00 UTC) mira las 24 h previas: 12:59 UTC entra en la del
    mismo día, 13:01 UTC en la del día siguiente."""
    antes = _dia_de_corrida(datetime(2026, 10, 26, 12, 59))
    despues = _dia_de_corrida(datetime(2026, 10, 26, 13, 1))
    assert str(antes) == "2026-10-26" and str(despues) == "2026-10-27"


def test_ventana_de_una_corrida_es_de_24_horas():
    assert _dia_de_corrida(datetime(2026, 10, 25, 13, 0)) == _dia_de_corrida(datetime(2026, 10, 26, 12, 59))
    assert _dia_de_corrida(datetime(2026, 10, 26, 0, 0)).isoformat() == "2026-10-26"
