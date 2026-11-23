"""src.mercado.sicop_publica — adjudicaciones públicas de SICOP (Costa Rica).

Contrato de la API: `docs/arquitectura/sicop-api.md` §2 y §6. Diseño:
`docs/arquitectura/portales.md` §2.4 y §10.4 (A8). Dos fuentes, ambas
anónimas (sin cuenta, sin login), por `POST` con un PageRequest de Spring:

- ``bid/api/v1/public/epAwardedProcedureSupplier/listPage`` con
  ``supplierCd=<cédula>``: concursos **adjudicados a un proveedor**. Es la
  única vía pública para saber qué ganó Multicómputos. ``cartelCate`` y
  ``currencyType`` vienen ``null`` en lo observado.
- ``bid/api/v1/public/epAdjuFirmAwarded/listPage``: mercado completo,
  adjudicado en firme **por partida**, con ``crcAmt`` y ``usdAmt`` juntos.
  **No trae adjudicatario.** Es pesado: ~7 s por página y ~11 000 filas por
  mes, así que la paginación tiene tope.

**Acceso.** Este módulo NO abre navegador ni hace HTTP: recibe un
`TransporteJSON` (`src.portales.contrato`) ya abierto, que en producción es el
navegador anónimo de A3 (`src.scraper.navegador_anonimo`, sin User-Agent
falsificado) y en las pruebas un transporte falso. El WAF de SICOP responde
``200`` con HTML cuando bloquea: eso es `BloqueoAcceso` y **no se reintenta**
(reintentar un bloqueo es evadirlo). Entre llamadas hay una pausa de al menos
`PAUSA_MIN_S` segundos.

**Limitación (GANADO sí, PERDIDO no).** Con estas fuentes se sabe qué concursos
se adjudicaron a una cédula de Multicómputos (GANADO), pero no en cuáles
ofertó sin ganar: los oferentes de un concurso solo salen en el reporte de
datos abiertos «Ofertas» (``SV_CONT_0012``), que exige un código por correo
(P3 del spike) o en JSP heredadas sin contrato. Por eso `src.mercado` escribe
en ``proceso_participado`` **solo GANADO** para SICOP; PERDIDO queda sin
inferir hasta que exista esa fuente.

Las rutas se definen acá a propósito: hasta que A3 mergee, `src.mercado` no
importa `src.scraper.sicop` (`portales.md` §10.3, E4).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable

from src.portales.contrato import BloqueoAcceso, ContratoRoto, PortalNoDisponible

logger = logging.getLogger(__name__)

PORTAL_ID = "sicop_cr"

# Iguales a las constantes de E4 (`src.scraper.sicop`); locales hasta que A3 mergee.
API_BASE = "https://prod-api.sicop.go.cr/"
ORIGEN_SPA = "https://www.sicop.go.cr/app/"

URL_ADJUDICADO_PROVEEDOR = f"{API_BASE}bid/api/v1/public/epAwardedProcedureSupplier/listPage"
URL_ADJUDICADO_FIRME = f"{API_BASE}bid/api/v1/public/epAdjuFirmAwarded/listPage"
URL_BUSCAR_PROVEEDOR = f"{API_BASE}search-engine/api/v1/public/umPublicSupplier/searchUmPublicSupplier"

PAUSA_MIN_S = 1.0
TAMANOS_PAGINA = (10, 20, 50)     # los únicos que la API acepta (422 con otro valor)
TAMANO_PAGINA = 50
# Tope de páginas por consulta. Un mes de `epAdjuFirmAwarded` son ~221 páginas de 50 (~26 min a ~7 s);
# 100 páginas acota una corrida a ~12 minutos. Lo que pase del tope se registra como truncado.
MAX_PAGINAS = 100
REINTENTOS_ESPERAS_S = (5.0, 15.0)  # solo para errores transitorios (5xx, red); nunca para un bloqueo

# Centinelas de la PK de `adjudicacion_partida`: Oracle trata '' como NULL y la PK no admite NULL.
PARTIDA_SIN_DETALLE = "*"         # epAwardedProcedureSupplier no trae partida (cartelCate null)
PROVEEDOR_DESCONOCIDO = "-"       # epAdjuFirmAwarded no trae adjudicatario

# Zona de SICOP: America/Costa_Rica = UTC-6 fijo, sin horario de verano (sicop-api.md §2).
_OFFSET_CR = timedelta(hours=6)

_MARCAS_WAF = ("No fue posible acceder",)


def es_bloqueo_waf(status: int, content_type: str, texto: str) -> bool:
    """Misma regla que E4 (`src.scraper.sicop.es_bloqueo_waf`): HTML o texto donde se esperaba JSON."""
    cuerpo = (texto or "").lstrip()
    if any(m in cuerpo for m in _MARCAS_WAF):
        return True
    if cuerpo.startswith("<"):
        return True
    return bool(content_type) and "application/json" not in content_type.lower()


def iso_utc_medianoche_cr(dia: date) -> str:
    """Medianoche de Costa Rica expresada en UTC, como la manda la SPA (``2025-11-23T06:00:00.000Z``)."""
    instante = datetime(dia.year, dia.month, dia.day) + _OFFSET_CR
    return instante.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cuerpo_pagina(numero: int, filtros: dict[str, Any], tamano: int = TAMANO_PAGINA) -> dict[str, Any]:
    """PageRequest de la SPA (`sicop-api.md` §3). Descarta filtros `None` o `""` como `createFormFilter`."""
    return {
        "pageNumber": numero,
        "pageSize": tamano if tamano in TAMANOS_PAGINA else TAMANO_PAGINA,
        "tableSorter": {"sorter": "", "order": "desc"},
        "tableFilters": [],
        "formFilters": [{"field": k, "value": v} for k, v in filtros.items() if v not in (None, "")],
    }


@dataclass
class ClienteSicopPublico:
    """Cliente de las adjudicaciones públicas sobre un `TransporteJSON` inyectado.

    `dormir` y `reloj` se inyectan para probar sin esperar de verdad. `truncado` queda en True si alguna
    consulta llegó al tope de páginas sin terminar.
    """
    transporte: Any
    pausa_s: float = PAUSA_MIN_S
    max_paginas: int = MAX_PAGINAS
    tamano_pagina: int = TAMANO_PAGINA
    dormir: Callable[[float], Awaitable[None]] = asyncio.sleep
    reloj: Callable[[], float] = time.monotonic
    llamadas: int = 0
    truncado: bool = False
    _ultima: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.pausa_s = max(float(self.pausa_s), PAUSA_MIN_S)
        self.max_paginas = max(int(self.max_paginas), 1)

    # ── llamada única ────────────────────────────────────────────────────────
    async def _pausa(self) -> None:
        if self._ultima is not None:
            espera = self.pausa_s - (self.reloj() - self._ultima)
            if espera > 0:
                await self.dormir(espera)

    async def _llamar(self, url: str, cuerpo: dict[str, Any]) -> Any:
        """Una llamada con pausa previa y reintentos SOLO para lo transitorio."""
        intentos = len(REINTENTOS_ESPERAS_S) + 1
        ultimo: str = ""
        for intento in range(intentos):
            await self._pausa()
            self.llamadas += 1
            try:
                crudo = await self.transporte.json("POST", url, cuerpo)
                return _interpretar(crudo, url)
            except (BloqueoAcceso, ContratoRoto):
                raise                                   # nunca se reintenta
            except (PortalNoDisponible, _Transitorio, TimeoutError, ConnectionError, OSError) as exc:
                ultimo = f"{type(exc).__name__}: {exc}"
            finally:
                self._ultima = self.reloj()
            if intento < intentos - 1:
                espera = REINTENTOS_ESPERAS_S[intento]
                logger.warning("SICOP %s: fallo transitorio (%s); reintento en %.0f s.", _ruta(url), ultimo, espera)
                await self.dormir(espera)
        raise PortalNoDisponible(PORTAL_ID, f"{_ruta(url)} no responde tras {intentos} intentos", detalle=ultimo)

    # ── paginación ───────────────────────────────────────────────────────────
    async def paginar(self, url: str, filtros: dict[str, Any], *, campos: tuple[str, ...]) -> AsyncIterator[list[dict]]:
        """Entrega una página (lista de filas validadas) por vez.

        Se detiene con `last`, con una página vacía o con `max_paginas` (WARNING y `truncado=True`).
        """
        for numero in range(self.max_paginas):
            pagina = await self._llamar(url, cuerpo_pagina(numero, filtros, self.tamano_pagina))
            filas = _validar_pagina(pagina, url, campos)
            if filas:
                yield filas
            ultima = pagina.get("last")
            if ultima is None:
                ultima = numero + 1 >= int(pagina.get("totalPages") or 0)
            if not filas or ultima:
                return
        self.truncado = True
        logger.warning(
            "SICOP %s: se llegó al tope de %d página(s) sin terminar (filtros=%s); el resto queda sin cargar.",
            _ruta(url), self.max_paginas, filtros,
        )

    # ── consultas ────────────────────────────────────────────────────────────
    def adjudicado_a_proveedor(self, cedula: str, desde: date, hasta: date) -> AsyncIterator[list[dict]]:
        """Concursos adjudicados a una cédula (`epAwardedProcedureSupplier`): la fuente de GANADO."""
        filtros = {"startDt": iso_utc_medianoche_cr(desde), "endDt": iso_utc_medianoche_cr(hasta),
                   "supplierCd": cedula}
        return self.paginar(URL_ADJUDICADO_PROVEEDOR, filtros, campos=("instCartelNo", "cartelNo"))

    def adjudicado_en_firme(self, desde: date, hasta: date, *, inst_cartel_no: str | None = None,
                            ) -> AsyncIterator[list[dict]]:
        """Mercado completo por partida (`epAdjuFirmAwarded`), en CRC y USD. Pesado: ~7 s por página."""
        filtros = {"bgnYmd": iso_utc_medianoche_cr(desde), "endYmd": iso_utc_medianoche_cr(hasta),
                   "instCartelNo": inst_cartel_no}
        return self.paginar(URL_ADJUDICADO_FIRME, filtros, campos=("instCartelNo",))

    async def buscar_proveedor(self, *, nombre: str | None = None, cedula: str | None = None) -> list[dict]:
        """Búsqueda pública de proveedores (una página). Sirve para verificar una cédula a mano."""
        pagina = await self._llamar(URL_BUSCAR_PROVEEDOR,
                                    cuerpo_pagina(0, {"supplierNm": nombre, "bizRegNo": cedula}, self.tamano_pagina))
        return _validar_pagina(pagina, URL_BUSCAR_PROVEEDOR, ("bizRegNo",))


class _Transitorio(Exception):
    """5xx dentro de un sobre `{status, contentType, texto}`: se reintenta."""


def _ruta(url: str) -> str:
    return url.removeprefix(API_BASE)


def _interpretar(crudo: Any, url: str) -> Any:
    """Normaliza lo que devuelve el transporte y clasifica errores (`portales.md` §2.4).

    Acepta el JSON ya parseado, un texto (cuerpo crudo) o el sobre ``{status, contentType, texto}`` que arma
    el `fetch` del navegador anónimo.
    """
    if isinstance(crudo, dict) and "texto" in crudo and "status" in crudo:
        status = int(crudo.get("status") or 0)
        tipo = str(crudo.get("contentType") or "")
        texto = str(crudo.get("texto") or "")
        if es_bloqueo_waf(status, tipo, texto):
            raise BloqueoAcceso(PORTAL_ID, f"{_ruta(url)}: el WAF respondió HTML", detalle=texto[:200])
        if status >= 500:
            raise _Transitorio(f"HTTP {status}")
        crudo = _json_o_contrato(texto, url)
        if status != 200 and not _es_error_api(crudo):
            raise ContratoRoto(PORTAL_ID, f"{_ruta(url)}: HTTP {status}", detalle=texto[:200])
    elif isinstance(crudo, (str, bytes)):
        texto = crudo.decode("utf-8", "replace") if isinstance(crudo, bytes) else crudo
        if es_bloqueo_waf(200, "", texto):
            raise BloqueoAcceso(PORTAL_ID, f"{_ruta(url)}: el WAF respondió HTML", detalle=texto[:200])
        crudo = _json_o_contrato(texto, url)
    if _es_error_api(crudo):
        raise ContratoRoto(PORTAL_ID, f"{_ruta(url)}: {crudo.get('message')}",
                           detalle=json.dumps(crudo, ensure_ascii=False)[:300])
    return crudo


def _json_o_contrato(texto: str, url: str) -> Any:
    try:
        return json.loads(texto)
    except ValueError:
        raise ContratoRoto(PORTAL_ID, f"{_ruta(url)}: la respuesta no es JSON", detalle=texto[:200]) from None


def _es_error_api(valor: Any) -> bool:
    """`422` de la API: ``{status, message, module, userError}``."""
    return isinstance(valor, dict) and "message" in valor and "status" in valor and "content" not in valor


def _validar_pagina(pagina: Any, url: str, campos: tuple[str, ...]) -> list[dict]:
    if not isinstance(pagina, dict) or not isinstance(pagina.get("content"), list):
        raise ContratoRoto(PORTAL_ID, f"{_ruta(url)}: falta `content` en la página", detalle=str(pagina)[:200])
    filas = pagina["content"]
    for fila in filas:
        if not isinstance(fila, dict):
            raise ContratoRoto(PORTAL_ID, f"{_ruta(url)}: fila que no es objeto", detalle=str(fila)[:200])
        for campo in campos:
            if fila.get(campo) in (None, ""):
                raise ContratoRoto(PORTAL_ID, f"{_ruta(url)}: falta `{campo}` en una fila",
                                   detalle=json.dumps(fila, ensure_ascii=False, default=str)[:300])
    return filas


# ── normalización a filas de `adjudicacion_partida` ─────────────────────────


def parse_fecha_sicop(valor: Any) -> datetime | None:
    """`"30/10/2026 17:26"` (epAdjuFirmAwarded), ISO (`"2026-04-13T15:52:16"`) o `dd-MM-yyyy HH:mm:ss`.

    Hora local de Costa Rica, sin zona (como la publica SICOP). Nunca lanza: un formato raro da None.
    """
    if not valor:
        return None
    texto = str(valor).strip().rstrip("Z").split(".")[0]
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    logger.warning("SICOP: fecha con formato inesperado, se ignora: %r", valor)
    return None


def _monto(valor: Any) -> float | None:
    if valor in (None, ""):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _texto(valor: Any) -> str:
    return str(valor).strip() if valor is not None else ""


def fila_firme(fila: dict) -> dict[str, Any]:
    """Una partida adjudicada en firme (mercado completo). Sin adjudicatario: proveedor desconocido."""
    partida = _texto(fila.get("cartelCate")) or PARTIDA_SIN_DETALLE
    return {
        "portal_id": PORTAL_ID,
        "codigo_proceso": _texto(fila.get("instCartelNo")),
        "id_externo": _texto(fila.get("cartelNo")),
        "partida": partida,
        "nombre_proceso": _texto(fila.get("cartelNm")),
        "institucion": _texto(fila.get("instNm")),
        "modalidad_codigo": _texto(fila.get("proceType")),
        "fecha_adjudicacion": parse_fecha_sicop(fila.get("adjuFinalDt")),
        "monto_crc": _monto(fila.get("crcAmt")),
        "monto_usd": _monto(fila.get("usdAmt")),
        "proveedor_id": PROVEEDOR_DESCONOCIDO,
        "es_propio": False,
        "datos_crudos": fila,
    }


def fila_adjudicada_a(fila: dict, cedula: str, *, es_propio: bool) -> dict[str, Any]:
    """Un concurso adjudicado a `cedula` (epAwardedProcedureSupplier).

    `submitAmt` se asigna a la columna de su `currencyType` (CRC o USD). Si la moneda viene `null` —lo
    observado— el monto queda solo en `datos_crudos`: un monto sin moneda conocida no se rotula (D9).
    """
    moneda = _texto(fila.get("currencyType")).upper()
    monto = _monto(fila.get("submitAmt"))
    return {
        "portal_id": PORTAL_ID,
        "codigo_proceso": _texto(fila.get("instCartelNo")),
        "id_externo": _texto(fila.get("cartelNo")),
        "partida": _texto(fila.get("cartelCate")) or PARTIDA_SIN_DETALLE,
        "nombre_proceso": _texto(fila.get("cartelNm")),
        "institucion": _texto(fila.get("instNm")),
        "modalidad_codigo": _texto(fila.get("proceType")),
        "fecha_adjudicacion": parse_fecha_sicop(fila.get("adjuFinalDt")),
        "monto_crc": monto if moneda == "CRC" else None,
        "monto_usd": monto if moneda == "USD" else None,
        "proveedor_id": cedula,
        "es_propio": es_propio,
        "datos_crudos": fila,
    }


__all__ = [
    "API_BASE", "ORIGEN_SPA", "PORTAL_ID", "URL_ADJUDICADO_FIRME", "URL_ADJUDICADO_PROVEEDOR",
    "URL_BUSCAR_PROVEEDOR", "ClienteSicopPublico", "PARTIDA_SIN_DETALLE",
    "PROVEEDOR_DESCONOCIDO", "cuerpo_pagina", "es_bloqueo_waf", "fila_adjudicada_a", "fila_firme",
    "iso_utc_medianoche_cr", "parse_fecha_sicop",
]
