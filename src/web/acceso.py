"""src.web.acceso — acceso al portal sin contraseña (plan de identidad, Entrega 3).

    GET  /login           formulario de correo                                   (NO escribe)
    GET  /login/mfa       segundo factor TOTP (admin_plataforma y admin_organizacion) (NO escribe)
    POST /login/mfa       completa la sesión con el TOTP o un código de recuperación
    POST /login           si el correo tiene cuenta activa, emite y envía un código
    POST /login/code      canjea el código por una sesión (cookie) y redirige
    POST /logout          cierra la sesión

Mismas reglas que `src/web/feedback.py`: un GET jamás escribe, las rutas son `def` (oracledb es
síncrono) y nada secreto llega al log: ni el código, ni el token, ni la cookie (T12).

- **Sin enumeración de cuentas (T2):** `POST /login` responde la MISMA página con o sin cuenta. La
  búsqueda del usuario, la emisión y el envío van a `BackgroundTasks`, así la latencia del SMTP no
  delata nada.
- **CSRF (T3):** token firmado + `Origin`/`Referer` contra `FEEDBACK_BASE_URL` (ver `csrf.py`).
- **Cookie (T4):** `__Host-amalia_sesion`, `Secure; HttpOnly; SameSite=Lax; Path=/`, sin `Domain`; el
  token es nuevo en cada login (anti-fijación) y en la base solo vive su hash.
- **Redirección (T11):** `next` (`?next=`) solo admite rutas internas del portal.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Form, Query, Request
from fastapi.responses import RedirectResponse

from src.identidad import service, sesiones
from src.memoria import db
from src.notifier import email_sender
from src.web import csrf, rutas
from src.web.feedback import no_encontrado
from src.web.plantillas import pagina
from src.web.sesion import (
    COOKIE,
    alcance_de,
    portal_con_identidad,
    token_de_request,
    token_pendiente_de_request,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_DESTINO_POR_DEFECTO = rutas.PORTAL
_MENSAJE_403 = "La solicitud no es válida o la página caducó. Vuelve a abrir el acceso e inténtalo de nuevo."
_MENSAJE_429 = "Demasiados intentos desde tu conexión. Espera unos minutos e inténtalo de nuevo."
_MENSAJE_503 = "No pudimos comunicarnos con el sistema. Inténtalo de nuevo en unos minutos."


def destino_seguro(valor: str | None) -> str:
    """Solo rutas internas del portal: nada de URLs absolutas ni `//host` (open redirect)."""
    v = (valor or "").strip()
    if v.startswith(rutas.PORTAL) and not v.startswith("//") and "\\" not in v and "://" not in v \
            and not any(c in v for c in "\r\n"):
        return v
    return _DESTINO_POR_DEFECTO


def _ip_hash(cfg, request: Request) -> str:
    """Hash con clave de la IP del cliente. `request.client` ya trae la IP real detrás de Caddy: uvicorn la
    saca de `X-Forwarded-For` solo si la conexión viene de un proxy confiable (`src/web/__main__.py`), así
    que una cabecera falsa de un cliente cualquiera no cambia el resultado."""
    ip = request.client.host if request.client else ""
    return hmac.new(cfg.identidad_secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _contexto_csrf(request: Request) -> str:
    token = token_de_request(request) or token_pendiente_de_request(request)
    return sesiones.hash_token(token) if token else "anon"


def csrf_para(request: Request) -> str:
    """Token CSRF para un formulario de esta petición (con o sin sesión)."""
    return csrf.emitir(request.app.state.cfg.identidad_secret, _contexto_csrf(request))


def _rechazo(request: Request, status: int, mensaje: str):
    return pagina(request, "feedback_error.html", status=status, titulo="No se pudo completar", mensaje=mensaje)


def verificar_post(request: Request, token_csrf: str, endpoint: str, clave_extra: str = "", limitador=None):
    """None si la petición puede seguir; si no, la respuesta de rechazo."""
    cfg = request.app.state.cfg
    if not csrf.origen_valido(cfg.feedback_base_url, request.headers) \
            or not csrf.verificar(cfg.identidad_secret, _contexto_csrf(request), token_csrf):
        logger.info("acceso: POST %s rechazado por CSRF", endpoint)
        return _rechazo(request, 403, _MENSAJE_403)
    if not (limitador or request.app.state.limitador).permitir(f"{endpoint}:{_ip_hash(cfg, request)}") or (
        clave_extra and not request.app.state.limitador_sesion.permitir(f"{endpoint}:{clave_extra}")
    ):
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


@router.get(rutas.LOGIN)
def formulario(request: Request, siguiente: str = Query("", alias=rutas.Q_NEXT)):
    if not portal_con_identidad(request.app.state.cfg):
        return no_encontrado(request)
    if alcance_de(request) is not None:
        return RedirectResponse(destino_seguro(siguiente), status_code=303)
    return pagina(request, "acceso_correo.html", csrf=csrf_para(request), siguiente=destino_seguro(siguiente))


@router.post(rutas.LOGIN)
def pedir_codigo(
    request: Request, background: BackgroundTasks,
    email: str = Form(""), csrf: str = Form(""), siguiente: str = Form(""),
):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    rechazo = verificar_post(request, csrf, "acceso")
    if rechazo is not None:
        return rechazo
    email = email.strip()[:255]
    background.add_task(_emitir_y_enviar, cfg, email)
    return pagina(request, "acceso_codigo.html", email=email, csrf=csrf_para(request),
                  siguiente=destino_seguro(siguiente), error=False)


@router.post(rutas.LOGIN_CODE)
def canjear_codigo(
    request: Request,
    email: str = Form(""), codigo: str = Form(""), csrf: str = Form(""), siguiente: str = Form(""),
):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    rechazo = verificar_post(request, csrf, "codigo")
    if rechazo is not None:
        return rechazo
    try:
        with db.connection(cfg) as conn:
            # `db.connection` hace commit al salir: también queda el intento fallido y su auditoría.
            token = service.verificar_codigo(
                conn, cfg, email.strip(), codigo, ip_hash=_ip_hash(cfg, request),
                user_agent=request.headers.get("user-agent"),
            )
            pendiente_mfa = bool(token) and service.mfa_pendiente(conn, token)
    except Exception:  # noqa: BLE001
        logger.exception("acceso: no se pudo verificar el código")
        return _rechazo(request, 503, _MENSAJE_503)
    if not token:
        # Correo inexistente, código errado, vencido o ya usado: la misma respuesta (T2).
        return pagina(request, "acceso_codigo.html", status=401, email=email.strip()[:255],
                      csrf=csrf_para(request), siguiente=destino_seguro(siguiente), error=True)
    # Con TOTP obligatorio (P4) la cookie es de una sesión PENDIENTE: no da acceso hasta el segundo factor.
    destino = destino_seguro(siguiente)
    respuesta = RedirectResponse(f"{rutas.LOGIN_MFA}?{rutas.Q_NEXT}={destino}" if pendiente_mfa else destino, status_code=303)
    respuesta.set_cookie(COOKIE, token, max_age=cfg.sesion_max_dias * 86400, path="/",
                         secure=True, httponly=True, samesite="lax")
    return respuesta


def _mfa_ctx(request: Request, cfg, token: str, siguiente: str, **extra):
    """Contexto de la página del TOTP: enrolamiento (primera vez) o solo el código."""
    with db.connection(cfg) as conn:
        info = service.iniciar_mfa(conn, cfg, token)
    if info is None:
        return None
    return dict(info, csrf=csrf_para(request), siguiente=destino_seguro(siguiente), error=False, **extra)


@router.get(rutas.LOGIN_MFA)
def formulario_mfa(request: Request, siguiente: str = Query("", alias=rutas.Q_NEXT)):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    token = token_pendiente_de_request(request)
    if token is None:
        return RedirectResponse(rutas.LOGIN, status_code=303)
    try:
        ctx = _mfa_ctx(request, cfg, token, siguiente)
    except Exception:  # noqa: BLE001
        logger.exception("acceso: no se pudo preparar el TOTP")
        return _rechazo(request, 503, _MENSAJE_503)
    if ctx is None:
        return RedirectResponse(rutas.LOGIN, status_code=303)
    return pagina(request, "acceso_mfa.html", **ctx)


@router.post(rutas.LOGIN_MFA)
def verificar_mfa(request: Request, codigo: str = Form(""), csrf: str = Form(""), siguiente: str = Form("")):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    token = token_pendiente_de_request(request)
    if token is None:
        return RedirectResponse(rutas.LOGIN, status_code=303)
    rechazo = verificar_post(request, csrf, "mfa", clave_extra=sesiones.hash_token(token))
    if rechazo is not None:
        return rechazo
    try:
        with db.connection(cfg) as conn:
            resultado = service.verificar_mfa(conn, cfg, token, codigo, ip_hash=_ip_hash(cfg, request))
        if resultado is None:
            ctx = _mfa_ctx(request, cfg, token, siguiente)
    except Exception:  # noqa: BLE001
        logger.exception("acceso: no se pudo verificar el TOTP")
        return _rechazo(request, 503, _MENSAJE_503)
    if resultado is None:
        if ctx is None:
            return RedirectResponse(rutas.LOGIN, status_code=303)
        return pagina(request, "acceso_mfa.html", status=401, **dict(ctx, error=True))
    destino = destino_seguro(siguiente)
    if resultado["recuperacion"]:
        # Única vez que se ven en claro. La sesión ya es completa: el siguiente paso es seguir.
        return pagina(request, "acceso_recuperacion.html", codigos=resultado["recuperacion"], destino=destino)
    return RedirectResponse(destino, status_code=303)


@router.post(rutas.LOGOUT)
def salir(request: Request, csrf: str = Form("")):
    cfg = request.app.state.cfg
    if not portal_con_identidad(cfg):
        return no_encontrado(request)
    token = token_de_request(request)
    if token is None:
        return RedirectResponse(rutas.LOGIN, status_code=303)
    rechazo = verificar_post(request, csrf, "salir")
    if rechazo is not None:
        return rechazo
    try:
        with db.connection(cfg) as conn:
            service.cerrar_sesion(conn, token)
    except Exception:  # noqa: BLE001
        logger.exception("acceso: no se pudo cerrar la sesión")
        return _rechazo(request, 503, _MENSAJE_503)
    respuesta = RedirectResponse(rutas.LOGIN, status_code=303)
    respuesta.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return respuesta
