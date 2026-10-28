"""src.analytics.tablero — datos de la vista de país rediseñada (Entrega E11).

Solo lectura y sin commit, con el patrón de ``src.analytics.pais``: una conexión
ya abierta, binds con nombre y ``cliente_id`` SIEMPRE filtrado. La agrupación
(«Para decidir», «No competible», «Baja prioridad») la decide
``src.notifier.agrupamiento``, la misma regla del correo; aquí solo se añade el
cuarto grupo del portal, «Descartado» (lo que AMALIA no consideró relevante).

PRIVACIDAD: ``feedback.destinatario`` no se selecciona nunca; de los votos el
portal solo ve conteos y códigos de motivo.

E14 — ``solo_marcas`` (keyword, de ``Alcance.solo_marcas``): con un conjunto no vacío cada lectura se
restringe EN SQL a las oportunidades de esas marcas (``relevancia.sql_marca`` sobre ``cp``, o un EXISTS
correlacionado a ``cliente_proceso`` donde la consulta no lo une), de modo que filas, conteos, ``total`` y
``truncado`` son todos de la marca. Lo que no se puede filtrar por marca (``revisados`` de
``tendencia_semanal``, que sale de ``ejecucion``) se devuelve como None bajo restricción.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.analytics.operativo import _SQL_AHORA
from src.analytics.pais import _SQL_ES_RELEVANTE
from src.drilldown.analysis import evaluar_competibilidad
from src.memoria.relevancia import (
    MARCAS_RESTRINGIBLES,
    SQL_DIA_DE_CORRIDA,
    SQL_MATCH,
    es_de_marca,
    sql_marca,
)
from src.notifier import agrupamiento as ag
from src.oportunidades.repo import _TOKEN, sql_existe_marca
from src.scraper.models import MOTIVOS_BLOQUEO, MatrizRiesgos, RiesgoItem

logger = logging.getLogger(__name__)

TOPE_FILAS = 300
TZ_DEFECTO = "America/El_Salvador"
GRUPO_DESCARTADO = "descartado"
GRUPO_PENDIENTE = "pendiente"  # no relevante pero sin decisión explícita (en curso o con error)
ESTADOS_ACTIVOS = ("NUEVO", "EVALUANDO", "OFERTADO")
_SQL_ACTIVOS = "('NUEVO', 'EVALUANDO', 'OFERTADO')"

MOTIVO_DESCARTE_REGEX = "filtro de texto"
MOTIVO_DESCARTE_TAMANO = "pliego demasiado grande"
MOTIVO_DESCARTE_TRIAJE = "triaje NO-GO"
MOTIVO_DESCARTE_PORTAFOLIO = "fuera del portafolio"

# Columnas de `procesos_del_rango`, en el orden del SELECT.
_COLUMNAS = (
    "proceso_gid", "numero_referencia", "titulo", "institucion", "url", "modalidad_raw", "tz_portal",
    "fecha_limite_utc", "monto_usd", "monto_estimado", "match_tecnico", "match_categoria",
    "factibilidad", "triaje", "estado_cp", "es_relevante", "categoria_detectada", "marcas", "razon",
    "matriz_riesgos", "severidad_max", "resumen", "op_estado", "responsable_id",
    "responsable_nombre", "op_actualizado_en", "token",
)

# Ventana del tablero: lo publicado en el rango, MÁS toda oportunidad con estado
# comercial activo (aunque se publicara antes) o que se movió dentro del rango.
_SQL_DESDE_RANGO = f"""
      FROM cliente_proceso cp
      JOIN proceso p ON p.proceso_gid = cp.proceso_gid
      LEFT JOIN analisis a ON a.cliente_id = cp.cliente_id AND a.proceso_gid = cp.proceso_gid
      LEFT JOIN oportunidad_estado e ON e.cliente_id = cp.cliente_id AND e.proceso_gid = cp.proceso_gid
      LEFT JOIN usuario u ON u.usuario_id = e.responsable_usuario_id
     WHERE cp.cliente_id = :cliente_id
       AND (p.fecha_pub_utc >= SYSDATE - :dias
            OR e.estado IN {_SQL_ACTIVOS}
            OR e.actualizado_en >= SYSDATE - :dias)
       -- Los no relevantes sin recuperar (E11.1) viven en `descartados_del_rango`: aquí harían que el tope
       -- de filas se cortara siempre y que «truncado» quedara permanentemente en verdadero.
       AND (e.cliente_id IS NOT NULL OR {_SQL_ES_RELEVANTE}
            OR CASE WHEN cp.es_relevante = 0 OR cp.estado LIKE 'DESCARTADO\\_%' ESCAPE '\\' THEN 1 ELSE 0 END = 0)
"""


def _sql_desde_rango(solo_marcas) -> str:
    # El AND de la marca va al final, FUERA de los paréntesis de los OR de `e.estado`/`e.actualizado_en`.
    return _SQL_DESDE_RANGO + f"       AND {sql_marca(solo_marcas, 'cp')}\n"


def _solo_de_marca(marcas: list[str], solo_marcas) -> list[str]:
    """Con restricción, la tarjeta no nombra co-marcas de otros fabricantes: solo las de `solo_marcas`."""
    if not solo_marcas:
        return marcas
    return [m for m in marcas if es_de_marca(m, solo_marcas)]


# ── utilidades puras ────────────────────────────────────────────────────────

def _json(valor: Any) -> Any:
    """Columna JSON → objeto Python. Acepta dict/list (JSON nativo), str/bytes o LOB.

    JSON malformado o de tipo inesperado → None, sin romper la vista."""
    if valor is None or isinstance(valor, (dict, list)):
        return valor
    try:
        if hasattr(valor, "read"):
            valor = valor.read()
        if isinstance(valor, (bytes, bytearray)):
            valor = valor.decode("utf-8")
        if isinstance(valor, str):
            return json.loads(valor) if valor.strip() else None
    except (ValueError, TypeError, UnicodeDecodeError):
        logger.warning("tablero: JSON malformado ignorado")
    return None


def _float(valor: Any) -> float | None:
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _marcas(valor: Any) -> list[str]:
    """`cliente_proceso.marcas` (lista de MarcaRecomendada serializada) → nombres de marca."""
    datos = _json(valor)
    if not isinstance(datos, list):
        return []
    marcas: list[str] = []
    for m in datos:
        nombre = m.get("marca") if isinstance(m, dict) else m if isinstance(m, str) else None
        if isinstance(nombre, str) and nombre.strip():
            marcas.append(nombre.strip())
    return marcas


def _motivos(valor: Any) -> list[str]:
    datos = _json(valor)
    if not isinstance(datos, list):
        return []
    return [m for m in datos if isinstance(m, str) and m]


def _a_matriz(datos: Any) -> MatrizRiesgos | None:
    """dict de `analisis.matriz_riesgos` → MatrizRiesgos; None si no tiene la forma esperada."""
    if not isinstance(datos, dict):
        return None
    try:
        items = [
            RiesgoItem(
                categoria=str(i.get("categoria") or ""),
                descripcion=str(i.get("descripcion") or ""),
                severidad=str(i.get("severidad") or ""),
                clausula_ref=str(i.get("clausula_ref") or ""),
            )
            for i in (datos.get("items") or [])
            if isinstance(i, dict)
        ]
        return MatrizRiesgos(items=items, resumen=str(datos.get("resumen") or ""))
    except (TypeError, AttributeError):
        return None


def _zona(tz_portal: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_portal or TZ_DEFECTO)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(TZ_DEFECTO)


def _utc(d: datetime | None) -> datetime | None:
    """Los TIMESTAMP de la BD vienen sin zona y están en UTC."""
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def es_relevante(fila: dict) -> bool:
    """Misma condición que `pais._SQL_ES_RELEVANTE`, en Python."""
    rel = fila.get("es_relevante")
    try:
        rel = int(rel) if rel is not None else None
    except (TypeError, ValueError):
        rel = None
    return rel == 1 or fila.get("triaje") == "GO" or fila.get("estado_cp") in ("ANALIZADO", "NOTIFICADO")


def es_descartado(fila: dict) -> bool:
    """Descartado por AMALIA: no relevante Y con una decisión explícita (`DESCARTADO_*` o `es_relevante = 0`).
    Lo que aún está en curso o falló (`DESCUBIERTO`, `APROBADO`, `ERROR_*`...) no es un descarte."""
    if es_relevante(fila):
        return False
    return str(fila.get("estado_cp") or "").startswith("DESCARTADO_") or fila.get("es_relevante") in (False, 0)


def _descartado_por_triaje(fila: dict) -> bool:
    return fila.get("estado_cp") == "DESCARTADO_TRIAJE" or fila.get("triaje") == "NO-GO"


def motivo_descarte(fila: dict) -> str:
    estado = fila.get("estado_cp")
    if estado == "DESCARTADO_REGEX":
        return MOTIVO_DESCARTE_REGEX
    if estado == "DESCARTADO_TAMANO":
        return MOTIVO_DESCARTE_TAMANO
    if _descartado_por_triaje(fila):
        return MOTIVO_DESCARTE_TRIAJE
    return MOTIVO_DESCARTE_PORTAFOLIO


# ── lecturas ────────────────────────────────────────────────────────────────

def procesos_del_rango(conn: Any, cliente_id: str, dias: int, limite: int = TOPE_FILAS, *,
                       solo_marcas: frozenset[str] = frozenset()) -> dict:
    """Todos los procesos del cliente del rango (relevantes y descartados) con su análisis y su
    estado comercial; más las oportunidades activas aunque se publicaran antes del rango.

    Ordena por cierre ascendente (sin fecha al final). Tope de `TOPE_FILAS`; `total` es el
    conteo real y `truncado` dice si se cortó."""
    limite = max(1, min(int(limite), TOPE_FILAS))
    desde = _sql_desde_rango(solo_marcas)
    sql_total = "SELECT COUNT(*)" + desde
    sql = f"""
    SELECT cp.proceso_gid, p.numero_referencia, p.titulo, p.institucion, p.url_portal, p.modalidad_raw,
           p.tz_portal, p.fecha_limite_utc, p.monto_usd, a.monto_estimado, cp.match_tecnico,
           cp.match_categoria, cp.factibilidad, cp.triaje, cp.estado, cp.es_relevante,
           cp.categoria_detectada, cp.marcas, cp.razon, a.matriz_riesgos, a.severidad_max, a.resumen,
           e.estado, e.responsable_usuario_id, u.nombre, e.actualizado_en, {_TOKEN}
    {desde}
     ORDER BY CASE WHEN e.cliente_id IS NOT NULL OR {_SQL_ES_RELEVANTE} THEN 0 ELSE 1 END,
              CASE WHEN p.fecha_limite_utc >= SYS_EXTRACT_UTC(SYSTIMESTAMP) THEN 0 ELSE 1 END,
              p.fecha_limite_utc ASC NULLS LAST, cp.proceso_gid
     FETCH FIRST :limite ROWS ONLY
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql_total, {"cliente_id": cliente_id, "dias": dias})
        total = int((cursor.fetchone() or (0,))[0] or 0)
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias, "limite": limite})
        crudas = cursor.fetchall()
    finally:
        cursor.close()

    filas = []
    for cruda in crudas:
        f = dict(zip(_COLUMNAS, cruda))
        for clave in ("monto_usd", "monto_estimado", "match_tecnico", "match_categoria", "factibilidad"):
            f[clave] = _float(f[clave])
        f["match"] = ag.match_de_valores(f["match_tecnico"], f["match_categoria"], f["factibilidad"])
        f["es_relevante"] = None if f["es_relevante"] is None else bool(int(f["es_relevante"]))
        f["marcas"] = _solo_de_marca(_marcas(f["marcas"]), solo_marcas)
        matriz = _json(f["matriz_riesgos"])
        f["matriz_riesgos"] = matriz if isinstance(matriz, dict) else None
        f["token"] = f["token"] or ""
        filas.append(f)

    total = max(total, len(filas))
    logger.info("procesos_del_rango: cliente_id=%s dias=%s filas=%s total=%s",
                cliente_id, dias, len(filas), total)
    return {"filas": filas, "total": total, "truncado": total > len(filas)}


def clasificar(filas: list[dict], marcas_cliente, hoy: datetime) -> dict:
    """PURA. Anota cada fila con grupo, competibilidad y urgencia, y las reparte.

    Devuelve `{"filas": [...anotadas], "grupos": {grupo: [...]}, "conteos": {grupo: n}}`.
    «descartado» = no relevante para AMALIA y SIN estado comercial; uno recuperado
    (con `oportunidad_estado`) se agrupa como cualquier relevante.
    `hoy` debe venir con zona: los días al cierre se cuentan en la zona del portal."""
    grupos: dict[str, list[dict]] = {
        ag.GRUPO_DECIDIR: [], ag.GRUPO_NO_COMPETIBLE: [], ag.GRUPO_BAJA: [], GRUPO_DESCARTADO: [],
        GRUPO_PENDIENTE: [],
    }
    anotadas = []
    for fila in filas:
        f = dict(fila)
        zona = _zona(f.get("tz_portal"))
        hoy_local = hoy.astimezone(zona)
        limite = _utc(f.get("fecha_limite_utc"))
        limite_local = limite.astimezone(zona) if limite is not None else None
        dias = ag.dias_hasta(limite_local, hoy_local)

        matriz = _a_matriz(f.get("matriz_riesgos"))
        try:
            competible, motivo, _evidencia = evaluar_competibilidad(
                matriz, forma_contratacion=f.get("modalidad_raw") or "", marcas_cliente=marcas_cliente)
        except Exception:  # noqa: BLE001 — una fila rara no tumba la vista
            logger.warning("tablero: competibilidad no evaluable para %s", f.get("proceso_gid"))
            competible, motivo = None, ""

        match = f.get("match")
        if match is None:
            match = ag.match_de_valores(f.get("match_tecnico"), f.get("match_categoria"), f.get("factibilidad"))

        if not es_relevante(f) and not f.get("op_estado"):
            # Descarte explícito de AMALIA, o «pendiente» (aún en curso o con error): no se muestra ni se recupera.
            grupo = GRUPO_DESCARTADO if es_descartado(f) else GRUPO_PENDIENTE
            f["motivo_descarte"] = motivo_descarte(f) if grupo == GRUPO_DESCARTADO else None
        else:
            grupo = ag.grupo_de_valores(match, competible, _descartado_por_triaje(f))
            f["motivo_descarte"] = None

        actualizado = _utc(f.get("op_actualizado_en"))
        f.update({
            "match": match,
            "grupo": grupo,
            "competible": competible,
            "motivo_bloqueo": motivo,
            "motivo_bloqueo_txt": MOTIVOS_BLOQUEO.get(motivo, motivo) if motivo else "",
            "recuperado": not es_relevante(f) and bool(f.get("op_estado")),
            "dias_restantes": dias,
            "vencida": dias is not None and dias < 0,
            "urgencia": ag.urgencia(dias),
            "cierre_texto": ag.texto_cierre(limite_local, dias),
            "fecha_limite_local": limite_local,
            # Nombres que lee el portal (`_contexto_pais`): el estado comercial, el cierre con zona y el
            # instante del último cambio; el monto sigue la regla de `pipeline_en_juego` (análisis primero).
            "estado": f.get("op_estado"),
            "fecha_limite": limite,
            "actualizado_en": actualizado,
            "monto_usd": f.get("monto_estimado") if f.get("monto_estimado") is not None else f.get("monto_usd"),
            "dias_en_estado": (hoy - actualizado).days if actualizado is not None else None,
        })
        anotadas.append(f)
        grupos[grupo].append(f)

    def por_cierre(o):
        d = o["dias_restantes"]
        return (2 if d is None else (1 if d < 0 else 0), d if d is not None else 0, -o["match"])

    grupos[ag.GRUPO_DECIDIR].sort(key=por_cierre)
    grupos[ag.GRUPO_NO_COMPETIBLE].sort(key=por_cierre)
    grupos[ag.GRUPO_BAJA].sort(key=lambda o: -o["match"])
    grupos[GRUPO_DESCARTADO].sort(key=lambda o: -o["match"])
    return {"filas": anotadas, "grupos": grupos, "conteos": {g: len(v) for g, v in grupos.items()}}


def votos_resumen(conn: Any, cliente_id: str, dias: int, *, solo_marcas: frozenset[str] = frozenset()) -> dict:
    """Totales de votos y motivos de «Irrelevante» del rango (por `votado_en`). Sin destinatarios."""
    marca = sql_existe_marca(solo_marcas, "f.cliente_id", "f.proceso_gid")
    sql_totales = f"""
        SELECT SUM(CASE WHEN f.voto = 'RELEVANTE' THEN 1 ELSE 0 END),
               SUM(CASE WHEN f.voto = 'IRRELEVANTE' THEN 1 ELSE 0 END)
          FROM feedback f
         WHERE f.cliente_id = :cliente_id AND f.votado_en >= SYSDATE - :dias
           AND {marca}
    """
    # `motivos` (JSON) guarda todos; las filas anteriores a esa columna solo traen `motivo`.
    sql_motivos = f"""
        SELECT jt.codigo, COUNT(*)
          FROM (SELECT f.motivos FROM feedback f
                 WHERE f.cliente_id = :cliente_id AND f.voto = 'IRRELEVANTE'
                   AND f.votado_en >= SYSDATE - :dias AND f.motivos IS NOT NULL
                   AND {marca}) r,
               JSON_TABLE(r.motivos, '$[*]' COLUMNS (codigo VARCHAR2(64) PATH '$')) jt
         WHERE jt.codigo IS NOT NULL
         GROUP BY jt.codigo
    """
    sql_motivo_suelto = f"""
        SELECT f.motivo, COUNT(*)
          FROM feedback f
         WHERE f.cliente_id = :cliente_id AND f.voto = 'IRRELEVANTE'
           AND f.votado_en >= SYSDATE - :dias AND f.motivos IS NULL AND f.motivo IS NOT NULL
           AND {marca}
         GROUP BY f.motivo
    """
    binds = {"cliente_id": cliente_id, "dias": dias}
    cursor = conn.cursor()
    try:
        cursor.execute(sql_totales, binds)
        fila = cursor.fetchone() or (0, 0)
        relevante, irrelevante = fila[0], fila[1]
        conteos: dict[str, int] = {}
        for sql in (sql_motivos, sql_motivo_suelto):
            cursor.execute(sql, binds)
            for codigo, n in cursor.fetchall():
                if codigo:
                    conteos[codigo] = conteos.get(codigo, 0) + int(n or 0)
    finally:
        cursor.close()
    motivos = sorted(conteos.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"relevante": int(relevante or 0), "irrelevante": int(irrelevante or 0), "motivos": motivos}


def votos_por_proceso(conn: Any, cliente_id: str, proceso_gids, *,
                      solo_marcas: frozenset[str] = frozenset()) -> dict[str, dict]:
    """Votos agregados por proceso (hasta `TOPE_FILAS` gids). Sin destinatarios."""
    gids = list(dict.fromkeys(g for g in (proceso_gids or []) if g))[:TOPE_FILAS]
    if not gids:
        return {}
    marcadores = ", ".join(f":g{i}" for i in range(len(gids)))
    binds: dict[str, Any] = {f"g{i}": g for i, g in enumerate(gids)}
    binds["cliente_id"] = cliente_id
    sql = f"""
        SELECT f.proceso_gid, f.voto, f.motivos, f.motivo
          FROM feedback f
         WHERE f.cliente_id = :cliente_id AND f.proceso_gid IN ({marcadores})
           AND {sql_existe_marca(solo_marcas, "f.cliente_id", "f.proceso_gid")}
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        crudas = cursor.fetchall()
    finally:
        cursor.close()
    resultado: dict[str, dict] = {}
    for gid, voto, motivos, motivo in crudas:
        r = resultado.setdefault(gid, {"relevante": 0, "irrelevante": 0, "motivos": []})
        if voto == "RELEVANTE":
            r["relevante"] += 1
        elif voto == "IRRELEVANTE":
            r["irrelevante"] += 1
            codigos = _motivos(motivos) or ([motivo] if motivo else [])
            r["motivos"].extend(c for c in codigos if c not in r["motivos"])
    return resultado


def tendencia_semanal(conn: Any, cliente_id: str, semanas: int = 12, *,
                      solo_marcas: frozenset[str] = frozenset()) -> list[dict]:
    """Revisados vs. relevantes por semana ISO, las últimas `semanas` (incluida la actual), con ceros.

    `revisados` sale de `ejecucion` (no de lo persistido): por día (hora de El Salvador) el
    `MAX(procesos_revisados)` de las corridas OK —las repeticiones no suman—, sumado por semana.
    `relevantes` = filas de `cliente_proceso` con `SQL_ES_RELEVANTE`, asignadas a su día de corrida
    (`SQL_DIA_DE_CORRIDA`) y de ahí a la semana.

    Con `solo_marcas`, `relevantes` cuenta solo los de la marca y `revisados` (global: `ejecucion` no sabe de
    marcas) se devuelve None, para que el portal lo oculte y nunca lo reste ni lo use de denominador."""
    sql = f"""
        WITH semanas AS (
            SELECT TRUNC({_SQL_AHORA}, 'IW') - 7 * (LEVEL - 1) AS semana
              FROM dual CONNECT BY LEVEL <= :semanas
        ), corridas AS (
            SELECT TRUNC(e.run_timestamp) AS dia, MAX(e.procesos_revisados) AS revisados
              FROM ejecucion e
             WHERE e.success = 1
               AND e.run_timestamp >= TRUNC({_SQL_AHORA}, 'IW') - 7 * (:semanas - 1)
             GROUP BY TRUNC(e.run_timestamp)
        ), revisados AS (
            SELECT TRUNC(c.dia, 'IW') AS semana, SUM(c.revisados) AS revisados
              FROM corridas c
             GROUP BY TRUNC(c.dia, 'IW')
        ), relevantes AS (
            SELECT TRUNC({SQL_DIA_DE_CORRIDA}, 'IW') AS semana, COUNT(*) AS relevantes
              FROM cliente_proceso cp
              JOIN proceso p ON p.proceso_gid = cp.proceso_gid
             WHERE cp.cliente_id = :cliente_id
               AND {_SQL_ES_RELEVANTE}
               AND {sql_marca(solo_marcas, 'cp')}
               AND p.fecha_pub_utc >= TRUNC({_SQL_AHORA}, 'IW') - 7 * (:semanas - 1) - 1
             GROUP BY TRUNC({SQL_DIA_DE_CORRIDA}, 'IW')
        )
        SELECT s.semana, NVL(r.revisados, 0), NVL(l.relevantes, 0)
          FROM semanas s
          LEFT JOIN revisados r ON r.semana = s.semana
          LEFT JOIN relevantes l ON l.semana = s.semana
         ORDER BY s.semana
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "semanas": int(semanas)})
        crudas = cursor.fetchall()
    finally:
        cursor.close()
    return [
        {"semana": s.date() if isinstance(s, datetime) else s,
         "revisados": None if solo_marcas else int(rev or 0),
         "relevantes": int(rel or 0)}
        for s, rev, rel in crudas
        if isinstance(s, (date, datetime))
    ]


# Descartado persistido: no relevante con decisión explícita (`es_relevante = 0` o `DESCARTADO_*`). La negación
# va por CASE porque `NOT (... OR cp.es_relevante = 1 ...)` con NULLs da UNKNOWN y perdería filas.
_SQL_DESCARTADO = f"""(cp.es_relevante = 0 OR cp.estado LIKE 'DESCARTADO\\_%' ESCAPE '\\')
       AND CASE WHEN {_SQL_ES_RELEVANTE} THEN 1 ELSE 0 END = 0"""

_SQL_DESDE_DESCARTADOS = f"""
      FROM cliente_proceso cp
      JOIN proceso p ON p.proceso_gid = cp.proceso_gid
      LEFT JOIN oportunidad_estado e ON e.cliente_id = cp.cliente_id AND e.proceso_gid = cp.proceso_gid
     WHERE cp.cliente_id = :cliente_id
       AND p.fecha_pub_utc >= SYSDATE - :dias
       AND e.cliente_id IS NULL
       AND {_SQL_DESCARTADO}
"""


def _sql_desde_descartados(solo_marcas) -> str:
    # Fuera de `_SQL_DESCARTADO` (que niega la relevancia con CASE): ahí la marca invertiría el sentido.
    return _SQL_DESDE_DESCARTADOS + f"       AND {sql_marca(solo_marcas, 'cp')}\n"


def descartados_del_rango(conn: Any, cliente_id: str, dias: int, limite: int = 10, *,
                          solo_marcas: frozenset[str] = frozenset()) -> dict:
    """Procesos que AMALIA descartó en el rango (por `fecha_pub_utc`) y que nadie recuperó (sin
    `oportunidad_estado`), de mayor a menor match: los más cercanos a entrar van primero.

    Devuelve `{"filas": [...], "total": int, "desde": datetime|None}`. `total` es el conteo real
    (sin el tope); `desde` = `MIN(clasificado_en)` de las filas `es_relevante = 0` del cliente, es decir,
    desde cuándo se guardan los no relevantes (None: aún no se guarda ninguno)."""
    limite = max(1, min(int(limite), TOPE_FILAS))
    desde_sql = _sql_desde_descartados(solo_marcas)
    sql_total = "SELECT COUNT(*)" + desde_sql
    sql_desde = f"""
        SELECT MIN(cp.clasificado_en)
          FROM cliente_proceso cp
         WHERE cp.cliente_id = :cliente_id AND cp.es_relevante = 0
           AND {sql_marca(solo_marcas, 'cp')}
    """
    sql = f"""
    SELECT cp.proceso_gid, p.numero_referencia, p.titulo, p.institucion, cp.match_tecnico,
           cp.match_categoria, cp.factibilidad, cp.triaje, cp.estado, cp.es_relevante, {_TOKEN}
    {desde_sql}
     ORDER BY {SQL_MATCH} DESC, cp.proceso_gid
     FETCH FIRST :limite ROWS ONLY
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql_total, {"cliente_id": cliente_id, "dias": dias})
        total = int((cursor.fetchone() or (0,))[0] or 0)
        cursor.execute(sql_desde, {"cliente_id": cliente_id})
        desde = (cursor.fetchone() or (None,))[0]
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias, "limite": limite})
        crudas = cursor.fetchall()
    finally:
        cursor.close()

    filas = []
    for gid, ref, titulo, inst, tec, cat, fac, triaje, estado, rel, token in crudas:
        f = {"proceso_gid": gid, "numero_referencia": ref, "titulo": titulo, "institucion": inst,
             "triaje": triaje, "estado_cp": estado, "es_relevante": rel}
        f["match"] = ag.match_de_valores(_float(tec), _float(cat), _float(fac))
        f["motivo_descarte"] = motivo_descarte(f)
        f["token"] = token or ""
        filas.append({k: f[k] for k in ("proceso_gid", "numero_referencia", "titulo", "institucion", "match",
                                        "motivo_descarte", "token")})
    total = max(total, len(filas))
    logger.info("descartados_del_rango: cliente_id=%s dias=%s filas=%s total=%s desde=%s",
                cliente_id, dias, len(filas), total, desde)
    return {"filas": filas, "total": total, "desde": desde}


def marcas_top(conn: Any, cliente_id: str, dias: int, limite: int = 5, *,
               solo_marcas: frozenset[str] = frozenset()) -> list[dict]:
    """Marcas más recomendadas en los relevantes del rango (procesos distintos por marca).

    Con `solo_marcas`: solo procesos de esa marca, y de ellos solo se cuentan esas marcas (las co-marcas de otros
    fabricantes no aparecen)."""
    solo_nombres = ""
    if solo_marcas:
        nombres = " OR ".join(f"REGEXP_LIKE(jt.marca, '^{MARCAS_RESTRINGIBLES[m]}([^[:alnum:]_]|$)', 'i')" for m in sorted(solo_marcas))
        solo_nombres = f"AND ({nombres})"
    sql = f"""
        SELECT jt.marca, COUNT(DISTINCT r.proceso_gid) AS conteo
          FROM (SELECT cp.proceso_gid, cp.marcas
                  FROM cliente_proceso cp
                  JOIN proceso p ON p.proceso_gid = cp.proceso_gid
                 WHERE cp.cliente_id = :cliente_id
                   AND p.fecha_pub_utc >= SYSDATE - :dias
                   AND {_SQL_ES_RELEVANTE}
                   AND {sql_marca(solo_marcas, 'cp')}
                   AND cp.marcas IS NOT NULL) r,
               JSON_TABLE(r.marcas, '$[*]' COLUMNS (marca VARCHAR2(200) PATH '$.marca')) jt
         WHERE jt.marca IS NOT NULL {solo_nombres}
         GROUP BY jt.marca
         ORDER BY conteo DESC, jt.marca
         FETCH FIRST :limite ROWS ONLY
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias, "limite": int(limite)})
        crudas = cursor.fetchall()
    finally:
        cursor.close()
    return [{"marca": m, "conteo": int(n or 0)} for m, n in crudas if m]


def pipeline_en_juego(conn: Any, cliente_id: str, *, solo_marcas: frozenset[str] = frozenset()) -> dict:
    """Monto de las oportunidades activas (NUEVO/EVALUANDO/OFERTADO). `monto_usd` es None si
    ninguna tiene monto: nunca se inventa un 0."""
    sql = f"""
        SELECT COUNT(*),
               COUNT(COALESCE(a.monto_estimado, p.monto_usd)),
               SUM(COALESCE(a.monto_estimado, p.monto_usd))
          FROM oportunidad_estado e
          JOIN proceso p ON p.proceso_gid = e.proceso_gid
          LEFT JOIN analisis a ON a.cliente_id = e.cliente_id AND a.proceso_gid = e.proceso_gid
         WHERE e.cliente_id = :cliente_id AND e.estado IN {_SQL_ACTIVOS}
           AND {sql_existe_marca(solo_marcas, "e.cliente_id", "e.proceso_gid")}
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id})
        activas, con_monto, suma = cursor.fetchone() or (0, 0, None)
    finally:
        cursor.close()
    con_monto = int(con_monto or 0)
    return {
        "monto_usd": _float(suma) if con_monto > 0 else None,
        "activas": int(activas or 0),
        "con_monto": con_monto,
    }


def estancadas(conn: Any, cliente_id: str, dias_sin_mover: int = 7, *,
               solo_marcas: frozenset[str] = frozenset()) -> int:
    """Oportunidades en EVALUANDO sin moverse hace más de `dias_sin_mover` días."""
    sql = f"""
        SELECT COUNT(*)
          FROM oportunidad_estado e
         WHERE e.cliente_id = :cliente_id AND e.estado = 'EVALUANDO'
           AND e.actualizado_en < SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')
           AND {sql_existe_marca(solo_marcas, "e.cliente_id", "e.proceso_gid")}
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": int(dias_sin_mover)})
        fila = cursor.fetchone()
    finally:
        cursor.close()
    return int((fila or (0,))[0] or 0)
