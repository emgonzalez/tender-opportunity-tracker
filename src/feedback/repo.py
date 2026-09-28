"""src.feedback.repo — acceso a `licitaciones_reportadas` y `licitaciones_feedback`.

Mismas reglas que `src.memoria.repo`: reciben una conexión ya abierta y no
gestionan su ciclo de vida (eso es de `src.memoria.db.connection`), y todo valor
viaja como bind variable. El nombre de tabla y de columna son literales de este
archivo, nunca de la entrada.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _recortar(valor: Any, largo: int) -> str | None:
    if valor is None:
        return None
    return str(valor)[:largo]


def upsert_reportada(conn: Any, cliente_id: str, oportunidad: Any) -> None:
    """Registra la foto del proceso al enviarlo. Si ya se había enviado, cuenta el reenvío.

    No hay deduplicación entre corridas (RB-004): el mismo proceso puede volver a
    salir en el reporte de mañana. La foto se refresca y `veces_enviada` sube,
    pero `primer_envio_en` no se toca. El voto ya emitido tampoco: el token es
    determinista, así que sigue siendo la misma fila.
    """
    sql = """
        MERGE INTO licitaciones_reportadas tgt
        USING (SELECT :proceso_id AS proceso_id, :cliente_id AS cliente_id FROM dual) src
        ON (tgt.proceso_id = src.proceso_id AND tgt.cliente_id = src.cliente_id)
        WHEN MATCHED THEN UPDATE SET
            tgt.institucion     = :institucion,
            tgt.titulo          = :titulo,
            tgt.categoria       = :categoria,
            tgt.match_tecnico   = :match_tecnico,
            tgt.match_categoria = :match_categoria,
            tgt.factibilidad    = :factibilidad,
            tgt.tags            = JSON(:tags),
            tgt.url_comprasal   = :url_comprasal,
            tgt.ultimo_envio_en = SYSTIMESTAMP,
            tgt.veces_enviada   = tgt.veces_enviada + 1
        WHEN NOT MATCHED THEN INSERT (
            proceso_id, cliente_id, institucion, titulo, categoria,
            match_tecnico, match_categoria, factibilidad, tags, url_comprasal
        ) VALUES (
            :proceso_id, :cliente_id, :institucion, :titulo, :categoria,
            :match_tecnico, :match_categoria, :factibilidad, JSON(:tags), :url_comprasal
        )
    """
    binds = {
        "proceso_id": oportunidad.numero_referencia,
        "cliente_id": cliente_id,
        "institucion": _recortar(oportunidad.institucion, 400),
        "titulo": _recortar(oportunidad.descripcion, 1000),
        "categoria": _recortar(getattr(oportunidad, "categoria_detectada", ""), 120),
        "match_tecnico": getattr(oportunidad, "match_tecnico", None),
        "match_categoria": getattr(oportunidad, "match_categoria", None),
        "factibilidad": getattr(oportunidad, "factibilidad", None),
        "tags": json.dumps(list(getattr(oportunidad, "tags", []) or []), ensure_ascii=False),
        "url_comprasal": _recortar(oportunidad.url_directa, 1000),
    }
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def get_reportada(conn: Any, proceso_id: str, cliente_id: str) -> dict | None:
    """La foto del proceso enviado, o `None` si no se registró."""
    sql = """
        SELECT institucion, titulo, categoria, url_comprasal
          FROM licitaciones_reportadas
         WHERE proceso_id = :proceso_id AND cliente_id = :cliente_id
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"proceso_id": proceso_id, "cliente_id": cliente_id})
        fila = cursor.fetchone()
    finally:
        cursor.close()
    if not fila:
        return None
    return {"institucion": fila[0], "titulo": fila[1], "categoria": fila[2], "url_comprasal": fila[3]}


def upsert_voto(
    conn: Any,
    *,
    proceso_id: str,
    cliente_id: str,
    destinatario_id: str,
    destinatario_email: str | None,
    voto: str,
    motivos: list[str],
    comentario: str,
) -> None:
    """Guarda el voto de un destinatario. Cambiar de opinión sobrescribe la fila.

    Un voto RELEVANTE limpia motivos y comentario: no aplican, y dejar los de un
    voto anterior IRRELEVANTE mezclaría dos criterios distintos en una fila.
    """
    if voto not in ("RELEVANTE", "IRRELEVANTE"):
        raise ValueError(f"voto inválido: {voto!r}")
    if voto == "RELEVANTE":
        motivos, comentario = [], ""

    sql = """
        MERGE INTO licitaciones_feedback tgt
        USING (SELECT :proceso_id AS proceso_id, :cliente_id AS cliente_id,
                      :destinatario_id AS destinatario_id FROM dual) src
        ON (tgt.proceso_id = src.proceso_id AND tgt.cliente_id = src.cliente_id
            AND tgt.destinatario_id = src.destinatario_id)
        WHEN MATCHED THEN UPDATE SET
            tgt.destinatario_email = :destinatario_email,
            tgt.voto               = :voto,
            tgt.motivos            = JSON(:motivos),
            tgt.comentario         = :comentario,
            tgt.actualizado_en     = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            proceso_id, cliente_id, destinatario_id, destinatario_email,
            voto, motivos, comentario
        ) VALUES (
            :proceso_id, :cliente_id, :destinatario_id, :destinatario_email,
            :voto, JSON(:motivos), :comentario
        )
    """
    binds = {
        "proceso_id": proceso_id,
        "cliente_id": cliente_id,
        "destinatario_id": destinatario_id,
        "destinatario_email": _recortar(destinatario_email, 320),
        "voto": voto,
        "motivos": json.dumps(motivos, ensure_ascii=False),
        "comentario": comentario or None,
    }
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


# ── Espejo del esquema v2 multi-tenant/multi-país (db/schema_v2.sql) ────────
#
# Fase "Doble escritura" de la migración (plan del portal AMALIA §3.3). Se
# llaman ADEMÁS de upsert_reportada/upsert_voto, desde src.feedback.service,
# cada una en su propio try/except — un fallo acá nunca debe poder revertir
# ni bloquear la escritura del esquema actual.
#
# `reportada`/`feedback` v2 son deliberadamente MÁS DELGADAS que sus
# equivalentes v1: institución/título/categoría/scores ya viven en
# `proceso`/`cliente_proceso` (v2 no los duplica, v1 sí — v1 es
# autocontenida porque nació sin esas tablas). `feedback` v2 también
# simplifica `motivos` (JSON, varios) a `motivo` (un solo VARCHAR2(64)): es
# una limitación real y documentada del esquema v2 tal como está hoy, no un
# bug — se ensancha si en algún momento hace falta más de un motivo por
# voto.

# Duplicada de src.memoria.repo.PORTAL_ID_DEFAULT (mismo valor): este módulo
# no depende de memoria.repo por una sola constante.
_PORTAL_ID_DEFAULT_V2 = "comprasal_sv"


def _proceso_gid_v2(numero_referencia: str, portal_id: str = _PORTAL_ID_DEFAULT_V2) -> str:
    return f"{portal_id}:{numero_referencia}"


def upsert_reportada_v2(conn: Any, cliente_id: str, oportunidad: Any) -> None:
    """Espejo v2 de :func:`upsert_reportada`: solo marca "esto se reportó"
    en `reportada` (cliente_id, proceso_gid) — sin foto de los campos
    descriptivos, que ya viven en `proceso`/`cliente_proceso`.
    """
    proceso_gid = _proceso_gid_v2(oportunidad.numero_referencia)
    sql = """
        MERGE INTO reportada tgt
        USING (SELECT :cliente_id AS cliente_id, :proceso_gid AS proceso_gid FROM dual) src
        ON (tgt.cliente_id = src.cliente_id AND tgt.proceso_gid = src.proceso_gid)
        WHEN MATCHED THEN UPDATE SET tgt.reportado_en = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (cliente_id, proceso_gid)
            VALUES (:cliente_id, :proceso_gid)
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "proceso_gid": proceso_gid})
    finally:
        cursor.close()


def upsert_voto_v2(
    conn: Any,
    *,
    proceso_id: str,
    cliente_id: str,
    destinatario_id: str,
    voto: str,
    motivos: list[str],
    comentario: str,
) -> None:
    """Espejo v2 de :func:`upsert_voto`: MERGE en `feedback` con clave
    (cliente_id, proceso_gid, destinatario). ``destinatario`` es el mismo
    ``destinatario_id`` que v1 usa como clave (no el email: ver la nota del
    módulo sobre la simplificación de `motivos`→`motivo`).
    """
    if voto not in ("RELEVANTE", "IRRELEVANTE"):
        raise ValueError(f"voto inválido: {voto!r}")
    if voto == "RELEVANTE":
        motivos, comentario = [], ""

    if len(motivos) > 1:
        logger.warning(
            "upsert_voto_v2: %d motivos recibidos para %s/%s, `feedback` v2 solo "
            "guarda uno (motivo=%r); el resto solo queda en v1",
            len(motivos), cliente_id, proceso_id, motivos[0],
        )

    proceso_gid = _proceso_gid_v2(proceso_id)
    sql = """
        MERGE INTO feedback tgt
        USING (SELECT :cliente_id AS cliente_id, :proceso_gid AS proceso_gid,
                      :destinatario AS destinatario FROM dual) src
        ON (tgt.cliente_id = src.cliente_id AND tgt.proceso_gid = src.proceso_gid
            AND tgt.destinatario = src.destinatario)
        WHEN MATCHED THEN UPDATE SET
            tgt.voto       = :voto,
            tgt.motivo     = :motivo,
            tgt.comentario = :comentario,
            tgt.votado_en  = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            cliente_id, proceso_gid, destinatario, voto, motivo, comentario
        ) VALUES (
            :cliente_id, :proceso_gid, :destinatario, :voto, :motivo, :comentario
        )
    """
    binds = {
        "cliente_id": cliente_id,
        "proceso_gid": proceso_gid,
        "destinatario": destinatario_id,
        "voto": voto,
        "motivo": _recortar(motivos[0], 64) if motivos else None,
        "comentario": _recortar(comentario, 2000) if comentario else None,
    }
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def get_voto(conn: Any, proceso_id: str, cliente_id: str, destinatario_id: str) -> dict | None:
    """El voto vigente de un destinatario, o `None` si no votó."""
    sql = """
        SELECT voto, motivos, comentario
          FROM licitaciones_feedback
         WHERE proceso_id = :proceso_id AND cliente_id = :cliente_id
           AND destinatario_id = :destinatario_id
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"proceso_id": proceso_id, "cliente_id": cliente_id,
                             "destinatario_id": destinatario_id})
        fila = cursor.fetchone()
    finally:
        cursor.close()
    if not fila:
        return None
    motivos = fila[1]
    if isinstance(motivos, str):
        try:
            motivos = json.loads(motivos)
        except ValueError:
            motivos = []
    return {"voto": fila[0], "motivos": list(motivos or []), "comentario": fila[2] or ""}
