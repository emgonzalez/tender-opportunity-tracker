"""src.web.sesion — resuelve la cookie de sesión a un `Alcance` (plan de identidad, Entrega 3).

Middleware ASGI puro, como los de `seguridad.py`. Solo RESUELVE: deja el alcance en
``scope["state"]["alcance"]`` (o nada) y no bloquea nada. El bloqueo lo hace cada ruta con
`alcance_de(request)`, de modo que `/f/*` y `/healthz` siguen funcionando sin sesión.

Solo actúa sobre `/portal*`, `/acceso*` y `/salir`, y solo con el portal encendido y el secreto
configurado: en el resto del servicio no toca la base. La sesión se desliza (`renovar_sesion`).
"""
from __future__ import annotations

import logging
from http.cookies import SimpleCookie

from starlette.concurrency import run_in_threadpool

from src.identidad import alcance as alcance_mod
from src.identidad import repo, service, sesiones
from src.memoria import db

logger = logging.getLogger(__name__)

COOKIE = "__Host-amalia_sesion"
_PREFIJOS = ("/portal", "/acceso", "/salir")


def portal_con_identidad(cfg) -> bool:
    """Las rutas de acceso existen solo con el portal encendido, el secreto y la URL pública."""
    return bool(getattr(cfg, "enable_portal", False) and getattr(cfg, "identidad_secret", "")
                and getattr(cfg, "feedback_base_url", ""))


def token_de(scope) -> str | None:
    crudo = dict(scope.get("headers", [])).get(b"cookie", b"").decode("latin-1")
    if not crudo:
        return None
    try:
        jar = SimpleCookie()
        jar.load(crudo)
    except Exception:  # noqa: BLE001 — una cookie mal formada es "sin sesión", no un error
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel and morsel.value else None


def alcance_de(request):
    """El `Alcance` de la persona con sesión, o None. Lo usan las rutas que exigen sesión."""
    return request.scope.get("state", {}).get("alcance")


def token_de_request(request) -> str | None:
    return request.scope.get("state", {}).get("sesion_token")


def _resolver(cfg, token: str):
    with db.connection(cfg) as conn:
        usuario_id = service.usuario_de_sesion(conn, token)
        if not usuario_id:
            return None
        repo.renovar_sesion(conn, sesiones.hash_token(token), cfg.sesion_ttl_horas, cfg.sesion_max_dias)
        return alcance_mod.resolver_alcance(conn, usuario_id)


class ResolverSesion:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(_PREFIJOS):
            cfg = scope["app"].state.cfg
            token = token_de(scope) if portal_con_identidad(cfg) else None
            if token:
                try:
                    alcance = await run_in_threadpool(_resolver, cfg, token)
                except Exception:  # noqa: BLE001 — con la ADB caída, nadie queda "con sesión"
                    logger.exception("sesión: no se pudo resolver la cookie")
                    alcance = None
                if alcance is not None:
                    estado = scope.setdefault("state", {})
                    estado["alcance"] = alcance
                    estado["sesion_token"] = token
        await self.app(scope, receive, send)
