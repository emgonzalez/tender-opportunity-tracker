"""src.web.sesion — resuelve la cookie de sesión a un `Alcance` (plan de identidad, Entrega 3).

Middleware ASGI puro, como los de `seguridad.py`. Solo RESUELVE: deja el alcance en
``scope["state"]["alcance"]`` (o nada) y no bloquea nada. El bloqueo lo hace cada ruta con
`alcance_de(request)`, de modo que `/f/*` y `/healthz` siguen funcionando sin sesión.

Una sesión que pasó el correo pero no el TOTP NO tiene alcance: solo queda marcada como pendiente
(`sesion_pendiente_mfa`) para que `/login/mfa` pueda completarla.

Solo actúa sobre `/portal*`, `/login*` y `/logout`, y solo con el portal encendido y el secreto
configurado: en el resto del servicio no toca la base. La sesión se desliza (`renovar_sesion`).

«Ver como» (E10, `docs/planes/portal-fase3-identidad.md`): ``state["alcance"]`` es el alcance EFECTIVO
(el simulado, si quien tiene la sesión es de `ROLES_VER_COMO` y eligió una vista vigente) y
``state["alcance_real"]`` el de la persona. Así las rutas existentes muestran lo simulado sin cambios, y
lo que autoriza la simulación misma se decide con el real (`alcance_real_de`). Cualquier falla al leer la
simulación (p. ej. una imagen nueva con el DDL aún sin aplicar) cae a la vista real: nunca rompe el login.
"""
from __future__ import annotations

import logging
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any, NamedTuple

from starlette.concurrency import run_in_threadpool

from src.identidad import alcance as alcance_mod
from src.identidad import politica, repo, service, sesiones
from src.memoria import db
from src.web import rutas

logger = logging.getLogger(__name__)

COOKIE = "__Host-amalia_sesion"
_PREFIJOS = (rutas.PORTAL, rutas.LOGIN, rutas.LOGOUT)


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
    """El `Alcance` EFECTIVO de la persona con sesión (el simulado, si está en «ver como»), o None.
    Lo usan las rutas que exigen sesión."""
    return request.scope.get("state", {}).get("alcance")


def alcance_real_de(request):
    """El `Alcance` de la persona tal cual, ignorando «ver como». Lo que autoriza la simulación se
    decide con este, nunca con el efectivo (un alcance simulado no puede elegir otra simulación)."""
    estado = request.scope.get("state", {})
    return estado.get("alcance_real", estado.get("alcance"))


def ver_como_de(request) -> tuple[str | None, datetime | None]:
    """``(cliente, desde)`` de la simulación vigente; ``(None, None)`` sin simulación."""
    estado = request.scope.get("state", {})
    return estado.get("ver_como_cliente"), estado.get("ver_como_desde")


def token_de_request(request) -> str | None:
    return request.scope.get("state", {}).get("sesion_token")


def token_pendiente_de_request(request) -> str | None:
    """Token de una sesión que pasó el código del correo y espera el TOTP."""
    return request.scope.get("state", {}).get("sesion_pendiente_mfa")


def _pendiente(cfg, token: str) -> bool:
    with db.connection(cfg) as conn:
        return service.mfa_pendiente(conn, token)


class Resolucion(NamedTuple):
    """Lo que deja el middleware: el alcance efectivo, el real y, si hay simulación, su país y su inicio."""
    efectivo: Any
    real: Any
    cliente: str | None = None
    desde: datetime | None = None


def _simulacion(conn, token: str, real) -> Resolucion:
    """Aplica «ver como» si corresponde. Nunca levanta: ante cualquier falla, la vista real."""
    if not (getattr(real, "roles", frozenset()) & politica.ROLES_VER_COMO):
        return Resolucion(real, real)  # a quien no puede simular ni se le consulta (columnas pobladas a mano: ignoradas)
    try:
        vigente = repo.obtener_ver_como(conn, sesiones.hash_token(token), politica.TTL_VER_COMO_MIN)
        if vigente is None:
            return Resolucion(real, real)
        rol, cliente, desde = vigente
        simulado = alcance_mod.alcance_simulado(conn, real, rol, cliente)
    except Exception:  # noqa: BLE001 — p. ej. el DDL de E10 aún sin aplicar: el admin ve su vista real
        logger.exception("sesión: no se pudo resolver «ver como»; se usa la vista real")
        return Resolucion(real, real)
    if simulado is None:
        # Ya no es válida (dejó de ser admin, el país se desactivó...): se ignora, sin escribir en un GET.
        return Resolucion(real, real)
    return Resolucion(simulado, real, cliente, desde)


def _resolver(cfg, token: str) -> Resolucion | None:
    with db.connection(cfg) as conn:
        usuario_id = service.usuario_de_sesion(conn, token)
        if not usuario_id:
            return None
        repo.renovar_sesion(conn, sesiones.hash_token(token), cfg.sesion_ttl_horas, cfg.sesion_max_dias)
        real = alcance_mod.resolver_alcance(conn, usuario_id)
        return _simulacion(conn, token, real) if real is not None else None


class ResolverSesion:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(_PREFIJOS):
            cfg = scope["app"].state.cfg
            token = token_de(scope) if portal_con_identidad(cfg) else None
            if token:
                try:
                    resuelto = await run_in_threadpool(_resolver, cfg, token)
                except Exception:  # noqa: BLE001 — con la ADB caída, nadie queda "con sesión"
                    logger.exception("sesión: no se pudo resolver la cookie")
                    resuelto = None
                if resuelto is not None and not isinstance(resuelto, Resolucion):
                    resuelto = Resolucion(resuelto, resuelto)  # un `Alcance` a secas: sin simulación
                if resuelto is not None:
                    estado = scope.setdefault("state", {})
                    estado["alcance"] = resuelto.efectivo
                    estado["alcance_real"] = resuelto.real
                    estado["ver_como_cliente"] = resuelto.cliente
                    estado["ver_como_desde"] = resuelto.desde
                    estado["sesion_token"] = token
                else:
                    try:
                        if await run_in_threadpool(_pendiente, cfg, token):
                            scope.setdefault("state", {})["sesion_pendiente_mfa"] = token
                    except Exception:  # noqa: BLE001
                        logger.exception("sesión: no se pudo consultar el TOTP pendiente")
        await self.app(scope, receive, send)
