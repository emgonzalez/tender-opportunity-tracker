"""src.identidad.repo — sesiones y auditoría (P5/P6). Mismas reglas que `src.feedback.repo`:
reciben una conexión ya abierta, no hacen commit, y todo valor viaja como bind variable.
"""
from __future__ import annotations

import json
from typing import Any

from src.identidad.politica import ACCIONES_AUDITADAS, AccionNoAuditable


def renovar_sesion(conn: Any, sesion_id: str, ttl_horas: int, max_dias: int) -> bool:
    """Desliza la expiración: ahora + ``ttl_horas``, sin pasar nunca de ``max_dias`` desde el login.

    Solo renueva una sesión viva (no revocada y no vencida): una vencida no se resucita.
    :return: True si se renovó; False si no existe, está revocada o ya venció.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE sesion
               SET expira_en = LEAST(SYSTIMESTAMP + NUMTODSINTERVAL(:ttl_horas, 'HOUR'),
                                     creado_en + NUMTODSINTERVAL(:max_dias, 'DAY'))
             WHERE sesion_id = :sesion_id
               AND revocada = 0
               AND expira_en > SYSTIMESTAMP
            """,
            {"ttl_horas": ttl_horas, "max_dias": max_dias, "sesion_id": sesion_id},
        )
        return cursor.rowcount > 0
    finally:
        cursor.close()


def purgar_sesiones_vencidas(conn: Any, dias: int, dry_run: bool = False) -> int:
    """Borra las sesiones que vencieron hace más de ``dias`` (30 por P5). Idempotente.

    Una sesión revocada pero aún no vencida se conserva hasta que venza: la traza de por qué se cerró
    es la fila de `auditoria`, y esta tabla solo guarda el hash del token.
    :return: filas borradas (o a borrar con ``dry_run``).
    """
    cursor = conn.cursor()
    try:
        binds = {"dias": dias}
        if dry_run:
            cursor.execute(
                "SELECT COUNT(*) FROM sesion WHERE expira_en < SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')",
                binds,
            )
            return cursor.fetchone()[0]
        cursor.execute("DELETE FROM sesion WHERE expira_en < SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')", binds)
        return cursor.rowcount
    finally:
        cursor.close()


def registrar_auditoria(
    conn: Any,
    org_id: str,
    accion: str,
    usuario_id: str | None = None,
    recurso: str | None = None,
    alcance: dict[str, Any] | None = None,
    ip_hash: str | None = None,
) -> None:
    """Agrega una fila a `auditoria` (append-only: nunca se actualiza ni se borra).

    :raises AccionNoAuditable: si ``accion`` no está en `ACCIONES_AUDITADAS` (P6).
    """
    if accion not in ACCIONES_AUDITADAS:
        raise AccionNoAuditable(accion)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO auditoria (usuario_id, org_id, accion, recurso, alcance, ip_hash)
            VALUES (:usuario_id, :org_id, :accion, :recurso, JSON(:alcance), :ip_hash)
            """,
            {
                "usuario_id": usuario_id,
                "org_id": org_id,
                "accion": accion,
                "recurso": recurso[:200] if recurso else None,
                "alcance": json.dumps(alcance, ensure_ascii=False) if alcance is not None else None,
                "ip_hash": ip_hash,
            },
        )
    finally:
        cursor.close()
