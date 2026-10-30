"""src.web.plantillas — entorno Jinja2 del servicio web.

Se renderiza con Jinja directo y no con `Jinja2Templates` de Starlette: la firma de esa
clase ha cambiado entre versiones y acá solo hacen falta cinco páginas.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from src.web import rutas

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
    env = Environment(loader=FileSystemLoader(carpeta), autoescape=True)
    env.globals.update(rutas.GLOBALES_JINJA)  # rutas y nombres de query: las plantillas no llevan literales
    return env


# ── Tema del portal (E12): Auto · Claro · Oscuro ────────────────────────────────
#
# La preferencia vive en una cookie (por navegador, no por usuario) que fija `POST /portal/theme`.
# Sin JavaScript: el servidor pone `data-tema` en el `<html>` y el CSS hace el resto, así no hay parpadeo.
# Cualquier valor fuera de la lista cerrada es `auto` = seguir al sistema, el comportamiento de siempre.
COOKIE_TEMA = "__Host-amalia_tema"
TEMAS = ("auto", "claro", "oscuro")
TEMA_POR_DEFECTO = "auto"
MAX_VOLVER = 300
VOLVER_POR_DEFECTO = rutas.PORTAL


def tema_de(request: Request) -> str:
    """El tema elegido en este navegador; `auto` ante cookie ausente, vacía, larga o desconocida."""
    try:
        valor = request.cookies.get(COOKIE_TEMA)
    except Exception:  # noqa: BLE001 — una cabecera Cookie mal formada es «sin preferencia»
        return TEMA_POR_DEFECTO
    return valor if isinstance(valor, str) and valor in TEMAS else TEMA_POR_DEFECTO


def _tiene_control(texto: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in texto)


def volver_seguro(valor) -> str:
    """Destino del PRG del selector de tema: solo una ruta interna del portal; si no, `/portal`.

    Rechaza todo lo que un navegador pueda leer como otro origen (`//host`, `/\\host`, `esquema:`,
    `user@host`), los saltos de línea y cualquier carácter de control, también codificados (`%0d%0a`)."""
    if not isinstance(valor, str) or not valor or len(valor) > MAX_VOLVER:
        return VOLVER_POR_DEFECTO
    if not valor.startswith(VOLVER_POR_DEFECTO) or valor[len(VOLVER_POR_DEFECTO):][:1] not in ("", "/", "?"):
        return VOLVER_POR_DEFECTO
    if "//" in valor or any(c in valor for c in "\\:@") or _tiene_control(valor):
        return VOLVER_POR_DEFECTO
    decodificado = unquote(valor)
    ruta = decodificado.split("?", 1)[0]
    if "\\" in decodificado or "//" in ruta or _tiene_control(decodificado):
        return VOLVER_POR_DEFECTO
    # `/portal/../login` o `/portal/%2e%2e/logout`: el navegador normaliza los segmentos punto y sale de /portal.
    if any(seg in (".", "..") for seg in ruta.split("/")) or any(seg in (".", "..") for seg in valor.split("?", 1)[0].split("/")):
        return VOLVER_POR_DEFECTO
    return valor


def ruta_actual(request: Request) -> str:
    """Path (y query, sin `notice`) de esta petición si es del portal, saneado con `volver_seguro`.

    Es el `volver` del formulario del tema: tras cambiarlo se vuelve a la misma vista y filtros, pero no
    se repite un aviso de una escritura anterior."""
    try:
        path = request.url.path
        if not path.startswith(VOLVER_POR_DEFECTO):
            return VOLVER_POR_DEFECTO
        pares = [(k, v) for k, v in request.query_params.multi_items() if k not in (rutas.Q_NOTICE, "aviso")]
        return volver_seguro(path + ("?" + urlencode(pares) if pares else ""))
    except Exception:  # noqa: BLE001 — sin URL legible se vuelve al inicio del portal
        return VOLVER_POR_DEFECTO


def pagina(request: Request, nombre: str, status: int = 200, **contexto) -> HTMLResponse:
    """Renderiza `templates/<nombre>` con el entorno de la app.

    Todo render recibe además `tema` y `ruta_actual` (E12); las plantillas que no los usan los ignoran.
    Lo que pase quien llama tiene prioridad."""
    contexto = {"tema": tema_de(request), "ruta_actual": ruta_actual(request), **contexto}
    html = request.app.state.plantillas.get_template(nombre).render(**contexto)
    return HTMLResponse(html, status_code=status)
