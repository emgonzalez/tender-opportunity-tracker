"""src.identidad.repo — usuarios, membresías, códigos de acceso, sesiones y auditoría (P5/P6). Mismas reglas que `src.feedback.repo`:
reciben una conexión ya abierta, no hacen commit, y todo valor viaja como bind variable.
"""
from __future__ import annotations

import json
from typing import Any

from src.identidad.politica import (
    ACCIONES_AUDITADAS,
    TTL_SESION_MFA_PENDIENTE_MIN,
    AccionNoAuditable,
)


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
               AND mfa_ok = 1
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


# ── Lecturas de identidad ──────────────────────────────────────────────────

def _uno(conn: Any, sql: str, binds: dict[str, Any]) -> tuple | None:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        return cursor.fetchone()
    finally:
        cursor.close()


def _todos(conn: Any, sql: str, binds: dict[str, Any]) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def obtener_usuario(conn: Any, usuario_id: str) -> dict[str, Any] | None:
    fila = _uno(conn, "SELECT usuario_id, org_id, email, activo FROM usuario WHERE usuario_id = :u",
                {"u": usuario_id})
    return {"usuario_id": fila[0], "org_id": fila[1], "email": fila[2], "activo": bool(fila[3])} if fila else None


def obtener_usuario_por_email(conn: Any, org_id: str, email: str) -> dict[str, Any] | None:
    fila = _uno(conn, "SELECT usuario_id, org_id, email, activo FROM usuario "
                      "WHERE org_id = :o AND LOWER(email) = :e", {"o": org_id, "e": email.strip().lower()})
    return {"usuario_id": fila[0], "org_id": fila[1], "email": fila[2], "activo": bool(fila[3])} if fila else None


def buscar_usuarios_activos_por_email(conn: Any, email: str) -> list[dict[str, Any]]:
    """Todos los usuarios activos con ese correo, de cualquier organización (el login no conoce la org)."""
    filas = _todos(conn, "SELECT usuario_id, org_id FROM usuario WHERE LOWER(email) = :e AND activo = 1",
                   {"e": email.strip().lower()})
    return [{"usuario_id": f[0], "org_id": f[1]} for f in filas]


def roles_org(conn: Any, usuario_id: str) -> list[str]:
    return [f[0] for f in _todos(conn, "SELECT rol FROM membresia_org WHERE usuario_id = :u", {"u": usuario_id})]


def membresias_pais(conn: Any, usuario_id: str) -> list[tuple[str, str]]:
    """(cliente_id, rol) de los países ACTIVOS de la organización del usuario."""
    filas = _todos(
        conn,
        """
        SELECT m.cliente_id, m.rol
          FROM membresia m
          JOIN usuario u ON u.usuario_id = m.usuario_id
          JOIN cliente c ON c.cliente_id = m.cliente_id AND c.org_id = u.org_id
         WHERE m.usuario_id = :u AND c.activo = 1
        """,
        {"u": usuario_id},
    )
    return [(f[0], f[1]) for f in filas]


def clientes_activos(conn: Any, org_id: str) -> list[str]:
    return [f[0] for f in _todos(conn, "SELECT cliente_id FROM cliente WHERE org_id = :o AND activo = 1",
                                 {"o": org_id})]


def cliente_existe(conn: Any, org_id: str, cliente_id: str) -> bool:
    return _uno(conn, "SELECT 1 FROM cliente WHERE org_id = :o AND cliente_id = :c",
                {"o": org_id, "c": cliente_id}) is not None


# ── Escrituras de usuarios y membresías (sin commit) ───────────────────────

def _ejecutar(conn: Any, sql: str, binds: dict[str, Any]) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        return cursor.rowcount
    finally:
        cursor.close()


def crear_usuario(conn: Any, usuario_id: str, org_id: str, email: str, nombre: str | None = None) -> None:
    _ejecutar(conn, "INSERT INTO usuario (usuario_id, org_id, email, nombre) VALUES (:u, :o, :e, :n)",
              {"u": usuario_id, "o": org_id, "e": email.strip().lower(), "n": nombre})


def desactivar_usuario(conn: Any, usuario_id: str) -> bool:
    return _ejecutar(conn, "UPDATE usuario SET activo = 0 WHERE usuario_id = :u AND activo = 1",
                     {"u": usuario_id}) > 0


def insertar_membresia(conn: Any, usuario_id: str, cliente_id: str, rol: str) -> None:
    _ejecutar(conn, "INSERT INTO membresia (usuario_id, cliente_id, rol) VALUES (:u, :c, :r)",
              {"u": usuario_id, "c": cliente_id, "r": rol})


def insertar_membresia_org(conn: Any, usuario_id: str, org_id: str, rol: str) -> None:
    _ejecutar(conn, "INSERT INTO membresia_org (usuario_id, org_id, rol) VALUES (:u, :o, :r)",
              {"u": usuario_id, "o": org_id, "r": rol})


# ── Códigos de acceso (sin commit) ─────────────────────────────────────────

def contar_codigos_recientes(conn: Any, usuario_id: str, minutos: int = 60) -> int:
    fila = _uno(conn, "SELECT COUNT(*) FROM codigo_acceso WHERE usuario_id = :u "
                      "AND creado_en > SYSTIMESTAMP - NUMTODSINTERVAL(:m, 'MINUTE')",
                {"u": usuario_id, "m": minutos})
    return fila[0]


def invalidar_codigos_previos(conn: Any, usuario_id: str) -> int:
    return _ejecutar(conn, "UPDATE codigo_acceso SET expira_en = SYSTIMESTAMP WHERE usuario_id = :u "
                           "AND usado_en IS NULL AND expira_en > SYSTIMESTAMP", {"u": usuario_id})


def insertar_codigo(conn: Any, usuario_id: str, codigo_hash: str, ttl_min: int) -> None:
    _ejecutar(conn, "INSERT INTO codigo_acceso (codigo_hash, usuario_id, expira_en) "
                    "VALUES (:h, :u, SYSTIMESTAMP + NUMTODSINTERVAL(:ttl, 'MINUTE'))",
              {"h": codigo_hash, "u": usuario_id, "ttl": ttl_min})


def obtener_codigo_vigente(conn: Any, usuario_id: str) -> dict[str, Any] | None:
    fila = _uno(conn, "SELECT codigo_id, codigo_hash, intentos FROM codigo_acceso WHERE usuario_id = :u "
                      "AND usado_en IS NULL AND expira_en > SYSTIMESTAMP "
                      "ORDER BY creado_en DESC FETCH FIRST 1 ROWS ONLY", {"u": usuario_id})
    return {"codigo_id": fila[0], "codigo_hash": fila[1], "intentos": fila[2]} if fila else None


def registrar_intento_fallido(conn: Any, codigo_id: int, max_intentos: int) -> None:
    """Suma un intento; al llegar al máximo el código vence en el acto."""
    _ejecutar(conn, "UPDATE codigo_acceso SET intentos = intentos + 1, "
                    "expira_en = CASE WHEN intentos + 1 >= :mx THEN SYSTIMESTAMP ELSE expira_en END "
                    "WHERE codigo_id = :id", {"id": codigo_id, "mx": max_intentos})


def marcar_codigo_usado(conn: Any, codigo_id: int) -> bool:
    """Atómico: de dos peticiones simultáneas con el mismo código, solo una gana."""
    return _ejecutar(conn, "UPDATE codigo_acceso SET usado_en = SYSTIMESTAMP WHERE codigo_id = :id "
                           "AND usado_en IS NULL AND expira_en > SYSTIMESTAMP", {"id": codigo_id}) > 0


# ── Sesiones (sin commit) ──────────────────────────────────────────────────

def crear_sesion(conn: Any, token_hash: str, usuario_id: str, ttl_horas: int,
                 ip_hash: str | None = None, user_agent: str | None = None, mfa_ok: bool = True) -> None:
    """Con ``mfa_ok=False`` la sesión nace PENDIENTE del TOTP y vive solo unos minutos."""
    segundos = ttl_horas * 3600 if mfa_ok else TTL_SESION_MFA_PENDIENTE_MIN * 60
    _ejecutar(conn, "INSERT INTO sesion (sesion_id, usuario_id, expira_en, ip_hash, user_agent, mfa_ok) "
                    "VALUES (:s, :u, SYSTIMESTAMP + NUMTODSINTERVAL(:seg, 'SECOND'), :ip, :ua, :mfa)",
              {"s": token_hash, "u": usuario_id, "seg": segundos, "ip": ip_hash,
               "ua": user_agent[:255] if user_agent else None, "mfa": 1 if mfa_ok else 0})


def obtener_usuario_de_sesion(conn: Any, token_hash: str) -> str | None:
    """``usuario_id`` de una sesión viva (no revocada, no vencida) de un usuario activo."""
    fila = _uno(conn, "SELECT s.usuario_id FROM sesion s JOIN usuario u ON u.usuario_id = s.usuario_id "
                      "WHERE s.sesion_id = :s AND s.revocada = 0 AND s.mfa_ok = 1 "
                      "AND s.expira_en > SYSTIMESTAMP AND u.activo = 1", {"s": token_hash})
    return fila[0] if fila else None


def revocar_sesion(conn: Any, token_hash: str) -> bool:
    return _ejecutar(conn, "UPDATE sesion SET revocada = 1 WHERE sesion_id = :s AND revocada = 0",
                     {"s": token_hash}) > 0


def revocar_sesiones_de(conn: Any, usuario_id: str) -> int:
    return _ejecutar(conn, "UPDATE sesion SET revocada = 1 WHERE usuario_id = :u AND revocada = 0",
                     {"u": usuario_id})


# ── Segundo factor TOTP (sin commit) ───────────────────────────────────────

def obtener_usuario_de_sesion_pendiente(conn: Any, token_hash: str) -> str | None:
    """``usuario_id`` de una sesión viva que pasó el correo pero todavía no el TOTP."""
    fila = _uno(conn, "SELECT s.usuario_id FROM sesion s JOIN usuario u ON u.usuario_id = s.usuario_id "
                      "WHERE s.sesion_id = :s AND s.revocada = 0 AND s.mfa_ok = 0 "
                      "AND s.expira_en > SYSTIMESTAMP AND u.activo = 1", {"s": token_hash})
    return fila[0] if fila else None


def completar_mfa(conn: Any, token_hash: str, ttl_horas: int) -> bool:
    """Convierte la sesión pendiente en una sesión completa, con su vigencia normal."""
    return _ejecutar(conn, "UPDATE sesion SET mfa_ok = 1, "
                           "expira_en = SYSTIMESTAMP + NUMTODSINTERVAL(:ttl, 'HOUR') "
                           "WHERE sesion_id = :s AND mfa_ok = 0 AND revocada = 0 AND expira_en > SYSTIMESTAMP",
                     {"s": token_hash, "ttl": ttl_horas}) > 0


def obtener_mfa(conn: Any, usuario_id: str) -> dict[str, Any]:
    fila = _uno(conn, "SELECT mfa_totp_secret, mfa_confirmado_en, mfa_ultimo_paso FROM usuario "
                      "WHERE usuario_id = :u", {"u": usuario_id})
    if not fila:
        return {"secreto_cifrado": None, "confirmado": False, "ultimo_paso": None}
    return {"secreto_cifrado": fila[0], "confirmado": fila[1] is not None, "ultimo_paso": fila[2]}


def guardar_secreto_mfa(conn: Any, usuario_id: str, cifrado: str) -> bool:
    """Guarda un secreto SIN confirmar. Nunca pisa uno ya confirmado."""
    return _ejecutar(conn, "UPDATE usuario SET mfa_totp_secret = :s, mfa_ultimo_paso = NULL "
                           "WHERE usuario_id = :u AND mfa_confirmado_en IS NULL",
                     {"s": cifrado, "u": usuario_id}) > 0


def confirmar_mfa(conn: Any, usuario_id: str, paso: int) -> bool:
    return _ejecutar(conn, "UPDATE usuario SET mfa_confirmado_en = SYSTIMESTAMP, mfa_ultimo_paso = :p "
                           "WHERE usuario_id = :u AND mfa_confirmado_en IS NULL AND mfa_totp_secret IS NOT NULL",
                     {"p": paso, "u": usuario_id}) > 0


def registrar_paso_mfa(conn: Any, usuario_id: str, paso: int) -> bool:
    """Anti-repetición atómica: un paso ya usado (o anterior) no sirve otra vez."""
    return _ejecutar(conn, "UPDATE usuario SET mfa_ultimo_paso = :p WHERE usuario_id = :u "
                           "AND mfa_confirmado_en IS NOT NULL AND (mfa_ultimo_paso IS NULL OR mfa_ultimo_paso < :p)",
                     {"p": paso, "u": usuario_id}) > 0


def reemplazar_recuperacion(conn: Any, usuario_id: str, hashes: list[str]) -> None:
    _ejecutar(conn, "DELETE FROM recuperacion_mfa WHERE usuario_id = :u", {"u": usuario_id})
    for h in hashes:
        _ejecutar(conn, "INSERT INTO recuperacion_mfa (usuario_id, codigo_hash) VALUES (:u, :h)",
                  {"u": usuario_id, "h": h})


def consumir_recuperacion(conn: Any, usuario_id: str, codigo_hash: str) -> bool:
    """Atómico: de dos peticiones con el mismo código de recuperación, solo una gana."""
    return _ejecutar(conn, "UPDATE recuperacion_mfa SET usado_en = SYSTIMESTAMP WHERE usuario_id = :u "
                           "AND codigo_hash = :h AND usado_en IS NULL",
                     {"u": usuario_id, "h": codigo_hash}) > 0
