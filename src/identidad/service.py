"""src.identidad.service — casos de uso de identidad, sin HTTP (plan de identidad, Entrega 2).

Reciben una conexión ya abierta y NO hacen commit: quien llama (CLI o ruta web) decide la
transacción, de modo que el alta de un usuario y su auditoría entran o no entran juntas.
Ningún mensaje de error ni log lleva un código o un token (T12).
"""
from __future__ import annotations

import secrets
from typing import Any

from src.identidad import codigos, politica, repo, sesiones


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
    repo.crear_sesion(conn, sesiones.hash_token(token), uid, cfg.sesion_ttl_horas, ip_hash, user_agent)
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
