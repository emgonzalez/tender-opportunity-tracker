"""src.web.acceso — acceso al portal sin contraseña (plan de identidad, Entrega 3).

    GET  /acceso          formulario de correo                                   (NO escribe)
    POST /acceso          si el correo tiene cuenta activa, emite y envía un código
    POST /acceso/codigo   canjea el código por una sesión (cookie) y redirige
    POST /salir           cierra la sesión

Mismas reglas que `src/web/feedback.py`: un GET jamás escribe, las rutas son `def` (oracledb es
síncrono) y nada secreto llega al log: ni el código, ni el token, ni la cookie (T12).

- **Sin enumeración de cuentas (T2):** `POST /acceso` responde la MISMA página con o sin cuenta. La
  búsqueda del usuario, la emisión y el envío van a `BackgroundTasks`, así la latencia del SMTP no
  delata nada.
- **CSRF (T3):** token firmado + `Origin`/`Referer` contra `FEEDBACK_BASE_URL` (ver `csrf.py`).
- **Cookie (T4):** `__Host-amalia_sesion`, `Secure; HttpOnly; SameSite=Lax; Path=/`, sin `Domain`; el
  token es nuevo en cada login (anti-fijación) y en la base solo vive su hash.
- **Redirección (T11):** `siguiente` solo admite rutas internas del portal.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import RedirectResponse

from src.identidad import service, sesiones
from src.memoria import db
from src.notifier import email_sender
from src.web import csrf
from src.web.feedback import no_encontrado
from src.web.plantillas import pagina
from src.web.sesion import COOKIE, alcance_de, portal_con_identidad, token_de_request

logger = logging.getLogger(__name__)

router = APIRouter()

_DESTINO_POR_DEFECTO = "/portal"
_MENSAJE_403 = "La solicitud no es válida o la página caducó. Vuelve a abrir el acceso e inténtalo de nuevo."
_MENSAJE_429 = "Demasiados intentos desde tu conexión. Espera unos minutos e inténtalo de nuevo."
_MENSAJE_503 = "No pudimos comunicarnos con el sistema. Inténtalo de nuevo en unos minutos."


def destino_seguro(valor: str | None) -> str:
    """Solo rutas internas del portal: nada de URLs absolutas ni `//host` (open redirect)."""
    v = (valor or "").strip()
    if v.startswith("/portal") and not v.startswith("//") and "\\" not in v and "://" not in v \
            and not any(c in v for c in "\r\n"):
        return v
    return _DESTINO_POR_DEFECTO


def _ip_hash(cfg, request: Request) -> str:
    """Hash con clave de la IP del cliente. Detrás de Caddy la IP real viene en `X-Forwarded-For`
    (Caddy la reescribe; el contenedor web no se publica fuera de la red de Caddy)."""
    reenviado = request.headers.get("x-forwarded-for", "")
    ip = reenviado.split(",")[-1].strip() if reenviado else (request.client.host if request.client else "")
    return hmac.new(cfg.identidad_secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _contexto_csrf(request: Request) -> str:
    token = token_de_request(request)
    return sesiones.hash_token(token) if token else "anon"


def csrf_para(request: Request) -> str:
    """Token CSRF para un formulario de esta petición (con o sin sesión)."""
    return csrf.emitir(request.app.state.cfg.identidad_secret, _contexto_csrf(request))


def _rechazo(request: Request, status: int, mensaje: str):
    return pagina(request, "feedback_error.html", status=status, titulo="No se pudo completar", mensaje=mensaje)


def _verificar_post(request: Request, token_csrf: str, endpoint: str):
    """None si la petición puede seguir; si no, la respuesta de rechazo."""
    cfg = request.app.state.cfg
    if not csrf.origen_valido(cfg.feedback_base_url, request.headers) \
            or not csrf.verificar(cfg.identidad_secret, _contexto_csrf(request), token_csrf):
        logger.info("acceso: POST %s rechazado por CSRF", endpoint)
        return _rechazo(request, 403, _MENSAJE_403)
    if not request.app.state.limitador.permitir(f"{endpoint}:{_ip_hash(cfg, request)}"):
        logger.info("acceso: POST %s limitado por IP", endpoint)
        return _rechazo(request, 429, _MENSAJE_429)
    return None


def _emitir_y_enviar(cfg, email: str) -> None:
    """Todo lo que depende de que el correo tenga cuenta, fuera de la respuesta (T2)."""
    try:
        with db.connection(cfg) as conn:
            emitido = service.emitir_codigo(conn, cfg, email)
        if emitido:
            email_sender.send_access_code(cfg, emitido[0], emitido[1], cfg.codigo_ttl_min)
    except Exception:  # noqa: BLE001 — la página ya se respondió; jamás con el código en el log
        logger.exception("acceso: no se pudo emitir o enviar el código")


@router.get("/acceso")
def formulario(request: Request, siguiente: str = ""):
    if not portal_con_identidad(request.app.state.cfg):
        return no_encontrado(request)
    if alcance_de(request) is not None:
        return RedirectResponse(destino_seguro(siguiente), status_code=303)
    return pagina(request, "acceso_correo.html", csrf=csrf_para(request), siguiente=destino_seguro(siguiente))


@router.post("/acceso")
def pedir_codigo(
    request: Request, background: BackgroundTasks,
    email: str = Form(""), csrf: str = Form(""), siguiente: str = Form(""),
):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    rechazo = _verificar_post(request, csrf, "acceso")
    if rechazo is not None:
        return rechazo
    email = email.strip()[:255]
    background.add_task(_emitir_y_enviar, cfg, email)
    return pagina(request, "acceso_codigo.html", email=email, csrf=csrf_para(request),
                  siguiente=destino_seguro(siguiente), error=False)


@router.post("/acceso/codigo")
def canjear_codigo(
    request: Request,
    email: str = Form(""), codigo: str = Form(""), csrf: str = Form(""), siguiente: str = Form(""),
):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    rechazo = _verificar_post(request, csrf, "codigo")
    if rechazo is not None:
        return rechazo
    try:
        with db.connection(cfg) as conn:
            # `db.connection` hace commit al salir: también queda el intento fallido y su auditoría.
            token = service.verificar_codigo(
                conn, cfg, email.strip(), codigo, ip_hash=_ip_hash(cfg, request),
                user_agent=request.headers.get("user-agent"),
            )
    except Exception:  # noqa: BLE001
        logger.exception("acceso: no se pudo verificar el código")
        return _rechazo(request, 503, _MENSAJE_503)
    if not token:
        # Correo inexistente, código errado, vencido o ya usado: la misma respuesta (T2).
        return pagina(request, "acceso_codigo.html", status=401, email=email.strip()[:255],
                      csrf=csrf_para(request), siguiente=destino_seguro(siguiente), error=True)
    respuesta = RedirectResponse(destino_seguro(siguiente), status_code=303)
    respuesta.set_cookie(COOKIE, token, max_age=cfg.sesion_max_dias * 86400, path="/",
                         secure=True, httponly=True, samesite="lax")
    return respuesta


@router.post("/salir")
def salir(request: Request, csrf: str = Form("")):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    token = token_de_request(request)
    if token is None:
        return RedirectResponse("/acceso", status_code=303)
    rechazo = _verificar_post(request, csrf, "salir")
    if rechazo is not None:
        return rechazo
    try:
        with db.connection(cfg) as conn:
            service.cerrar_sesion(conn, token)
    except Exception:  # noqa: BLE001
        logger.exception("acceso: no se pudo cerrar la sesión")
        return _rechazo(request, 503, _MENSAJE_503)
    respuesta = RedirectResponse("/acceso", status_code=303)
    respuesta.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return respuesta
