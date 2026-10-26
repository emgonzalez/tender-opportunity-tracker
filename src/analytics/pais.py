"""src.analytics.pais — KPIs de la vista de país (Fase 1 del portal).

Ver ``docs/planes/portal-amalia.md`` §3, tabla maestra, KPIs 1-6 — todos sobre
el esquema v2 (``cliente_proceso``/``proceso``/``analisis``), que son las
únicas tablas con datos reales verificados a la fecha del plan (🟢/🟡).

Reusa ``src.memoria.repo.stats_por_institucion_v2`` para el KPI 3 en vez de
reescribir la query: esa función ya existe, ya está probada (PR #50,
2026-09-30) y ya documenta por qué usa ``p.institucion`` (crudo) en vez de
``institucion_norm`` (sin poblar — ver el hallazgo 1 del plan).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Orden del "camino feliz" del embudo — DESCARTADO_*/ERROR_* son salidas, no
# etapas del camino, y se reportan aparte (ver `embudo_pipeline`). Mismo
# enum que `db/schema_v2.sql` (`ck_cliente_proceso_estado`).
_ETAPAS_CAMINO_FELIZ = (
    "DESCUBIERTO", "DESCARGADO", "FORMATO_NATIVO", "FORMATO_ESCANEADO",
    "APROBADO", "VECTORIZADO", "ANALIZADO", "NOTIFICADO",
)
_ETAPAS_SALIDA = (
    "DESCARTADO_REGEX", "DESCARTADO_TRIAJE", "DESCARTADO_TAMANO",
    "REQUIERE_REVISION_MANUAL", "ERROR_DESCARGA", "ERROR_PARSEO",
    "ERROR_LLM", "ERROR_BD",
)

# Orden de severidad para el KPI 6 — mismo criterio que
# `src.memoria.repo._ORDEN_SEVERIDAD`, pero de mayor a menor para el chart
# (se lee "lo más urgente arriba").
_ORDEN_SEVERIDAD_DESC = ("alta", "media", "baja")


_SQL_ES_RELEVANTE = """(cp.es_relevante = 1 OR cp.triaje = 'GO'
          OR cp.estado IN ('ANALIZADO', 'NOTIFICADO'))"""
# "Relevante" combina tres señales con OR, no solo `es_relevante`/`triaje`:
# verificado en vivo contra multicomputos_dev (2026-10-01) que las 37 filas
# migradas tienen `es_relevante`/`triaje` NULL — y lo mismo en la v1 de
# origen (`licitaciones_metadata.triaje`, también NULL en las 37), así que
# no es un hueco del backfill, es que ese campo rara vez se puebla en
# producción real. `estado IN ('ANALIZADO','NOTIFICADO')` es la señal que sí
# está siempre presente (llegar a ANALIZADO implica haber pasado el filtro
# de relevancia del pipeline) — sin este fallback, los KPIs 1 y 2 muestran
# un cero permanente y falso en vez del dato real que sí existe, solo que
# bajo otra columna. Las tres condiciones conviven a propósito: el día que
# `es_relevante`/`triaje` se empiecen a poblar de verdad, siguen sumando
# (nunca restan) sin tocar esta función.


def oportunidades_del_mes(conn: Any, cliente_id: str) -> dict:
    """KPI 1 — stat tile: valor del mes en curso, delta % vs. el mismo tramo
    del mes anterior, y una serie diaria (sparkline) del mes en curso.

    El delta compara "del 1 a hoy" contra "del 1 a este mismo instante del mes
    pasado", no contra el mes anterior completo: el 2 de octubre, comparar 2
    días contra 30 mostraba un "-100 %" que no era una caída real (verificado
    contra multicomputos_dev, 2026-10-02).

    "Relevante" = `_SQL_ES_RELEVANTE` (ver esa constante para el porqué del
    fallback a `estado`). Se agrupa por `proceso.fecha_pub_utc` (cuándo se
    publicó la licitación), no por `cliente_proceso.clasificado_en` (cuándo
    AMALIA la procesó): la pregunta de negocio es "cuántas oportunidades
    hubo este mes", no "cuánto trabajamos este mes".
    """
    sql_totales = f"""
        SELECT
            SUM(CASE WHEN p.fecha_pub_utc >= TRUNC(SYSDATE, 'MM') THEN 1 ELSE 0 END) AS mes_actual,
            SUM(CASE WHEN p.fecha_pub_utc >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -1)
                      AND p.fecha_pub_utc <= ADD_MONTHS(SYSDATE, -1) THEN 1 ELSE 0 END) AS mes_anterior
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  {_SQL_ES_RELEVANTE}
          AND  p.fecha_pub_utc >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -1)
    """
    sql_serie = f"""
        SELECT TRUNC(p.fecha_pub_utc) AS dia, COUNT(*) AS conteo
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  {_SQL_ES_RELEVANTE}
          AND  p.fecha_pub_utc >= TRUNC(SYSDATE, 'MM')
        GROUP  BY TRUNC(p.fecha_pub_utc)
        ORDER  BY dia
    """

    cursor = conn.cursor()
    try:
        cursor.execute(sql_totales, {"cliente_id": cliente_id})
        mes_actual, mes_anterior = cursor.fetchone()
        mes_actual = int(mes_actual or 0)
        mes_anterior = int(mes_anterior or 0)

        cursor.execute(sql_serie, {"cliente_id": cliente_id})
        serie = [int(conteo) for _dia, conteo in cursor.fetchall()]
    finally:
        cursor.close()

    # Sin mes anterior para comparar: delta_pct queda None (no 0 ni ∞), para
    # que la plantilla lo muestre como "sin dato previo", no como "0% de
    # cambio" — una mentira distinta sería peor que admitir que no hay base.
    delta_pct = None
    if mes_anterior > 0:
        delta_pct = round(100 * (mes_actual - mes_anterior) / mes_anterior, 1)

    logger.info(
        "oportunidades_del_mes: cliente_id=%s mes_actual=%s mes_anterior=%s delta_pct=%s",
        cliente_id, mes_actual, mes_anterior, delta_pct,
    )
    return {"valor": mes_actual, "delta_pct": delta_pct, "serie": serie}


def pct_relevancia_clasificador(conn: Any, cliente_id: str, dias: int = 30) -> dict:
    """KPI 2 — stat tile: % de los procesos de los últimos ``dias`` días que
    el CLASIFICADOR marcó como relevantes. Etiqueta obligatoria en la UI:
    "según el clasificador" — no es precisión real (eso es el KPI 9, que
    depende de ``feedback`` y hoy no tiene escritor activo). Mostrar los dos
    bajo el mismo label sería la fuga de significado que el plan señala
    explícitamente (docs/planes/portal-amalia.md §3, KPI 2).
    """
    sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN {_SQL_ES_RELEVANTE} THEN 1 ELSE 0 END) AS relevantes
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias})
        total, relevantes = cursor.fetchone()
        total = int(total or 0)
        relevantes = int(relevantes or 0)
    finally:
        cursor.close()

    valor = round(100 * relevantes / total, 1) if total > 0 else None
    logger.info(
        "pct_relevancia_clasificador: cliente_id=%s dias=%s total=%s relevantes=%s valor=%s",
        cliente_id, dias, total, relevantes, valor,
    )
    return {"valor": valor, "total": total, "relevantes": relevantes}


def instituciones_top(conn: Any, cliente_id: str, dias: int = 90, limite: int = 10) -> list[dict]:
    """KPI 3 — bar horizontal: instituciones que más publican procesos
    relevantes, agregadas desde `repo.stats_por_institucion_v2` (no
    reescribe la query — ver el docstring del módulo).

    `stats_por_institucion_v2` agrupa por (institución, categoría); acá se
    re-agrega por institución sola y se corta a `limite`, que es lo que
    necesita un bar chart de ranking (una barra por institución, no por
    combinación institución×categoría).
    """
    from src.memoria import repo

    filas = repo.stats_por_institucion_v2(conn, cliente_id, dias=dias)

    por_institucion: dict[str, int] = {}
    for fila in filas:
        inst = fila["institucion"] or "(sin institución)"
        por_institucion[inst] = por_institucion.get(inst, 0) + fila["conteo"]

    ranking = sorted(por_institucion.items(), key=lambda kv: kv[1], reverse=True)[:limite]
    resultado = [{"institucion": inst, "conteo": conteo} for inst, conteo in ranking]

    logger.info(
        "instituciones_top: cliente_id=%s dias=%s -> %d institución(es), top %d",
        cliente_id, dias, len(por_institucion), len(resultado),
    )
    return resultado


def embudo_pipeline(conn: Any, cliente_id: str, dias: int = 90) -> dict:
    """KPI 4 — funnel: conteo por etapa del "camino feliz"
    (DESCUBIERTO→NOTIFICADO) más un desglose aparte de las salidas
    (descartes/errores) — nunca mezclados en el mismo funnel: una salida no
    es "la etapa siguiente", es por lo que el proceso NO llegó a la
    siguiente etapa.

    :return: ``{"camino": [{"estado", "conteo"}, ...], "salidas": [...]}``,
        ambos en el orden canónico del enum (`_ETAPAS_CAMINO_FELIZ`/
        `_ETAPAS_SALIDA`), con 0 explícito para una etapa sin filas (un
        funnel con un hueco silencioso es peor que uno con un escalón en 0).
    """
    sql = """
        SELECT cp.estado, COUNT(*) AS conteo
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
        GROUP  BY cp.estado
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias})
        conteos = {estado: int(conteo) for estado, conteo in cursor.fetchall()}
    finally:
        cursor.close()

    camino = [{"estado": e, "conteo": conteos.get(e, 0)} for e in _ETAPAS_CAMINO_FELIZ]
    salidas = [{"estado": e, "conteo": conteos.get(e, 0)} for e in _ETAPAS_SALIDA if conteos.get(e, 0) > 0]

    logger.info("embudo_pipeline: cliente_id=%s dias=%s -> %s", cliente_id, dias, conteos)
    return {"camino": camino, "salidas": salidas}


def severidad_riesgos(conn: Any, cliente_id: str, dias: int = 90) -> list[dict]:
    """KPI 6 — bar horizontal ordinal: conteo de pliegos analizados por
    severidad máxima de riesgo detectada (alta/media/baja), orden
    descendente (lo más urgente primero).

    Nota del plan (hallazgo 3): con poco volumen esto puede salir sin
    variación (todo "alta") — es un dato real, no un bug de esta función;
    la plantilla lo muestra igual, no lo oculta.
    """
    sql = """
        SELECT a.severidad_max, COUNT(*) AS conteo
        FROM   analisis a
        JOIN   cliente_proceso cp ON cp.cliente_id = a.cliente_id AND cp.proceso_gid = a.proceso_gid
        JOIN   proceso p ON p.proceso_gid = a.proceso_gid
        WHERE  a.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
          AND  a.severidad_max IS NOT NULL
        GROUP  BY a.severidad_max
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias})
        conteos = {sev: int(conteo) for sev, conteo in cursor.fetchall()}
    finally:
        cursor.close()

    resultado = [
        {"severidad": sev, "conteo": conteos[sev]}
        for sev in _ORDEN_SEVERIDAD_DESC
        if sev in conteos
    ]
    # Severidades fuera del enum esperado (no debería pasar, pero un dato
    # inesperado se muestra, no se descarta en silencio).
    resultado += [
        {"severidad": sev, "conteo": n}
        for sev, n in conteos.items()
        if sev not in _ORDEN_SEVERIDAD_DESC
    ]

    logger.info("severidad_riesgos: cliente_id=%s dias=%s -> %s", cliente_id, dias, conteos)
    return resultado


__all__ = [
    "oportunidades_del_mes",
    "pct_relevancia_clasificador",
    "instituciones_top",
    "embudo_pipeline",
    "severidad_riesgos",
]


# Etapas del pipeline comercial que cuentan para la conversión (KPI 15) y el tiempo por etapa (KPI 16).
_ETAPAS_COMERCIALES = ("EVALUANDO", "OFERTADO", "GANADO")
_ETAPAS_CON_DURACION = ("EVALUANDO", "OFERTADO")


def conversion_comercial(conn: Any, cliente_id: str, dias: int = 90) -> dict:
    """KPI 15 — funnel: detectado → evaluando → ofertado → ganado, sobre `oportunidad_evento`.

    "Detectadas" son las oportunidades relevantes del rango (como el KPI 1); las demás etapas cuentan
    procesos DISTINTOS con un evento hacia ese estado en el rango. Las conversiones son None si el
    denominador es 0: sin eventos no hay tasa, no un 0 % falso."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT COUNT(*) FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid
             WHERE cp.cliente_id = :cliente_id AND p.fecha_pub_utc >= SYSDATE - :dias AND {_SQL_ES_RELEVANTE}
            """, {"cliente_id": cliente_id, "dias": dias})
        detectadas = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            SELECT estado_nuevo, COUNT(DISTINCT proceso_gid) FROM oportunidad_evento
             WHERE cliente_id = :cliente_id AND ts >= SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')
             GROUP BY estado_nuevo
            """, {"cliente_id": cliente_id, "dias": dias})
        por_estado = {fila[0]: int(fila[1]) for fila in cursor.fetchall()}
    finally:
        cursor.close()

    def pct(num: int, den: int):
        return round(100 * num / den, 1) if den > 0 else None

    return {
        "detectadas": detectadas,
        "etapas": [{"estado": e, "conteo": por_estado.get(e, 0)} for e in _ETAPAS_COMERCIALES],
        "perdido": por_estado.get("PERDIDO", 0),
        "descartado": por_estado.get("DESCARTADO", 0),
        "conv_ofertado_pct": pct(por_estado.get("OFERTADO", 0), detectadas),
        "conv_ganado_pct": pct(por_estado.get("GANADO", 0), por_estado.get("OFERTADO", 0)),
    }


def tiempo_por_etapa(conn: Any, cliente_id: str, dias: int = 90) -> list[dict]:
    """KPI 16 — bar horizontal: días promedio que una oportunidad pasa en EVALUANDO y en OFERTADO.

    Se mide entre un evento y el siguiente del mismo proceso; una etapa que todavía no terminó no
    cuenta (no hay duración aún). Etapas sin muestra no aparecen."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT estado_nuevo, AVG(duracion), COUNT(*) FROM (
                SELECT estado_nuevo,
                       CAST(LEAD(ts) OVER (PARTITION BY proceso_gid ORDER BY ts, evento_id) AS DATE)
                         - CAST(ts AS DATE) AS duracion
                  FROM oportunidad_evento
                 WHERE cliente_id = :cliente_id AND ts >= SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY'))
             WHERE duracion IS NOT NULL AND estado_nuevo IN ('EVALUANDO', 'OFERTADO')
             GROUP BY estado_nuevo
            """, {"cliente_id": cliente_id, "dias": dias})
        filas = {f[0]: (float(f[1]), int(f[2])) for f in cursor.fetchall()}
    finally:
        cursor.close()
    return [{"etapa": e, "dias_promedio": round(filas[e][0], 1), "muestra": filas[e][1]}
            for e in _ETAPAS_CON_DURACION if e in filas]
