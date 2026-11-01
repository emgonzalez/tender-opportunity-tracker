"""src.web.admin_clientes — administración de clientes (países) y de su perfil (Fase G2).

    GET  /portal/admin/clients                                   listado (+ alta, solo admin_plataforma)
    POST /portal/admin/clients/create                            alta: `cliente` + versión 1 de `cliente_config` (plataforma)
    GET  /portal/admin/clients/{id}                              ficha: datos, estado, secciones, portales (solo lectura)
    POST /portal/admin/clients/{id}/general                      nombre, locale y zona horaria
    POST /portal/admin/clients/{id}/activate|deactivate          activar o desactivar (plataforma)
    GET|POST /portal/admin/clients/{id}/profile/{seccion}        catalog | rules | thresholds | email | feedback
    GET  /portal/admin/clients/{id}/profile/intelligence         solo lectura: la genera el trainer
    GET  /portal/admin/clients/{id}/versions[?a=&b=]             historial y comparación de dos versiones
    POST /portal/admin/clients/{id}/versions/{n}/revert          versión nueva con el contenido de la n

Toda escritura pasa por `admin.escritura_admin` (acceso → CSRF+Origin+límite → «ver como» rechazado → permiso →
servicio → auditoría → 303). La lógica vive en `src.clientes.servicio`; acá solo hay HTTP. Lo ajeno (otra
organización, inexistente) es el 404 uniforme. Los campos de texto libre del perfil se muestran con el autoescape
de Jinja; nunca con `|safe`.

`cliente_portal` (credencial_ref, lookback_dias, horario_cron) se muestra de SOLO LECTURA: ningún código del
pipeline lo lee todavía (el portal de un cliente sale de `client.portal` del perfil), así que editarlo no tendría
efecto.
"""
from __future__ import annotations

import difflib
import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request

from src.clientes import repo, servicio
from src.memoria import db
from src.portales import PORTALES
from src.web import admin, feedback, rutas
from src.web.admin import acceso_admin, contexto_admin, escritura_admin
from src.web.plantillas import pagina
from src.web.portal import _sin_datos

logger = logging.getLogger(__name__)

router = APIRouter()

BASE = rutas.ADMIN_CLIENTS
_MAX_LINEAS_DIFF = 400

# El resultado «sin cambios» no existe en `admin` (G0): se registra su aviso acá, sin tocar ese archivo.
admin.AVISOS_ADMIN[servicio.SIN_CAMBIOS] = "admin_sin_cambios"
admin._TEXTOS_AVISO["admin_sin_cambios"] = ("ok", "No había cambios que guardar.")


def _url(cliente_id: str, *partes: str) -> str:
    return "/".join([BASE, quote(cliente_id, safe=""), *partes])


def _ficha(conn, alcance, cliente_id: str):
    """``(fila, versión, perfil)`` del cliente administrable, o None."""
    fila = servicio.visible(conn, alcance, cliente_id)
    if fila is None:
        return None
    activo = repo.activo(conn, cliente_id)
    if activo is None:
        return None
    return fila, activo[0], activo[1]


def _leer(request: Request, cliente_id: str | None, construir):
    """Esqueleto de las GET: acceso, conexión, 404 uniforme si `construir` devuelve None, 503 si la ADB falla."""
    alcance, respuesta = acceso_admin(request)
    if respuesta is not None:
        return respuesta
    try:
        with db.connection(request.app.state.cfg) as conn:
            if cliente_id is None:
                resultado = construir(conn, alcance, None)
            else:
                ficha = _ficha(conn, alcance, cliente_id)
                resultado = None if ficha is None else construir(conn, alcance, ficha)
    except Exception:
        logger.exception("admin clientes: no se pudo leer")
        return _sin_datos(request)
    if resultado is None:
        return feedback.no_encontrado(request)
    plantilla, contexto = resultado
    return pagina(request, plantilla, **contexto_admin(request, alcance), **contexto)


# ── Listado y alta ──────────────────────────────────────────────────────────────

@router.get(BASE)
def listado(request: Request):
    def construir(conn, alcance, _):
        return "portal_admin_clientes.html", {
            "clientes": servicio.listar(conn, alcance), "base": BASE,
            "organizaciones": repo.organizaciones(conn) if alcance.es_plataforma else [],
            "portales": [{"id": p.portal_id, "nombre": p.nombre} for p in PORTALES.values()],
        }
    return _leer(request, None, construir)


@router.post(BASE + "/create")
def crear(request: Request, csrf: str = Form(""), cliente_id: str = Form(""), nombre: str = Form(""),
          portal: str = Form(""), org_id: str = Form("")):
    def escribir(conn, alcance, ip_hash):
        return servicio.crear_cliente(conn, alcance, cliente_id, nombre, portal, org_id)

    return escritura_admin(request, csrf, "cliente_crear", "CLIENTE_CREADO", f"cliente={cliente_id[:64]}",
                           escribir, destino=BASE, plataforma=True)


# ── Ficha ───────────────────────────────────────────────────────────────────────

@router.get(BASE + "/{cliente_id}")
def ficha(request: Request, cliente_id: str):
    def construir(conn, alcance, f):
        fila, version, perfil = f
        cat = perfil.get("catalog") or {}
        categorias = cat.get("categories") or []
        return "portal_admin_clientes_detalle.html", {
            "c": fila, "version": version, "base": BASE, "url": _url(cliente_id),
            "secciones": servicio.SECCIONES, "portales_cliente": repo.portales_de(conn, cliente_id),
            "portal_perfil": (perfil.get("client") or {}).get("portal") or repo.PORTAL_DEFAULT,
            "resumen": {"categorias": len(categorias),
                        "marcas": sum(len(x.get("brands") or []) for x in categorias if isinstance(x, dict)),
                        "tags": len(cat.get("tag_taxonomy") or [])},
            "entrenado": bool((perfil.get("intelligence") or {}).get("profile")),
        }
    return _leer(request, cliente_id, construir)


@router.post(BASE + "/{cliente_id}/general")
def editar_general(request: Request, cliente_id: str, csrf: str = Form(""), nombre: str = Form(""),
                   locale: str = Form(""), zona_horaria: str = Form(""), version_base: str = Form("")):
    def escribir(conn, alcance, ip_hash):
        return servicio.editar_general(conn, alcance, cliente_id, nombre, locale, zona_horaria, version_base)

    return escritura_admin(request, csrf, "cliente_general", "PERFIL_VERSION_CREADA",
                           f"cliente={cliente_id[:64]} general", escribir, destino=_url(cliente_id))


@router.post(BASE + "/{cliente_id}/activate")
def activar(request: Request, cliente_id: str, csrf: str = Form(""), version_base: str = Form("")):
    def escribir(conn, alcance, ip_hash):
        return servicio.fijar_activo(conn, alcance, cliente_id, True, version_base)

    return escritura_admin(request, csrf, "cliente_activar", "CLIENTE_ACTIVADO", f"cliente={cliente_id[:64]}",
                           escribir, destino=_url(cliente_id), plataforma=True)


@router.post(BASE + "/{cliente_id}/deactivate")
def desactivar(request: Request, cliente_id: str, csrf: str = Form(""), version_base: str = Form("")):
    def escribir(conn, alcance, ip_hash):
        return servicio.fijar_activo(conn, alcance, cliente_id, False, version_base)

    return escritura_admin(request, csrf, "cliente_desactivar", "CLIENTE_DESACTIVADO", f"cliente={cliente_id[:64]}",
                           escribir, destino=_url(cliente_id), plataforma=True)


# ── Perfil por secciones ────────────────────────────────────────────────────────

@router.get(BASE + "/{cliente_id}/profile/{seccion}")
def perfil(request: Request, cliente_id: str, seccion: str):
    if seccion not in servicio.SECCIONES and seccion != servicio.SECCION_INTELIGENCIA:
        _, respuesta = acceso_admin(request)
        return respuesta if respuesta is not None else feedback.no_encontrado(request)

    def construir(conn, alcance, f):
        fila, version, datos = f
        solo_lectura = seccion == servicio.SECCION_INTELIGENCIA
        return "portal_admin_clientes_perfil.html", {
            "c": fila, "version": version, "seccion": seccion, "url": _url(cliente_id),
            "titulo_seccion": "Inteligencia histórica" if solo_lectura else servicio.SECCIONES[seccion],
            "solo_lectura": solo_lectura,
            "valores": servicio.inteligencia(datos) if solo_lectura else servicio.valores_formulario(seccion, datos),
        }
    return _leer(request, cliente_id, construir)


@router.post(BASE + "/{cliente_id}/profile/{seccion}")
async def guardar_perfil(request: Request, cliente_id: str, seccion: str):
    # Los campos dependen de la sección: se leen del formulario y el servicio aplica su lista cerrada de claves.
    form = await request.form()
    campos = {k: v for k, v in form.items() if isinstance(v, str) and k not in ("csrf", "version_base")}

    def escribir(conn, alcance, ip_hash):
        return servicio.editar_seccion(conn, alcance, cliente_id, seccion, campos, form.get("version_base", ""))

    return escritura_admin(request, str(form.get("csrf", "")), "cliente_perfil", "PERFIL_VERSION_CREADA",
                           f"cliente={cliente_id[:64]} seccion={seccion[:20]}", escribir,
                           destino=_url(cliente_id, "profile", seccion))


# ── Versiones ───────────────────────────────────────────────────────────────────

def _numero(valor: str | None) -> int | None:
    try:
        return int(valor) if valor is not None else None
    except ValueError:
        return None


def _sin_inteligencia(datos: dict | None) -> list[str]:
    if datos is None:
        return []
    visible_ = {k: v for k, v in datos.items() if k != "intelligence"}
    return json.dumps(visible_, ensure_ascii=False, indent=2, sort_keys=True, default=str).splitlines()


@router.get(BASE + "/{cliente_id}/versions")
def versiones(request: Request, cliente_id: str, a: str | None = None, b: str | None = None):
    def construir(conn, alcance, f):
        fila, version, _ = f
        diff, par = None, None
        na, nb = _numero(a), _numero(b)
        if na is not None and nb is not None:
            va, vb = repo.version(conn, cliente_id, na), repo.version(conn, cliente_id, nb)
            if va is None or vb is None:
                return None
            lineas = list(difflib.unified_diff(_sin_inteligencia(va), _sin_inteligencia(vb),
                                               f"versión {na}", f"versión {nb}", lineterm="", n=2))
            diff = {"lineas": lineas[:_MAX_LINEAS_DIFF], "truncado": len(lineas) > _MAX_LINEAS_DIFF,
                    "igual": not lineas}
            par = (na, nb)
        return "portal_admin_clientes_versiones.html", {
            "c": fila, "version": version, "url": _url(cliente_id), "versiones": repo.versiones(conn, cliente_id),
            "diff": diff, "par": par,
        }
    return _leer(request, cliente_id, construir)


@router.post(BASE + "/{cliente_id}/versions/{numero}/revert")
def revertir(request: Request, cliente_id: str, numero: str, csrf: str = Form(""), version_base: str = Form("")):
    def escribir(conn, alcance, ip_hash):
        return servicio.revertir(conn, alcance, cliente_id, numero, version_base)

    return escritura_admin(request, csrf, "cliente_revertir", "PERFIL_VERSION_REVERTIDA",
                           f"cliente={cliente_id[:64]} version={numero[:8]}", escribir,
                           destino=_url(cliente_id, "versions"))
