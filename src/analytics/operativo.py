"""src.analytics.operativo — KPIs operativos 11-13 del portal (Fase 2 del plan).

Ver ``docs/planes/portal-amalia.md``, tabla maestra, KPIs 11-13. A diferencia de
``pais.py``, estas tablas (``ejecucion``, ``llm_llamada``) no tienen
``cliente_id``: describen la salud del sistema, no de un cliente.

Las marcas de tiempo (``run_timestamp``, ``ts``) se guardan en hora de El
Salvador sin zona. "Ahora" y "hoy" se calculan en la base, convirtiendo
``SYSTIMESTAMP`` a esa zona: no dependen del reloj de Python ni de la zona de
sesión de la ADB (que suele ser UTC).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SQL_AHORA = "CAST(SYSTIMESTAMP AT TIME ZONE 'America/El_Salvador' AS TIMESTAMP)"


def estado_ultima_corrida(conn: Any) -> dict:
    """KPI 11 — estado de la última corrida: ``estado`` OK/ERROR, hace cuántos
    minutos, y el detalle. Sin filas: todo ``None`` (la plantilla dice "sin datos")."""
    sql = f"""
        SELECT success, procesos_revisados, version, error_msg,
               ROUND((CAST({_SQL_AHORA} AS DATE) - CAST(run_timestamp AS DATE)) * 1440) AS hace_min
        FROM   ejecucion
        ORDER  BY run_timestamp DESC
        FETCH  FIRST 1 ROWS ONLY
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        fila = cursor.fetchone()
    finally:
        cursor.close()

    if not fila:
        logger.info("estado_ultima_corrida: sin corridas registradas")
        return {"estado": None, "hace_minutos": None, "procesos_revisados": None,
                "version": None, "error_msg": None}

    success, revisados, version, error_msg, hace_min = fila
    resultado = {
        "estado": "OK" if success else "ERROR",
        "hace_minutos": max(int(hace_min), 0) if hace_min is not None else None,
        "procesos_revisados": int(revisados or 0),
        "version": version,
        "error_msg": error_msg,
    }
    logger.info("estado_ultima_corrida: estado=%s hace_minutos=%s",
                resultado["estado"], resultado["hace_minutos"])
    return resultado


def gasto_llm_hoy(conn: Any, tope_usd: float) -> dict:
    """KPI 12 — gasto LLM del día (hora de El Salvador) contra el tope diario.
    Lee ``llm_llamada``, no el libro JSON local de ``quota.py``: el contenedor
    web puede no ver ``data_dir``. Sin llamadas hoy: ``gasto_usd`` = 0.0."""
    sql = f"""
        SELECT SUM(costo_estimado_usd), COUNT(*)
        FROM   llm_llamada
        WHERE  ts >= TRUNC({_SQL_AHORA})
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        gasto, llamadas = cursor.fetchone()
    finally:
        cursor.close()

    gasto = float(gasto or 0)
    tope = float(tope_usd)
    pct = round(100 * gasto / tope, 1) if tope > 0 else None
    logger.info("gasto_llm_hoy: gasto=%.4f tope=%.2f llamadas=%s", gasto, tope, llamadas)
    return {"gasto_usd": gasto, "tope_usd": tope, "pct": pct, "llamadas": int(llamadas or 0)}


def latencia_y_errores_llm(conn: Any, dias: int = 14) -> list[dict]:
    """KPI 13 — por día, últimos ``dias``: llamadas, latencia p50/p95 (ms, ``None``
    si ninguna llamada del día midió latencia) y tasa de error en %. Solo
    aparecen los días con llamadas; ordenado de más viejo a más reciente."""
    sql = f"""
        SELECT TRUNC(ts) AS dia,
               COUNT(*) AS llamadas,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latencia_ms) AS p50,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latencia_ms) AS p95,
               ROUND(100 * (1 - AVG(ok)), 1) AS tasa_error
        FROM   llm_llamada
        WHERE  ts >= TRUNC({_SQL_AHORA}) - :dias + 1
        GROUP  BY TRUNC(ts)
        ORDER  BY dia
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"dias": int(dias)})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    serie = [
        {
            "dia": dia,
            "llamadas": int(llamadas),
            "p50_ms": round(float(p50)) if p50 is not None else None,
            "p95_ms": round(float(p95)) if p95 is not None else None,
            "tasa_error": float(tasa) if tasa is not None else 0.0,
        }
        for dia, llamadas, p50, p95, tasa in filas
    ]
    logger.info("latencia_y_errores_llm: dias=%s filas=%s", dias, len(serie))
    return serie


__all__ = ["estado_ultima_corrida", "gasto_llm_hoy", "latencia_y_errores_llm"]
