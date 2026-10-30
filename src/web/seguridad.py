"""src.web.seguridad — endurecimiento que aplica a TODAS las respuestas del servicio.

Son middlewares ASGI puros y no `BaseHTTPMiddleware`: este último envuelve el cuerpo en
un stream y complica leerlo antes de que lo consuma el framework, que es justo lo que
hace falta para el tope de tamaño.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.responses import Response

# Tope del cuerpo de un POST. El formulario más grande lleva 9 motivos y 1000
# caracteres de comentario (unos pocos KB aun escapados en UTF-8): 16 KB sobra y
# corta a quien intente colar megabytes en una VM de 1 GB.
MAX_CUERPO = 16 * 1024

ENCABEZADOS = {
    # El token va en la URL: que no viaje en `Referer` hacia ningún otro sitio. `same-origin` y no
    # `no-referrer`: con éste el navegador manda `Origin: null` en los POST de formularios y la
    # verificación CSRF de /login rechaza hasta al usuario legítimo.
    "Referrer-Policy": "same-origin",
    "X-Robots-Tag": "noindex",
    # Cada página lleva el título del proceso y el voto vigente de una persona.
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    # Sin scripts, sin imágenes, sin fuentes: solo CSS en línea. `form-action 'self'`
    # impide que una inyección reencamine el formulario a otro dominio.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    # Caddy ya fuerza HTTPS; esto le dice al navegador que no vuelva a intentar HTTP.
    # Sin `includeSubDomains`/`preload`: el dominio padre tiene otros servicios.
    "Strict-Transport-Security": "max-age=31536000",
}


# El portal necesita scripts propios (hover de los gráficos): mismo origen, nunca inline.
CSP_PORTAL = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; "
              "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


class EncabezadosSeguros:
    """Agrega los encabezados de `ENCABEZADOS` a cada respuesta, incluidas las de error."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        es_portal = scope.get("path", "").startswith("/portal")

        async def enviar(mensaje):
            if mensaje["type"] == "http.response.start":
                cabeceras = MutableHeaders(scope=mensaje)
                for nombre, valor in ENCABEZADOS.items():
                    cabeceras[nombre] = valor
                if es_portal:
                    cabeceras["Content-Security-Policy"] = CSP_PORTAL
            await send(mensaje)

        await self.app(scope, receive, enviar)


class LimiteCuerpo:
    """Responde 413 si el cuerpo de un POST supera `MAX_CUERPO`.

    No basta mirar `Content-Length`: un cliente puede mentir o usar transferencia por
    trozos. Se cuenta lo que realmente llega y se rebobina para el framework. Bufferar
    es aceptable porque el tope es de 16 KB.
    """

    def __init__(self, app, max_bytes: int = MAX_CUERPO):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        demasiado_grande = Response("Solicitud demasiado grande.", 413, media_type="text/plain")
        declarado = dict(scope["headers"]).get(b"content-length", b"")
        if declarado.isdigit() and int(declarado) > self.max_bytes:
            await demasiado_grande(scope, receive, send)
            return

        partes: list[bytes] = []
        total = 0
        while True:
            mensaje = await receive()
            if mensaje["type"] == "http.disconnect":
                return
            trozo = mensaje.get("body", b"")
            total += len(trozo)
            if total > self.max_bytes:
                await demasiado_grande(scope, receive, send)
                return
            partes.append(trozo)
            if not mensaje.get("more_body", False):
                break

        entregado = False

        async def rebobinar():
            nonlocal entregado
            if not entregado:
                entregado = True
                return {"type": "http.request", "body": b"".join(partes), "more_body": False}
            return await receive()

        await self.app(scope, rebobinar, send)
