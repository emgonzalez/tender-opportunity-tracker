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

import asyncio
import json
import logging
import os
import re
import urllib.parse
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.drilldown import downloader
from src.portales.contrato import DocumentoPliego

if TYPE_CHECKING:  # sólo type hints: no exige Playwright para importar el módulo
    from playwright.async_api import Page

    from src.config import Config
    from src.portales import Portal
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


# Umbrales compartidos con `pipeline.descargar_pliegos`: un documento con puntaje >= PUNTAJE_NOMBRE
# está identificado POR SU NOMBRE como el documento base; por debajo es un anexo o un último recurso.
PUNTAJE_NOMBRE = 50
PUNTAJE_CATEGORIA = 30   # solo la categoría del portal coincide (SICOP: fileTypeNm)
PUNTAJE_SIN_CLAVE = 10   # archivo utilizable sin palabra clave conocida


def puntuar_documento(doc: dict, palabras: tuple[str, ...] = (),
                      extensiones: tuple[str, ...] = ("pdf",)) -> int:
    """Puntúa un documento por su nombre. Devuelve -1 si no es un archivo utilizable.

    `doc` trae `nombre_documento`, `ruta_documento` y, opcionalmente, `categoria`.

    - Sin `palabras` (COMPRASAL, `Portal.palabras_documento == ()`): la prioridad histórica
      `_PRIORIDAD`, sin cambios; la categoría no se mira.
    - Con `palabras` (SICOP): la primera palabra que aparece en el nombre da `90 - 5·i` (el orden de
      `palabras` es la preferencia); si solo coincide la categoría, `PUNTAJE_CATEGORIA`; si nada,
      `PUNTAJE_SIN_CLAVE`.
    """
    ruta = (doc.get("ruta_documento") or "").strip()
    if not ruta or ruta.lower().rsplit(".", 1)[-1] not in extensiones or "." not in ruta:
        return -1
    nombre = _normalizar(doc.get("nombre_documento") or "") + " " + _normalizar(ruta)
    if palabras:
        claves = [_normalizar(p).strip() for p in palabras]
        for i, clave in enumerate(claves):
            if clave and clave in nombre:
                return max(PUNTAJE_NOMBRE, 90 - 5 * i)
        categoria = _normalizar(doc.get("categoria") or "")
        if any(c and c in categoria for c in claves):
            return PUNTAJE_CATEGORIA
        return PUNTAJE_SIN_CLAVE
    for puntos, claves in _PRIORIDAD:
        if any(k in nombre for k in claves):
            return puntos
    return PUNTAJE_SIN_CLAVE  # PDF sin palabra clave conocida: sirve como último recurso


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


def mejor_documento(payload: dict, palabras: tuple[str, ...] = ()) -> dict | None:
    """Aplana las etapas del payload y devuelve el documento de mayor prioridad.

    `palabras` es `Portal.palabras_documento`; vacío conserva la puntuación histórica.
    """
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
            p = puntuar_documento(doc, palabras)
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
    elegido = await _localizar_pliego(page, proceso, cfg)
    return elegido[1] if elegido else None


async def _localizar_pliego(page: "Page", proceso: "Proceso", cfg: "Config",
                            palabras: tuple[str, ...] = ()) -> tuple[dict, str] | None:
    """`(documento elegido, URL prefirmada)` o None. Mismas llamadas que `find_pliego_pdf_url`."""
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

    doc = mejor_documento(payload, palabras)
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
    return doc, url


# ── Localizador de COMPRASAL (contrato `LocalizadorPliego`, docs/arquitectura/portales.md §2.3) ──
#
# Envuelve SIN cambiar comportamiento lo que antes hacía `pipeline.descargar_pliegos`: un Chromium con
# la sesión `storage_state`, `obtener_fecha_limite`, `find_pliego_pdf_url`, `requests` primero y el
# request autenticado de Playwright como respaldo. El User-Agent fijo es el de siempre para COMPRASAL
# (mismo que `scraper/extractor.py`); SICOP, en cambio, NUNCA fija el UA (ver `localizador_sicop.py`).

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def _download_via_playwright(page, url: str, dest_dir: str, max_bytes: int) -> str | None:
    """Descarga el PDF usando la sesión autenticada del navegador (auth garantizada).

    Fallback cuando `requests` no lleva las cookies/token de COMPRASAL. Verifica
    Content-Length antes de traer el cuerpo; aun así el cuerpo entra en RAM una vez
    (acotado por max_bytes), aceptable con concurrencia=1 y PDFs <= 32 MB.
    """
    try:
        resp = await page.request.get(url, timeout=45_000)
        if not resp.ok:
            logger.warning("Descarga Playwright falló (%s) para %s", resp.status, url)
            return None
        cl = resp.headers.get("content-length")
        if cl and int(cl) > max_bytes:
            logger.warning("DESCARTADO_TAMANO (Content-Length %s) para %s", cl, url)
            return None
        body = await resp.body()
        if len(body) > max_bytes:
            logger.warning("DESCARTADO_TAMANO (cuerpo %d bytes) para %s", len(body), url)
            return None
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f"{_uuid.uuid4().hex}.pdf")
        with open(path, "wb") as f:
            f.write(body)
        return path
    except Exception as exc:  # noqa: BLE001 — no tumbar el batch
        logger.warning("Error en descarga Playwright de %s: %s", url, exc)
        return None


class LocalizadorComprasal:
    """`LocalizadorPliego` de COMPRASAL: un navegador con la sesión autenticada para todo el lote."""

    def __init__(self, config: "Config", portal: "Portal | None" = None) -> None:
        self.cfg = config
        self.portal = portal
        self.page = None
        self._playwright = None
        self._browser = None

    async def __aenter__(self) -> "LocalizadorComprasal":
        from playwright.async_api import async_playwright

        from src.scraper.browser import build_chromium_args

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.cfg.headless,
                executable_path=self.cfg.chromium_path or None,
                args=build_chromium_args(self.cfg),
            )
            context = await self._browser.new_context(
                storage_state=self.cfg.storage_state_path,
                user_agent=_USER_AGENT,
            )
            self.page = await context.new_page()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def completar(self, proceso: "Proceso") -> None:
        """La API de listado no trae `fecha_limite`: se pide al detalle, solo para relevantes. Nunca lanza."""
        try:
            if getattr(proceso, "fecha_limite", None) is None:
                proceso.fecha_limite = await obtener_fecha_limite(self.page, proceso, self.cfg)
        except Exception as exc:  # noqa: BLE001 — completar nunca impide la descarga
            logger.warning("No se pudo completar %s: %s", getattr(proceso, "numero_referencia", "?"), exc)

    async def documentos(self, proceso: "Proceso") -> list[DocumentoPliego]:
        """El mejor PDF, con la URL ya firmada (caduca a los ~30 s: el pipeline lo baja de inmediato)."""
        palabras = getattr(self.portal, "palabras_documento", ()) or ()
        elegido = await _localizar_pliego(self.page, proceso, self.cfg, palabras)
        if not elegido:
            return []
        doc, url = elegido
        return [DocumentoPliego(
            nombre=str(doc.get("nombre_documento") or doc.get("ruta_documento") or ""),
            url=url, extension="pdf", puntaje=puntuar_documento(doc, palabras))]

    async def texto_base(self, proceso: "Proceso") -> str:
        return ""  # COMPRASAL no publica el pliego como texto fuera del PDF

    async def descargar(self, doc: DocumentoPliego, destino_dir: str, max_bytes: int) -> str | None:
        """1) `requests` en streaming (cero memoria de navegador); 2) request autenticado de Playwright."""
        path = None
        try:
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(
                None, downloader.download_pliego, doc.url, destino_dir, self.cfg.pliego_max_mb)
        except Exception as exc:  # noqa: BLE001
            logger.info("requests no pudo descargar %s (%s); probando Playwright", doc.nombre, exc)
        if not path:
            path = await _download_via_playwright(self.page, doc.url, destino_dir, max_bytes)
        return path
