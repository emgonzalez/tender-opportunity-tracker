"""src.oportunidades.repo — SQL del pipeline comercial. Reciben una conexión ya abierta, no hacen commit
y todo valor viaja como bind variable."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import oracledb

from src.analytics.pais import _SQL_ES_RELEVANTE

# Token de concurrencia: el `actualizado_en` leído, con microsegundos, como texto estable.
_TOKEN = "TO_CHAR(e.actualizado_en, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6')"


def proceso_visible(conn: Any, cliente_id: str, proceso_gid: str) -> bool:
    """IDOR: un `proceso_gid` solo es válido si ese cliente lo tiene en `cliente_proceso`."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM cliente_proceso WHERE cliente_id = :c AND proceso_gid = :g",
                       {"c": cliente_id, "g": proceso_gid})
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def obtener(conn: Any, cliente_id: str, proceso_gid: str) -> dict[str, Any] | None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT e.estado, {_TOKEN}, e.responsable_usuario_id FROM oportunidad_estado e "
            "WHERE e.cliente_id = :c AND e.proceso_gid = :g",
            {"c": cliente_id, "g": proceso_gid})
        fila = cursor.fetchone()
        return {"estado": fila[0], "token": fila[1], "responsable": fila[2]} if fila else None
    finally:
        cursor.close()


def listar(conn: Any, cliente_id: str, dias: int = 90, limite: int = 50) -> list[dict[str, Any]]:
    """Oportunidades del cliente con su estado comercial; las más recientes primero.

    Es oportunidad lo que AMALIA juzgó relevante O lo que ya tiene fila en `oportunidad_estado` (E11: un
    descartado que la persona recuperó, sin tocar su clasificación en `cliente_proceso`).
    Sin fila en `oportunidad_estado` el estado es NUEVO y el token va vacío."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT cp.proceso_gid, p.numero_referencia, p.titulo, p.institucion, p.fecha_limite_utc,
                   COALESCE(e.estado, 'NUEVO'), e.origen, {_TOKEN}
              FROM cliente_proceso cp
              JOIN proceso p ON p.proceso_gid = cp.proceso_gid
              LEFT JOIN oportunidad_estado e ON e.cliente_id = cp.cliente_id AND e.proceso_gid = cp.proceso_gid
             WHERE cp.cliente_id = :c AND p.fecha_pub_utc >= SYSDATE - :dias AND ({_SQL_ES_RELEVANTE} OR e.cliente_id IS NOT NULL)
             ORDER BY p.fecha_pub_utc DESC
             FETCH FIRST :limite ROWS ONLY
            """,
            {"c": cliente_id, "dias": dias, "limite": limite})
        return [{"proceso_gid": f[0], "numero_referencia": f[1], "titulo": f[2], "institucion": f[3],
                 "fecha_limite": f[4], "estado": f[5], "origen": f[6], "token": f[7] or ""}
                for f in cursor.fetchall()]
    finally:
        cursor.close()


def crear_estado(conn: Any, cliente_id: str, proceso_gid: str, estado: str, usuario_id: str | None,
                 motivo: str | None, monto: Decimal | None, competidor: str | None) -> bool:
    """INSERT de la primera captura. False si alguien la creó antes (conflicto).

    ``usuario_id`` queda como responsable (None = sin responsable)."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, responsable_usuario_id, motivo,
                                            monto_ofertado, competidor, origen)
            VALUES (:c, :g, :estado, :u, :motivo, :monto, :competidor, 'MANUAL')
            """,
            {"c": cliente_id, "g": proceso_gid, "estado": estado, "u": usuario_id, "motivo": motivo,
             "monto": monto, "competidor": competidor})
        return True
    except oracledb.IntegrityError:
        return False
    finally:
        cursor.close()


def actualizar_estado(conn: Any, cliente_id: str, proceso_gid: str, token: str, estado: str, usuario_id: str,
                      motivo: str | None, monto: Decimal | None, competidor: str | None) -> bool:
    """UPDATE condicionado al token leído: False si la fila cambió mientras la persona editaba.

    La persona toma la fila (origen MANUAL) aunque la hubiera movido el mercado (P8)."""
    cursor = conn.cursor()
    try:
        # Un `None` ligado a COALESCE se tiparía como texto (ORA-00932 contra la columna NUMBER).
        cursor.setinputsizes(monto=oracledb.DB_TYPE_NUMBER)
        cursor.execute(
            """
            UPDATE oportunidad_estado e
               SET estado = :estado, responsable_usuario_id = COALESCE(responsable_usuario_id, :u), motivo = :motivo,
                   monto_ofertado = COALESCE(:monto, monto_ofertado),
                   competidor = COALESCE(:competidor, competidor),
                   origen = 'MANUAL', actualizado_en = SYSTIMESTAMP
             WHERE e.cliente_id = :c AND e.proceso_gid = :g AND """ + _TOKEN + " = :token",
            {"estado": estado, "u": usuario_id, "motivo": motivo, "monto": monto, "competidor": competidor,
             "c": cliente_id, "g": proceso_gid, "token": token})
        return cursor.rowcount > 0
    finally:
        cursor.close()


def fijar_responsable(conn: Any, cliente_id: str, proceso_gid: str, responsable_id: str | None, token: str) -> bool:
    """Cambia SOLO el responsable (None lo quita), condicionado al token leído como `actualizar_estado`.

    No toca estado, motivo, montos ni origen. False si la fila cambió mientras la persona editaba."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE oportunidad_estado e
               SET responsable_usuario_id = :r, actualizado_en = SYSTIMESTAMP
             WHERE e.cliente_id = :c AND e.proceso_gid = :g AND """ + _TOKEN + " = :token",
            {"r": responsable_id, "c": cliente_id, "g": proceso_gid, "token": token})
        return cursor.rowcount > 0
    finally:
        cursor.close()


def es_descartado_por_amalia(conn: Any, cliente_id: str, proceso_gid: str) -> bool | None:
    """True si AMALIA descartó el proceso (no cumple `_SQL_ES_RELEVANTE`) y nadie lo recuperó todavía
    (sin fila en `oportunidad_estado`). False si es relevante o ya tiene fila; None si no es de ese cliente."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT CASE WHEN {_SQL_ES_RELEVANTE} OR e.cliente_id IS NOT NULL THEN 0
                        WHEN cp.estado LIKE 'DESCARTADO\\_%' ESCAPE '\\' OR cp.es_relevante = 0 THEN 1
                        ELSE 0 END
              FROM cliente_proceso cp
              LEFT JOIN oportunidad_estado e ON e.cliente_id = cp.cliente_id AND e.proceso_gid = cp.proceso_gid
             WHERE cp.cliente_id = :c AND cp.proceso_gid = :g
            """,
            {"c": cliente_id, "g": proceso_gid})
        fila = cursor.fetchone()
        return bool(fila[0]) if fila else None
    finally:
        cursor.close()


def insertar_evento(conn: Any, cliente_id: str, proceso_gid: str, anterior: str | None, nuevo: str,
                    usuario_id: str, nota: str | None) -> None:
    """``anterior`` None = la oportunidad nace (recuperada de los descartados de AMALIA)."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO oportunidad_evento (cliente_id, proceso_gid, estado_anterior, estado_nuevo, usuario_id, nota) "
            "VALUES (:c, :g, :anterior, :nuevo, :u, :nota)",
            {"c": cliente_id, "g": proceso_gid, "anterior": anterior, "nuevo": nuevo, "u": usuario_id,
             "nota": nota[:1000] if nota else None})
    finally:
        cursor.close()
