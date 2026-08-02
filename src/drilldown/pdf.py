"""src.drilldown.pdf — Fase 2 (MVP Track A). Ver amalia-oci-serverless-arch/PLAN_FASE2_TRACK_A.md.

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
