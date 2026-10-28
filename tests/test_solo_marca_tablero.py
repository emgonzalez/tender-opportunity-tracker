"""E14 — `solo_marcas` en tablero, oportunidades y conflictos: SQL emitido (cursor falso, sin Oracle) e IDOR por marca.

Con restricción, cada consulta lleva la cláusula de marca (sobre `cp`, o un EXISTS correlacionado a `cliente_proceso`
donde la consulta no lo une), FUERA de los paréntesis de los OR; sin restricción, el SQL no cambia. Un gid del mismo
cliente que no es de la marca se comporta EXACTAMENTE como uno inexistente: NO_ENCONTRADO y nada escrito.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.analytics import tablero
from src.identidad.alcance import Alcance
from src.memoria.relevancia import sql_marca
from src.mercado import repo as mercado_repo
from src.oportunidades import repo, service

ORACLE = frozenset({"oracle"})
CLAUSULA_CP = sql_marca(ORACLE, "cp")
CLAUSULA_CPM = sql_marca(ORACLE, "cpm")
EXISTS_CPM = "EXISTS (SELECT 1 FROM cliente_proceso cpm WHERE cpm.cliente_id = "


@pytest.fixture
def cursor():
    c = MagicMock()
    c.fetchone = MagicMock(return_value=(0, 0, None))
    c.fetchall = MagicMock(return_value=[])
    return c


@pytest.fixture
def conn(cursor):
    cn = MagicMock()
    cn.cursor = MagicMock(return_value=cursor)
    return cn


def _sqls(cursor):
    return [c[0][0] for c in cursor.execute.call_args_list]


def _profundidad_en(sql: str, desde: str, clausula: str) -> int:
    """Paréntesis abiertos entre `desde` (p. ej. el WHERE) y la cláusula: 0 = está al nivel de los AND."""
    i, j = sql.index(desde), sql.index(clausula)
    tramo = sql[i:j]
    return tramo.count("(") - tramo.count(")")


# ── tablero: cada lectura con y sin restricción ────────────────────────────

CON_CP = {  # consultas que unen `cliente_proceso cp`: `sql_marca(…, "cp")`
    "procesos_del_rango": (lambda cn, m: tablero.procesos_del_rango(cn, "cli", 30, solo_marcas=m), 2),
    "descartados_del_rango": (lambda cn, m: tablero.descartados_del_rango(cn, "cli", 30, solo_marcas=m), 3),
    "tendencia_semanal": (lambda cn, m: tablero.tendencia_semanal(cn, "cli", solo_marcas=m), 1),
    "marcas_top": (lambda cn, m: tablero.marcas_top(cn, "cli", 30, solo_marcas=m), 1),
}
SIN_CP = {  # consultas sin `cp`: EXISTS correlacionado por (cliente_id, proceso_gid)
    "votos_resumen": (lambda cn, m: tablero.votos_resumen(cn, "cli", 30, solo_marcas=m), 3),
    "votos_por_proceso": (lambda cn, m: tablero.votos_por_proceso(cn, "cli", ["g1"], solo_marcas=m), 1),
    "pipeline_en_juego": (lambda cn, m: tablero.pipeline_en_juego(cn, "cli", solo_marcas=m), 1),
    "estancadas": (lambda cn, m: tablero.estancadas(cn, "cli", solo_marcas=m), 1),
    "listar_conflictos_oportunidad_v2": (
        lambda cn, m: mercado_repo.listar_conflictos_oportunidad_v2(cn, "cli", solo_marcas=m), 1),
}


@pytest.mark.parametrize("nombre", sorted(CON_CP))
def test_con_cp_todas_las_sql_llevan_la_marca(nombre, conn, cursor):
    llamada, n = CON_CP[nombre]
    llamada(conn, ORACLE)
    sqls = _sqls(cursor)
    assert len(sqls) == n
    for sql in sqls:
        assert CLAUSULA_CP in sql, nombre


@pytest.mark.parametrize("nombre", sorted(SIN_CP))
def test_sin_cp_todas_las_sql_llevan_el_exists(nombre, conn, cursor):
    llamada, n = SIN_CP[nombre]
    llamada(conn, ORACLE)
    sqls = _sqls(cursor)
    assert len(sqls) == n
    for sql in sqls:
        assert EXISTS_CPM in sql and CLAUSULA_CPM in sql, nombre
        assert "cpm.proceso_gid = " in sql


@pytest.mark.parametrize("nombre", sorted(CON_CP) + sorted(SIN_CP))
def test_sin_restriccion_no_hay_clausula(nombre, conn, cursor):
    llamada, _ = {**CON_CP, **SIN_CP}[nombre]
    llamada(conn, frozenset())
    for sql in _sqls(cursor):
        assert "cpm" not in sql
        assert "REGEXP_LIKE(jm.m" not in sql and "REGEXP_LIKE(pm.titulo" not in sql


@pytest.mark.parametrize("nombre", ["procesos_del_rango", "descartados_del_rango"])
def test_la_marca_va_fuera_de_los_parentesis_de_los_or(nombre, conn, cursor):
    CON_CP[nombre][0](conn, ORACLE)
    for sql in _sqls(cursor):
        if "SELECT MIN(cp.clasificado_en)" in sql:
            continue
        assert _profundidad_en(sql, "WHERE cp.cliente_id = :cliente_id", CLAUSULA_CP) == 0
        assert f"AND {CLAUSULA_CP}" in sql


def test_procesos_del_rango_total_y_filas_comparten_el_desde(conn, cursor):
    tablero.procesos_del_rango(conn, "cli", 30, solo_marcas=ORACLE)
    total, filas = _sqls(cursor)
    assert total.startswith("SELECT COUNT(*)")
    assert total[len("SELECT COUNT(*)"):] in filas  # el conteo es exactamente del mismo universo filtrado


def test_descartados_total_desde_y_filas_son_de_la_marca(conn, cursor):
    tablero.descartados_del_rango(conn, "cli", 30, solo_marcas=ORACLE)
    total, desde, filas = _sqls(cursor)
    assert "SELECT COUNT(*)" in total and CLAUSULA_CP in total
    assert "MIN(cp.clasificado_en)" in desde and CLAUSULA_CP in desde
    assert CLAUSULA_CP in filas
    # la marca no se mete en `_SQL_DESCARTADO` (que niega la relevancia con CASE): va después, al nivel de los AND
    assert filas.index(CLAUSULA_CP) > filas.index("THEN 1 ELSE 0 END = 0")


def test_tendencia_marca_solo_en_relevantes_y_revisados_oculto(conn, cursor):
    from datetime import datetime
    cursor.fetchall.return_value = [(datetime(2026, 10, 19), 120, 3)]
    r = tablero.tendencia_semanal(conn, "cli", semanas=1, solo_marcas=ORACLE)
    (sql,) = _sqls(cursor)
    corridas = sql[sql.index("corridas AS"):sql.index("revisados AS")]
    relevantes = sql[sql.index("relevantes AS ("):]
    assert CLAUSULA_CP not in corridas and CLAUSULA_CP in relevantes
    assert r == [{"semana": datetime(2026, 10, 19).date(), "revisados": None, "relevantes": 3}]


def test_tendencia_sin_restriccion_mantiene_revisados(conn, cursor):
    from datetime import datetime
    cursor.fetchall.return_value = [(datetime(2026, 10, 19), 120, 3)]
    assert tablero.tendencia_semanal(conn, "cli", semanas=1)[0]["revisados"] == 120


def test_marcas_top_no_nombra_co_marcas(conn, cursor):
    tablero.marcas_top(conn, "cli", 30, solo_marcas=ORACLE)
    (sql,) = _sqls(cursor)
    assert "REGEXP_LIKE(jt.marca, '^oracle([^[:alnum:]_]|$)', 'i')" in sql
    tablero.marcas_top(conn, "cli", 30)
    assert "jt.marca, '^oracle'" not in _sqls(cursor)[-1]


def test_procesos_del_rango_recorta_co_marcas_de_la_tarjeta(conn, cursor):
    filas = [tuple({"proceso_gid": "g1", "marcas": '[{"marca": "Dell"}, {"marca": "Oracle"}, '
                                                    '{"marca": "Oracle NetSuite"}]'}.get(c) for c in tablero._COLUMNAS)]
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = filas
    assert tablero.procesos_del_rango(conn, "cli", 30, solo_marcas=ORACLE)["filas"][0]["marcas"] == \
        ["Oracle", "Oracle NetSuite"]
    assert tablero.procesos_del_rango(conn, "cli", 30)["filas"][0]["marcas"] == ["Dell", "Oracle", "Oracle NetSuite"]


def test_conflictos_correlaciona_por_el_gid_de_la_oportunidad(conn, cursor):
    mercado_repo.listar_conflictos_oportunidad_v2(conn, "cli", solo_marcas=ORACLE)
    (sql,) = _sqls(cursor)
    assert "cpm.cliente_id = e.cliente_id AND cpm.proceso_gid = e.proceso_gid" in sql
    # el gid del participado se arma como portal_id:codigo_proceso y se iguala a e.proceso_gid
    assert ":portal_id || ':' || p.codigo_proceso = e.proceso_gid" in sql


# ── oportunidades.repo ─────────────────────────────────────────────────────

@pytest.mark.parametrize("llamada,clausula", [
    (lambda cn, m: repo.proceso_visible(cn, "cli", "g", solo_marcas=m), CLAUSULA_CP),
    (lambda cn, m: repo.es_descartado_por_amalia(cn, "cli", "g", solo_marcas=m), CLAUSULA_CP),
    (lambda cn, m: repo.listar(cn, "cli", solo_marcas=m), CLAUSULA_CP),
    (lambda cn, m: repo.obtener(cn, "cli", "g", solo_marcas=m), CLAUSULA_CPM),
])
def test_repo_por_gid_lleva_la_marca(llamada, clausula, conn, cursor):
    cursor.fetchone.return_value = None
    llamada(conn, ORACLE)
    (sql,) = _sqls(cursor)
    assert clausula in sql
    cursor.execute.reset_mock()
    llamada(conn, frozenset())
    (sql,) = _sqls(cursor)
    assert "JSON_TABLE" not in sql and "cpm" not in sql


# ── IDOR por marca en el servicio ──────────────────────────────────────────

class _BD:
    """Conexión falsa: el gid EXISTE para el cliente pero NO es Oracle. Toda consulta con la cláusula de marca no
    devuelve fila; sin ella, sí. Con `inexistente`, nunca hay fila. Registra cualquier escritura."""

    def __init__(self, inexistente=False):
        self.inexistente = inexistente
        self.escrituras: list[str] = []

    def cursor(self):
        bd = self
        c = MagicMock()

        def execute(sql, binds=None):
            c._sql = sql
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "MERGE")):
                bd.escrituras.append(sql)

        def fetchone():
            sql = c._sql
            if bd.inexistente or "JSON_TABLE" in sql:
                return None
            if "oportunidad_estado e" in sql and "SELECT e.estado" in sql:
                return None  # sin estado comercial todavía
            return (1,)

        c.execute = MagicMock(side_effect=execute)
        c.fetchone = MagicMock(side_effect=fetchone)
        c.rowcount = 1
        return c


def _alcance(solo_marcas=frozenset()):
    return Alcance(usuario_id="u1", org_id="org", roles=frozenset({"gerente_pais"}), clientes=frozenset({"cli"}),
                   cambia_estado=frozenset({"cli"}), ve_operativo=False, solo_marcas=solo_marcas)


ESCRITURAS = {
    "cambiar_estado": lambda bd, a: service.cambiar_estado(bd, a, "cli", "comprasal_sv:X", "EVALUANDO", ""),
    "asignar_responsable": lambda bd, a: service.asignar_responsable(bd, a, "cli", "comprasal_sv:X", None, ""),
    "recuperar_descartado": lambda bd, a: service.recuperar_descartado(bd, a, "cli", "comprasal_sv:X"),
}


@pytest.mark.parametrize("nombre", sorted(ESCRITURAS))
def test_gid_de_otra_marca_es_igual_que_inexistente(nombre, monkeypatch):
    auditoria = []
    monkeypatch.setattr(service.identidad, "registrar_auditoria", lambda *a, **k: auditoria.append(a))
    escribir = ESCRITURAS[nombre]

    ajeno = _BD()
    r_ajeno = escribir(ajeno, _alcance(ORACLE))
    inexistente = _BD(inexistente=True)
    r_inexistente = escribir(inexistente, _alcance(ORACLE))

    assert r_ajeno == r_inexistente == service.NO_ENCONTRADO
    assert ajeno.escrituras == inexistente.escrituras == [] and auditoria == []


def test_sin_restriccion_el_mismo_gid_si_se_escribe(monkeypatch):
    """Contraste: el falso no bloquea por sí solo; sin `solo_marcas` el mismo gid pasa la barrera y se escribe."""
    monkeypatch.setattr(service.identidad, "registrar_auditoria", lambda *a, **k: None)
    bd = _BD()
    assert service.cambiar_estado(bd, _alcance(), "cli", "comprasal_sv:X", "EVALUANDO", "") == service.OK
    assert bd.escrituras


def test_puede_escribir_pasa_la_marca_del_alcance(monkeypatch):
    vistas = []
    monkeypatch.setattr(repo, "proceso_visible", lambda c, cl, g, **k: vistas.append(k) or False)
    assert service._puede_escribir(None, _alcance(ORACLE), "cli", "g") == service.NO_ENCONTRADO
    assert vistas == [{"solo_marcas": ORACLE}]
