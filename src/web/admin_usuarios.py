"""src.web.admin_usuarios — administración de usuarios, membresías, invitaciones, sesiones y MFA (Fase G1).

Reemplaza al CLI `python -m src.identidad` (que queda como vía de emergencia). Todo cuelga de `/portal/admin/users` y
`/portal/admin/invitations`. Las GET usan `acceso_admin` (404 uniforme a quien no administra); las escrituras pasan por
`admin.escritura_admin`: acceso → CSRF + Origin + límite → «ver como» rechazado → permiso → servicio → auditoría → 303.
Las reglas (alcance por organización, quién puede otorgar qué, no dejarse sin acceso a uno mismo) viven en
`src.identidad.service`; acá solo se leen formularios y se pintan las pantallas.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request

from src.identidad import politica, service
from src.identidad import repo as identidad_repo
from src.memoria import db
from src.web import feedback, rutas
from src.web.admin import acceso_admin, contexto_admin, escritura_admin
from src.web.plantillas import pagina
from src.web.portal import _sin_datos

logger = logging.getLogger(__name__)

router = APIRouter()

# Hasta que `politica.ACCIONES_AUDITADAS` traiga USUARIO_EDITADO (SOLICITUD DE INTEGRACIÓN de G1) la edición del
# nombre se registra como USUARIO_ALTA con `campo=nombre` en el recurso; después usa la acción propia sin tocar acá.
ACCION_EDITAR_NOMBRE = "USUARIO_EDITADO" if "USUARIO_EDITADO" in politica.ACCIONES_AUDITADAS else "USUARIO_ALTA"

_MAX_CAMPO = 300  # tope de lo que se lee de un campo de texto antes de pasarlo al servicio


def _txt(valor: str) -> str:
    return (valor or "")[:_MAX_CAMPO]


def _url_usuario(usuario_id: str) -> str:
    return f"{rutas.ADMIN_USERS}/{quote(usuario_id, safe='')}"


def _roles_asignables(alcance) -> tuple[list[str], list[str]]:
    org = sorted(r for r in politica.ROLES_ORG if service.puede_otorgar(alcance, r))
    return org, sorted(politica.ROLES_PAIS)


def _escribir(request: Request, csrf: str, ambito: str, accion: str, recurso: str, servicio, destino: str,
              plataforma: bool = False):
    """``servicio(conn, alcance)`` → resultado; el resto es `escritura_admin`."""
    return escritura_admin(request, csrf, ambito, accion, recurso, lambda conn, alcance, ip: servicio(conn, alcance),
                           destino=destino, plataforma=plataforma)


# ── Usuarios: lista, alta, ficha ───────────────────────────────────────────────

@router.get(rutas.ADMIN_USERS)
def lista(request: Request):
    alcance, respuesta = acceso_admin(request)
    if respuesta is not None:
        return respuesta
    org = None if alcance.es_plataforma else alcance.org_id
    try:
        with db.connection(request.app.state.cfg) as conn:
            usuarios = identidad_repo.listar_usuarios(conn, org)
            roles = identidad_repo.roles_de_usuarios(conn, org)
            clientes = identidad_repo.listar_clientes(conn, alcance.org_id)
            organizaciones = identidad_repo.listar_organizaciones(conn) if alcance.es_plataforma else []
    except Exception:
        logger.exception("admin usuarios: no se pudo listar")
        return _sin_datos(request)
    if not alcance.es_plataforma:  # un admin_organizacion no ve (ni toca) a los admin_plataforma
        usuarios = [u for u in usuarios if "admin_plataforma" not in roles.get(u["usuario_id"], {}).get("org", [])]
    roles_org, roles_pais = _roles_asignables(alcance)
    return pagina(request, "portal_admin_usuarios.html", **contexto_admin(request, alcance, "admin"),
                  usuarios=usuarios, roles=roles, clientes=clientes, organizaciones=organizaciones,
                  roles_org=roles_org, roles_pais=roles_pais, org_actual=alcance.org_id,
                  usuario_actual=alcance.usuario_id, R_USERS=rutas.ADMIN_USERS, R_INVITATIONS=rutas.ADMIN_INVITATIONS)


@router.post(rutas.ADMIN_USERS + "/create")
def crear(request: Request, email: str = Form(""), nombre: str = Form(""), rol: str = Form(""),
          cliente: str = Form(""), org: str = Form(""), csrf: str = Form("")):
    email, nombre, rol, cliente, org = _txt(email), _txt(nombre), _txt(rol), _txt(cliente), _txt(org)
    return _escribir(
        request, csrf, "usuarios_alta", "USUARIO_ALTA", f"email={email.strip().lower()[:150]};rol={rol[:40]}",
        lambda conn, a: service.admin_crear_usuario(conn, a, email, nombre, rol, cliente, org), rutas.ADMIN_USERS)


@router.get(rutas.ADMIN_USERS + "/{usuario_id}")
def ficha(request: Request, usuario_id: str):
    alcance, respuesta = acceso_admin(request)
    if respuesta is not None:
        return respuesta
    try:
        with db.connection(request.app.state.cfg) as conn:
            usuario = service.usuario_visible(conn, alcance, usuario_id[:64])
            if usuario is None:
                return feedback.no_encontrado(request)
            en_org, en_pais = service.roles_de_usuario(conn, usuario["usuario_id"])
            sesiones = identidad_repo.listar_sesiones_vigentes(conn, usuario["usuario_id"])
            clientes = identidad_repo.listar_clientes(conn, usuario["org_id"])
    except Exception:
        logger.exception("admin usuarios: no se pudo leer la ficha")
        return _sin_datos(request)
    roles_org, roles_pais = _roles_asignables(alcance)
    propio = usuario["usuario_id"] == alcance.usuario_id
    exige_mfa = bool({*en_org, *(r for _, r in en_pais)} & politica.ROLES_MFA)
    return pagina(request, "portal_admin_usuarios_detalle.html", **contexto_admin(request, alcance, "admin"),
                  u=usuario, roles_org_actual=en_org, membresias=en_pais, sesiones=sesiones, clientes=clientes,
                  roles_org=roles_org, roles_pais=roles_pais, propio=propio, exige_mfa=exige_mfa,
                  puede_reiniciar_mfa=alcance.es_plataforma and not propio and exige_mfa and usuario["mfa"],
                  R_USERS=rutas.ADMIN_USERS, url_usuario=_url_usuario(usuario["usuario_id"]))


# ── Escrituras sobre un usuario ────────────────────────────────────────────────

def _sobre_usuario(request: Request, csrf: str, usuario_id: str, ambito: str, accion: str, extra: str, servicio,
                   plataforma: bool = False):
    usuario_id = usuario_id[:64]
    return _escribir(request, csrf, ambito, accion, f"usuario={usuario_id}{extra}"[:200],
                     lambda conn, a: servicio(conn, a, usuario_id), _url_usuario(usuario_id), plataforma)


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/name")
def editar_nombre(request: Request, usuario_id: str, nombre: str = Form(""), csrf: str = Form("")):
    nombre = _txt(nombre)
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_nombre", ACCION_EDITAR_NOMBRE, ";campo=nombre",
                          lambda conn, a, uid: service.admin_editar_nombre(conn, a, uid, nombre))


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/deactivate")
def baja(request: Request, usuario_id: str, csrf: str = Form("")):
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_baja", "USUARIO_BAJA", "", service.admin_baja_usuario)


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/reactivate")
def reactivar(request: Request, usuario_id: str, csrf: str = Form("")):
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_reactivar", "USUARIO_REACTIVADO", "",
                          service.admin_reactivar_usuario)


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/role")
def cambiar_rol(request: Request, usuario_id: str, rol: str = Form(""), cliente: str = Form(""),
                csrf: str = Form("")):
    rol, cliente = _txt(rol), _txt(cliente)
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_rol", "ROL_CAMBIADO",
                          f";rol={rol[:40]};cliente={cliente[:64]}",
                          lambda conn, a, uid: service.admin_cambiar_rol(conn, a, uid, cliente, rol))


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/country/add")
def agregar_pais(request: Request, usuario_id: str, rol: str = Form(""), cliente: str = Form(""),
                 csrf: str = Form("")):
    rol, cliente = _txt(rol), _txt(cliente)
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_pais_alta", "MEMBRESIA_ALTA",
                          f";rol={rol[:40]};cliente={cliente[:64]}",
                          lambda conn, a, uid: service.admin_agregar_pais(conn, a, uid, cliente, rol))


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/country/remove")
def quitar_pais(request: Request, usuario_id: str, cliente: str = Form(""), csrf: str = Form("")):
    cliente = _txt(cliente)
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_pais_baja", "MEMBRESIA_BAJA",
                          f";cliente={cliente[:64]}",
                          lambda conn, a, uid: service.admin_quitar_pais(conn, a, uid, cliente))


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/org-role/add")
def agregar_rol_org(request: Request, usuario_id: str, rol: str = Form(""), csrf: str = Form("")):
    rol = _txt(rol)
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_rolorg_alta", "MEMBRESIA_ALTA", f";rol={rol[:40]}",
                          lambda conn, a, uid: service.admin_agregar_rol_org(conn, a, uid, rol))


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/org-role/remove")
def quitar_rol_org(request: Request, usuario_id: str, csrf: str = Form("")):
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_rolorg_baja", "MEMBRESIA_BAJA", ";ambito=org",
                          service.admin_quitar_rol_org)


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/sessions/revoke")
def revocar_sesion(request: Request, usuario_id: str, sesion: str = Form(""), csrf: str = Form("")):
    sesion = _txt(sesion)
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_sesion", "SESION_REVOCADA_POR_ADMIN",
                          f";sesion={sesion[:16]}",
                          lambda conn, a, uid: service.admin_revocar_sesion(conn, a, uid, sesion))


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/sessions/revoke-all")
def cerrar_sesiones(request: Request, usuario_id: str, csrf: str = Form("")):
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_sesiones", "SESION_REVOCADA_POR_ADMIN", ";sesion=todas",
                          service.admin_cerrar_sesiones)


@router.post(rutas.ADMIN_USERS + "/{usuario_id}/mfa/reset")
def reiniciar_mfa(request: Request, usuario_id: str, csrf: str = Form("")):
    # Lo global: solo un admin_plataforma, y el servicio además rechaza que lo haga sobre sí mismo.
    return _sobre_usuario(request, csrf, usuario_id, "usuarios_mfa", "MFA_REINICIADO", "",
                          service.admin_reiniciar_mfa, plataforma=True)


# ── Invitaciones ───────────────────────────────────────────────────────────────

@router.get(rutas.ADMIN_INVITATIONS)
def invitaciones(request: Request):
    alcance, respuesta = acceso_admin(request)
    if respuesta is not None:
        return respuesta
    org = None if alcance.es_plataforma else alcance.org_id
    try:
        with db.connection(request.app.state.cfg) as conn:
            lista_inv = identidad_repo.listar_invitaciones(conn, org)
            clientes = identidad_repo.listar_clientes(conn, alcance.org_id)
            organizaciones = identidad_repo.listar_organizaciones(conn) if alcance.es_plataforma else []
    except Exception:
        logger.exception("admin invitaciones: no se pudo listar")
        return _sin_datos(request)
    roles_org, roles_pais = _roles_asignables(alcance)
    return pagina(request, "portal_admin_usuarios_invitaciones.html", **contexto_admin(request, alcance, "admin"),
                  invitaciones=lista_inv, clientes=clientes, organizaciones=organizaciones, roles_org=roles_org,
                  roles_pais=roles_pais, org_actual=alcance.org_id, dias=service.INVITACION_DIAS,
                  R_USERS=rutas.ADMIN_USERS, R_INVITATIONS=rutas.ADMIN_INVITATIONS)


@router.post(rutas.ADMIN_INVITATIONS + "/create")
def invitar(request: Request, email: str = Form(""), rol: str = Form(""), cliente: str = Form(""),
            org: str = Form(""), csrf: str = Form("")):
    email, rol, cliente, org = _txt(email), _txt(rol), _txt(cliente), _txt(org)
    return _escribir(
        request, csrf, "invitaciones_alta", "INVITACION_CREADA",
        f"email={email.strip().lower()[:150]};rol={rol[:40]}",
        lambda conn, a: service.admin_invitar(conn, a, email, rol, cliente, org), rutas.ADMIN_INVITATIONS)


@router.post(rutas.ADMIN_INVITATIONS + "/{invitacion_id}/revoke")
def revocar_invitacion(request: Request, invitacion_id: str, csrf: str = Form("")):
    invitacion_id = invitacion_id[:64]
    return _escribir(
        request, csrf, "invitaciones_revocar", "INVITACION_REVOCADA", f"invitacion={invitacion_id}",
        lambda conn, a: service.admin_revocar_invitacion(conn, a, invitacion_id), rutas.ADMIN_INVITATIONS)
