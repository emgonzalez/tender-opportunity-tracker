"""Extractor de SICOP (Costa Rica) sobre su API pública, con un navegador anónimo real.

Contrato de la API: `docs/arquitectura/sicop-api.md`. Diseño: `docs/arquitectura/portales.md` §2.4.

Reglas que este módulo no negocia:

- **Sin login, sin cookies propias y sin falsificar el User-Agent.** El transporte por defecto es
  `NavegadorAnonimo` (importado tarde: cargar este módulo no carga Playwright).
- **Un bloqueo del WAF no es un dato ni un error transitorio.** El WAF de `prod-api` responde
  HTTP 200 con una página HTML; eso es `BloqueoAcceso` y **no se reintenta** (reintentar un bloqueo
  es evadirlo). Un JSON con forma inesperada es `ContratoRoto`.
- **Cortesía de tráfico:** pausa entre llamadas (la aplica el transporte), ventana por fecha de
  publicación con tope de 31 días, `pageSize` 50, `openbidDt` en un rango de menos de un año,
  partidas solo hasta `SICOP_MAX_DETALLES` y con caché de `SICOP_CACHE_DIAS` en `data/cache/sicop/`.
  El detalle pesado (`findPublishedById`) no se pide acá: lo pide el localizador solo para los
  candidatos.
- **Sin datos personales:** de cada fila se copian solo campos de una lista blanca; `executorNm`,
  `executorId` y demás funcionarios nunca llegan a `Proceso`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.persistencia.cache import escribir_json, leer_json
from src.portales import Portal
from src.portales.contrato import BloqueoAcceso, ContratoRoto, PortalNoDisponible
from src.portales.modalidades import MODALIDADES
from src.scraper.models import Proceso

if TYPE_CHECKING:
    from src.config import Config
    from src.portales.contrato import TransporteJSON

logger = logging.getLogger(__name__)

PORTAL_ID = "sicop_cr"
API_BASE = "https://prod-api.sicop.go.cr/"
ORIGEN_SPA = "https://www.sicop.go.cr/app/"

RUTA_LISTADO = "bid/api/v1/public/epCartel/searchEpCartels"
RUTA_PARTIDAS = "bid/api/v1/public/epCartelProd/findByEpCartelId"
RUTA_TOTALES = "bid/api/v1/public/epCartel/searchEpCartelsByType"  # health-check barato (GET)

ZONA = ZoneInfo("America/Costa_Rica")

# ── Perillas de cortesía (docs/arquitectura/portales.md §6). Se leen con getattr: funcionan aunque
# `Config` todavía no declare los atributos `sicop_*`. ─────────────────────────────────────────────
PAGE_SIZES_VALIDOS = (10, 20, 50)
PAGE_SIZE_DEFAULT = 50
MAX_PAGINAS_DEFAULT = 20
MAX_DETALLES_DEFAULT = 300
CACHE_DIAS_DEFAULT = 3
VENTANA_MAX_DIAS = 31
# [hoy−180, hoy+183]: 363 días, < 1 año. Es el rango de la petición REAL grabada en el spike
# (`epCartel_searchEpCartels_request.json`: 24/05/2026–22/05/2027 para el 20/11/2026). El texto de
# §4 de portales.md dice «+185», que daría 365 días justos, en el borde del 422 «rango máximo de un año».
OPENBID_ATRAS_DIAS = 180
OPENBID_ADELANTE_DIAS = 183
PARTIDAS_PAGE_SIZE = 100
PARTIDAS_MAX_PAGINAS = 5
DETALLE_MAX_CHARS = 600
UNSPSC_MAX = 20

# Modalidades a las que no se les piden partidas: la institución vende (remate).
SIN_PARTIDAS = frozenset({"RE"})

MARCA_BLOQUEO = "No fue posible acceder"


# ── Clasificación de respuestas ───────────────────────────────────────────────────────────────────

class _Transitorio(Exception):
    """5xx, timeout o error de red: el transporte reintenta y, si persiste, es `PortalNoDisponible`."""


def es_bloqueo_waf(status: int, content_type: str, texto: str) -> bool:
    """True si la respuesta es la página de bloqueo del WAF (o cualquier no-JSON donde se esperaba JSON).

    El WAF de SICOP (F5, `server: volt-adc`) responde **HTTP 200** con `text/html` o `text/plain`.
    Un 5xx sin la marca del WAF no es un bloqueo: es una caída transitoria del portal.
    """
    texto = texto or ""
    if MARCA_BLOQUEO in texto:
        return True
    if status >= 500 or status == 0:
        return False
    if "application/json" not in (content_type or "").lower():
        return True
    return texto.lstrip().startswith("<")


def _mensaje_error(texto: str) -> str:
    try:
        datos = json.loads(texto)
    except ValueError:
        return (texto or "")[:200]
    if isinstance(datos, dict):
        return str(datos.get("message") or datos.get("status") or "")[:200]
    return ""


def interpretar_respuesta(status: int, content_type: str, texto: str, *, url: str = "") -> Any:
    """JSON de la respuesta, o la excepción del contrato que corresponda (§2.4).

    Orden: transitorio (5xx/red, sin marca del WAF) → `BloqueoAcceso` → 4xx/no-2xx → JSON inválido.
    """
    ruta = url.replace(API_BASE, "")
    if (status >= 500 or status == 0) and MARCA_BLOQUEO not in (texto or ""):
        raise _Transitorio(f"HTTP {status} en {ruta}: {(texto or '')[:120]}")
    if es_bloqueo_waf(status, content_type, texto):
        raise BloqueoAcceso(
            PORTAL_ID, "el WAF de SICOP bloqueó la consulta (respuesta no JSON)",
            detalle=f"HTTP {status} {content_type!r} en {ruta}: {(texto or '')[:200]}",
        )
    if not 200 <= status < 300:
        raise ContratoRoto(
            PORTAL_ID, f"HTTP {status} en {ruta}", detalle=_mensaje_error(texto),
        )
    try:
        return json.loads(texto)
    except ValueError as exc:
        raise ContratoRoto(PORTAL_ID, f"JSON inválido en {ruta}", detalle=str(exc)) from exc


# ── Perillas ──────────────────────────────────────────────────────────────────────────────────────

def _perilla_int(config, nombre: str, default: int) -> int:
    valor = getattr(config, nombre, None)
    try:
        return int(valor) if valor is not None else default
    except (TypeError, ValueError):
        return default


def page_size(config) -> int:
    valor = _perilla_int(config, "sicop_page_size", PAGE_SIZE_DEFAULT)
    if valor not in PAGE_SIZES_VALIDOS:
        logger.warning("SICOP_PAGE_SIZE=%s no es válido (10, 20 o 50): se usa %s", valor, PAGE_SIZE_DEFAULT)
        return PAGE_SIZE_DEFAULT
    return valor


# ── Fechas ────────────────────────────────────────────────────────────────────────────────────────

_FORMATOS_FECHA = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parsear_fecha(valor: Any) -> datetime | None:
    """Fecha de SICOP (sin zona) en `America/Costa_Rica`; acepta `dd-MM-yyyy HH:mm:ss` e ISO."""
    if not valor or not isinstance(valor, str):
        return None
    texto = valor.strip()
    texto = re.sub(r"\.\d+$", "", texto)  # ISO con fracción de segundo
    for formato in _FORMATOS_FECHA:
        try:
            return datetime.strptime(texto, formato).replace(tzinfo=ZONA)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        return None
    return dt.astimezone(ZONA) if dt.tzinfo else dt.replace(tzinfo=ZONA)


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def ventana(desde: datetime, ahora: datetime | None = None) -> tuple[datetime, date]:
    """(`desde` efectivo en la zona del portal, hoy). Recorta a `VENTANA_MAX_DIAS` con un WARNING."""
    ahora = (ahora or datetime.now(ZONA)).astimezone(ZONA)
    desde = desde.astimezone(ZONA) if desde.tzinfo else desde.replace(tzinfo=ZONA)
    minimo = ahora - timedelta(days=VENTANA_MAX_DIAS)
    if desde < minimo:
        logger.warning(
            "SICOP: la ventana pedida (%s) supera %s días; se recorta a %s",
            desde.isoformat(), VENTANA_MAX_DIAS, minimo.isoformat(),
        )
        desde = minimo
    return desde, ahora.date()


def cuerpo_listado(desde: date, hoy: date, pagina: int, tamano: int) -> dict:
    """PageRequest de la búsqueda diaria (`epCartel_searchEpCartels_request.json`)."""
    return {
        "pageNumber": pagina,
        "pageSize": tamano,
        "tableSorter": {"sorter": "regDt", "order": "desc"},
        "tableFilters": [],
        "formFilters": [
            {"field": "cartelTestYn", "value": "Y"},
            {"field": "regDtFrom", "value": _ddmmyyyy(desde)},
            {"field": "regDtTo", "value": _ddmmyyyy(hoy)},
            {"field": "openbidDtFrom", "value": _ddmmyyyy(hoy - timedelta(days=OPENBID_ATRAS_DIAS))},
            {"field": "openbidDtTo", "value": _ddmmyyyy(hoy + timedelta(days=OPENBID_ADELANTE_DIAS))},
        ],
    }


def cuerpo_partidas(cartel_no: str, cartel_seq: str, pagina: int = 0) -> dict:
    return {
        "pageNumber": pagina,
        "pageSize": PARTIDAS_PAGE_SIZE,
        "tableSorter": {"sorter": "", "order": "desc"},
        "tableFilters": [],
        "formFilters": [
            {"field": "cartelNo", "value": cartel_no},
            {"field": "cartelSeq", "value": cartel_seq},
        ],
    }


# ── Validación del contrato ───────────────────────────────────────────────────────────────────────

CAMPOS_PAGINA = ("content", "totalElements")
CAMPOS_FILA = ("instCartelNo", "cartelNo", "regDt")


def _validar_pagina(datos: Any, ruta: str) -> list[dict]:
    if not isinstance(datos, dict):
        raise ContratoRoto(PORTAL_ID, f"{ruta}: se esperaba un objeto Page", detalle=type(datos).__name__)
    for campo in CAMPOS_PAGINA:
        if campo not in datos:
            raise ContratoRoto(PORTAL_ID, f"{ruta}: falta el campo {campo!r}", detalle=campo)
    filas = datos["content"]
    if not isinstance(filas, list) or not all(isinstance(f, dict) for f in filas):
        raise ContratoRoto(PORTAL_ID, f"{ruta}: 'content' no es una lista de objetos", detalle="content")
    return filas


def _validar_fila(fila: dict) -> None:
    for campo in CAMPOS_FILA:
        if not fila.get(campo):
            raise ContratoRoto(PORTAL_ID, f"fila del listado sin {campo!r}", detalle=campo)
    if parsear_fecha(fila["regDt"]) is None:
        raise ContratoRoto(PORTAL_ID, "regDt con formato desconocido", detalle=str(fila["regDt"])[:60])


# ── Caché de partidas ─────────────────────────────────────────────────────────────────────────────

class CacheSicop:
    """`data/cache/sicop/<cartelNo>-<cartelSeq>.<tipo>.json` con escritura atómica y TTL por mtime."""

    def __init__(self, config, *, dias: int | None = None) -> None:
        base = getattr(config, "data_dir", None) or "data"
        self.dir = os.path.join(base, "cache", "sicop")
        self.ttl_s = max(0, dias if dias is not None else _perilla_int(config, "sicop_cache_dias", CACHE_DIAS_DEFAULT)) * 86400

    @staticmethod
    def _seguro(texto: str) -> str:
        return re.sub(r"[^0-9A-Za-z_-]", "_", texto or "")

    def ruta(self, cartel_no: str, cartel_seq: str, tipo: str) -> str:
        return os.path.join(self.dir, f"{self._seguro(cartel_no)}-{self._seguro(cartel_seq)}.{self._seguro(tipo)}.json")

    def _vigente(self, ruta: str, ahora: float | None = None) -> bool:
        try:
            edad = (ahora if ahora is not None else time.time()) - os.path.getmtime(ruta)
        except OSError:
            return False
        return edad < self.ttl_s

    def leer(self, cartel_no: str, cartel_seq: str, tipo: str) -> Any | None:
        ruta = self.ruta(cartel_no, cartel_seq, tipo)
        if not self._vigente(ruta):
            return None
        return leer_json(ruta)

    def guardar(self, cartel_no: str, cartel_seq: str, tipo: str, dato: Any) -> None:
        try:
            escribir_json(self.ruta(cartel_no, cartel_seq, tipo), dato)
        except OSError:
            logger.warning("SICOP: no se pudo escribir la caché de %s-%s", cartel_no, cartel_seq, exc_info=True)

    def purgar(self, ahora: float | None = None) -> int:
        """Borra lo vencido. Nunca levanta."""
        borrados = 0
        try:
            nombres = os.listdir(self.dir)
        except OSError:
            return 0
        for nombre in nombres:
            ruta = os.path.join(self.dir, nombre)
            if nombre.endswith(".json") and not self._vigente(ruta, ahora):
                try:
                    os.unlink(ruta)
                    borrados += 1
                except OSError:
                    pass
        return borrados


# ── Mapeo a Proceso ───────────────────────────────────────────────────────────────────────────────

_NOMBRE_MODALIDAD = {m.codigo: m.nombre for m in MODALIDADES if m.portal_id == PORTAL_ID}


def instruccion_busqueda(numero_referencia: str) -> str:
    return f"Búscalo por número de procedimiento en SICOP: {numero_referencia}"


def _float(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def resumir_partidas(lineas: Iterable[dict]) -> dict[str, Any]:
    """Monto por moneda, `detalle` (≤ 600) y UNSPSC de las líneas de `epCartelProd`.

    Nunca se suman monedas distintas ni se rotula una moneda que la línea no trae.
    """
    por_moneda: dict[str, float] = {}
    nombres: list[str] = []
    unspsc: list[str] = []
    for linea in lineas:
        monto = _float(linea.get("budgetAmt"))
        if monto is not None:
            moneda = str(linea.get("currencyType") or "").strip().upper()
            por_moneda[moneda] = por_moneda.get(moneda, 0.0) + monto
        nombre = " ".join(str(linea.get("prodNm") or "").split())
        if nombre and nombre not in nombres:
            nombres.append(nombre)
        digitos = re.sub(r"\D", "", str(linea.get("prodId") or ""))[:8]
        if len(digitos) == 8 and digitos not in unspsc and len(unspsc) < UNSPSC_MAX:
            unspsc.append(digitos)

    resultado: dict[str, Any] = {"monto": None, "moneda": "", "montos_por_moneda": {}, "sin_moneda": None}
    conocidas = {k: v for k, v in por_moneda.items() if k}
    if "" in por_moneda:
        resultado["sin_moneda"] = round(por_moneda[""], 2)
    if len(conocidas) == 1 and "" not in por_moneda:
        (moneda, monto), = conocidas.items()
        resultado["monto"], resultado["moneda"] = round(monto, 2), moneda
    elif len(conocidas) > 1 or (conocidas and "" in por_moneda):
        resultado["montos_por_moneda"] = {k: round(v, 2) for k, v in sorted(conocidas.items())}
    elif "" in por_moneda:
        resultado["monto"] = round(por_moneda[""], 2)  # moneda "" = sin confirmar, nunca «USD»

    detalle = ""
    for nombre in nombres:
        candidato = f"{detalle} · {nombre}" if detalle else nombre
        if len(candidato) > DETALLE_MAX_CHARS:
            if not detalle:
                detalle = nombre[: DETALLE_MAX_CHARS - 1].rstrip() + "…"
            break
        detalle = candidato
    resultado["detalle"] = detalle
    resultado["unspsc"] = unspsc
    return resultado


def fila_a_proceso(fila: dict, portal: Portal) -> Proceso:
    """Una fila del listado → `Proceso` (sin partidas). Solo campos de la lista blanca: nada personal."""
    numero = str(fila["instCartelNo"]).strip()
    codigo = str(fila.get("proceType") or "").strip().upper()
    apertura = parsear_fecha(fila.get("openbidDt"))
    datos: dict[str, Any] = {
        "cartel_seq": str(fila.get("cartelSeq") or "00"),
        "estado": str(fila.get("cartelStatStr") or fila.get("cartelStat") or ""),
        "instruccion_busqueda": instruccion_busqueda(numero),
    }
    if apertura:
        datos["fecha_apertura"] = apertura.isoformat()
    if str(fila.get("cartelSecretYn") or "").upper() == "Y":
        datos["confidencial"] = True
    if str(fila.get("cartelClsYn") or "").upper() == "Y":
        datos["sin_efecto"] = True
    return Proceso(
        institucion=str(fila.get("cartelInstNm") or "").strip(),
        descripcion=str(fila.get("cartelNm") or "").strip(),
        fecha_publicacion=parsear_fecha(fila["regDt"]),
        url_directa=portal.url_consulta or ORIGEN_SPA,
        numero_referencia=numero,
        forma_contratacion=_NOMBRE_MODALIDAD.get(codigo, codigo),
        tipo_actividad="",
        fecha_limite=parsear_fecha(fila.get("biddocEndDt")),
        portal_id=portal.portal_id,
        id_externo=str(fila["cartelNo"]).strip(),
        modalidad_codigo=codigo,
        datos_portal=datos,
    )


def aplicar_partidas(proceso: Proceso, lineas: list[dict]) -> None:
    resumen = resumir_partidas(lineas)
    proceso.monto = resumen["monto"]
    proceso.moneda = resumen["moneda"]
    proceso.montos_por_moneda = resumen["montos_por_moneda"]
    proceso.detalle = resumen["detalle"]
    if resumen["unspsc"]:
        proceso.datos_portal["unspsc"] = resumen["unspsc"]
    if resumen["sin_moneda"] is not None and proceso.montos_por_moneda:
        proceso.datos_portal["monto_sin_moneda"] = resumen["sin_moneda"]


# ── Llamadas ──────────────────────────────────────────────────────────────────────────────────────

async def _listar(transporte: TransporteJSON, config, desde: datetime, hoy: date) -> list[dict]:
    tamano = page_size(config)
    max_paginas = max(1, _perilla_int(config, "sicop_max_paginas", MAX_PAGINAS_DEFAULT))
    filas: list[dict] = []
    for pagina in range(max_paginas):
        datos = await transporte.json("POST", API_BASE + RUTA_LISTADO, cuerpo_listado(desde.date(), hoy, pagina, tamano))
        contenido = _validar_pagina(datos, "searchEpCartels")
        for fila in contenido:
            _validar_fila(fila)
        filas.extend(contenido)
        if not contenido or datos.get("last", True):
            break
    else:
        logger.warning("SICOP: se alcanzó SICOP_MAX_PAGINAS=%s; puede haber concursos sin leer", max_paginas)
    return filas


async def _partidas(transporte: TransporteJSON, cartel_no: str, cartel_seq: str) -> list[dict]:
    lineas: list[dict] = []
    for pagina in range(PARTIDAS_MAX_PAGINAS):
        datos = await transporte.json("POST", API_BASE + RUTA_PARTIDAS, cuerpo_partidas(cartel_no, cartel_seq, pagina))
        contenido = _validar_pagina(datos, "epCartelProd")
        lineas.extend(contenido)
        if not contenido or datos.get("last", True):
            break
    return lineas


def _deduplicar(procesos: list[Proceso]) -> list[Proceso]:
    """Una fila por `numero_referencia`: la más reciente."""
    por_ref: dict[str, Proceso] = {}
    for p in procesos:
        actual = por_ref.get(p.numero_referencia)
        if actual is None or p.fecha_publicacion > actual.fecha_publicacion:
            por_ref[p.numero_referencia] = p
    return sorted(por_ref.values(), key=lambda p: p.fecha_publicacion, reverse=True)


async def _extraer_con(transporte: TransporteJSON, config, portal: Portal, desde: datetime) -> list[Proceso]:
    desde_ef, hoy = ventana(desde)
    cache = CacheSicop(config)
    purgados = cache.purgar()
    if purgados:
        logger.info("SICOP: %s archivo(s) de caché vencidos purgados", purgados)

    filas = await _listar(transporte, config, desde_ef, hoy)
    procesos = _deduplicar([
        p for p in (fila_a_proceso(f, portal) for f in filas) if p.fecha_publicacion >= desde_ef
    ])

    tope = max(0, _perilla_int(config, "sicop_max_detalles", MAX_DETALLES_DEFAULT))
    pedidos = 0
    sin_monto = 0
    for p in procesos:
        if p.modalidad_codigo in SIN_PARTIDAS or p.datos_portal.get("confidencial"):
            continue
        seq = p.datos_portal["cartel_seq"]
        lineas = cache.leer(p.id_externo, seq, "partidas")
        if lineas is None:
            if pedidos >= tope:
                sin_monto += 1
                continue
            pedidos += 1
            lineas = await _partidas(transporte, p.id_externo, seq)
            cache.guardar(p.id_externo, seq, "partidas", lineas)
        aplicar_partidas(p, lineas)
    if sin_monto:
        logger.warning(
            "SICOP: tope SICOP_MAX_DETALLES=%s alcanzado; %s concurso(s) salen sin partidas ni monto", tope, sin_monto,
        )
    logger.info("SICOP: %s concurso(s) desde %s (%s llamada(s) de partidas)", len(procesos), desde_ef.isoformat(), pedidos)
    return procesos


async def extraer(config: Config, portal: Portal, desde: datetime, *, transporte: TransporteJSON | None = None) -> list[Proceso]:
    """Concursos publicados en SICOP con `fecha_publicacion >= desde` (contrato de §2.2).

    `transporte` se inyecta en los tests; por defecto se abre un `NavegadorAnonimo` sobre la SPA.
    Lista vacía = el día no tuvo concursos. `BloqueoAcceso`/`ContratoRoto`/`PortalNoDisponible` se
    propagan al bucle por portal de `main`.
    """
    if transporte is not None:
        return await _extraer_con(transporte, config, portal, desde)
    from src.scraper.navegador_anonimo import NavegadorAnonimo

    async with NavegadorAnonimo(config, ORIGEN_SPA) as navegador:
        return await _extraer_con(navegador, config, portal, desde)


__all__ = [
    "API_BASE", "ORIGEN_SPA", "PORTAL_ID", "BloqueoAcceso", "ContratoRoto", "PortalNoDisponible",
    "es_bloqueo_waf", "extraer", "interpretar_respuesta",
]
