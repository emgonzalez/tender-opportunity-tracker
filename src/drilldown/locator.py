"""src.drilldown.locator — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md.

Localiza la URL de descarga del PDF del pliego de condiciones (bases) dentro
de la página de detalle de un proceso de COMPRASAL.

Estilo: async, para ser consistente con el resto del scraper (Playwright
async API, ver src/scraper/extractor.py) — reutiliza una `page` ya
autenticada del batch en curso, sin abrir un browser/context nuevo.

IMPORTANTE — COMPRASAL es una SPA: la estructura del DOM y las clases CSS
pueden cambiar sin aviso. Por eso NO dependemos de selectores CSS frágiles
(clases, ids) sino de una heurística sobre `href`/texto visible de los
`<a>` presentes en la página, que es más resistente a rediseños visuales.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    # Solo para type hints — evita que Playwright sea un import obligatorio
    # a nivel de módulo (py_compile debe pasar aunque no esté instalado).
    from playwright.async_api import Page

    from src.config import Config
    from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30_000

# Palabras clave (case-insensitive) que indican que un enlace apunta al
# pliego de condiciones / bases del proceso, ya sea en el href o en el
# texto visible del <a>.
_KEYWORDS_FUERTES = ("pliego", "condiciones", "bases")
_KEYWORDS_DEBILES = ("documento", "descargar")

# Caracteres no seguros para nombres de archivo (Windows + POSIX).
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(value: str) -> str:
    """Sanea un string para usarlo como nombre de archivo."""
    value = value.strip() or "sin_referencia"
    return _UNSAFE_FILENAME_CHARS.sub("_", value)


async def _guardar_html_debug(page: "Page", cfg: "Config", numero_referencia: str) -> None:
    """Guarda el HTML actual de la página para diagnóstico manual cuando
    no se logra localizar el enlace del pliego. No lanza excepciones."""
    try:
        os.makedirs(cfg.debug_dir, exist_ok=True)
        filename = f"locator_no_match_{_sanitize_filename(numero_referencia)}.html"
        filepath = os.path.join(cfg.debug_dir, filename)
        html = await page.content()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning(
            "No se encontró enlace de pliego para %s. HTML de debug guardado en %s",
            numero_referencia,
            filepath,
        )
    except Exception:
        # El guardado de debug es best-effort: nunca debe tumbar el flujo principal.
        logger.exception(
            "No se pudo guardar HTML de debug para %s", numero_referencia
        )


def _es_candidato_pdf(href: str) -> bool:
    """True si el href parece apuntar a un documento descargable relevante."""
    href_lower = href.lower()
    if href_lower.endswith(".pdf"):
        return True
    return any(kw in href_lower for kw in (*_KEYWORDS_FUERTES, *_KEYWORDS_DEBILES))


def _score_candidato(href: str, texto: str) -> int:
    """
    Puntúa un enlace candidato para elegir el más probable como pliego.

    Prioridad (mayor score = más probable):
      3 → texto visible o href mencionan "pliego"/"condiciones"/"bases"
      2 → href termina en .pdf (sin keyword fuerte)
      1 → href/texto mencionan keyword débil ("documento"/"descargar")
      0 → no debería llegar aquí (ya filtrado por _es_candidato_pdf)
    """
    href_lower = href.lower()
    texto_lower = (texto or "").lower()
    combinado = f"{href_lower} {texto_lower}"

    if any(kw in combinado for kw in _KEYWORDS_FUERTES):
        return 3
    if href_lower.endswith(".pdf"):
        return 2
    if any(kw in combinado for kw in _KEYWORDS_DEBILES):
        return 1
    return 0


async def find_pliego_pdf_url(page: "Page", proceso: "Proceso", cfg: "Config") -> str | None:
    """
    Navega al detalle del proceso y localiza la URL de descarga del PDF del
    pliego de condiciones (bases).

    Reutiliza la `page` de Playwright ya autenticada (misma sesión del
    batch) — NO crea un browser/context nuevo.

    Estrategia (documentada porque COMPRASAL es una SPA con selectores que
    pueden cambiar sin aviso; evitamos depender de clases CSS específicas):
      1. Recorrer todos los <a href> del DOM tras cargar la página.
      2. Quedarnos con los que parezcan apuntar a un documento descargable:
         href que termina en ".pdf" o que contiene alguna de las keywords
         "pliego" / "bases" / "condiciones" / "documento" / "descargar"
         (case-insensitive, tanto en el href como en el texto visible).
      3. Si hay varios candidatos, priorizar el que mencione keywords
         fuertes ("pliego"/"condiciones"/"bases"); si ninguno las tiene,
         usar el primer ".pdf" encontrado.
      4. Resolver la URL a absoluta con urljoin (proceso.url_directa como
         base), por si el href es relativo.

    Si no se encuentra ningún candidato: guarda el HTML de la página en
    cfg.debug_dir para diagnóstico posterior, loguea un warning y retorna
    None (nunca lanza excepción, para no tumbar el batch completo).

    Returns:
        URL absoluta del PDF del pliego, o None si no se pudo localizar.
    """
    try:
        logger.info(
            "Buscando enlace de pliego para proceso %s (%s)...",
            proceso.numero_referencia,
            proceso.url_directa,
        )
        # 'domcontentloaded': patrón usado en todo el repo (ver extractor.py) —
        # 'networkidle' se cuelga en esta SPA por polling de fondo.
        await page.goto(
            proceso.url_directa, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT
        )

        # Recolectar todos los <a href> visibles en el DOM actual, junto con
        # su texto, en una sola evaluación en el browser (más rápido que
        # iterar locators uno por uno).
        anchors: list[dict] = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(el => ({ href: el.getAttribute('href') || '', "
            "text: (el.textContent || '').trim() }))",
        )

        candidatos = [
            (a["href"], a["text"]) for a in anchors if a["href"] and _es_candidato_pdf(a["href"])
        ]

        if not candidatos:
            await _guardar_html_debug(page, cfg, proceso.numero_referencia)
            return None

        # Elegir el candidato con mayor score; ante empate, se queda el
        # primero encontrado (orden de aparición en el DOM).
        mejor_href, _mejor_texto = max(candidatos, key=lambda c: _score_candidato(c[0], c[1]))

        url_absoluta = urljoin(proceso.url_directa, mejor_href)
        logger.info(
            "Enlace de pliego localizado para %s: %s",
            proceso.numero_referencia,
            url_absoluta,
        )
        return url_absoluta

    except Exception as exc:
        logger.warning(
            "Error localizando pliego para proceso %s: %s",
            getattr(proceso, "numero_referencia", "?"),
            exc,
        )
        return None
