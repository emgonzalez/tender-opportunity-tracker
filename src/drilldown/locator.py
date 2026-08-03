"""src.drilldown.locator — Localiza el PDF del pliego vía la API REST de COMPRASAL.

**Por qué API y no scraping del DOM:** la página de detalle es una SPA que no
renderiza NINGÚN `<a href>` para los documentos — se descargan con handlers JS.
La versión previa escaneaba anclas y falló en el 100% de los procesos (61/61)
en la validación del 2026-08-03: el HTML de debug pesaba 181 KB y tenía cero
elementos `<a href>`.

Contrato real del portal (extraído de su bundle `app.*.js`):

  1. GET /api/v1/compra/proceso/{id}/documentos
       → {"documentos": [{id, nombre, documentos: [{ruta_documento, ...}]}], ...}
  2. GET /api/v1/documentos/descargar?ruta=<ruta>&solo_url=true
       → {"url": "https://archivoscomprasal.s3.../app/<ruta>?X-Amz-..."}

⚠️ La URL devuelta es **prefirmada de S3 con `X-Amz-Expires=30`**: caduca a los
~30 segundos. Hay que descargarla de inmediato; cachearla no sirve.

Se reutiliza la `page` autenticada de Playwright para los `fetch`, igual que
`scraper/extractor.py`: así se aprovechan cookies + JWT sin duplicar el login.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # sólo type hints: no exige Playwright para importar el módulo
    from playwright.async_api import Page
    from src.config import Config
    from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

COMPRASAL_BASE = "https://www.comprasal.gob.sv"
PAAC_URL = f"{COMPRASAL_BASE}/proveedor-paac"
DEFAULT_TIMEOUT = 60_000

# Prioridad del documento: el pliego/bases describe alcance y condiciones, que
# es lo que el análisis necesita. Mayor puntaje = mejor candidato.
_PRIORIDAD: tuple[tuple[int, tuple[str, ...]], ...] = (
    (90, ("bases de licitacion", "bases de concurso", "pliego", "terminos de referencia",
          "especificaciones tecnicas")),
    (70, ("condiciones", "requisitos", "anexo tecnico")),
    (50, ("convocatoria",)),
    (30, ("solicitud de proceso", "adenda", "aclaracion")),
)


def _normalizar(texto: str) -> str:
    """Minúsculas sin tildes, para comparar nombres de documento."""
    t = (texto or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u")):
        t = t.replace(a, b)
    return t


def puntuar_documento(doc: dict) -> int:
    """Puntúa un documento por su nombre. Devuelve -1 si no es un PDF utilizable."""
    ruta = (doc.get("ruta_documento") or "").strip()
    if not ruta or not ruta.lower().endswith(".pdf"):
        return -1
    nombre = _normalizar(doc.get("nombre_documento") or "") + " " + _normalizar(ruta)
    for puntos, claves in _PRIORIDAD:
        if any(k in nombre for k in claves):
            return puntos
    return 10  # PDF sin palabra clave conocida: sirve como último recurso


def proceso_id_desde_url(url_directa: str) -> str | None:
    """Extrae el id numérico de `.../pac-procesos/etapas/{id}`."""
    m = re.search(r"/etapas/(\d+)", url_directa or "")
    return m.group(1) if m else None


def mejor_documento(payload: dict) -> dict | None:
    """Aplana las etapas del payload y devuelve el documento de mayor prioridad."""
    etapas = payload.get("documentos") if isinstance(payload, dict) else None
    if not isinstance(etapas, list):
        return None
    candidatos: list[tuple[int, dict]] = []
    for etapa in etapas:
        if not isinstance(etapa, dict):
            continue
        for doc in etapa.get("documentos") or []:
            if not isinstance(doc, dict):
                continue
            if doc.get("visibilidad_proveedor") is False:
                continue  # no visible para el proveedor
            p = puntuar_documento(doc)
            if p > 0:
                candidatos.append((p, doc))
    if not candidatos:
        return None
    candidatos.sort(key=lambda c: c[0], reverse=True)
    return candidatos[0][1]


async def _token(page: "Page") -> str | None:
    """JWT desde localStorage; navega a COMPRASAL si la página está en otro origen."""
    try:
        if COMPRASAL_BASE not in (page.url or ""):
            await page.goto(PAAC_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        return await page.evaluate("() => localStorage.getItem('token')")
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo obtener el token JWT: %s", exc)
        return None


async def _api_get(page: "Page", token: str, path: str) -> dict | None:
    """GET autenticado contra la API, ejecutado dentro del contexto del browser."""
    try:
        raw = await page.evaluate(
            """async ([p, t]) => {
                const r = await fetch(p, {headers: {'Authorization': 'Bearer ' + t,
                                                    'Accept': 'application/json'}});
                return JSON.stringify({status: r.status, body: await r.text()});
            }""",
            [path, token],
        )
        out = json.loads(raw)
        if out["status"] != 200:
            logger.warning("API %s devolvió HTTP %s", path.split("?")[0], out["status"])
            return None
        return json.loads(out["body"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo llamando %s: %s", path.split("?")[0], exc)
        return None


async def find_pliego_pdf_url(page: "Page", proceso: "Proceso", cfg: "Config") -> str | None:
    """URL de descarga del pliego, o None si el proceso no tiene documentos.

    ⚠️ La URL es prefirmada y **caduca a los ~30 s**: descargarla de inmediato.
    Nunca lanza: ante cualquier fallo loguea y devuelve None para no tumbar el batch.
    """
    ref = getattr(proceso, "numero_referencia", "?")
    pid = proceso_id_desde_url(getattr(proceso, "url_directa", "") or "")
    if not pid:
        logger.warning("No se pudo extraer el id de proceso de %r", getattr(proceso, "url_directa", ""))
        return None

    token = await _token(page)
    if not token:
        return None

    payload = await _api_get(page, token, f"/api/v1/compra/proceso/{pid}/documentos")
    if payload is None:
        return None

    doc = mejor_documento(payload)
    if not doc:
        logger.info("Proceso %s: sin documentos PDF visibles.", ref)
        return None

    ruta = doc["ruta_documento"]
    logger.info("Pliego elegido para %s: %s", ref, (doc.get("nombre_documento") or ruta)[:80])

    firmado = await _api_get(
        page, token,
        f"/api/v1/documentos/descargar?ruta={urllib.parse.quote(ruta)}&solo_url=true",
    )
    if not isinstance(firmado, dict):
        return None
    # Respuesta directa {"url": ...}; algunos wrappers la anidan bajo "data".
    url = firmado.get("url") or (firmado.get("data") or {}).get("url")
    if not url:
        logger.warning("La API no devolvió URL de descarga para %s", ref)
        return None
    return url
