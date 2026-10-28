"""E14 · `solo_marcas` en las lecturas de la vista de país y la regional — cursor falso, sin Oracle.

Fija que, con restricción, CADA SQL emitido lleva la cláusula de `relevancia.sql_marca` (fuera del OR de
«relevante»), y que sin restricción no aparece. Que la cláusula filtre bien contra Oracle real lo cubre
`tests/integration/test_oracle_local.py`.
"""
import re
from unittest.mock import MagicMock

import pytest

from src.analytics import pais, regional
from src.memoria import repo
from src.memoria.relevancia import SQL_ES_RELEVANTE, sql_marca

ORACLE = frozenset({"oracle"})
CLAUSULA = sql_marca(ORACLE)
MARCADOR = "JSON_TABLE"  # solo lo emite sql_marca con restricción


@pytest.fixture
def cursor():
    c = MagicMock()
    c.fetchone = MagicMock(return_value=(0, 0))
    c.fetchall = MagicMock(return_value=[])
    return c


@pytest.fixture
def conn(cursor):
    cn = MagicMock()
    cn.cursor = MagicMock(return_value=cursor)
    return cn


def _sqls(cursor) -> list[str]:
    return [llamada[0][0] for llamada in cursor.execute.call_args_list]


def _fila(cn, fila):
    cn.cursor().fetchone.return_value = fila


LECTURAS = {
    "oportunidades_del_mes": lambda cn, sm: pais.oportunidades_del_mes(cn, "cli", solo_marcas=sm),
    "pct_relevancia_clasificador": lambda cn, sm: pais.pct_relevancia_clasificador(cn, "cli", solo_marcas=sm),
    "relevancia_estricta": lambda cn, sm: (_fila(cn, (0, 0, 0)), pais.relevancia_estricta(cn, "cli", 30, solo_marcas=sm)),
    "stats_por_institucion_v2": lambda cn, sm: repo.stats_por_institucion_v2(cn, "cli", 90, solo_marcas=sm),
    "instituciones_top": lambda cn, sm: pais.instituciones_top(cn, "cli", solo_marcas=sm),
    "embudo_pipeline": lambda cn, sm: pais.embudo_pipeline(cn, "cli", solo_marcas=sm),
    "severidad_riesgos": lambda cn, sm: pais.severidad_riesgos(cn, "cli", solo_marcas=sm),
    "conversion_comercial": lambda cn, sm: pais.conversion_comercial(cn, "cli", solo_marcas=sm),
    "tiempo_por_etapa": lambda cn, sm: pais.tiempo_por_etapa(cn, "cli", solo_marcas=sm),
    "oportunidades_por_pais": lambda cn, sm: regional.oportunidades_por_pais(cn, ["a", "b"], solo_marcas=sm),
}


@pytest.mark.parametrize("nombre", sorted(LECTURAS))
def test_con_restriccion_todo_sql_lleva_la_clausula(nombre, conn, cursor):
    LECTURAS[nombre](conn, ORACLE)
    sqls = _sqls(cursor)
    assert sqls, "debe emitir SQL"
    for sql in sqls:
        assert CLAUSULA in sql, f"{nombre}: SQL sin filtro de marca:\n{sql}"


@pytest.mark.parametrize("nombre", sorted(LECTURAS))
def test_sin_restriccion_no_hay_clausula(nombre, conn, cursor):
    LECTURAS[nombre](conn, frozenset())
    for sql in _sqls(cursor):
        assert MARCADOR not in sql


@pytest.mark.parametrize("nombre", sorted(LECTURAS))
def test_la_marca_nunca_viaja_dentro_del_or_de_relevancia(nombre, conn, cursor):
    LECTURAS[nombre](conn, ORACLE)
    for sql in _sqls(cursor):
        if SQL_ES_RELEVANTE in sql:
            assert CLAUSULA not in SQL_ES_RELEVANTE
            assert re.search(r"\bAND\s+" + re.escape(CLAUSULA), sql)


def test_relevancia_estricta_filtra_solo_clasificados_no_corridas(conn, cursor):
    cursor.fetchone.return_value = (0, 0, 0)
    pais.relevancia_estricta(conn, "cli", 30, solo_marcas=ORACLE)
    sql = _sqls(cursor)[0]
    corridas = sql.split("corridas AS (")[1].split("clasificados AS (")[0]
    clasificados = sql.split("clasificados AS (")[1]
    assert MARCADOR not in corridas
    assert CLAUSULA in clasificados


def test_relevancia_estricta_con_restriccion_oculta_cifras_globales(conn, cursor):
    cursor.fetchone.return_value = (1139, 19, 12)
    r = pais.relevancia_estricta(conn, "cli", 30, solo_marcas=ORACLE)
    assert r == {"revisados": None, "relevantes": 19, "aptos": 12, "pct_aptos": None,
                 "pct_relevantes": None, "filtrado_por_marca": True}


def test_relevancia_estricta_sin_restriccion_igual_que_siempre(conn, cursor):
    cursor.fetchone.return_value = (1139, 187, 85)
    r = pais.relevancia_estricta(conn, "cli", 30)
    assert r["revisados"] == 1139 and r["pct_aptos"] == 7.5 and r["filtrado_por_marca"] is False


def test_conversion_comercial_eventos_atados_a_cliente_proceso(conn, cursor):
    cursor.fetchone.return_value = (3,)
    pais.conversion_comercial(conn, "cli", solo_marcas=ORACLE)
    eventos = _sqls(cursor)[1]
    assert "FROM oportunidad_evento oe" in eventos
    assert ("EXISTS (SELECT 1 FROM cliente_proceso cp WHERE cp.cliente_id = oe.cliente_id "
            "AND cp.proceso_gid = oe.proceso_gid AND " + CLAUSULA + ")") in eventos


def test_tiempo_por_etapa_filtra_antes_del_lead(conn, cursor):
    pais.tiempo_por_etapa(conn, "cli", solo_marcas=ORACLE)
    sql = _sqls(cursor)[0]
    interior = sql.split("FROM (", 1)[1]
    # El EXISTS vive en la subconsulta que calcula el LEAD, no en el filtro exterior posterior.
    cierre_subconsulta = interior.index("WHERE duracion IS NOT NULL")
    assert "LEAD(" in interior[:cierre_subconsulta]
    assert CLAUSULA in interior[:cierre_subconsulta]
    assert CLAUSULA not in interior[cierre_subconsulta:]


def test_instituciones_top_propaga_solo_marcas(conn, monkeypatch):
    llamado = MagicMock(return_value=[])
    monkeypatch.setattr("src.memoria.repo.stats_por_institucion_v2", llamado)
    pais.instituciones_top(conn, "cli", dias=30, solo_marcas=ORACLE)
    llamado.assert_called_once_with(conn, "cli", dias=30, solo_marcas=ORACLE)


def test_oportunidades_por_pais_con_restriccion_conserva_los_paises_en_cero(conn, cursor):
    cursor.fetchall.return_value = [("a", 2)]
    filas = regional.oportunidades_por_pais(conn, ["a", "b"], solo_marcas=ORACLE)
    assert filas == [{"cliente_id": "a", "conteo": 2}, {"cliente_id": "b", "conteo": 0}]


def test_marca_desconocida_falla_en_vez_de_dejar_pasar_todo(conn):
    with pytest.raises(ValueError):
        pais.embudo_pipeline(conn, "cli", solo_marcas=frozenset({"x' OR 1=1 --"}))
