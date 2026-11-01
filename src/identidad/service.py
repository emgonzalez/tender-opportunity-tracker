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
        # G1: quien fue invitado y aún no tiene cuenta la obtiene al pedir su primer código (prueba el correo).
        usuario = canjear_invitacion(conn, email)
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


# ── Administración desde el portal (Fase G1) ───────────────────────────────
# Cada función recibe el `Alcance` de QUIEN opera (`actor`) y devuelve OK / NO_ENCONTRADO / INVALIDO / CONFLICTO
# (los mismos valores que `src.web.admin`, que audita la acción principal si el resultado es OK). El servicio
# revalida todo: los `<select>` y las rutas no son de fiar. Lo ajeno (otra organización, un admin_plataforma visto
# por un admin_organizacion) es NO_ENCONTRADO, que la web convierte en el 404 uniforme.

OK = "ok"
NO_ENCONTRADO = "no_encontrado"
INVALIDO = "invalido"
CONFLICTO = "conflicto"

INVITACION_DIAS = 7
_MAX_EMAIL_BYTES = 255
_MAX_NOMBRE_BYTES = 200
_MAX_ROL_PROPUESTO_BYTES = 40  # VARCHAR2 cuenta BYTES: `rol:cliente` tiene que caber en 40


def _correo_valido(email: str) -> bool:
    local, _, dominio = email.partition("@")
    return bool(local and "." in dominio and not any(c.isspace() for c in email)
                and email.count("@") == 1 and len(email.encode()) <= _MAX_EMAIL_BYTES)


def puede_otorgar(actor: Any, rol: str) -> bool:
    """Un admin_organizacion NO puede dar el rol de admin_plataforma."""
    return rol != "admin_plataforma" or bool(actor.es_plataforma)


def _roles_de(conn: Any, usuario_id: str) -> tuple[list[str], list[tuple[str, str]]]:
    return repo.roles_org(conn, usuario_id), repo.membresias_pais_todas(conn, usuario_id)


def _objetivo(conn: Any, actor: Any, usuario_id: str) -> dict[str, Any] | None:
    """El usuario sobre el que se opera, o None (⇒ 404) si no existe, es de otra organización o es un
    admin_plataforma visto por quien no lo es."""
    usuario = repo.obtener_usuario_admin(conn, usuario_id)
    if not usuario or (not actor.es_plataforma and usuario["org_id"] != actor.org_id):
        return None
    if not actor.es_plataforma and "admin_plataforma" in repo.roles_org(conn, usuario_id):
        return None
    return usuario


usuario_visible = _objetivo        # para las pantallas de solo lectura: mismo alcance que las escrituras
roles_de_usuario = _roles_de


def _se_queda_sin_admin(actor: Any, usuario_id: str, roles_resultantes: set[str]) -> bool:
    """Nadie se deja sin acceso de administración a sí mismo."""
    return usuario_id == actor.usuario_id and not (roles_resultantes & politica.ROLES_ADMIN)


def _destino_de_rol(conn: Any, actor: Any, org_id: str, rol: str, cliente_id: str) -> bool:
    """True si ``rol`` (+ ``cliente_id`` para un rol de país) es asignable por ``actor`` en ``org_id``."""
    if rol not in politica.ROLES or not puede_otorgar(actor, rol):
        return False
    if rol in politica.ROLES_PAIS:
        return bool(cliente_id) and repo.cliente_existe(conn, org_id, cliente_id)
    return not cliente_id


def _org_de_alta(conn: Any, actor: Any, org_id: str) -> str | None:
    org = org_id or actor.org_id
    if org == actor.org_id:
        return org
    return org if actor.es_plataforma and repo.organizacion_existe(conn, org) else None


def _nombre_limpio(nombre: str) -> str | None | bool:
    """``None`` si viene vacío; ``False`` si no cabe; si no, el nombre sin espacios sobrantes."""
    nombre = " ".join((nombre or "").split())
    if not nombre:
        return None
    return nombre if len(nombre.encode()) <= _MAX_NOMBRE_BYTES else False


def admin_crear_usuario(conn: Any, actor: Any, email: str, nombre: str, rol: str, cliente_id: str = "",
                        org_id: str = "") -> str:
    email = (email or "").strip().lower()
    org = _org_de_alta(conn, actor, org_id)
    nombre_ok = _nombre_limpio(nombre)
    if not org or not _correo_valido(email) or nombre_ok is False or not _destino_de_rol(conn, actor, org, rol, cliente_id):
        return INVALIDO
    if repo.obtener_usuario_por_email(conn, org, email):
        return CONFLICTO
    usuario_id = secrets.token_hex(16)
    repo.crear_usuario(conn, usuario_id, org, email, nombre_ok or None)
    if rol in politica.ROLES_PAIS:
        repo.insertar_membresia(conn, usuario_id, cliente_id, rol)
    else:
        repo.insertar_membresia_org(conn, usuario_id, org, rol)
    repo.registrar_auditoria(conn, actor.org_id, "MEMBRESIA_ALTA", usuario_id=actor.usuario_id,
                             recurso=f"usuario={usuario_id};rol={rol};cliente={cliente_id or org}")
    return OK


def admin_editar_nombre(conn: Any, actor: Any, usuario_id: str, nombre: str) -> str:
    if _objetivo(conn, actor, usuario_id) is None:
        return NO_ENCONTRADO
    limpio = _nombre_limpio(nombre)
    if limpio is False:
        return INVALIDO
    repo.actualizar_nombre(conn, usuario_id, limpio)
    return OK


def admin_baja_usuario(conn: Any, actor: Any, usuario_id: str) -> str:
    usuario = _objetivo(conn, actor, usuario_id)
    if usuario is None:
        return NO_ENCONTRADO
    if usuario_id == actor.usuario_id:
        return INVALIDO  # nadie se da de baja a sí mismo
    if not usuario["activo"]:
        return CONFLICTO
    repo.desactivar_usuario(conn, usuario_id)
    repo.revocar_sesiones_de(conn, usuario_id)
    repo.invalidar_codigos_previos(conn, usuario_id)
    return OK


def admin_reactivar_usuario(conn: Any, actor: Any, usuario_id: str) -> str:
    usuario = _objetivo(conn, actor, usuario_id)
    if usuario is None:
        return NO_ENCONTRADO
    if usuario["activo"]:
        return CONFLICTO
    return OK if repo.reactivar_usuario(conn, usuario_id) else CONFLICTO


def _endurecer_si_exige_mfa(conn: Any, actor: Any, usuario_id: str, rol: str) -> None:
    """Quien recibe un rol con TOTP obligatorio pierde sus sesiones abiertas: la próxima entrada pasa por el MFA."""
    if rol in politica.ROLES_MFA and usuario_id != actor.usuario_id:
        repo.revocar_sesiones_de(conn, usuario_id)


def admin_cambiar_rol(conn: Any, actor: Any, usuario_id: str, cliente_id: str, rol: str) -> str:
    """Cambia el rol de la membresía de ``cliente_id``; con ``cliente_id`` vacío, el rol de organización."""
    usuario = _objetivo(conn, actor, usuario_id)
    if usuario is None:
        return NO_ENCONTRADO
    if not (rol in politica.ROLES_PAIS if cliente_id else rol in politica.ROLES_ORG) or not puede_otorgar(actor, rol):
        return INVALIDO
    en_org, en_pais = _roles_de(conn, usuario_id)
    if cliente_id:
        actual = dict(en_pais).get(cliente_id)
        resultantes = set(en_org) | {r for c, r in en_pais if c != cliente_id} | {rol}
    else:
        if not en_org:
            return NO_ENCONTRADO
        actual = en_org[0]
        resultantes = {rol} | {r for _, r in en_pais}
    if actual is None:
        return NO_ENCONTRADO
    if actual == rol:
        return CONFLICTO
    if _se_queda_sin_admin(actor, usuario_id, resultantes):
        return INVALIDO
    if cliente_id:
        cambio = repo.cambiar_rol(conn, usuario_id, cliente_id, rol)
    else:
        cambio = repo.cambiar_rol_org(conn, usuario_id, usuario["org_id"], rol)
    if not cambio:
        return CONFLICTO
    _endurecer_si_exige_mfa(conn, actor, usuario_id, rol)
    return OK


def admin_agregar_pais(conn: Any, actor: Any, usuario_id: str, cliente_id: str, rol: str) -> str:
    usuario = _objetivo(conn, actor, usuario_id)
    if usuario is None:
        return NO_ENCONTRADO
    if rol not in politica.ROLES_PAIS or not _destino_de_rol(conn, actor, usuario["org_id"], rol, cliente_id):
        return INVALIDO
    if cliente_id in dict(repo.membresias_pais_todas(conn, usuario_id)):
        return CONFLICTO
    repo.insertar_membresia(conn, usuario_id, cliente_id, rol)
    return OK


def admin_quitar_pais(conn: Any, actor: Any, usuario_id: str, cliente_id: str) -> str:
    if _objetivo(conn, actor, usuario_id) is None:
        return NO_ENCONTRADO
    if cliente_id not in dict(repo.membresias_pais_todas(conn, usuario_id)):
        return NO_ENCONTRADO
    return OK if repo.baja_membresia(conn, usuario_id, cliente_id) else CONFLICTO


def admin_agregar_rol_org(conn: Any, actor: Any, usuario_id: str, rol: str) -> str:
    usuario = _objetivo(conn, actor, usuario_id)
    if usuario is None:
        return NO_ENCONTRADO
    if rol not in politica.ROLES_ORG or not puede_otorgar(actor, rol):
        return INVALIDO
    if repo.roles_org(conn, usuario_id):
        return CONFLICTO  # `membresia_org` admite un rol por persona y organización: se cambia, no se suma
    repo.insertar_membresia_org(conn, usuario_id, usuario["org_id"], rol)
    _endurecer_si_exige_mfa(conn, actor, usuario_id, rol)
    return OK


def admin_quitar_rol_org(conn: Any, actor: Any, usuario_id: str) -> str:
    usuario = _objetivo(conn, actor, usuario_id)
    if usuario is None:
        return NO_ENCONTRADO
    en_org, en_pais = _roles_de(conn, usuario_id)
    if not en_org:
        return NO_ENCONTRADO
    if _se_queda_sin_admin(actor, usuario_id, {r for _, r in en_pais}):
        return INVALIDO
    return OK if repo.baja_membresia_org(conn, usuario_id, usuario["org_id"]) else CONFLICTO


# Invitaciones: `invitacion` no tiene columna de país, así que un rol de país viaja como ``rol:cliente``.

def _rol_propuesto(rol: str, cliente_id: str) -> str:
    return f"{rol}:{cliente_id}" if cliente_id else rol


def _separar_rol_propuesto(valor: str) -> tuple[str, str]:
    rol, _, cliente = valor.partition(":")
    return rol, cliente


def admin_invitar(conn: Any, actor: Any, email: str, rol: str, cliente_id: str = "", org_id: str = "") -> str:
    email = (email or "").strip().lower()
    org = _org_de_alta(conn, actor, org_id)
    propuesto = _rol_propuesto(rol, cliente_id)
    if (not org or not _correo_valido(email) or len(propuesto.encode()) > _MAX_ROL_PROPUESTO_BYTES
            or not _destino_de_rol(conn, actor, org, rol, cliente_id)):
        return INVALIDO
    if repo.obtener_usuario_por_email(conn, org, email):
        return CONFLICTO  # ya tiene cuenta: el rol se asigna desde su ficha
    if any(i["org_id"] == org and i["rol_propuesto"] == propuesto
           for i in repo.invitaciones_pendientes_por_email(conn, email)):
        return CONFLICTO
    repo.crear_invitacion(conn, secrets.token_hex(16), org, email, propuesto, actor.usuario_id, INVITACION_DIAS)
    return OK


def admin_revocar_invitacion(conn: Any, actor: Any, invitacion_id: str) -> str:
    inv = repo.obtener_invitacion(conn, invitacion_id)
    if not inv or (not actor.es_plataforma and inv["org_id"] != actor.org_id):
        return NO_ENCONTRADO
    if inv["estado"] != "PENDIENTE":
        return CONFLICTO
    return OK if repo.cambiar_estado_invitacion(conn, invitacion_id, "PENDIENTE", "REVOCADA") else CONFLICTO


def canjear_invitacion(conn: Any, email: str) -> dict[str, Any] | None:
    """Canje al primer acceso: si ese correo NO tiene cuenta y tiene invitaciones vigentes de UNA organización,
    crea la cuenta con los roles propuestos. Devuelve ``{"usuario_id", "org_id"}`` o None. Una invitación cuyo
    rol ya no es asignable (país borrado, rol que choca con uno ya concedido) se deja PENDIENTE."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    pendientes = repo.invitaciones_pendientes_por_email(conn, email)
    orgs = {i["org_id"] for i in pendientes}
    if len(orgs) != 1:
        return None
    org = next(iter(orgs))
    if repo.obtener_usuario_por_email(conn, org, email):
        return None  # con cuenta (activa o de baja): no se resucita por una invitación
    usuario_id: str | None = None
    for inv in pendientes:
        rol, cliente_id = _separar_rol_propuesto(inv["rol_propuesto"])
        if rol not in politica.ROLES or (rol in politica.ROLES_PAIS) != bool(cliente_id):
            continue
        if cliente_id and not repo.cliente_existe(conn, org, cliente_id):
            continue
        if rol in politica.ROLES_ORG and usuario_id and repo.roles_org(conn, usuario_id):
            continue
        if not repo.cambiar_estado_invitacion(conn, inv["invitacion_id"], "PENDIENTE", "ACEPTADA"):
            continue
        if usuario_id is None:
            usuario_id = secrets.token_hex(16)
            repo.crear_usuario(conn, usuario_id, org, email, None)
            repo.registrar_auditoria(conn, org, "USUARIO_ALTA", usuario_id=usuario_id, recurso=email)
        if rol in politica.ROLES_PAIS:
            repo.insertar_membresia(conn, usuario_id, cliente_id, rol)
        else:
            repo.insertar_membresia_org(conn, usuario_id, org, rol)
        repo.registrar_auditoria(conn, org, "INVITACION_USADA", usuario_id=usuario_id,
                                 recurso=f"invitacion={inv['invitacion_id']};rol={inv['rol_propuesto']}")
    return {"usuario_id": usuario_id, "org_id": org} if usuario_id else None


# Sesiones y MFA

def admin_revocar_sesion(conn: Any, actor: Any, usuario_id: str, sesion: str) -> str:
    if _objetivo(conn, actor, usuario_id) is None:
        return NO_ENCONTRADO
    return OK if repo.revocar_sesion_es_de(conn, usuario_id, (sesion or "").strip().lower()) else CONFLICTO


def admin_cerrar_sesiones(conn: Any, actor: Any, usuario_id: str) -> str:
    if _objetivo(conn, actor, usuario_id) is None:
        return NO_ENCONTRADO
    repo.revocar_sesiones_de(conn, usuario_id)
    return OK


def admin_reiniciar_mfa(conn: Any, actor: Any, usuario_id: str) -> str:
    """Borra el TOTP y los códigos de recuperación de un admin. Solo `admin_plataforma`, y nunca sobre sí mismo
    (si se pudiera, una sesión robada se saltaría el segundo factor). Cierra sus sesiones: tiene que enrolarse
    otra vez y recibe códigos de recuperación nuevos."""
    if not actor.es_plataforma:
        return NO_ENCONTRADO
    if _objetivo(conn, actor, usuario_id) is None:
        return NO_ENCONTRADO
    if usuario_id == actor.usuario_id:
        return INVALIDO
    en_org, en_pais = _roles_de(conn, usuario_id)
    if not ({*en_org, *(r for _, r in en_pais)} & politica.ROLES_MFA):
        return INVALIDO
    estado = repo.obtener_mfa(conn, usuario_id)
    if not estado["confirmado"] and not estado["secreto_cifrado"]:
        return CONFLICTO
    repo.reiniciar_mfa(conn, usuario_id)
    repo.revocar_sesiones_de(conn, usuario_id)
    repo.invalidar_codigos_previos(conn, usuario_id)
    return OK
