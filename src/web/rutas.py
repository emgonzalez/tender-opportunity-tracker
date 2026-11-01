"""src.web.rutas — URLs públicas en inglés, con las españolas siguiendo vivas (Fase U del plan de persistencia).

Única fuente de las rutas y de los nombres de query: handlers, redirects, `_nav`, plantillas (globales Jinja,
ver `GLOBALES_JINJA`) y los enlaces de los correos las leen de acá; no se escriben literales sueltos.

Mapa (lo que ya es una palabra inglesa se queda: `/portal`, `/portal/regional`, `/healthz`, `/f`, `/portal/static`):

    /login  /login/code  /login/mfa   ←  /acceso  /acceso/codigo  /acceso/mfa
    /logout                            ←  /salir
    /portal/operations                 ←  /portal/operativo
    /portal/theme                      ←  /portal/tema
    /portal/view-as                    ←  /portal/ver-como
    /portal/opportunity/{gid}/status|owner|recover  ←  /portal/oportunidad/{gid}/estado|responsable|recuperar
    /f/{token}/relevant|irrelevant     ←  /f/{token}/relevante|irrelevante
    ?client ?days ?day ?owner(=none) ?brand ?notice ?next  ←  ?cliente ?dias ?dia ?responsable(=sin) ?marca ?aviso ?siguiente

**Paso B (este):** los handlers se registran con la ruta NUEVA y todo lo que la app EMITE es nuevo. `TraduceRutas`
(el middleware más externo) es el alias de lo viejo:

- GET/HEAD a una ruta vieja → 308 a la nueva, con la query traducida (favoritos).
- POST a una ruta vieja → el MISMO handler, sin redirigir (un 3xx perdería el cuerpo del formulario; el CSRF no
  depende de la ruta, así que los formularios abiertos durante el despliegue siguen válidos).
- Query con nombres viejos en cualquier ruta → se traduce; si vienen el nuevo y el viejo, manda el nuevo. En un GET
  que solo cambia la query, además se redirige (308) para que la barra de direcciones quede en lo nuevo.
- `/f/{token}/relevante|irrelevante` es contrato firmado en correos ya enviados: su alias es PERMANENTE y se sirve
  directo (GET y POST), sin redirigir.

No cambian: las cookies `__Host-amalia_*` (renombrarlas cierra las sesiones), el ancla `#op-{gid}`, los estados del
pipeline, los nombres de campos de formulario ni los valores de `?notice=`.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode

# Ventana de retiro de los alias de `/login*`, `/logout` y `/portal/*` viejos, en días desde el despliegue del paso B.
# Documental: NO hay retiro automático todavía; cuando se decida retirarlos se quitan de `LEGADO`. Los
# `/f/…relevante|irrelevante` viejos NO están en esta ventana: son permanentes.
VENTANA_ALIAS_DIAS = 60

# --- Rutas nuevas (lo que la app registra y emite) -------------------------------------------------------------
LOGIN = "/login"
LOGIN_CODE = "/login/code"
LOGIN_MFA = "/login/mfa"
LOGOUT = "/logout"
PORTAL = "/portal"
PORTAL_REGIONAL = "/portal/regional"
PORTAL_OPERATIONS = "/portal/operations"
PORTAL_THEME = "/portal/theme"
PORTAL_VIEW_AS = "/portal/view-as"
# Fase G: administración. Cada recurso (users, clients…) cuelga de `ADMIN`; Caddy ya reenvía `/portal/*`.
ADMIN = "/portal/admin"
ADMIN_LEARNING = ADMIN + "/learning"
ADMIN_CLIENTS = ADMIN + "/clients"  # G2: clientes y perfiles
ADMIN_USERS = ADMIN + "/users"
ADMIN_INVITATIONS = ADMIN + "/invitations"
# Con `{proceso_gid}` para registrar el handler; `ruta_oportunidad` arma la URL de un proceso concreto.
OPORTUNIDAD_STATUS = "/portal/opportunity/{proceso_gid}/status"
OPORTUNIDAD_OWNER = "/portal/opportunity/{proceso_gid}/owner"
OPORTUNIDAD_RECOVER = "/portal/opportunity/{proceso_gid}/recover"
VOTO_RELEVANT = "relevant"
VOTO_IRRELEVANT = "irrelevant"

# --- Nombres de query nuevos -----------------------------------------------------------------------------------
Q_CLIENT = "client"
Q_DAYS = "days"
Q_DAY = "day"
Q_OWNER = "owner"
Q_BRAND = "brand"
Q_NOTICE = "notice"
Q_NEXT = "next"
OWNER_NONE = "none"  # `?owner=none` ≡ el viejo `?responsable=sin`


def ruta_oportunidad(plantilla: str, gid: str) -> str:
    """La URL de una de las `OPORTUNIDAD_*` para un proceso (el gid va escapado)."""
    return plantilla.replace("{proceso_gid}", quote(str(gid), safe=""))


def ruta_voto(voto: str) -> str:
    """`relevante|irrelevante` (el vocabulario de los tokens) → el segmento nuevo de `/f/{token}/…`."""
    return {"relevante": VOTO_RELEVANT, "irrelevante": VOTO_IRRELEVANT}[voto]


# --- Alias (lo viejo) ------------------------------------------------------------------------------------------
# vieja → nueva. Es la tabla que recorre el test de compatibilidad.
LEGADO: dict[str, str] = {
    "/acceso": LOGIN,
    "/acceso/codigo": LOGIN_CODE,
    "/acceso/mfa": LOGIN_MFA,
    "/salir": LOGOUT,
    "/portal/operativo": PORTAL_OPERATIONS,
    "/portal/tema": PORTAL_THEME,
    "/portal/ver-como": PORTAL_VIEW_AS,
}
_OPORTUNIDAD_LEGADO = {"estado": "status", "responsable": "owner", "recuperar": "recover"}
_VOTO_LEGADO = {"relevante": VOTO_RELEVANT, "irrelevante": VOTO_IRRELEVANT}
_RE_OPORTUNIDAD = re.compile(r"^/portal/oportunidad/([^/]+)/(estado|responsable|recuperar)$")
_RE_VOTO = re.compile(r"^/f/([^/]+)/(relevante|irrelevante)$")

# vieja → nueva
QUERY_LEGADO: dict[str, str] = {
    "cliente": Q_CLIENT, "dias": Q_DAYS, "dia": Q_DAY, "responsable": Q_OWNER,
    "marca": Q_BRAND, "aviso": Q_NOTICE, "siguiente": Q_NEXT,
}
_VALORES_LEGADO = {Q_OWNER: {"sin": OWNER_NONE}}

GLOBALES_JINJA: dict[str, object] = {
    "R_LOGIN": LOGIN, "R_LOGIN_CODE": LOGIN_CODE, "R_LOGIN_MFA": LOGIN_MFA, "R_LOGOUT": LOGOUT,
    "R_PORTAL": PORTAL, "R_REGIONAL": PORTAL_REGIONAL, "R_OPERATIONS": PORTAL_OPERATIONS,
    "R_THEME": PORTAL_THEME, "R_VIEW_AS": PORTAL_VIEW_AS, "R_ADMIN": ADMIN,
    "R_ADMIN_USERS": ADMIN_USERS, "R_ADMIN_INVITATIONS": ADMIN_INVITATIONS,
    "R_OP_STATUS":OPORTUNIDAD_STATUS, "R_OP_OWNER": OPORTUNIDAD_OWNER, "R_OP_RECOVER": OPORTUNIDAD_RECOVER,
    "ruta_oportunidad": ruta_oportunidad,
    "Q_CLIENT": Q_CLIENT, "Q_DAYS": Q_DAYS, "Q_DAY": Q_DAY, "Q_OWNER": Q_OWNER, "Q_BRAND": Q_BRAND,
    "Q_NOTICE": Q_NOTICE, "Q_NEXT": Q_NEXT, "OWNER_NONE": OWNER_NONE,
}


def es_permanente(path: str) -> bool:
    """Los alias de los votos de correo no se retiran nunca."""
    return bool(_RE_VOTO.match(path))


def nueva_de_legada(path: str) -> str | None:
    """La ruta vieja → la nueva, o None si `path` no es una ruta vieja."""
    if path in LEGADO:
        return LEGADO[path]
    m = _RE_OPORTUNIDAD.match(path)
    if m:
        return f"/portal/opportunity/{m.group(1)}/{_OPORTUNIDAD_LEGADO[m.group(2)]}"
    m = _RE_VOTO.match(path)
    if m:
        return f"/f/{m.group(1)}/{_VOTO_LEGADO[m.group(2)]}"
    return None


def traducir_query(qs: str) -> str:
    """Nombres viejos → nuevos. Si vienen el nuevo y el viejo, manda el nuevo."""
    if not qs:
        return qs
    pares = parse_qsl(qs, keep_blank_values=True)
    presentes = {k for k, _ in pares}
    if not any(k in QUERY_LEGADO for k in presentes):
        return qs
    salida = []
    for k, v in pares:
        if k in QUERY_LEGADO:
            destino = QUERY_LEGADO[k]
            if destino in presentes:
                continue  # el viejo pierde contra el nuevo
            salida.append((destino, _VALORES_LEGADO.get(destino, {}).get(v, v)))
        else:
            salida.append((k, v))
    return urlencode(salida)


class TraduceRutas:
    """ASGI puro (no `BaseHTTPMiddleware`): sirve o redirige lo viejo antes de enrutar (ver el docstring del módulo)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        qs0 = scope.get("query_string", b"").decode("latin-1")
        nueva = nueva_de_legada(path)
        qs = traducir_query(qs0)
        if nueva is None and qs == qs0:
            await self.app(scope, receive, send)
            return
        destino = nueva or path
        # GET/HEAD: se redirige para que favoritos y la barra queden en lo nuevo. El voto de correo viejo se sirve
        # directo (permanente) y el resto de POST conserva su cuerpo.
        if scope["method"] in ("GET", "HEAD") and not es_permanente(path):
            ubicacion = quote(destino, safe="/:@") + ("?" + qs if qs else "")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", ubicacion.encode("latin-1")), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        scope = dict(scope)
        scope["path"] = destino
        scope["raw_path"] = quote(destino, safe="/:@").encode("latin-1")
        scope["query_string"] = qs.encode("latin-1")
        await self.app(scope, receive, send)
