"""src.analytics.regional — KPIs de la vista regional (R1 y el nombre de cada país).

Toda función recibe ``clientes: Sequence[str]`` YA filtrado por el alcance de la persona
(`src.identidad.alcance`): estas funciones no deciden quién ve qué, solo agregan lo que se les
da. Una lista vacía devuelve vacío sin tocar la base. Los demás KPI regionales (R2-R5) siguen
bloqueados por datos (ver `docs/planes/portal-amalia.md`).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from src.analytics.pais import _SIN_RESTRICCION, _SQL_ES_RELEVANTE
from src.memoria.relevancia import sql_marca

logger = logging.getLogger(__name__)


def _binds_in(clientes: Sequence[str]) -> tuple[str, dict[str, str]]:
    """``:c0, :c1, ...`` y sus valores: todo valor viaja como bind, nunca en el texto del SQL."""
    binds = {f"c{i}": c for i, c in enumerate(clientes)}
    return ", ".join(f":{k}" for k in binds), binds


def nombres_clientes(conn: Any, clientes: Sequence[str]) -> dict[str, str]:
    """``cliente_id`` → nombre legible, solo de los clientes pedidos."""
    if not clientes:
        return {}
    marcadores, binds = _binds_in(clientes)
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT cliente_id, nombre FROM cliente WHERE cliente_id IN ({marcadores})", binds)
        return {fila[0]: fila[1] for fila in cursor.fetchall()}
    finally:
        cursor.close()


def oportunidades_por_pais(conn: Any, clientes: Sequence[str], dias: int = 90, *,
                           solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> list[dict]:
    """R1 — bar horizontal: oportunidades (procesos relevantes) de los últimos ``dias`` días por país.

    Devuelve UNA fila por cliente pedido, también los que no tienen datos (conteo 0): quien llama
    decide cómo mostrar un país sin integración, y un cero nunca se confunde con «no hay país».
    Orden: mayor a menor, empates por ``cliente_id``. Con ``solo_marcas`` (E14) cuenta solo las oportunidades
    de esas marcas; los países sin ninguna siguen apareciendo en 0 (ocultarlos revelaría cobertura).
    """
    if not clientes:
        return []
    marcadores, binds = _binds_in(clientes)
    binds["dias"] = dias
    sql = f"""
        SELECT cp.cliente_id, COUNT(*)
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id IN ({marcadores})
          AND  p.fecha_pub_utc >= SYSDATE - :dias
          AND  {_SQL_ES_RELEVANTE}
          AND  {sql_marca(solo_marcas)}
        GROUP  BY cp.cliente_id
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        conteos = {fila[0]: int(fila[1]) for fila in cursor.fetchall()}
    finally:
        cursor.close()
    filas = [{"cliente_id": c, "conteo": conteos.get(c, 0)} for c in clientes]
    return sorted(filas, key=lambda f: (-f["conteo"], f["cliente_id"]))
