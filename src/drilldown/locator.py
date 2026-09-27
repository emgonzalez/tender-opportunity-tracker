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

**`fecha_limite` (investigación aparte, misma técnica):** la API de listado
(`/api/v1/procesos/proveedores`, la que usa `extractor.py`) NO trae una fecha
límite — verificado contra el endpoint real, autenticado, en vivo. La fecha
real vive en el detalle de cada proceso:

  3. GET /api/v1/compra/proceso/{id}
       → {..., "EtapaPorProcesos": [{"nombre": "...", "fecha_hora_inicio": "...",
                                      "fecha_hora_fin": "..."}, ...]}

`EtapaPorProcesos` es el cronograma completo del proceso, y el nombre de la
etapa de cierre **varía por modalidad y NO es un valor fijo**: "Recepción de
ofertas" (Contratación directa, Comparación de precios, Licitación
competitiva, Convenios), "Recepción de ofertas técnicas" (Subasta Inversa),
"Recepción de propuesta técnica y económica" (consultoría por calidad y
costo, o por menor costo), "Recepción de CV de consultores y propuesta
económica" (consultor individual), "Recepción de propuesta" (fuente única) —
verificado contra un proceso real de cada una de las 9 modalidades vigentes.
Por eso `_es_etapa_de_cierre_de_ofertas` empareja por contenido normalizado
("recepcion" + "oferta" o "propuesta"), no por una lista de nombres exactos
que se desactualizaría con la próxima modalidad nueva que agregue el portal.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # sólo type hints: no exige Playwright para importar el módulo
    from playwright.async_api import Page

    from src.config import Config
    from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

COMPRASAL_BASE = "https://www.comprasal.gob.sv"
PAAC_URL = f"{COMPRASAL_BASE}/proveedor-paac"
DEFAULT_TIMEOUT = 60_000

# El Salvador es UTC-6 (mismo valor que scraper/extractor.py; no varía por DST).
TZ_ELSALVADOR = timezone(timedelta(hours=-6))

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


def _es_etapa_de_cierre_de_ofertas(nombre: str) -> bool:
    """True si `nombre` es la etapa donde se cierra la recepción de ofertas/propuestas.

    Empareja por contenido, no por una lista fija de nombres exactos: el
    nombre de esta etapa varía por modalidad ("Recepción de ofertas",
    "Recepción de ofertas técnicas", "Recepción de propuesta técnica y
    económica", "Recepción de CV de consultores y propuesta económica",
    "Recepción de propuesta"...) — verificado contra las 9 modalidades
    vigentes. "Recepción de consultas" (una etapa distinta, anterior) queda
    excluida a propósito: no contiene "oferta" ni "propuesta".
    """
    n = _normalizar(nombre)
    return "recepcion" in n and ("oferta" in n or "propuesta" in n)


def _parse_fecha_etapa(fecha_str: str | None) -> "datetime | None":
    """Parsea una fecha ISO UTC de `EtapaPorProcesos` a hora de El Salvador.

    Duplica (a propósito, es autocontenido) la lógica de
    `scraper/extractor.py::_parse_fecha_api`: mismo formato de fecha, mismo
    origen (la API de COMPRASAL), pero este módulo no importa símbolos
    privados de otro para no acoplar dos módulos por un detalle interno.
    """
    if not fecha_str:
        return None
    limpia = fecha_str.rstrip("Z").split(".")[0]  # "2026-10-07T18:00:00.000Z" → "...T18:00:00"
    try:
        dt = datetime.strptime(limpia, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ_ELSALVADOR)


async def obtener_fecha_limite(page: "Page", proceso: "Proceso", cfg: "Config") -> "datetime | None":
    """Fecha límite de recepción de ofertas/propuestas, o None si no se puede obtener.

    `/api/v1/procesos/proveedores` (la API de LISTADO que usa `extractor.py`)
    **no trae esta fecha** — verificado contra el endpoint real, autenticado,
    en vivo: no es un campo faltante por un bug de mapeo, sencillamente no
    está en esa respuesta. La única fuente es el DETALLE de cada proceso
    (`/api/v1/compra/proceso/{id}`), que trae el cronograma completo en
    `EtapaPorProcesos`. Si esa etapa tiene más de una ocurrencia (no se vio en
    ninguna de las 9 modalidades probadas, pero el portal podría agregar una
    nueva), se toma la de `fecha_hora_fin` más tardía: es la que de verdad
    cierra la ventana para ofertar, sea cual sea la estructura de etapas.

    Nunca lanza: ante cualquier fallo (proceso sin id, HTTP no-200, JSON
    inesperado, sin etapa de cierre) loguea y devuelve None. Es una llamada
    extra por proceso —no gratis— por eso el llamador la reserva para
    procesos ya relevantes, no para el listado completo del día.
    """
    ref = getattr(proceso, "numero_referencia", "?")
    pid = proceso_id_desde_url(getattr(proceso, "url_directa", "") or "")
    if not pid:
        logger.warning("No se pudo extraer el id de proceso de %r", getattr(proceso, "url_directa", ""))
        return None

    token = await _token(page)
    if not token:
        return None

    payload = await _api_get(page, token, f"/api/v1/compra/proceso/{pid}")
    if not isinstance(payload, dict):
        return None
    detalle = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    etapas = detalle.get("EtapaPorProcesos")
    if not isinstance(etapas, list):
        logger.info("Proceso %s: detalle sin EtapaPorProcesos.", ref)
        return None

    candidatas = [
        e for e in etapas
        if isinstance(e, dict) and _es_etapa_de_cierre_de_ofertas(e.get("nombre") or "")
    ]
    if not candidatas:
        logger.info("Proceso %s: sin etapa de recepción de ofertas/propuestas reconocible.", ref)
        return None

    fechas = [_parse_fecha_etapa(e.get("fecha_hora_fin")) for e in candidatas]
    fechas = [f for f in fechas if f is not None]
    if not fechas:
        return None
    return max(fechas)


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
