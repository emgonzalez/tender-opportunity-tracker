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
