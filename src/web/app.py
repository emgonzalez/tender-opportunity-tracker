"""src.web.app — fábrica de la aplicación web.

`crear_app(cfg)` y no una `app` global: los tests inyectan su propia configuración y el
portal futuro sumará routers acá sin tocar a los existentes. `/admin` queda reservado y
NO se implementa (ni autenticación): hoy todo el servicio es público y la única
credencial es la firma del token de cada enlace.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import Config
from src.web import feedback
from src.web.plantillas import crear_entorno
from src.web.seguridad import EncabezadosSeguros, LimiteCuerpo


def crear_app(cfg=None) -> FastAPI:
    """Arma la app. `cfg=None` toma la configuración del entorno (`Config()`)."""
    # Sin /docs, /redoc ni /openapi.json: no hay razón para publicar el esquema de un
    # servicio cuyas únicas rutas viven detrás de un token.
    app = FastAPI(title="AMALIA", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg if cfg is not None else Config()
    app.state.plantillas = crear_entorno()

    # El último que se agrega es el más externo: los encabezados envuelven al límite de
    # cuerpo, así hasta el 413 sale con ellos.
    app.add_middleware(LimiteCuerpo)
    app.add_middleware(EncabezadosSeguros)

    app.include_router(feedback.router)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        # Sin tocar la ADB a propósito: es la sonda de "el proceso vive", no de "la base
        # responde". Una ADB caída se ve en el 503 del voto, no reiniciando el servicio.
        return "ok"

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        # Una ruta inexistente y un token inválido deben verse IDÉNTICOS: si no, quien
        # sondea distingue "esto no es una ruta" de "esto es una ruta con firma mala".
        if exc.status_code == 404:
            return feedback.no_encontrado(request)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    return app
