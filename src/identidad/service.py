"""src.identidad.service — casos de uso de identidad, sin HTTP (plan de identidad, Entrega 2).

Reciben una conexión ya abierta y NO hacen commit: quien llama (CLI o ruta web) decide la
transacción, de modo que el alta de un usuario y su auditoría entran o no entran juntas.
Ningún mensaje de error ni log lleva un código o un token (T12).
"""
from __future__ import annotations

import secrets
from typing import Any

from src.identidad import codigos, politica, repo, sesiones
from src.identidad import mfa as mfa_mod


class IdentidadError(ValueError):
    """Dato inválido en el alta: un error de quien opera, con mensaje legible."""


def alta_usuario(conn: Any, org_id: str, email: str, rol: str,
                 cliente_id: str | None = None, nombre: str | None = None) -> str:
    """Da de alta (o amplía) a una persona con un rol. Devuelve su ``usuario_id``.

    Un rol de organización va a `membresia_org` y no lleva país; ``gerente_pais`` y
    ``equipo_ventas`` van a `membresia` y exigen país. La misma persona puede sumar otro país.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        raise IdentidadError(f"correo inválido: {email!r}")
    if rol not in politica.ROLES:
        raise IdentidadError(f"rol desconocido: {rol!r} (válidos: {', '.join(sorted(politica.ROLES))})")
    if rol in politica.ROLES_PAIS:
        if not cliente_id:
            raise IdentidadError(f"el rol {rol} exige --cliente (es por país)")
        if not repo.cliente_existe(conn, org_id, cliente_id):
            raise IdentidadError(f"el cliente {cliente_id!r} no existe en la organización {org_id!r}")
    elif cliente_id:
        raise IdentidadError(f"el rol {rol} es de organización: no lleva --cliente")

    usuario = repo.obtener_usuario_por_email(conn, org_id, email)
    if usuario and not usuario["activo"]:
        raise IdentidadError(f"{email} está dado de baja; reactivarlo no está soportado todavía")
    if usuario:
        usuario_id = usuario["usuario_id"]
    else:
        usuario_id = secrets.token_hex(16)
        repo.crear_usuario(conn, usuario_id, org_id, email, nombre)
        repo.registrar_auditoria(conn, org_id, "USUARIO_ALTA", usuario_id=usuario_id, recurso=email)

    if rol in politica.ROLES_PAIS:
        repo.insertar_membresia(conn, usuario_id, cliente_id, rol)
    else:
        repo.insertar_membresia_org(conn, usuario_id, org_id, rol)
    repo.registrar_auditoria(conn, org_id, "MEMBRESIA_ALTA", usuario_id=usuario_id,
                             recurso=f"{rol}:{cliente_id or org_id}")
    return usuario_id


def baja_usuario(conn: Any, org_id: str, email: str) -> bool:
    """Desactiva a la persona y cierra todas sus sesiones. False si no existía o ya estaba de baja."""
    usuario = repo.obtener_usuario_por_email(conn, org_id, email)
    if not usuario or not usuario["activo"]:
        return False
    repo.desactivar_usuario(conn, usuario["usuario_id"])
    repo.revocar_sesiones_de(conn, usuario["usuario_id"])
    repo.invalidar_codigos_previos(conn, usuario["usuario_id"])
    repo.registrar_auditoria(conn, org_id, "USUARIO_BAJA", usuario_id=usuario["usuario_id"],
                             recurso=usuario["email"])
    return True


def _usuario_unico(conn: Any, email: str) -> dict[str, Any] | None:
    """El usuario activo de ese correo, o None si no hay o es ambiguo (no se adivina la organización)."""
    candidatos = repo.buscar_usuarios_activos_por_email(conn, email)
    return candidatos[0] if len(candidatos) == 1 else None


def emitir_codigo(conn: Any, cfg: Any, email: str) -> tuple[str, str] | None:
    """Genera un código para ese correo. Devuelve ``(email, codigo)`` para enviar, o None.

    None cubre tres casos que la web debe tratar IGUAL que un éxito (T2, sin enumeración de
    cuentas): correo sin cuenta activa, límite de códigos por hora alcanzado, o ambiguo.
    """
    usuario = _usuario_unico(conn, email)
    if not usuario:
        return None
    if repo.contar_codigos_recientes(conn, usuario["usuario_id"], 60) >= politica.MAX_CODIGOS_POR_HORA:
        return None
    repo.invalidar_codigos_previos(conn, usuario["usuario_id"])
    codigo = codigos.generar_codigo()
    repo.insertar_codigo(conn, usuario["usuario_id"],
                         codigos.hashear_codigo(cfg.identidad_secret, usuario["usuario_id"], codigo),
                         cfg.codigo_ttl_min)
    return email.strip().lower(), codigo


def verificar_codigo(conn: Any, cfg: Any, email: str, codigo: str,
                     ip_hash: str | None = None, user_agent: str | None = None) -> str | None:
    """Canjea el código por una sesión. Devuelve el TOKEN (para la cookie) o None.

    El token es lo único que sale en claro; en la base queda su sha256. Un acierto consume el
    código; el quinto fallo lo invalida.
    """
    usuario = _usuario_unico(conn, email)
    if not usuario:
        return None
    uid, org = usuario["usuario_id"], usuario["org_id"]
    vigente = repo.obtener_codigo_vigente(conn, uid)
    if not vigente:
        repo.registrar_auditoria(conn, org, "CODIGO_VENCIDO", usuario_id=uid, ip_hash=ip_hash)
        return None
    if not codigos.verificar_codigo(cfg.identidad_secret, uid, codigo, vigente["codigo_hash"]):
        repo.registrar_intento_fallido(conn, vigente["codigo_id"], politica.MAX_INTENTOS_CODIGO)
        repo.registrar_auditoria(conn, org, "LOGIN_FALLIDO", usuario_id=uid, ip_hash=ip_hash)
        return None
    if not repo.marcar_codigo_usado(conn, vigente["codigo_id"]):
        return None  # otra petición lo canjeó primero
    token = sesiones.nuevo_token()
    pendiente = requiere_mfa(conn, uid)
    repo.crear_sesion(conn, sesiones.hash_token(token), uid, cfg.sesion_ttl_horas, ip_hash, user_agent,
                      mfa_ok=not pendiente)
    if not pendiente:
        # Con TOTP pendiente el acceso se da (y se audita) al completar el segundo factor.
        repo.registrar_auditoria(conn, org, "LOGIN_OK", usuario_id=uid, ip_hash=ip_hash)
    return token


def usuario_de_sesion(conn: Any, token: str) -> str | None:
    return repo.obtener_usuario_de_sesion(conn, sesiones.hash_token(token))


def cerrar_sesion(conn: Any, token: str) -> bool:
    th = sesiones.hash_token(token)
    uid = repo.obtener_usuario_de_sesion(conn, th)
    if not repo.revocar_sesion(conn, th):
        return False
    usuario = repo.obtener_usuario(conn, uid) if uid else None
    if usuario:
        repo.registrar_auditoria(conn, usuario["org_id"], "SESION_REVOCADA", usuario_id=uid)
    return True


# ── Segundo factor TOTP (P4) ───────────────────────────────────────────────

def requiere_mfa(conn: Any, usuario_id: str) -> bool:
    roles = set(repo.roles_org(conn, usuario_id)) | {rol for _, rol in repo.membresias_pais(conn, usuario_id)}
    return bool(roles & politica.ROLES_MFA)


def mfa_pendiente(conn: Any, token: str) -> bool:
    """La sesión pasó el código del correo pero falta el TOTP."""
    return repo.obtener_usuario_de_sesion_pendiente(conn, sesiones.hash_token(token)) is not None


def iniciar_mfa(conn: Any, cfg: Any, token: str) -> dict[str, Any] | None:
    """Qué mostrar en el paso del TOTP. None si la sesión no está pendiente.

    Primera vez: genera (o recupera, si dejó la página a medias) el secreto y lo devuelve para
    registrarlo en la app. Después: solo pide el código."""
    uid = repo.obtener_usuario_de_sesion_pendiente(conn, sesiones.hash_token(token))
    if not uid:
        return None
    estado = repo.obtener_mfa(conn, uid)
    if estado["confirmado"]:
        return {"enrolamiento": False}
    secreto = (mfa_mod.descifrar(cfg.identidad_secret, estado["secreto_cifrado"])
               if estado["secreto_cifrado"] else None)
    if secreto is None:
        secreto = mfa_mod.nuevo_secreto()
        repo.guardar_secreto_mfa(conn, uid, mfa_mod.cifrar(cfg.identidad_secret, secreto))
    usuario = repo.obtener_usuario(conn, uid) or {}
    return {"enrolamiento": True, "secreto": mfa_mod.agrupar(secreto),
            "uri": mfa_mod.uri_aprovisionamiento(secreto, usuario.get("email", ""))}


def verificar_mfa(conn: Any, cfg: Any, token: str, codigo: str, ip_hash: str | None = None) -> dict[str, Any] | None:
    """Completa la sesión con un TOTP (o un código de recuperación). None si falla, por cualquier causa.

    Devuelve ``{"recuperacion": [...]}``: los códigos de recuperación NUEVOS, solo en el enrolamiento
    (únicas veces que se ven en claro)."""
    th = sesiones.hash_token(token)
    uid = repo.obtener_usuario_de_sesion_pendiente(conn, th)
    if not uid:
        return None
    org = (repo.obtener_usuario(conn, uid) or {}).get("org_id", "")

    def fallo() -> None:
        repo.registrar_auditoria(conn, org, "LOGIN_FALLIDO", usuario_id=uid, ip_hash=ip_hash)
        return None

    estado = repo.obtener_mfa(conn, uid)
    nuevos: list[str] = []
    recuperacion = False
    if estado["confirmado"] and mfa_mod.es_recuperacion(codigo):
        h = mfa_mod.hash_recuperacion(cfg.identidad_secret, uid, codigo)
        if not repo.consumir_recuperacion(conn, uid, h):
            return fallo()
        recuperacion = True
    else:
        secreto = (mfa_mod.descifrar(cfg.identidad_secret, estado["secreto_cifrado"])
                   if estado["secreto_cifrado"] else None)
        paso = mfa_mod.verificar(secreto, codigo) if secreto else None
        if paso is None:
            return fallo()
        if estado["confirmado"]:
            if not repo.registrar_paso_mfa(conn, uid, paso):
                return fallo()  # el mismo código no sirve dos veces
        else:
            if not repo.confirmar_mfa(conn, uid, paso):
                return fallo()
            nuevos = mfa_mod.generar_recuperacion()
            repo.reemplazar_recuperacion(
                conn, uid, [mfa_mod.hash_recuperacion(cfg.identidad_secret, uid, c) for c in nuevos])
            repo.registrar_auditoria(conn, org, "MFA_ENROLADO", usuario_id=uid, ip_hash=ip_hash)

    if not repo.completar_mfa(conn, th, cfg.sesion_ttl_horas):
        return fallo()
    if recuperacion:
        repo.registrar_auditoria(conn, org, "MFA_RECUPERACION_USADA", usuario_id=uid, ip_hash=ip_hash)
    repo.registrar_auditoria(conn, org, "LOGIN_OK", usuario_id=uid, ip_hash=ip_hash)
    return {"recuperacion": nuevos}
