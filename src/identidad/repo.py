"""src.identidad.repo — usuarios, membresías, códigos de acceso, sesiones y auditoría (P5/P6). Mismas reglas que `src.feedback.repo`:
reciben una conexión ya abierta, no hacen commit, y todo valor viaja como bind variable.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.identidad.politica import (
    ACCIONES_AUDITADAS,
    ROLES_CAMBIAN_ESTADO,
    ROLES_ORG_NEGOCIO,
    ROLES_PAIS,
    ROLES_TECNICOS,
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


def _binds_en(prefijo: str, valores: frozenset[str]) -> tuple[str, dict[str, str]]:
    """``:p0, :p1, …`` y sus binds, en orden estable: los roles no se interpolan en el SQL."""
    binds = {f"{prefijo}{i}": v for i, v in enumerate(sorted(valores))}
    return ", ".join(f":{k}" for k in binds) or "NULL", binds  # `IN (NULL)` no casa con nada


def usuarios_asignables(conn: Any, org_id: str, cliente_id: str) -> list[dict[str, Any]]:
    """Quiénes pueden ser responsables de una oportunidad de ``cliente_id`` (E11).

    Replica `alcance._alcance_desde` para «cambia estado en ese cliente»: usuario ACTIVO de ``org_id``,
    con el cliente ACTIVO y de la misma org, y o bien una membresía de país en ese cliente con un rol de
    `ROLES_CAMBIAN_ESTADO`, o bien un rol de organización de negocio que cambia estado en todos los
    países (hoy `director_comercial`). Sin nombre se muestra la parte local del correo.
    :return: ``[{"usuario_id", "nombre"}]`` ordenado por nombre.
    """
    en_pais, b_pais = _binds_en("rp", ROLES_CAMBIAN_ESTADO & ROLES_PAIS)
    en_org, b_org = _binds_en("ro", ROLES_CAMBIAN_ESTADO & ROLES_ORG_NEGOCIO)
    filas = _todos(
        conn,
        f"""
        SELECT u.usuario_id, u.nombre, u.email
          FROM usuario u
          JOIN cliente c ON c.cliente_id = :c AND c.org_id = u.org_id AND c.activo = 1
         WHERE u.org_id = :o AND u.activo = 1
           AND (EXISTS (SELECT 1 FROM membresia m
                         WHERE m.usuario_id = u.usuario_id AND m.cliente_id = c.cliente_id
                           AND m.rol IN ({en_pais}))
                OR EXISTS (SELECT 1 FROM membresia_org mo
                            WHERE mo.usuario_id = u.usuario_id AND mo.rol IN ({en_org})))
        """,
        {"o": org_id, "c": cliente_id, **b_pais, **b_org},
    )
    usuarios = [{"usuario_id": f[0], "nombre": (f[1] or "").strip() or (f[2] or "").split("@")[0] or f[0]}
                for f in filas]
    return sorted(usuarios, key=lambda u: (u["nombre"].casefold(), u["usuario_id"]))


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


# ── «Ver como» (E10, sin commit) ───────────────────────────────────────────
# La simulación vive en la sesión del admin de plataforma. NO se desliza: `renovar_sesion` no la
# toca, así que renovar no reinicia su `ver_como_desde` y el TTL corre desde que se fijó.

def fijar_ver_como(conn: Any, token_hash: str, rol: str, cliente: str | None) -> bool:
    """Fija (o reemplaza) la simulación en una sesión viva y completa. ``False`` si no hay tal sesión."""
    return _ejecutar(conn, "UPDATE sesion SET ver_como_rol = :r, ver_como_cliente = :c, "
                           "ver_como_desde = CAST(SYSTIMESTAMP AS TIMESTAMP) "
                           "WHERE sesion_id = :s AND revocada = 0 AND mfa_ok = 1 AND expira_en > SYSTIMESTAMP",
                     {"r": rol, "c": cliente, "s": token_hash}) > 0


def limpiar_ver_como(conn: Any, token_hash: str) -> bool:
    """Apaga la simulación. Idempotente: ``False`` si no había ninguna que apagar."""
    return _ejecutar(conn, "UPDATE sesion SET ver_como_rol = NULL, ver_como_cliente = NULL, "
                           "ver_como_desde = NULL WHERE sesion_id = :s AND ver_como_rol IS NOT NULL",
                     {"s": token_hash}) > 0


def obtener_ver_como(conn: Any, token_hash: str, ttl_min: int) -> tuple[str, str | None, datetime] | None:
    """``(rol, cliente, desde)`` de la simulación vigente de una sesión viva; ``None`` si no hay o venció."""
    fila = _uno(conn, "SELECT ver_como_rol, ver_como_cliente, ver_como_desde FROM sesion "
                      "WHERE sesion_id = :s AND revocada = 0 AND mfa_ok = 1 AND expira_en > SYSTIMESTAMP "
                      "AND ver_como_rol IS NOT NULL "
                      "AND ver_como_desde > CAST(SYSTIMESTAMP AS TIMESTAMP) - NUMTODSINTERVAL(:ttl, 'MINUTE')",
                {"s": token_hash, "ttl": ttl_min})
    return (fila[0], fila[1], fila[2]) if fila else None


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


# ── Destinatarios de correo (reporte diario y alertas de operación) ────────

def org_de_cliente(conn: Any, cliente_id: str) -> str | None:
    """Organización dueña de un cliente ACTIVO, o None si no existe o está apagado."""
    fila = _uno(conn, "SELECT org_id FROM cliente WHERE cliente_id = :c AND activo = 1", {"c": cliente_id})
    return fila[0] if fila else None


def destinatarios_reporte(conn: Any, org_id: str, cliente_id: str) -> list[tuple[str, frozenset[str]]]:
    """Quién recibe el reporte de ``cliente_id`` y con qué restricción de marca: ``[(email, solo_marcas)]``.

    Recibe quien tiene un rol de negocio con acceso a ese país (`Alcance.ve_negocio and puede_ver`): es la
    misma regla del portal, así que el correo y «Ver como» no divergen y la restricción E14 (rol Oracle ⇒ solo
    Oracle) la decide `resolver_alcance`, no este módulo. Los roles técnicos no reciben reporte, y
    `admin_organizacion` solo si además tiene un rol comercial. Sin repetidos
    (por correo en minúsculas); ``solo_marcas`` vacío = perfil completo.
    """
    from src.identidad.alcance import resolver_alcance  # import tardío: alcance importa este módulo

    filas = _todos(conn, "SELECT usuario_id, email FROM usuario WHERE org_id = :o AND activo = 1 ORDER BY email",
                   {"o": org_id})
    vistos: dict[str, frozenset[str]] = {}
    for usuario_id, correo in filas:
        correo = (correo or "").strip().lower()
        if not correo or correo in vistos:
            continue
        alcance = resolver_alcance(conn, usuario_id)
        if alcance is None or not alcance.ve_negocio or not alcance.puede_ver(cliente_id):
            continue
        # `admin_organizacion` administra pero no es un rol comercial: solo recibe si además tiene uno.
        if not (alcance.roles - ROLES_TECNICOS - {"admin_organizacion"}):
            continue
        vistos[correo] = alcance.solo_marcas
    return list(vistos.items())


def usuarios_con_rol(conn: Any, org_id: str | None, rol: str) -> list[str]:
    """Correos (minúsculas, sin repetir) de los usuarios ACTIVOS con ``rol`` de organización.

    ``org_id=None`` = de todas las organizaciones: una alerta de infraestructura no pertenece a una org.
    """
    filtro = "" if org_id is None else "AND u.org_id = :o "
    binds: dict[str, Any] = {"r": rol}
    if org_id is not None:
        binds["o"] = org_id
    filas = _todos(
        conn,
        "SELECT DISTINCT LOWER(u.email) FROM usuario u JOIN membresia_org mo ON mo.usuario_id = u.usuario_id "
        f"WHERE u.activo = 1 AND mo.rol = :r {filtro}ORDER BY 1",
        binds,
    )
    return [f[0] for f in filas if f[0]]


# ── Administración de usuarios desde el portal (Fase G1; sin commit) ───────
# Lecturas con `org_id=None` = de todas las organizaciones (solo `admin_plataforma`; el servicio decide).

def organizacion_existe(conn: Any, org_id: str) -> bool:
    return _uno(conn, "SELECT 1 FROM organizacion WHERE org_id = :o", {"o": org_id}) is not None


def listar_organizaciones(conn: Any) -> list[dict[str, str]]:
    filas = _todos(conn, "SELECT org_id, nombre FROM organizacion ORDER BY nombre, org_id", {})
    return [{"org_id": f[0], "nombre": f[1] or f[0]} for f in filas]


def listar_clientes(conn: Any, org_id: str) -> list[str]:
    """Todos los clientes (países) de la organización, activos o no, para los `<select>` de administración."""
    return [f[0] for f in _todos(conn, "SELECT cliente_id FROM cliente WHERE org_id = :o ORDER BY cliente_id",
                                 {"o": org_id})]


def _usuario_admin(f: tuple) -> dict[str, Any]:
    return {"usuario_id": f[0], "org_id": f[1], "email": f[2], "nombre": f[3] or "", "activo": bool(f[4]),
            "mfa": f[5] is not None, "creado_en": f[6]}


_COLUMNAS_USUARIO = "u.usuario_id, u.org_id, u.email, u.nombre, u.activo, u.mfa_confirmado_en, u.creado_en"


def listar_usuarios(conn: Any, org_id: str | None) -> list[dict[str, Any]]:
    filtro, binds = ("", {}) if org_id is None else ("WHERE u.org_id = :o ", {"o": org_id})
    filas = _todos(conn, f"SELECT {_COLUMNAS_USUARIO} FROM usuario u {filtro}ORDER BY u.org_id, LOWER(u.email)", binds)
    return [_usuario_admin(f) for f in filas]


def obtener_usuario_admin(conn: Any, usuario_id: str) -> dict[str, Any] | None:
    fila = _uno(conn, f"SELECT {_COLUMNAS_USUARIO} FROM usuario u WHERE u.usuario_id = :u", {"u": usuario_id})
    return _usuario_admin(fila) if fila else None


def roles_de_usuarios(conn: Any, org_id: str | None) -> dict[str, dict[str, list]]:
    """``{usuario_id: {"org": [rol], "pais": [(cliente_id, rol)]}}`` de todos los usuarios (de una org o de todas).

    A diferencia de `membresias_pais`, incluye también los países dados de baja: para administrar hay que verlos."""
    filtro, binds = ("", {}) if org_id is None else ("WHERE u.org_id = :o ", {"o": org_id})
    salida: dict[str, dict[str, list]] = {}
    for uid, rol in _todos(conn, "SELECT mo.usuario_id, mo.rol FROM membresia_org mo "
                                 f"JOIN usuario u ON u.usuario_id = mo.usuario_id {filtro}ORDER BY mo.rol", binds):
        salida.setdefault(uid, {"org": [], "pais": []})["org"].append(rol)
    for uid, cliente, rol in _todos(conn, "SELECT m.usuario_id, m.cliente_id, m.rol FROM membresia m "
                                          f"JOIN usuario u ON u.usuario_id = m.usuario_id {filtro}"
                                          "ORDER BY m.cliente_id", binds):
        salida.setdefault(uid, {"org": [], "pais": []})["pais"].append((cliente, rol))
    return salida


def membresias_pais_todas(conn: Any, usuario_id: str) -> list[tuple[str, str]]:
    """``(cliente_id, rol)`` de TODAS sus membresías de país, también las de países dados de baja."""
    return [(f[0], f[1]) for f in _todos(
        conn, "SELECT cliente_id, rol FROM membresia WHERE usuario_id = :u ORDER BY cliente_id", {"u": usuario_id})]


def actualizar_nombre(conn: Any, usuario_id: str, nombre: str | None) -> bool:
    return _ejecutar(conn, "UPDATE usuario SET nombre = :n WHERE usuario_id = :u",
                     {"n": nombre, "u": usuario_id}) > 0


def reactivar_usuario(conn: Any, usuario_id: str) -> bool:
    return _ejecutar(conn, "UPDATE usuario SET activo = 1 WHERE usuario_id = :u AND activo = 0",
                     {"u": usuario_id}) > 0


def cambiar_rol(conn: Any, usuario_id: str, cliente_id: str, rol: str) -> bool:
    """Cambia el rol de una membresía de país (``membresia``)."""
    return _ejecutar(conn, "UPDATE membresia SET rol = :r WHERE usuario_id = :u AND cliente_id = :c",
                     {"r": rol, "u": usuario_id, "c": cliente_id}) > 0


def cambiar_rol_org(conn: Any, usuario_id: str, org_id: str, rol: str) -> bool:
    """Cambia el rol de organización (``membresia_org`` admite uno por persona y organización)."""
    return _ejecutar(conn, "UPDATE membresia_org SET rol = :r WHERE usuario_id = :u AND org_id = :o",
                     {"r": rol, "u": usuario_id, "o": org_id}) > 0


def baja_membresia(conn: Any, usuario_id: str, cliente_id: str) -> bool:
    return _ejecutar(conn, "DELETE FROM membresia WHERE usuario_id = :u AND cliente_id = :c",
                     {"u": usuario_id, "c": cliente_id}) > 0


def baja_membresia_org(conn: Any, usuario_id: str, org_id: str) -> bool:
    return _ejecutar(conn, "DELETE FROM membresia_org WHERE usuario_id = :u AND org_id = :o",
                     {"u": usuario_id, "o": org_id}) > 0


def reiniciar_mfa(conn: Any, usuario_id: str) -> bool:
    """Borra el TOTP y sus códigos de recuperación: en el próximo acceso la persona se enrola de nuevo y recibe
    códigos nuevos (el admin nunca los ve)."""
    hecho = _ejecutar(conn, "UPDATE usuario SET mfa_totp_secret = NULL, mfa_confirmado_en = NULL, "
                            "mfa_ultimo_paso = NULL WHERE usuario_id = :u", {"u": usuario_id}) > 0
    reemplazar_recuperacion(conn, usuario_id, [])
    return hecho


# Sesiones: el id que se muestra y se recibe es el PREFIJO de 16 caracteres del hash (nunca el token ni el hash completo).
PREFIJO_SESION = 16


def listar_sesiones_vigentes(conn: Any, usuario_id: str) -> list[dict[str, Any]]:
    filas = _todos(
        conn,
        "SELECT SUBSTR(sesion_id, 1, :n), creado_en, expira_en, mfa_ok, user_agent FROM sesion "
        "WHERE usuario_id = :u AND revocada = 0 AND expira_en > SYSTIMESTAMP ORDER BY creado_en DESC",
        {"n": PREFIJO_SESION, "u": usuario_id},
    )
    return [{"id": f[0], "creado_en": f[1], "expira_en": f[2], "mfa_ok": bool(f[3]), "user_agent": f[4] or ""}
            for f in filas]


def revocar_sesion_es_de(conn: Any, usuario_id: str, prefijo: str) -> bool:
    """Revoca UNA sesión viva del usuario por su prefijo; si el prefijo casara con varias no toca ninguna."""
    if len(prefijo) != PREFIJO_SESION or not all(c in "0123456789abcdef" for c in prefijo):
        return False
    fila = _uno(conn, "SELECT COUNT(*) FROM sesion WHERE usuario_id = :u AND revocada = 0 "
                      "AND SUBSTR(sesion_id, 1, :n) = :p", {"u": usuario_id, "n": PREFIJO_SESION, "p": prefijo})
    if not fila or fila[0] != 1:
        return False
    return _ejecutar(conn, "UPDATE sesion SET revocada = 1 WHERE usuario_id = :u AND revocada = 0 "
                           "AND SUBSTR(sesion_id, 1, :n) = :p",
                     {"u": usuario_id, "n": PREFIJO_SESION, "p": prefijo}) > 0


# Invitaciones (`invitacion`). Sin columna de país: un rol de país viaja como ``rol:cliente`` en `rol_propuesto`.

def crear_invitacion(conn: Any, invitacion_id: str, org_id: str, email: str, rol_propuesto: str,
                     invitado_por: str, dias: int) -> None:
    _ejecutar(conn, "INSERT INTO invitacion (invitacion_id, org_id, email, rol_propuesto, invitado_por, expira_en) "
                    "VALUES (:i, :o, :e, :r, :p, SYSTIMESTAMP + NUMTODSINTERVAL(:d, 'DAY'))",
              {"i": invitacion_id, "o": org_id, "e": email.strip().lower(), "r": rol_propuesto,
               "p": invitado_por, "d": dias})


_COLUMNAS_INVITACION = (
    "invitacion_id, org_id, email, rol_propuesto, invitado_por, "
    "CASE WHEN estado = 'PENDIENTE' AND expira_en <= SYSTIMESTAMP THEN 'EXPIRADA' ELSE estado END, "
    "creado_en, expira_en"
)


def _invitacion(f: tuple) -> dict[str, Any]:
    return {"invitacion_id": f[0], "org_id": f[1], "email": f[2], "rol_propuesto": f[3], "invitado_por": f[4],
            "estado": f[5], "creado_en": f[6], "expira_en": f[7]}


def listar_invitaciones(conn: Any, org_id: str | None) -> list[dict[str, Any]]:
    filtro, binds = ("", {}) if org_id is None else ("WHERE org_id = :o ", {"o": org_id})
    filas = _todos(conn, f"SELECT {_COLUMNAS_INVITACION} FROM invitacion {filtro}ORDER BY creado_en DESC", binds)
    return [_invitacion(f) for f in filas]


def obtener_invitacion(conn: Any, invitacion_id: str) -> dict[str, Any] | None:
    fila = _uno(conn, f"SELECT {_COLUMNAS_INVITACION} FROM invitacion WHERE invitacion_id = :i",
                {"i": invitacion_id})
    return _invitacion(fila) if fila else None


def invitaciones_pendientes_por_email(conn: Any, email: str) -> list[dict[str, Any]]:
    """Las invitaciones VIGENTES (pendientes y no vencidas) de ese correo, de cualquier organización."""
    filas = _todos(conn, f"SELECT {_COLUMNAS_INVITACION} FROM invitacion WHERE LOWER(email) = :e "
                         "AND estado = 'PENDIENTE' AND expira_en > SYSTIMESTAMP ORDER BY creado_en",
                   {"e": email.strip().lower()})
    return [_invitacion(f) for f in filas]


def cambiar_estado_invitacion(conn: Any, invitacion_id: str, desde: str, hacia: str) -> bool:
    """Atómico: solo transita si sigue en ``desde`` y, al aceptar, no venció (de dos canjes gana uno)."""
    vigencia = " AND expira_en > SYSTIMESTAMP" if hacia == "ACEPTADA" else ""
    return _ejecutar(conn, f"UPDATE invitacion SET estado = :h WHERE invitacion_id = :i AND estado = :d{vigencia}",
                     {"h": hacia, "i": invitacion_id, "d": desde}) > 0
