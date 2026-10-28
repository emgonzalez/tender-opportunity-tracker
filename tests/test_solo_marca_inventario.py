"""E14 · inventario de `solo_marcas`: ninguna consulta por cliente puede olvidar la restricción por marca.

Recorre (AST + los objetos reales) toda función pública de `src/analytics/*.py`, `src/oportunidades/repo.py` y
`src/mercado/repo.py`. Si su SQL —el escrito en la función o el que arrastra de constantes y funciones de `src.*`
que referencia, de forma transitiva— lee `cliente_proceso`, `oportunidad_estado`, `oportunidad_evento` o
`feedback`, la función tiene que aceptar `solo_marcas` como parámetro keyword y usarlo en su cuerpo. Una consulta
nueva sin la restricción rompe este test antes de llegar a un rol Oracle; la única salida es la lista de
excepciones de abajo, explícita y comentada (y que no puede quedar vieja: una excepción que ya no lee esas
tablas también falla).

No necesita base de datos: solo lee código.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import textwrap
import types

import pytest

import src.analytics

_TABLAS = ("cliente_proceso", "oportunidad_estado", "oportunidad_evento", "feedback")
# Lectura (FROM/JOIN) o escritura (INSERT INTO / UPDATE / MERGE INTO / DELETE FROM) de una tabla vigilada.
_RE_TABLA = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(" + "|".join(_TABLAS) + r")\b", re.IGNORECASE)
_EJECUTA = "\x00<execute>\x00"  # marca que `_sql_alcanzable` deja donde hay un `cursor.execute(...)`


def _modulos() -> list[types.ModuleType]:
    nombres = [f"src.analytics.{m.name}" for m in pkgutil.iter_modules(src.analytics.__path__)]
    nombres += ["src.oportunidades.repo", "src.mercado.repo"]
    return [importlib.import_module(n) for n in sorted(nombres)]


# Excepciones: funciones públicas que tocan esas tablas SIN `solo_marcas`, con el porqué. Clave: «módulo.función».
EXCEPCIONES = {
    # Escrituras del pipeline comercial. No leen el alcance porque la barrera va ANTES, en
    # `src.oportunidades.service._puede_escribir` → `repo.proceso_visible(..., solo_marcas=alcance.solo_marcas)`
    # (lo fija `test_toda_escritura_del_servicio_pasa_por_la_barrera`, abajo). Un gid de otra marca nunca llega aquí.
    "src.oportunidades.repo.crear_estado": "escritura; la barrera es service._puede_escribir",
    "src.oportunidades.repo.actualizar_estado": "escritura; la barrera es service._puede_escribir",
    "src.oportunidades.repo.fijar_responsable": "escritura; la barrera es service._puede_escribir",
    "src.oportunidades.repo.insertar_evento": "escritura; la barrera es service._puede_escribir",
    # Cron semanal de `src.mercado` (sin persona ni alcance): refleja GANADO/PERDIDO de COMPRASAL en
    # `oportunidad_estado` para TODO el cliente. No sirve a ninguna vista; lo que el portal lee de ahí pasa por
    # `listar_conflictos_oportunidad_v2`, que sí filtra.
    "src.mercado.repo.prepoblar_oportunidad_estado_v2": "escritura del cron de mercado, sin alcance",
}
# Fuera del inventario por construcción (no necesitan excepción): `analytics.operativo.*` lee `ejecucion` y
# `llm_llamada` (salud global del sistema, sin cliente ni proceso; el rol Oracle no ve /portal/operativo) y
# `regional.nombres_clientes` lee `cliente` (el nombre del país, que el alcance geográfico ya permite).


# ── recolección del SQL alcanzable desde una función ─────────────────────────

def _cadena_atributo(nodo: ast.AST) -> list[str] | None:
    """`a.b.c` → ["a", "b", "c"]; None si la base no es un nombre."""
    partes: list[str] = []
    while isinstance(nodo, ast.Attribute):
        partes.append(nodo.attr)
        nodo = nodo.value
    if not isinstance(nodo, ast.Name):
        return None
    partes.append(nodo.id)
    return partes[::-1]


def _imports_locales(arbol: ast.AST) -> dict[str, object]:
    """Los `import`/`from ... import` DENTRO de la función (p. ej. `from src.memoria import repo`)."""
    nombres: dict[str, object] = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            mod = importlib.import_module(nodo.module)
            for alias in nodo.names:
                try:
                    nombres[alias.asname or alias.name] = getattr(mod, alias.name)
                except AttributeError:
                    nombres[alias.asname or alias.name] = importlib.import_module(f"{nodo.module}.{alias.name}")
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                nombres[alias.asname or alias.name.split(".")[0]] = importlib.import_module(alias.name.split(".")[0])
    return nombres


def _resolver(partes: list[str], espacio: dict[str, object]) -> object | None:
    if partes[0] not in espacio:
        return None
    obj = espacio[partes[0]]
    for parte in partes[1:]:
        obj = getattr(obj, parte, None)
        if obj is None:
            return None
    return obj


def _es_de_src(obj: object) -> bool:
    return isinstance(obj, types.FunctionType) and (obj.__module__ or "").startswith("src.")


def _sql_alcanzable(fn: types.FunctionType, visitados: set[int] | None = None) -> str:
    """Todo texto literal de la función y, transitivamente, de las constantes `str` y funciones de `src.*` que
    nombra. Sobre-aproxima a propósito: preferible pedir `solo_marcas` de más que de menos."""
    visitados = set() if visitados is None else visitados
    if id(fn) in visitados:
        return ""
    visitados.add(id(fn))
    arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    espacio = {**fn.__globals__, **_imports_locales(arbol)}
    textos: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr in (
                "execute", "executemany"):
            textos.append(_EJECUTA)
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            textos.append(nodo.value)
        elif isinstance(nodo, (ast.Name, ast.Attribute)):
            partes = _cadena_atributo(nodo)
            if not partes:
                continue
            obj = _resolver(partes, espacio)
            if isinstance(obj, str):
                textos.append(obj)
            elif _es_de_src(obj) and obj is not fn:
                textos.append(_sql_alcanzable(obj, visitados))
    return "\n".join(textos)


def _tablas(fn: types.FunctionType) -> set[str]:
    """Tablas vigiladas que la función CONSULTA: solo cuenta si ella (o algo que llama) ejecuta SQL. Un armador
    de fragmentos como `sql_existe_marca` menciona `cliente_proceso` pero no ejecuta nada."""
    texto = _sql_alcanzable(fn)
    if _EJECUTA not in texto:
        return set()
    return {m.lower() for m in _RE_TABLA.findall(texto)}


def _publicas(mod: types.ModuleType) -> list[tuple[str, types.FunctionType]]:
    return [(f"{mod.__name__}.{n}", f) for n, f in inspect.getmembers(mod, inspect.isfunction)
            if not n.startswith("_") and f.__module__ == mod.__name__]


def _usa_solo_marcas(fn: types.FunctionType) -> bool:
    """El parámetro no está de adorno: el cuerpo lo lee (o lo pasa hacia abajo)."""
    arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    cuerpo = arbol.body[0].body
    return any(isinstance(n, ast.Name) and n.id == "solo_marcas" and isinstance(n.ctx, ast.Load)
               for nodo in cuerpo for n in ast.walk(nodo))


def _inventario() -> dict[str, tuple[types.FunctionType, set[str]]]:
    inventario = {}
    for mod in _modulos():
        for nombre, fn in _publicas(mod):
            tablas = _tablas(fn)
            if tablas:
                inventario[nombre] = (fn, tablas)
    return inventario


INVENTARIO = _inventario()


# ── el inventario ─────────────────────────────────────────────────────────────

def test_el_inventario_no_esta_vacio_y_ve_lo_transitivo():
    """Si el detector se volviera ciego, el resto pasaría en verde sin mirar nada."""
    esperadas = {
        # SQL escrito en la propia función
        "src.analytics.pais.oportunidades_del_mes", "src.analytics.pais.conversion_comercial",
        "src.analytics.regional.oportunidades_por_pais", "src.analytics.tablero.votos_resumen",
        "src.analytics.tablero.pipeline_en_juego", "src.oportunidades.repo.proceso_visible",
        "src.mercado.repo.listar_conflictos_oportunidad_v2",
        # SQL que llega de una constante de módulo (`_SQL_DESDE_RANGO` vía `_sql_desde_rango`)
        "src.analytics.tablero.procesos_del_rango", "src.analytics.tablero.descartados_del_rango",
        # SQL que vive en OTRO módulo (`memoria.repo.stats_por_institucion_v2`, importado dentro de la función)
        "src.analytics.pais.instituciones_top",
        # escrituras (excepciones)
        "src.oportunidades.repo.insertar_evento", "src.mercado.repo.prepoblar_oportunidad_estado_v2",
    }
    assert esperadas <= set(INVENTARIO), sorted(esperadas - set(INVENTARIO))
    # Lo que no toca esas tablas queda fuera (el detector no marca todo).
    assert "src.analytics.operativo.estado_ultima_corrida" not in INVENTARIO
    assert "src.analytics.regional.nombres_clientes" not in INVENTARIO
    assert "src.analytics.tablero.clasificar" not in INVENTARIO
    assert "src.mercado.repo.contar_pendientes" not in INVENTARIO
    assert "src.oportunidades.repo.sql_existe_marca" not in INVENTARIO  # arma SQL, no lo ejecuta


@pytest.mark.parametrize("nombre", sorted(n for n in INVENTARIO if n not in EXCEPCIONES))
def test_toda_consulta_por_cliente_acepta_y_usa_solo_marcas(nombre):
    fn, tablas = INVENTARIO[nombre]
    param = inspect.signature(fn).parameters.get("solo_marcas")
    assert param is not None, (
        f"{nombre} lee {sorted(tablas)} y no acepta `solo_marcas`: un rol Oracle vería filas de otras marcas. "
        "Añade `*, solo_marcas` y `AND {sql_marca(...)}` (o `sql_existe_marca`), o justifica una excepción.")
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{nombre}: `solo_marcas` debe ser keyword-only"
    assert _usa_solo_marcas(fn), f"{nombre} acepta `solo_marcas` pero no lo usa"


@pytest.mark.parametrize("nombre", sorted(EXCEPCIONES))
def test_las_excepciones_siguen_vigentes(nombre):
    """Una excepción que ya no toca esas tablas (o que se renombró) se borra de la lista, no se arrastra."""
    assert nombre in INVENTARIO, f"{nombre} ya no toca {_TABLAS}: quítala de EXCEPCIONES"
    assert EXCEPCIONES[nombre].strip(), "toda excepción lleva su porqué"


def test_toda_escritura_del_servicio_pasa_por_la_barrera():
    """Las escrituras exceptuadas solo se llaman desde funciones de `service` que antes pasan por
    `_puede_escribir` (permiso + `proceso_visible` con `solo_marcas`)."""
    from src.oportunidades import service

    escrituras = {n.rsplit(".", 1)[1] for n in EXCEPCIONES if n.startswith("src.oportunidades.repo.")}
    llamadoras = 0
    for nombre, fn in _publicas(service):
        arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        llamadas = [n for n in ast.walk(arbol) if isinstance(n, ast.Call)]
        escribe = any(_cadena_atributo(c.func) in (["repo", e] for e in escrituras) for c in llamadas)
        if not escribe:
            continue
        llamadoras += 1
        assert any(_cadena_atributo(c.func) == ["_puede_escribir"] for c in llamadas), (
            f"service.{nombre} escribe sin pasar por `_puede_escribir`")
    assert llamadoras >= 3  # cambiar_estado, asignar_responsable, recuperar_descartado


_SINTETICO = '''
_SQL = "SELECT COUNT(*) FROM feedback f WHERE f.cliente_id = :c"


def _ayudante(conn):
    return conn.execute(_SQL)


def directa(conn, cliente_id):
    return conn.execute("SELECT 1 FROM cliente_proceso cp WHERE cp.cliente_id = :c")


def indirecta(conn, cliente_id):
    return _ayudante(conn)


def correcta(conn, cliente_id, *, solo_marcas=frozenset()):
    return conn.execute(f"SELECT 1 FROM oportunidad_evento oe WHERE {solo_marcas!r}")


def de_adorno(conn, cliente_id, *, solo_marcas=frozenset()):
    return conn.execute("UPDATE oportunidad_estado SET estado = 'X'")


def sin_tablas(conn):
    return conn.execute("SELECT 1 FROM ejecucion")
'''


def test_el_detector_atrapa_una_consulta_nueva_sin_restriccion(tmp_path):
    """Control negativo con el detector REAL sobre un módulo `src.*` sintético: lectura directa, lectura que llega
    por una función y una constante privadas, una correcta, una con el parámetro de adorno y una sin tablas."""
    import importlib.util
    import sys

    ruta = tmp_path / "sintetico_e14.py"
    ruta.write_text(_SINTETICO, encoding="utf-8")
    nombre = "src._sintetico_e14"
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    try:
        spec.loader.exec_module(mod)
        tablas = {n.rsplit(".", 1)[1]: _tablas(f) for n, f in _publicas(mod)}
        assert tablas == {"directa": {"cliente_proceso"}, "indirecta": {"feedback"},
                          "correcta": {"oportunidad_evento"}, "de_adorno": {"oportunidad_estado"},
                          "sin_tablas": set()}
        assert "solo_marcas" not in inspect.signature(mod.directa).parameters
        assert "solo_marcas" not in inspect.signature(mod.indirecta).parameters
        assert _usa_solo_marcas(mod.correcta)
        assert not _usa_solo_marcas(mod.de_adorno)
    finally:
        sys.modules.pop(nombre, None)
