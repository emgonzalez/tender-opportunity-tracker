"""src.web.plantillas — entorno Jinja2 del servicio web.

Se renderiza con Jinja directo y no con `Jinja2Templates` de Starlette: la firma de esa
clase ha cambiado entre versiones y acá solo hacen falta cinco páginas.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

# `templates/` cuelga de la raíz del repo, y la raíz está dos niveles sobre este archivo
# (src/web/plantillas.py). Resolverla así —y no con "/app/templates"— hace que los tests
# y la ejecución local funcionen igual que la imagen, donde el repo también vive en /app.
_TEMPLATES_POR_DEFECTO = Path(__file__).resolve().parents[2] / "templates"


def crear_entorno() -> Environment:
    """Entorno con autoescape SIEMPRE activo.

    El comentario libre de quien vota vuelve a mostrarse en el formulario; es la única
    entrada de texto arbitrario del servicio y su única defensa contra XSS es esta.
    `FEEDBACK_TEMPLATES_DIR` permite moverlas sin tocar el código.
    """
    carpeta = os.getenv("FEEDBACK_TEMPLATES_DIR") or str(_TEMPLATES_POR_DEFECTO)
    return Environment(loader=FileSystemLoader(carpeta), autoescape=True)


def pagina(request: Request, nombre: str, status: int = 200, **contexto) -> HTMLResponse:
    """Renderiza `templates/<nombre>` con el entorno de la app."""
    html = request.app.state.plantillas.get_template(nombre).render(**contexto)
    return HTMLResponse(html, status_code=status)
