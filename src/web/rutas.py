"""src.web.rutas — URLs públicas en inglés, con las españolas siguiendo vivas (Fase U del plan de persistencia).

Mapa (lo que ya es una palabra inglesa se queda: `/portal`, `/portal/regional`, `/healthz`, `/f`, `/portal/static`):

    /login  /login/code  /login/mfa   ←  /acceso  /acceso/codigo  /acceso/mfa
    /logout                            ←  /salir
    /portal/operations                 ←  /portal/operativo
    /portal/theme                      ←  /portal/tema
    /portal/view-as                    ←  /portal/ver-como
    /portal/opportunity/{gid}/status|owner|recover  ←  /portal/oportunidad/{gid}/estado|responsable|recuperar
    /f/{token}/relevant|irrelevant     ←  /f/{token}/relevante|irrelevante
    ?client ?days ?day ?owner(=none) ?brand ?notice ?next  ←  ?cliente ?dias ?dia ?responsable(=sin) ?marca ?aviso ?siguiente

**Paso A (este):** `TraduceRutas` es el middleware más externo y reescribe la ruta y la query NUEVAS a las que hoy
registran los handlers. Así la app sirve ambas, ningún handler cambia y lo que la app EMITE (correos, redirects,
plantillas) sigue siendo español: un rollback a la imagen anterior no deja enlaces rotos. **Paso B (después de
verificar A en DEV y PROD):** se invierte —los handlers se registran con la ruta nueva, se emite lo nuevo y las viejas
pasan a ser el alias— y los GET viejos redirigen con 308. `/f/…relevante|irrelevante` es contrato firmado en correos
ya enviados: su alias es permanente. Los demás alias se retiran tras la ventana acordada (60 días por defecto).

No cambian: las cookies `__Host-amalia_*` (renombrarlas cierra las sesiones), el ancla `#op-{gid}`, los estados del
pipeline, los nombres de campos de formulario ni los valores de `?aviso=`.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode

# nueva → vigente (la que registran los handlers hoy)
RUTAS_EXACTAS: dict[str, str] = {
    "/login": "/acceso",
    "/login/code": "/acceso/codigo",
    "/login/mfa": "/acceso/mfa",
    "/logout": "/salir",
    "/portal/operations": "/portal/operativo",
    "/portal/theme": "/portal/tema",
    "/portal/view-as": "/portal/ver-como",
}
_OPORTUNIDAD = {"status": "estado", "owner": "responsable", "recover": "recuperar"}
_VOTO = {"relevant": "relevante", "irrelevant": "irrelevante"}
_RE_OPORTUNIDAD = re.compile(r"^/portal/opportunity/([^/]+)/(status|owner|recover)$")
_RE_VOTO = re.compile(r"^/f/([^/]+)/(relevant|irrelevant)$")

QUERY: dict[str, str] = {
    "client": "cliente", "days": "dias", "day": "dia", "owner": "responsable",
    "brand": "marca", "notice": "aviso", "next": "siguiente",
}
_VALORES = {"responsable": {"none": "sin"}}  # owner=none ≡ responsable=sin


def traducir_ruta(path: str) -> str:
    """La ruta nueva → la que registra el handler; cualquier otra vuelve igual."""
    if path in RUTAS_EXACTAS:
        return RUTAS_EXACTAS[path]
    m = _RE_OPORTUNIDAD.match(path)
    if m:
        return f"/portal/oportunidad/{m.group(1)}/{_OPORTUNIDAD[m.group(2)]}"
    m = _RE_VOTO.match(path)
    if m:
        return f"/f/{m.group(1)}/{_VOTO[m.group(2)]}"
    return path


def traducir_query(qs: str) -> str:
    """Nombres nuevos → vigentes. Si vienen el nuevo y el viejo, manda el nuevo."""
    if not qs:
        return qs
    pares = parse_qsl(qs, keep_blank_values=True)
    nuevos = {QUERY[k] for k, _ in pares if k in QUERY}
    if not nuevos:
        return qs
    salida = []
    for k, v in pares:
        if k in QUERY:
            destino = QUERY[k]
            salida.append((destino, _VALORES.get(destino, {}).get(v, v)))
        elif k in nuevos:
            continue  # el viejo pierde contra el nuevo
        else:
            salida.append((k, v))
    return urlencode(salida)


class TraduceRutas:
    """ASGI puro (no `BaseHTTPMiddleware`): reescribe `path`, `raw_path` y `query_string` antes de enrutar."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = traducir_ruta(scope["path"])
            qs = traducir_query(scope.get("query_string", b"").decode("latin-1"))
            if path != scope["path"] or qs != scope.get("query_string", b"").decode("latin-1"):
                scope = dict(scope)
                scope["path"] = path
                scope["raw_path"] = path.encode("latin-1")
                scope["query_string"] = qs.encode("latin-1")
        await self.app(scope, receive, send)
