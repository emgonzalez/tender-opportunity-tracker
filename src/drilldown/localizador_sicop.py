"""src.drilldown.localizador_sicop — `LocalizadorPliego` de SICOP (Costa Rica).

Contrato en `docs/arquitectura/portales.md` §2.3; API en `docs/arquitectura/sicop-api.md` §3.2-§3.4 y §7.

Qué hace, por concurso relevante (concurrencia 1: la VM es de 1 GB):

- `completar`: pide el **detalle** (`epCartel/findPublishedById`, ~208 campos con HTML) y rellena lo que
  el listado no trajo o trajo viejo: `fecha_limite` (`biddocEndDt`), `tipo_actividad` (`ppsTypeNm`),
  `forma_contratacion`/`modalidad_codigo` si faltan, y `monto`/`moneda` **solo** si el extractor no
  los trajo de las partidas (`cartelBudget`). Nunca persiste datos de funcionarios. Nunca lanza.
- `documentos`: metadatos de adjuntos (`epCartelFile/getCartelFilesById`), puntuados con
  `Portal.palabras_documento` (`locator.puntuar_documento`). Solo PDF, DOCX y XLSX; `mp4`, `mpp`,
  `png` y `zip` (y cualquier otra cosa) quedan fuera.
- `texto_base`: el cartel que SICOP publica como HTML dentro del JSON del detalle
  (`generalCondsCont`, `specCondsCont`, `adminCondsCont`), pasado a texto.
- `descargar`: `comnFile/download?fileUrl=…` por el transporte, respetando `max_bytes` antes de bajar.

**Transporte.** Todo pasa por un `TransporteJSON` (el navegador anónimo de A3, que nunca fija el
User-Agent). Se inyecta en tests; en producción se importa tarde, dentro de `__aenter__`. Este módulo no
importa `src.scraper.sicop`: las rutas de los endpoints se declaran aquí (§10.3, E4).

**Bloqueo del WAF.** Si el transporte levanta `BloqueoAcceso` (o una descarga trae la página del WAF),
el localizador deja de llamar a SICOP por el resto del lote: reintentar un bloqueo sería evadirlo (D4).

**Datos no confiables.** El texto del cartel y los adjuntos son datos de terceros: aquí solo se
extraen y se registran los indicios de instrucciones (`drilldown.inyeccion`); nunca se interpretan.
"""
from __future__ import annotations

import logging
import math
import os
import re
import urllib.parse
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from src.drilldown.locator import puntuar_documento
from src.portales.contrato import BloqueoAcceso, DocumentoPliego

if TYPE_CHECKING:
    from src.config import Config
    from src.portales import Portal
    from src.portales.contrato import TransporteJSON
    from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

# Rutas declaradas localmente hasta que A3 mergee `src/scraper/sicop.py` (mismos valores que E4).
API_BASE = "https://prod-api.sicop.go.cr/"
ORIGEN_SPA = "https://www.sicop.go.cr/app/"
RUTA_DETALLE = "bid/api/v1/public/epCartel/findPublishedById"
RUTA_ADJUNTOS = "bid/api/v1/public/epCartelFile/getCartelFilesById"
RUTA_DESCARGA = "comn/api/v1/public/comnFile/download"

# Formatos que el drill-down sabe leer, en orden de preferencia (PDF primero; DOCX y XLSX de respaldo).
EXTENSIONES_ACEPTADAS: tuple[str, ...] = ("pdf", "docx", "xlsx")
# Medidos en la muestra del spike (§7) y excluidos a propósito: no son texto analizable.
EXTENSIONES_EXCLUIDAS = frozenset({"mp4", "mpp", "png", "zip"})

_FIRMAS = {"pdf": (b"%PDF",), "docx": (b"PK",), "xlsx": (b"PK",)}

# Secciones del cartel que vienen como HTML en el detalle, con el rótulo que se antepone a cada una.
SECCIONES_CARTEL: tuple[tuple[str, str], ...] = (
    ("generalCondsCont", "Condiciones generales"),
    ("specCondsCont", "Condiciones específicas"),
    ("adminCondsCont", "Condiciones administrativas"),
)

_TZ_RESPALDO = timezone(timedelta(hours=-6))  # Costa Rica: UTC-6 sin horario de verano


# ── Utilidades puras ─────────────────────────────────────────────────────────

class _ExtractorTexto(HTMLParser):
    """HTML → texto: un salto de línea por bloque, sin scripts ni estilos. Las entidades se decodifican."""

    _BLOQUES = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                          "table", "ul", "ol", "section", "article", "blockquote", "pre"})
    _CELDAS = frozenset({"td", "th"})
    _OMITIR = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._omitir = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._OMITIR:
            self._omitir += 1
        elif tag in self._BLOQUES:
            self.partes.append("\n")
        elif tag in self._CELDAS:
            self.partes.append("\t")

    def handle_endtag(self, tag):
        if tag in self._OMITIR:
            self._omitir = max(0, self._omitir - 1)
        elif tag in self._BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self._omitir:
            self.partes.append(data)


def html_a_texto(html: Any) -> str:
    """Texto legible de un fragmento HTML; "" si no es texto. Nunca lanza."""
    if not isinstance(html, str) or not html.strip():
        return ""
    parser = _ExtractorTexto()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — HTML malformado: lo que se alcanzó a leer
        pass
    lineas = []
    for linea in "".join(parser.partes).replace("\r", "").split("\n"):
        linea = re.sub(r"[ \t\xa0]+", " ", linea).strip()
        if linea:
            lineas.append(linea)
    return "\n".join(lineas)


def _zona(portal: Portal | None):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(getattr(portal, "zona_horaria", "") or "America/Costa_Rica")
    except Exception:  # noqa: BLE001 — sin base de zonas: el offset fijo es exacto para CR
        return _TZ_RESPALDO


def parse_fecha(texto: Any, zona=_TZ_RESPALDO) -> datetime | None:
    """Fecha de SICOP (sin zona) como datetime aware en la zona del portal.

    Acepta ISO (`2026-11-06T23:59:00`, detalle) y `dd-MM-yyyy HH:mm:ss` (listado); None si no es fecha.
    """
    if not isinstance(texto, str) or not texto.strip():
        return None
    limpio = texto.strip().split(".")[0].rstrip("Z")
    for formato in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(limpio, formato).replace(tzinfo=zona)
        except ValueError:
            continue
    return None


def _monto(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) and numero > 0 else None


def _tamano_bytes(file_size_mb: Any) -> int | None:
    try:
        mb = float(str(file_size_mb).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(mb * 1024 * 1024) if math.isfinite(mb) and mb >= 0 else None


def _extension(*nombres: Any) -> str:
    for nombre in nombres:
        if isinstance(nombre, str) and "." in nombre:
            return nombre.rsplit(".", 1)[-1].strip().lower()
    return ""


def url_descarga(adjunto: dict) -> str:
    """`comnFile/download?fileUrl=<fileServerPath sin "/" inicial>/<serverFileNm>`, codificada."""
    ruta = f"{str(adjunto.get('fileServerPath') or '').strip('/')}/{adjunto.get('serverFileNm') or ''}"
    return f"{API_BASE}{RUTA_DESCARGA}?fileUrl={urllib.parse.quote(ruta.lstrip('/'), safe='')}"


def documentos_de_adjuntos(adjuntos: Any, palabras: tuple[str, ...] = ()) -> list[DocumentoPliego]:
    """Adjuntos de SICOP → `DocumentoPliego` aceptables, del mejor candidato al peor.

    Orden: extensión (PDF, DOCX, XLSX) y, dentro de cada una, puntaje por nombre y luego el más grande
    (el método del spike: «el que tenga pliego/cartel/condiciones en el nombre; si no, el PDF más
    grande»). `pipeline.descargar_pliegos` decide entre ellos y el texto del detalle.
    """
    if isinstance(adjuntos, dict):
        adjuntos = adjuntos.get("content") or adjuntos.get("data") or []
    if not isinstance(adjuntos, list):
        return []
    docs = []
    for a in adjuntos:
        if not isinstance(a, dict) or not a.get("serverFileNm"):
            continue
        extension = _extension(a.get("clientFileNm"), a.get("serverFileNm"))
        if extension in EXTENSIONES_EXCLUIDAS or extension not in EXTENSIONES_ACEPTADAS:
            continue
        nombre = str(a.get("clientFileNm") or a.get("docNm") or a.get("serverFileNm"))
        puntaje = puntuar_documento(
            {"nombre_documento": f"{a.get('docNm') or ''} {nombre}",
             "ruta_documento": f"{a.get('serverFileNm')}",
             "categoria": a.get("fileTypeNm") or ""},
            palabras, EXTENSIONES_ACEPTADAS)
        if puntaje < 0:
            continue
        docs.append(DocumentoPliego(
            nombre=nombre, url=url_descarga(a), extension=extension,
            tamano_bytes=_tamano_bytes(a.get("fileSizeMb")),
            categoria=str(a.get("fileTypeNm") or ""), puntaje=puntaje))
    docs.sort(key=lambda d: (EXTENSIONES_ACEPTADAS.index(d.extension), -d.puntaje,
                             -(d.tamano_bytes or 0)))
    return docs


def texto_del_cartel(detalle: Any) -> str:
    """Las tres secciones `*CondsCont` del detalle, como texto, con su rótulo. "" si no hay ninguna."""
    if not isinstance(detalle, dict):
        return ""
    bloques = []
    for campo, rotulo in SECCIONES_CARTEL:
        texto = html_a_texto(detalle.get(campo))
        if texto:
            bloques.append(f"{rotulo}\n{texto}")
    return "\n\n".join(bloques)


def _es_pagina_waf(cuerpo: bytes) -> bool:
    cabeza = cuerpo[:4096].lstrip().lower()
    return cabeza.startswith(b"<") or "no fue posible acceder".encode() in cabeza


# ── Localizador ──────────────────────────────────────────────────────────────

class LocalizadorSicop:
    """`LocalizadorPliego` de SICOP sobre un `TransporteJSON` (inyectado o el navegador anónimo de A3)."""

    def __init__(self, config: Config, portal: Portal | None = None, *,
                 transporte: TransporteJSON | None = None) -> None:
        if portal is None:
            from src.portales import obtener_portal
            portal = obtener_portal("sicop_cr")
        self.cfg = config
        self.portal = portal
        self._transporte = transporte
        self._propio = None          # el navegador que abre este localizador (si no se inyectó)
        self._detalles: dict[tuple[str, str], dict | None] = {}
        self.bloqueado = False

    async def __aenter__(self) -> LocalizadorSicop:
        if self._transporte is None:
            # Import tardío: A3 (`src/scraper/navegador_anonimo.py`) es quien abre Chromium sin fijar el UA.
            from src.scraper.navegador_anonimo import NavegadorAnonimo
            self._propio = NavegadorAnonimo(self.cfg, ORIGEN_SPA)
            self._transporte = await self._propio.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._propio is not None:
            propio, self._propio = self._propio, None
            self._transporte = None
            await propio.__aexit__(*(exc or (None, None, None)))

    # -- llamadas ------------------------------------------------------------

    def _ids(self, proceso: Proceso) -> tuple[str, str] | None:
        cartel = str(getattr(proceso, "id_externo", "") or "").strip()
        if not cartel:
            return None
        datos = getattr(proceso, "datos_portal", None) or {}
        return cartel, str(datos.get("cartel_seq") or "00").strip() or "00"

    async def _json(self, metodo: str, ruta: str, cuerpo: Any | None = None) -> Any:
        if self.bloqueado or self._transporte is None:
            return None
        try:
            return await self._transporte.json(metodo, f"{API_BASE}{ruta}", cuerpo)
        except BloqueoAcceso as exc:
            self.bloqueado = True
            logger.error("SICOP bloqueó el acceso (WAF); se detiene el drill-down del lote: %s", exc)
            return None

    async def _detalle(self, proceso: Proceso) -> dict | None:
        ids = self._ids(proceso)
        if ids is None:
            logger.info("Concurso %s sin id_externo (cartelNo): sin detalle",
                        getattr(proceso, "numero_referencia", "?"))
            return None
        if ids not in self._detalles:
            datos = await self._json("POST", RUTA_DETALLE, {"cartelNo": ids[0], "cartelSeq": ids[1]})
            if isinstance(datos, dict) and isinstance(datos.get("data"), dict):
                datos = datos["data"]
            self._detalles[ids] = datos if isinstance(datos, dict) and datos else None
        return self._detalles[ids]

    # -- contrato LocalizadorPliego -------------------------------------------

    async def completar(self, proceso: Proceso) -> None:
        """Fecha límite, tipo de actividad y monto del detalle. Muta `proceso`; NUNCA lanza."""
        ref = getattr(proceso, "numero_referencia", "?")
        try:
            detalle = await self._detalle(proceso)
            if not detalle:
                return
            zona = _zona(self.portal)
            # El detalle es la versión vigente publicada: su cierre manda sobre el del listado.
            cierre = parse_fecha(detalle.get("biddocEndDt"), zona)
            if cierre is not None:
                proceso.fecha_limite = cierre
            if not getattr(proceso, "tipo_actividad", ""):
                proceso.tipo_actividad = str(detalle.get("ppsTypeNm") or "").strip()
            if not getattr(proceso, "modalidad_codigo", "") and detalle.get("proceType"):
                proceso.modalidad_codigo = str(detalle["proceType"]).strip().upper()
            if not getattr(proceso, "forma_contratacion", "") and detalle.get("proceTypeNm"):
                proceso.forma_contratacion = str(detalle["proceTypeNm"]).strip()
            # Monto: el de las partidas (extractor) manda. Solo se completa si el portal no lo trajo.
            if getattr(proceso, "monto", None) is None and not getattr(proceso, "montos_por_moneda", None):
                crc, usd = _monto(detalle.get("cartelBudget")), _monto(detalle.get("cartelBudgetUsd"))
                if crc is not None:
                    # `cartelBudget` coincidió con partidas en CRC en la muestra, pero sin un concurso en
                    # dólares no está verificado (P5): sin moneda explícita no se rotula (D9).
                    proceso.monto = crc
                elif usd is not None:
                    proceso.monto, proceso.moneda = usd, "USD"
            datos = getattr(proceso, "datos_portal", None)
            if isinstance(datos, dict):
                for clave, campo in (("fecha_apertura", "openbidDt"), ("estado", "cartelStatStr"),
                                     ("tipo_detalle", "proceTypeDetailNm"),
                                     ("fecha_aclaraciones", "explainRcvEndDt")):
                    valor = detalle.get(campo)
                    if isinstance(valor, str) and valor.strip() and not datos.get(clave):
                        datos[clave] = valor.strip()
        except Exception as exc:  # noqa: BLE001 — completar nunca tumba la descarga
            logger.warning("No se pudo completar el concurso %s: %s", ref, exc)

    def _confidencial(self, proceso: Proceso) -> bool:
        ids = self._ids(proceso)
        detalle = self._detalles.get(ids) if ids else None
        return bool(detalle and str(detalle.get("cartelSecretYn") or "").upper() == "Y")

    async def documentos(self, proceso: Proceso) -> list[DocumentoPliego]:
        ids = self._ids(proceso)
        if ids is None or self._confidencial(proceso):
            return []
        adjuntos = await self._json(
            "GET", f"{RUTA_ADJUNTOS}?{urllib.parse.urlencode({'cartelNo': ids[0], 'cartelSeq': ids[1], 'docFileSeqno': ''})}")
        return documentos_de_adjuntos(adjuntos, tuple(self.portal.palabras_documento or ()))

    async def texto_base(self, proceso: Proceso) -> str:
        if self._confidencial(proceso):
            return ""
        texto = texto_del_cartel(await self._detalle(proceso))
        if texto:
            from src.drilldown.inyeccion import detectar_inyeccion
            hallazgos = detectar_inyeccion(texto)
            if hallazgos:
                logger.warning("Concurso %s: %d indicio(s) de instrucciones en el cartel (%s); se trata como dato",
                               getattr(proceso, "numero_referencia", "?"), len(hallazgos),
                               ", ".join(sorted({h.patron for h in hallazgos})))
        return texto

    async def descargar(self, doc: DocumentoPliego, destino_dir: str, max_bytes: int) -> str | None:
        if self.bloqueado or self._transporte is None:
            return None
        if doc.extension not in EXTENSIONES_ACEPTADAS:
            return None
        if doc.tamano_bytes is not None and doc.tamano_bytes > max_bytes:
            logger.warning("DESCARTADO_TAMANO (%d bytes declarados) para %s", doc.tamano_bytes, doc.nombre)
            return None
        try:
            cuerpo = await self._transporte.binario(doc.url, max_bytes)
        except BloqueoAcceso as exc:
            self.bloqueado = True
            logger.error("SICOP bloqueó la descarga (WAF); se detiene el drill-down del lote: %s", exc)
            return None
        if not cuerpo:
            return None
        if len(cuerpo) > max_bytes:
            logger.warning("DESCARTADO_TAMANO (%d bytes) para %s", len(cuerpo), doc.nombre)
            return None
        if not cuerpo.startswith(_FIRMAS[doc.extension]):
            if _es_pagina_waf(cuerpo):
                self.bloqueado = True
                logger.error("La descarga de %s trajo la página del WAF; se detiene el drill-down del lote",
                             doc.nombre)
            else:
                logger.warning("La descarga de %s no es un %s válido; se descarta", doc.nombre,
                               doc.extension.upper())
            return None
        os.makedirs(destino_dir, exist_ok=True)
        ruta = os.path.join(destino_dir, f"{_uuid.uuid4().hex}.{doc.extension}")
        with open(ruta, "wb") as f:
            f.write(cuerpo)
        return ruta
