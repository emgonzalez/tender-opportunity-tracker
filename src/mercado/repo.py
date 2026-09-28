"""src.mercado.repo — acceso a datos de `adjudicacion_item` / `proceso_participado`.

Mismo patrón que `src.memoria.repo`: MERGE idempotente, todo por bind
variable (regla de seguridad innegociable del repo — ningún valor se
interpola en el texto SQL).

Todas las funciones reciben una conexión `conn` ya abierta (oracledb) como
primer parámetro y no gestionan su ciclo de vida (eso es de
`src.memoria.db.connection`, que este paquete reutiliza tal cual).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _parse_fecha(valor: Any) -> date | None:
    """Parsea una fecha que puede venir como `None`, `"YYYY-MM-DD"` o ISO con
    hora/zona (`"YYYY-MM-DDTHH:MM:SS.sssZ"`). Nunca lanza: un formato
    inesperado se registra y se trata como fecha ausente, no como error que
    tumbe la carga de toda la fila.
    """
    if not valor:
        return None
    if isinstance(valor, (date, datetime)):
        return valor
    texto = str(valor).strip()
    if not texto:
        return None
    texto = texto.rstrip("Z").split(".")[0]  # recorta milisegundos/Z si los trae
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            continue
    logger.warning("Fecha con formato inesperado, se ignora: %r", valor)
    return None


def upsert_adjudicacion_item(conn: Any, item: dict, es_multicomputos: bool) -> None:
    """Da de alta o actualiza una fila del registro público de adjudicaciones.

    `item` es la fila cruda tal como la entrega
    `mercado.api_publica.paginar_todo` (una fila = un renglón/lote de una
    adjudicación, no un proceso completo — ver el docstring de ese módulo).
    """
    proceso_compra = item.get("proceso_compra") or {}
    institucion = item.get("institucion") or {}
    proveedor = item.get("proveedor") or {}

    sql = """
        MERGE INTO adjudicacion_item tgt
        USING (SELECT :id AS id FROM dual) src
        ON (tgt.id = src.id)
        WHEN MATCHED THEN UPDATE SET
            tgt.codigo_proceso             = :codigo_proceso,
            tgt.nombre_proceso             = :nombre_proceso,
            tgt.id_institucion             = :id_institucion,
            tgt.institucion                = :institucion,
            tgt.fecha_adjudicacion         = :fecha_adjudicacion,
            tgt.monto                      = :monto,
            tgt.id_proveedor               = :id_proveedor,
            tgt.proveedor_nombre           = :proveedor_nombre,
            tgt.proveedor_nombre_comercial = :proveedor_nombre_comercial,
            tgt.es_multicomputos           = :es_multicomputos,
            tgt.datos_crudos               = JSON(:datos_crudos),
            tgt.actualizado_en             = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            id, codigo_proceso, nombre_proceso, id_institucion, institucion,
            fecha_adjudicacion, monto, id_proveedor, proveedor_nombre,
            proveedor_nombre_comercial, es_multicomputos, datos_crudos,
            cargado_en, actualizado_en
        ) VALUES (
            :id, :codigo_proceso, :nombre_proceso, :id_institucion, :institucion,
            :fecha_adjudicacion, :monto, :id_proveedor, :proveedor_nombre,
            :proveedor_nombre_comercial, :es_multicomputos, JSON(:datos_crudos),
            SYSTIMESTAMP, SYSTIMESTAMP
        )
    """
    id_proveedor = proveedor.get("id_proveedor", proveedor.get("id"))
    binds = {
        "id": item.get("id"),
        "codigo_proceso": (proceso_compra.get("codigo_proceso") or item.get("codigo_proceso") or "")[:64],
        "nombre_proceso": (proceso_compra.get("nombre_proceso") or item.get("nombre_proceso") or "")[:1000],
        "id_institucion": institucion.get("id"),
        "institucion": (institucion.get("nombre") or "")[:400],
        "fecha_adjudicacion": _parse_fecha(proceso_compra.get("fecha_adjudicacion")),
        "monto": item.get("monto"),
        "id_proveedor": id_proveedor,
        "proveedor_nombre": (proveedor.get("nombre") or "")[:400],
        "proveedor_nombre_comercial": (proveedor.get("nombre_comercial") or "")[:400],
        "es_multicomputos": 1 if es_multicomputos else 0,
        "datos_crudos": json.dumps(item, ensure_ascii=False, default=str),
    }

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def upsert_proceso_participado(conn: Any, cliente_id: str, item: dict) -> None:
    """Alta o refresco de un proceso en el que el cliente participó.

    `item` viene de `classifier.trainer.extract_all_participated`: claves
    `codigo`, `nombre`, `institucion`, `metodo`, `fecha` (ya recortada a
    10 caracteres `YYYY-MM-DD` por ese módulo).

    **NUNCA pisa `resultado`** — igual que `memoria.repo.upsert_proceso` no
    pisa `estado`: la resolución (`GANADO`/`PERDIDO`/`DESIERTO`) es
    responsabilidad exclusiva de `resolver_resultados`, para que reprocesar
    la lista de participados no resetee un resultado ya conocido.
    """
    sql = """
        MERGE INTO proceso_participado tgt
        USING (SELECT :cliente_id AS cliente_id, :codigo_proceso AS codigo_proceso FROM dual) src
        ON (tgt.cliente_id = src.cliente_id AND tgt.codigo_proceso = src.codigo_proceso)
        WHEN MATCHED THEN UPDATE SET
            tgt.nombre_proceso      = :nombre_proceso,
            tgt.institucion         = :institucion,
            tgt.forma_contratacion  = :forma_contratacion,
            tgt.fecha_publicacion   = :fecha_publicacion,
            tgt.actualizado_en      = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            cliente_id, codigo_proceso, nombre_proceso, institucion,
            forma_contratacion, fecha_publicacion, resultado,
            cargado_en, actualizado_en
        ) VALUES (
            :cliente_id, :codigo_proceso, :nombre_proceso, :institucion,
            :forma_contratacion, :fecha_publicacion, 'PENDIENTE',
            SYSTIMESTAMP, SYSTIMESTAMP
        )
    """
    binds = {
        "cliente_id": cliente_id,
        "codigo_proceso": (item.get("codigo") or "")[:64],
        "nombre_proceso": (item.get("nombre") or "")[:1000],
        "institucion": (item.get("institucion") or "")[:400],
        "forma_contratacion": (item.get("metodo") or "")[:120],
        "fecha_publicacion": _parse_fecha(item.get("fecha")),
    }

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def resolver_resultados(conn: Any, cliente_id: str) -> dict[str, int]:
    """Cruza `proceso_participado` (solo los `PENDIENTE`) contra
    `adjudicacion_item` por `codigo_proceso` y resuelve el resultado:

    - **GANADO**: existe al menos una fila de `adjudicacion_item` con ese
      `codigo_proceso` y `es_multicomputos = 1`. `monto_adjudicado_a_nosotros`
      es la suma de esas filas (un proceso puede tener varios lotes
      adjudicados a nosotros).
    - **PERDIDO**: existen filas para ese `codigo_proceso` pero ninguna
      nuestra. `proveedor_ganador` es el proveedor del renglón de mayor
      monto (heurística — un proceso puede tener varios ganadores por lote;
      esto muestra el más grande, no necesariamente el único) y
      `monto_ganador` es la suma de todos los renglones.
    - Sin ninguna fila en `adjudicacion_item` todavía: queda `PENDIENTE`
      (no se toca — podría no estar adjudicado aún, o no haberse cargado).

    No resuelve `DESIERTO` (requiere el detalle por proceso,
    `documento_proceso_desierto`, no este cruce — ver el plan del portal,
    §3.5, "pendiente de verificar").

    Devuelve `{"ganados": N, "perdidos": M}` (filas afectadas en esta
    corrida, no el total histórico).
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE proceso_participado p
               SET resultado = 'GANADO',
                   monto_adjudicado_a_nosotros = (
                       SELECT SUM(a.monto) FROM adjudicacion_item a
                        WHERE a.codigo_proceso = p.codigo_proceso
                          AND a.es_multicomputos = 1
                   ),
                   resuelto_en = SYSTIMESTAMP,
                   actualizado_en = SYSTIMESTAMP
             WHERE p.cliente_id = :cliente_id
               AND p.resultado = 'PENDIENTE'
               AND EXISTS (
                   SELECT 1 FROM adjudicacion_item a
                    WHERE a.codigo_proceso = p.codigo_proceso
                      AND a.es_multicomputos = 1
               )
            """,
            {"cliente_id": cliente_id},
        )
        ganados = cursor.rowcount

        cursor.execute(
            """
            UPDATE proceso_participado p
               SET resultado = 'PERDIDO',
                   proveedor_ganador = (
                       SELECT proveedor_nombre FROM (
                           SELECT a.proveedor_nombre
                             FROM adjudicacion_item a
                            WHERE a.codigo_proceso = p.codigo_proceso
                            ORDER BY a.monto DESC NULLS LAST
                       ) WHERE ROWNUM = 1
                   ),
                   monto_ganador = (
                       SELECT SUM(a.monto) FROM adjudicacion_item a
                        WHERE a.codigo_proceso = p.codigo_proceso
                   ),
                   resuelto_en = SYSTIMESTAMP,
                   actualizado_en = SYSTIMESTAMP
             WHERE p.cliente_id = :cliente_id
               AND p.resultado = 'PENDIENTE'
               AND EXISTS (
                   SELECT 1 FROM adjudicacion_item a
                    WHERE a.codigo_proceso = p.codigo_proceso
               )
            """,
            {"cliente_id": cliente_id},
        )
        perdidos = cursor.rowcount

        return {"ganados": ganados, "perdidos": perdidos}
    finally:
        cursor.close()


# ── Espejo del esquema v2 multi-tenant/multi-país (db/schema_v2.sql) ────────
#
# Fase "Doble escritura" de la migración (plan del portal AMALIA §3.3). Se
# llama ADEMÁS de resolver_resultados, desde src.mercado.service, en su
# propio try/except — un fallo acá nunca debe poder revertir ni bloquear la
# resolución que ya está en producción (§3.5, PR #35).
#
# Prepuebla el Pipeline comercial (`oportunidad_estado`) a partir de lo que
# ya resolvió `resolver_resultados`: GANADO/PERDIDO en `proceso_participado`
# se refleja como una fila con `origen = 'AUTOMATICO_MERCADO'`. La entrada
# MANUAL desde el portal (cuando exista, Fase 1) queda para enriquecer
# (motivo, competidor), no como única fuente — y por eso el MERGE NUNCA
# pisa una fila que ya tenga `origen = 'MANUAL'`: la condición `WHERE
# tgt.origen = 'AUTOMATICO_MERCADO'` del WHEN MATCHED es justamente esa
# guarda.

# Duplicada de src.memoria.repo.PORTAL_ID_DEFAULT (mismo valor): este módulo
# no depende de memoria.repo por una sola constante.
_PORTAL_ID_DEFAULT_V2 = "comprasal_sv"


def prepoblar_oportunidad_estado_v2(
    conn: Any, cliente_id: str, portal_id: str = _PORTAL_ID_DEFAULT_V2,
) -> int:
    """MERGE de todo `proceso_participado` ya resuelto (GANADO/PERDIDO) de
    ``cliente_id`` hacia `oportunidad_estado`, con `origen='AUTOMATICO_MERCADO'`.

    A diferencia de `resolver_resultados` (que solo toca lo recién resuelto
    en ESTA corrida), esto reprocesa TODO lo resuelto históricamente: es
    idempotente y barato (una fila por proceso participado, no por
    adjudicación), así que no hace falta rastrear qué cambió justo ahora.

    :return: filas insertadas o actualizadas (MERGE `rowcount`).
    """
    sql = """
        MERGE INTO oportunidad_estado tgt
        USING (
            SELECT :cliente_id AS cliente_id,
                   :portal_id || ':' || codigo_proceso AS proceso_gid,
                   resultado, monto_adjudicado_a_nosotros
              FROM proceso_participado
             WHERE cliente_id = :cliente_id
               AND resultado IN ('GANADO', 'PERDIDO')
        ) src
        ON (tgt.cliente_id = src.cliente_id AND tgt.proceso_gid = src.proceso_gid)
        WHEN MATCHED THEN UPDATE SET
            tgt.estado           = src.resultado,
            tgt.monto_adjudicado = src.monto_adjudicado_a_nosotros,
            tgt.actualizado_en   = SYSTIMESTAMP
          WHERE tgt.origen = 'AUTOMATICO_MERCADO'
        WHEN NOT MATCHED THEN INSERT (
            cliente_id, proceso_gid, estado, monto_adjudicado, origen
        ) VALUES (
            src.cliente_id, src.proceso_gid, src.resultado,
            src.monto_adjudicado_a_nosotros, 'AUTOMATICO_MERCADO'
        )
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "portal_id": portal_id})
        return cursor.rowcount
    finally:
        cursor.close()


def contar_pendientes(conn: Any, cliente_id: str) -> int:
    """Cuántos procesos participados siguen sin resolver. Útil para reportar
    progreso tras `resolver_resultados` (no todo lo pendiente se resuelve en
    una corrida: depende de qué ya esté adjudicado y cargado)."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM proceso_participado "
            "WHERE cliente_id = :cliente_id AND resultado = 'PENDIENTE'",
            {"cliente_id": cliente_id},
        )
        return cursor.fetchone()[0]
    finally:
        cursor.close()
