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

from src.analytics.operativo import _SQL_AHORA
from src.memoria.relevancia import SQL_DIA_DE_CORRIDA, SQL_ES_APTO, SQL_ES_RELEVANTE, sql_marca

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


# Definición única en `src.memoria.relevancia` (ver allí por qué «relevante» combina tres señales).
_SQL_ES_RELEVANTE = SQL_ES_RELEVANTE

# E14 · `solo_marcas` (del `Alcance`): vacío = sin restricción. Cada lectura añade `AND {sql_marca(...)}` FUERA de
# los paréntesis del OR de «relevante» (nunca dentro de `SQL_ES_RELEVANTE`, ver `src.memoria.relevancia.sql_marca`).
# Es keyword-only con default `frozenset()` solo para no romper a los llamadores actuales del portal.
_SIN_RESTRICCION: frozenset[str] = frozenset()


def _exists_marca_evento(solo_marcas: frozenset[str], alias: str = "oe") -> str:
    """`AND EXISTS (...)` que ata un evento de `oportunidad_evento` a su `cliente_proceso` filtrado por marca.

    Vacío sin restricción: el comportamiento de siempre queda idéntico (ni un EXISTS de más)."""
    if not solo_marcas:
        return ""
    return (f"AND EXISTS (SELECT 1 FROM cliente_proceso cp WHERE cp.cliente_id = {alias}.cliente_id "
            f"AND cp.proceso_gid = {alias}.proceso_gid AND {sql_marca(solo_marcas)})")


def oportunidades_del_mes(conn: Any, cliente_id: str, *,
                          solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> dict:
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
          AND  {sql_marca(solo_marcas)}
          AND  p.fecha_pub_utc >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -1)
    """
    sql_serie = f"""
        SELECT TRUNC(p.fecha_pub_utc) AS dia, COUNT(*) AS conteo
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  {_SQL_ES_RELEVANTE}
          AND  {sql_marca(solo_marcas)}
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


def pct_relevancia_clasificador(conn: Any, cliente_id: str, dias: int = 30, *,
                                solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> dict:
    """KPI 2 — stat tile: % de los procesos de los últimos ``dias`` días que
    el CLASIFICADOR marcó como relevantes. Etiqueta obligatoria en la UI:
    "según el clasificador" — no es precisión real (eso es el KPI 9, que
    depende de ``feedback`` y hoy no tiene escritor activo). Mostrar los dos
    bajo el mismo label sería la fuga de significado que el plan señala
    explícitamente (docs/planes/portal-amalia.md §3, KPI 2).

    OJO con el denominador: es lo PERSISTIDO en ``cliente_proceso``, no lo
    revisado. Hasta E11.1 el pipeline solo guardaba los relevantes, así que el
    cociente rondaba el 100 % (99,6 % medido en la ADB). La vista de país usa
    ``relevancia_estricta``, cuyo denominador sale de ``ejecucion``; esta
    función se conserva mientras otra vista la consuma.
    """
    sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN {_SQL_ES_RELEVANTE} THEN 1 ELSE 0 END) AS relevantes
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
          AND  {sql_marca(solo_marcas)}
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


def relevancia_estricta(conn: Any, cliente_id: str, dias: int = 30, *,
                        solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> dict:
    """KPI 2 (E11.1) — relevancia estricta: aptos / revisados, con relevantes / revisados de apoyo.

    - ``revisados``: por cada día (hora de El Salvador) de los últimos ``dias``, el
      ``MAX(procesos_revisados)`` de las corridas OK de ``ejecucion`` —las repeticiones del
      mismo día no suman—, sumado en el rango. Los días cuyo máximo es 0 no cuentan.
    - ``relevantes``/``aptos``: filas de ``cliente_proceso`` del cliente según
      ``SQL_ES_RELEVANTE``/``SQL_ES_APTO`` (``src.memoria.relevancia``), asignadas a su día de
      corrida con ``SQL_DIA_DE_CORRIDA``, y SOLO de los días que aportan al denominador:
      numerador y denominador se suman sobre los mismos días.
    - Por día se acota ``aptos ≤ relevantes ≤ revisados``: un día con solo una repetición
      mínima (1 revisado) no infla el cociente.

    ``pct_*`` es None si ``revisados`` = 0 (sin dato, no un 0 % falso); 1 decimal, ≤ 100.

    E14 · con ``solo_marcas`` el filtro va SOLO en la CTE ``clasificados``: ``revisados`` sale de ``ejecucion``,
    que es global (no sabe de marcas). Por eso, con restricción, ``revisados``, ``pct_aptos`` y
    ``pct_relevantes`` vuelven None (un % sobre el universo global, o ``revisados − relevantes``, revelaría el
    tamaño de lo oculto) y ``filtrado_por_marca`` es True: el portal muestra solo los conteos.
    """
    sql = f"""
        WITH corridas AS (
            SELECT TRUNC(e.run_timestamp) AS dia, MAX(e.procesos_revisados) AS revisados
              FROM ejecucion e
             WHERE e.success = 1
               AND e.run_timestamp >= TRUNC({_SQL_AHORA}) - :dias + 1
             GROUP BY TRUNC(e.run_timestamp)
            HAVING MAX(e.procesos_revisados) > 0
        ), clasificados AS (
            SELECT {SQL_DIA_DE_CORRIDA} AS dia,
                   SUM(CASE WHEN {SQL_ES_RELEVANTE} THEN 1 ELSE 0 END) AS relevantes,
                   SUM(CASE WHEN {SQL_ES_APTO} THEN 1 ELSE 0 END) AS aptos
              FROM cliente_proceso cp
              JOIN proceso p ON p.proceso_gid = cp.proceso_gid
             WHERE cp.cliente_id = :cliente_id
               AND p.fecha_pub_utc >= TRUNC({_SQL_AHORA}) - :dias
               AND {sql_marca(solo_marcas)}
             GROUP BY {SQL_DIA_DE_CORRIDA}
        )
        SELECT NVL(SUM(c.revisados), 0) AS revisados,
               NVL(SUM(LEAST(NVL(k.relevantes, 0), c.revisados)), 0) AS relevantes,
               NVL(SUM(LEAST(NVL(k.aptos, 0), NVL(k.relevantes, 0), c.revisados)), 0) AS aptos
          FROM corridas c
          LEFT JOIN clasificados k ON k.dia = c.dia
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": int(dias)})
        fila = cursor.fetchone() or (0, 0, 0)
    finally:
        cursor.close()

    revisados = max(int(fila[0] or 0), 0)
    relevantes = min(max(int(fila[1] or 0), 0), revisados)
    aptos = min(max(int(fila[2] or 0), 0), relevantes)

    def pct(num: int) -> float | None:
        return min(round(100 * num / revisados, 1), 100.0) if revisados > 0 else None

    filtrado = bool(solo_marcas)
    resultado = {"revisados": None if filtrado else revisados, "relevantes": relevantes, "aptos": aptos,
                 "pct_aptos": None if filtrado else pct(aptos),
                 "pct_relevantes": None if filtrado else pct(relevantes),
                 "filtrado_por_marca": filtrado}
    logger.info("relevancia_estricta: cliente_id=%s dias=%s -> %s", cliente_id, dias, resultado)
    return resultado


def instituciones_top(conn: Any, cliente_id: str, dias: int = 90, limite: int = 10, *,
                      solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> list[dict]:
    """KPI 3 — bar horizontal: instituciones que más publican procesos
    relevantes, agregadas desde `repo.stats_por_institucion_v2` (no
    reescribe la query — ver el docstring del módulo).

    `stats_por_institucion_v2` agrupa por (institución, categoría); acá se
    re-agrega por institución sola y se corta a `limite`, que es lo que
    necesita un bar chart de ranking (una barra por institución, no por
    combinación institución×categoría).
    """
    from src.memoria import repo

    filas = repo.stats_por_institucion_v2(conn, cliente_id, dias=dias, solo_marcas=solo_marcas)

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


def embudo_pipeline(conn: Any, cliente_id: str, dias: int = 90, *,
                    solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> dict:
    """KPI 4 — funnel: conteo por etapa del "camino feliz"
    (DESCUBIERTO→NOTIFICADO) más un desglose aparte de las salidas
    (descartes/errores) — nunca mezclados en el mismo funnel: una salida no
    es "la etapa siguiente", es por lo que el proceso NO llegó a la
    siguiente etapa.

    :return: ``{"camino": [{"estado", "conteo"}, ...], "salidas": [...]}``,
        ambos en el orden canónico del enum (`_ETAPAS_CAMINO_FELIZ`/
        `_ETAPAS_SALIDA`), con 0 explícito para una etapa sin filas (un
        funnel con un hueco silencioso es peor que uno con un escalón en 0).

    Solo cuenta relevantes (`_SQL_ES_RELEVANTE`): desde E11.1 el pipeline guarda también
    los NO relevantes (`DESCUBIERTO`, `es_relevante = 0`), que inflarían «descubierto».
    """
    sql = f"""
        SELECT cp.estado, COUNT(*) AS conteo
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
          AND  {_SQL_ES_RELEVANTE}
          AND  {sql_marca(solo_marcas)}
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


def severidad_riesgos(conn: Any, cliente_id: str, dias: int = 90, *,
                      solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> list[dict]:
    """KPI 6 — bar horizontal ordinal: conteo de pliegos analizados por
    severidad máxima de riesgo detectada (alta/media/baja), orden
    descendente (lo más urgente primero).

    Nota del plan (hallazgo 3): con poco volumen esto puede salir sin
    variación (todo "alta") — es un dato real, no un bug de esta función;
    la plantilla lo muestra igual, no lo oculta.

    Filtra por `_SQL_ES_RELEVANTE` (defensivo: un no relevante no se analiza).
    """
    sql = f"""
        SELECT a.severidad_max, COUNT(*) AS conteo
        FROM   analisis a
        JOIN   cliente_proceso cp ON cp.cliente_id = a.cliente_id AND cp.proceso_gid = a.proceso_gid
        JOIN   proceso p ON p.proceso_gid = a.proceso_gid
        WHERE  a.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
          AND  a.severidad_max IS NOT NULL
          AND  {_SQL_ES_RELEVANTE}
          AND  {sql_marca(solo_marcas)}
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
    "relevancia_estricta",
    "instituciones_top",
    "embudo_pipeline",
    "severidad_riesgos",
]


# Etapas del pipeline comercial que cuentan para la conversión (KPI 15) y el tiempo por etapa (KPI 16).
_ETAPAS_COMERCIALES = ("EVALUANDO", "OFERTADO", "GANADO")
_ETAPAS_CON_DURACION = ("EVALUANDO", "OFERTADO")


def conversion_comercial(conn: Any, cliente_id: str, dias: int = 90, *,
                         solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> dict:
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
               AND {sql_marca(solo_marcas)}
            """, {"cliente_id": cliente_id, "dias": dias})
        detectadas = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            f"""
            SELECT oe.estado_nuevo, COUNT(DISTINCT oe.proceso_gid) FROM oportunidad_evento oe
             WHERE oe.cliente_id = :cliente_id AND oe.ts >= SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')
               AND (oe.estado_anterior IS NULL OR oe.estado_anterior <> oe.estado_nuevo)  -- sin asignaciones
               {_exists_marca_evento(solo_marcas)}
             GROUP BY oe.estado_nuevo
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


def tiempo_por_etapa(conn: Any, cliente_id: str, dias: int = 90, *,
                     solo_marcas: frozenset[str] = _SIN_RESTRICCION) -> list[dict]:
    """KPI 16 — bar horizontal: días promedio que una oportunidad pasa en EVALUANDO y en OFERTADO.

    Se mide entre un evento y el siguiente del mismo proceso; una etapa que todavía no terminó no
    cuenta (no hay duración aún). Etapas sin muestra no aparecen.

    E14 · el filtro por marca va DENTRO de la subconsulta, antes del ``LEAD``: la ventana debe ver solo los
    eventos de procesos visibles, nunca calcular duraciones con eventos de procesos ocultos."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT estado_nuevo, AVG(duracion), COUNT(*) FROM (
                SELECT oe.estado_nuevo,
                       CAST(LEAD(oe.ts) OVER (PARTITION BY oe.proceso_gid ORDER BY oe.ts, oe.evento_id) AS DATE)
                         - CAST(oe.ts AS DATE) AS duracion
                  FROM oportunidad_evento oe
                 WHERE oe.cliente_id = :cliente_id AND oe.ts >= SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')
                   AND (oe.estado_anterior IS NULL OR oe.estado_anterior <> oe.estado_nuevo)
                   {_exists_marca_evento(solo_marcas)})
             WHERE duracion IS NOT NULL AND estado_nuevo IN ('EVALUANDO', 'OFERTADO')
             GROUP BY estado_nuevo
            """, {"cliente_id": cliente_id, "dias": dias})
        filas = {f[0]: (float(f[1]), int(f[2])) for f in cursor.fetchall()}
    finally:
        cursor.close()
    return [{"etapa": e, "dias_promedio": round(filas[e][0], 1), "muestra": filas[e][1]}
            for e in _ETAPAS_CON_DURACION if e in filas]
