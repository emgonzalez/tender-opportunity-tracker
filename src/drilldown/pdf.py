"""src.drilldown.pdf — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md.

Utilidades PDF memory-safe con PyMuPDF (fitz).

REGLA CRITICA DE MEMORIA: los documentos SIEMPRE se abren por ruta de archivo
con fitz.open(path) (usa mmap, carga de paginas lazy). NUNCA usar
fitz.open(stream=bytes), ya que eso cargaria el documento completo a RAM.
Cada documento abierto se cierra explicitamente con doc.close() en un
bloque finally.
"""

from __future__ import annotations

import base64
import logging

import fitz

logger = logging.getLogger(__name__)


class PdfCorruptoError(Exception):
    """Se lanza cuando un PDF no puede abrirse o esta corrupto."""

    pass


def page_count(path: str) -> int:
    """Devuelve el numero de paginas de un PDF, abriendo por ruta de archivo."""
    try:
        doc = fitz.open(path)
    except fitz.FileDataError as e:
        logger.error("PDF corrupto al abrir %s: %s", path, e)
        raise PdfCorruptoError(str(e)) from e
    except Exception as e:
        logger.error("Error al abrir PDF %s: %s", path, e)
        raise PdfCorruptoError(str(e)) from e

    try:
        return doc.page_count
    finally:
        doc.close()


def extract_text_first_pages(path: str, n: int) -> str:
    """Concatena el texto de las primeras n paginas (o menos si el doc tiene menos)."""
    try:
        doc = fitz.open(path)
    except fitz.FileDataError as e:
        logger.error("PDF corrupto al abrir %s: %s", path, e)
        raise PdfCorruptoError(str(e)) from e
    except Exception as e:
        logger.error("Error al abrir PDF %s: %s", path, e)
        raise PdfCorruptoError(str(e)) from e

    try:
        limit = min(n, doc.page_count)
        texts = []
        for i in range(limit):
            page = doc.load_page(i)
            texts.append(page.get_text("text"))
        return "".join(texts)
    finally:
        doc.close()


def is_scanned(path: str, sample_pages: int = 3) -> bool:
    """True si las primeras sample_pages tienen practicamente nula capa de texto (< 40 chars)."""
    try:
        doc = fitz.open(path)
    except fitz.FileDataError as e:
        logger.error("PDF corrupto al abrir %s: %s", path, e)
        raise PdfCorruptoError(str(e)) from e
    except Exception as e:
        logger.error("Error al abrir PDF %s: %s", path, e)
        raise PdfCorruptoError(str(e)) from e

    try:
        limit = min(sample_pages, doc.page_count)
        total_chars = 0
        for i in range(limit):
            page = doc.load_page(i)
            total_chars += len(page.get_text("text").strip())
        scanned = total_chars < 40
        logger.debug(
            "is_scanned(%s): sample_pages=%d total_chars=%d scanned=%s",
            path, limit, total_chars, scanned,
        )
        return scanned
    finally:
        doc.close()


def read_pdf_b64(path: str) -> str:
    """Lee el archivo en modo binario y devuelve base64 estandar sin saltos de linea."""
    with open(path, "rb") as f:
        data = f.read()
    return base64.standard_b64encode(data).decode("ascii")


# Lado largo OBJETIVO para las imagenes que se mandan al modelo. Es el optimo
# que recomienda Anthropic: por encima, el servidor reescala y solo se pagan
# mas tokens; por debajo, se pierde detalle.
MAX_LADO_PX = 1568
JPEG_QUALITY = 80


def _lado_largo_nativo_px(page) -> int:
    """Lado largo (en px) del raster mas grande embebido en la pagina; 0 si no hay.

    Lee los metadatos de get_images() (indices 2 y 3 = ancho y alto) en vez de
    decodificar la imagen: en una VM de 1 GB, decodificar un escaneo a 300 DPI
    por cada pagina es justamente lo que hay que evitar.
    """
    try:
        return max(
            (max(img[2], img[3]) for img in page.get_images(full=True)),
            default=0,
        )
    except Exception:  # noqa: BLE001 - la falta de metadatos no debe abortar
        return 0


def _zoom_de_pagina(page, max_lado_px: int) -> float:
    """Factor de render que acerca la pagina a `max_lado_px` en su lado largo.

    `page.rect` esta en PUNTOS PDF (1/72"), no en pixeles: una carta son
    612x792 pt sin importar a que resolucion se escaneo. Escalar contra los
    puntos —que es lo que se hacia antes con `min(1.0, max_lado_px / lado)`—
    dejaba `zoom=1` en toda pagina de tamano estandar y renderizaba a **72 DPI**,
    tirando la resolucion real del escaneo (medido: fuente 155 DPI -> render 72).

    Dos topes, en este orden:
      - objetivo: `max_lado_px` en el lado largo. Se aplica SIEMPRE, tambien
        hacia abajo: una pagina sobredimensionada (planos) se reduce en vez de
        costar una fortuna en tokens.
      - techo nativo: la resolucion del raster embebido, porque ampliar mas
        alla de lo que el escaneo tiene agrega tokens sin agregar detalle.

    El techo nativo solo se aplica si el raster cubre la pagina a >= 72 DPI
    (`zoom_nativo >= 1`). Por debajo de eso no es el escaneo de la pagina sino
    un adorno —un logo en una pagina vectorial— y hacerle caso reduciria la
    pagina entera a una miniatura ilegible. El costo de equivocarse en ese
    juicio es asimetrico: ignorar el techo en un escaneo malo de verdad solo
    gasta ~1.900 tokens de mas (centesimas de centavo con Haiku); respetarlo
    con un logo de 30 px rompe el triaje por completo.
    """
    rect = page.rect
    lado_pts = max(rect.width, rect.height) or 1
    zoom = max_lado_px / lado_pts
    nativo_px = _lado_largo_nativo_px(page)
    if nativo_px:
        zoom_nativo = nativo_px / lado_pts
        if zoom_nativo >= 1.0:
            zoom = min(zoom, zoom_nativo)
    return zoom


# Anthropic tokeniza una imagen en aproximadamente (ancho * alto) / 750 tokens.
_PX_POR_TOKEN = 750


def tokens_imagen_estimados(
    path: str, n: int | None = None, max_lado_px: int = MAX_LADO_PX
) -> int:
    """Estima los tokens que costarian las primeras `n` paginas como imagen.

    Usa EXACTAMENTE el mismo zoom que `rasterizar_paginas` (`_zoom_de_pagina`):
    si el estimador y el renderizador divergen, la estimacion describe un
    documento que nunca se manda. `n=None` estima el documento completo.

    Devuelve 0 si el PDF no se puede abrir (es una estimacion, no una guardia).
    """
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - estimar nunca debe tumbar el batch
        logger.warning("No se pudo estimar tokens de %s: %s", path, exc)
        return 0

    try:
        total = doc.page_count if n is None else min(max(int(n), 0), doc.page_count)
        tokens = 0
        for i in range(total):
            page = doc.load_page(i)
            rect = page.rect
            zoom = _zoom_de_pagina(page, max_lado_px)
            tokens += int((rect.width * zoom) * (rect.height * zoom) / _PX_POR_TOKEN)
        return tokens
    finally:
        doc.close()


def rasterizar_paginas(
    path: str,
    n: int | None = None,
    max_lado_px: int = MAX_LADO_PX,
    quality: int = JPEG_QUALITY,
    *,
    indices: list[int] | None = None,
) -> list[str]:
    """Rasteriza `indices` (base cero) o las primeras `n` páginas, UNA A LA VEZ.

    `n=None` incluye todo el documento. Los índices deben existir; se conserva
    el orden pedido y se eliminan duplicados, sin cargar bitmaps simultáneos.

    Pensado para el triaje de pliegos escaneados: enviar el PDF completo hacia
    el modelo hace que se rasterice cada pagina como imagen (~1.500-2.000 tokens
    por pagina), y en un corpus mayoritariamente escaneado eso domina el costo.
    Acotar a las primeras paginas basta porque ahi vive el "Objeto de la
    Contratacion", que es lo que el triaje necesita para decidir GO/NO-GO.

    Disciplina de memoria (VM de 1 GB): se procesa pagina por pagina y se libera
    el pixmap de inmediato; nunca se acumulan bitmaps en RAM.
    """
    doc = None
    salida: list[str] = []
    try:
        try:
            doc = fitz.open(path)
        except Exception as exc:
            raise PdfCorruptoError(str(exc)) from exc

        if indices is None:
            total = doc.page_count if n is None else min(max(int(n), 0), doc.page_count)
            elegidas = range(total)
        else:
            elegidas = list(dict.fromkeys(indices))
            if any(type(i) is not int or not 0 <= i < doc.page_count for i in elegidas):
                raise ValueError("Índice de página fuera del documento")
            total = len(elegidas)
        for i in elegidas:
            pix = None
            try:
                page = doc.load_page(i)
                zoom = _zoom_de_pagina(page, max_lado_px)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                salida.append(
                    base64.standard_b64encode(
                        pix.tobytes("jpeg", jpg_quality=quality)
                    ).decode("ascii")
                )
            except Exception as exc:  # noqa: BLE001 - una pagina mala no aborta el resto
                logger.warning("No se pudo rasterizar la pagina %d de %s: %s", i, path, exc)
            finally:
                pix = None  # liberar el bitmap cuanto antes
        logger.info("Rasterizadas %d/%d paginas de %s", len(salida), total, path)
        return salida
    finally:
        if doc is not None:
            doc.close()
