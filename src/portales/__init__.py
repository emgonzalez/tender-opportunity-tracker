"""src.portales — registro de los portales de compras públicas que AMALIA conoce.

Un cliente declara en su YAML (`client.portal`) de qué portal lee; sin ese campo es COMPRASAL
(El Salvador), de modo que los YAML existentes no cambian. El registro dice si el portal ya tiene
extractor (`integrado`): el pipeline agrupa a los clientes por portal, extrae una vez por portal
integrado y omite con aviso a los clientes de uno que todavía no lo está (ver
`docs/planes/multicomputos-costa-rica.md`).

Sin dependencias de `src.*`: lo importan memoria, feedback, mercado y el pipeline sin ciclos. Es la
única fuente del `portal_id` por defecto, que antes estaba duplicado en cinco módulos.

El `Portal` también dice cómo **despacharlo** (`extractor`, `localizador`) como rutas
`"paquete.modulo:atributo"` que se importan tarde, dentro de `cargar_*`: importar `src.portales` nunca
carga `src.scraper` ni Playwright. Diseño: `docs/arquitectura/portales.md` §1.
"""
from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING

import yaml

from src.classifier.alias_clientes import id_canonico
from src.portales.contrato import ErrorPortal

if TYPE_CHECKING:  # solo para anotaciones: importarlos de verdad crearía ciclos y cargaría Playwright
    from src.config import Config
    from src.portales.contrato import LocalizadorPliego
    from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

PORTAL_ID_DEFAULT = "comprasal_sv"


@dataclass(frozen=True)
class Portal:
    portal_id: str
    nombre: str
    pais: str                 # ISO 3166-1 alpha-2
    zona_horaria: str
    moneda: str               # moneda por defecto (ISO 4217); = portal.moneda_default en la ADB
    base_url: str
    idioma: str = "es"
    integrado: bool = False   # True solo si existe su extractor en `src/scraper/`
    pais_nombre: str = ""
    # ── despacho, rotulado y moneda (docs/arquitectura/portales.md §1) ──────────
    requiere_login: bool = False
    extractor: str = ""             # "paquete.modulo:funcion" → ExtractorFn
    localizador: str = ""           # "paquete.modulo:Clase" → LocalizadorPliego; "" = sin drill-down
    lookback_dias: int | None = None  # None = config.days_lookback (DAYS_LOOKBACK)
    prefijo_credenciales: str = ""  # "COMPRASAL" → COMPRASAL_EMAIL/_PASSWORD/_TOTP_SECRET
    monedas: tuple[str, ...] = ()   # monedas que el portal publica; () = solo `moneda`
    etiqueta_proceso: str = "proceso"           # «proceso» | «concurso»
    etiqueta_proceso_plural: str = "procesos"
    etiqueta_documento: str = "pliego"          # sustantivo del documento base
    palabras_documento: tuple[str, ...] = ()    # para puntuar adjuntos (localizador)
    url_consulta: str = ""          # página PÚBLICA donde una persona busca el proceso
    url_detalle: str = ""           # plantilla con {id_externo} / {numero_referencia}; "" = sin enlace profundo

    @property
    def moneda_unica(self) -> bool:
        """True ⇒ un monto sin moneda se asume en `moneda` (ver `src.portales.moneda.moneda_de`)."""
        return len(self.monedas) <= 1


# El orden de inserción es el orden de ejecución (`portales_en_orden`): COMPRASAL primero.
PORTALES: dict[str, Portal] = {
    "comprasal_sv": Portal(
        portal_id="comprasal_sv", nombre="COMPRASAL", pais="SV",
        zona_horaria="America/El_Salvador", moneda="USD",
        base_url="https://www.comprasal.gob.sv", integrado=True, pais_nombre="El Salvador",
        requiere_login=True,
        extractor="src.scraper.extractor:extraer_comprasal",
        localizador="src.drilldown.locator:LocalizadorComprasal",
        prefijo_credenciales="COMPRASAL",
        url_consulta="https://www.comprasal.gob.sv/",
        url_detalle="https://www.comprasal.gob.sv/pac-procesos/etapas/{id_externo}",
    ),
    # Costa Rica: extractor anónimo con navegador real (src/scraper/sicop.py). Medido en la VM de DEV: solo pasa el WAF
    # con pantalla virtual (SICOP_HEADLESS=false). PROD lo mantiene apagado con PORTALES_DESHABILITADOS=sicop_cr.
    "sicop_cr": Portal(
        portal_id="sicop_cr", nombre="SICOP", pais="CR",
        zona_horaria="America/Costa_Rica", moneda="CRC",
        base_url="https://www.sicop.go.cr", integrado=True, pais_nombre="Costa Rica",
        extractor="src.scraper.sicop:extraer",
        localizador="src.drilldown.localizador_sicop:LocalizadorSicop",
        monedas=("CRC", "USD"),
        etiqueta_proceso="concurso", etiqueta_proceso_plural="concursos",
        etiqueta_documento="cartel",  # decisión A-1 (§12): el correo de Costa Rica dice «cartel»
        palabras_documento=("pliego de condiciones", "cartel", "especificaciones tecnicas", "condiciones"),
        url_consulta="https://www.sicop.go.cr/app/module/bid/public/tenders",
        url_detalle="",  # SICOP no tiene enlace profundo: la SPA recibe el id por estado de navegación
    ),
}

ExtractorFn = Callable[["Config", Portal, datetime], Awaitable[list["Proceso"]]]


class PortalDesconocido(ValueError):
    """`client.portal` apunta a un portal que no está en `PORTALES`."""


def obtener_portal(portal_id: str) -> Portal:
    try:
        return PORTALES[portal_id]
    except KeyError:
        raise PortalDesconocido(
            f"portal desconocido: {portal_id!r} (conocidos: {', '.join(sorted(PORTALES))})"
        ) from None


def descripcion_portal(portal_id: str = PORTAL_ID_DEFAULT) -> str:
    """Nombre y país para los prompts; un id vacío conserva el portal histórico."""
    if not portal_id or not portal_id.strip():
        logger.warning("portal_id vacío; se usa %s", PORTAL_ID_DEFAULT)
        portal_id = PORTAL_ID_DEFAULT
    portal = obtener_portal(portal_id)
    return f"{portal.nombre}, {portal.pais_nombre}" if portal.pais_nombre else portal.nombre


@lru_cache(maxsize=64)
def _portal_en(ruta: str, mtime_ns: int) -> str:
    # `mtime_ns` solo existe para invalidar la caché cuando el YAML cambia.
    with open(ruta, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return str((data.get("client") or {}).get("portal") or PORTAL_ID_DEFAULT).strip()


def _manda_adb() -> bool:
    from src.persistencia.fuente import manda_adb
    return manda_adb("CLIENTES")


def portal_de_cliente(clients_dir: str, client_id: str) -> str:
    """`portal_id` del cliente según su YAML; COMPRASAL si no declara uno o no se puede leer.

    No levanta: se llama desde los servicios de memoria y feedback, que nunca deben caer por esto.
    """
    client_id = id_canonico(client_id)
    if _manda_adb():
        try:
            from src.classifier.client_profile import _datos_perfil
            data, _ = _datos_perfil(client_id, clients_dir or "")
            return str((data.get("client") or {}).get("portal") or PORTAL_ID_DEFAULT).strip()
        except Exception as exc:  # noqa: BLE001 — no levanta, igual que la rama de archivo
            logger.debug("portal de %s no legible vía ADB (%s); se usa %s", client_id, exc, PORTAL_ID_DEFAULT)
            return PORTAL_ID_DEFAULT
    ruta = os.path.join(clients_dir or "", f"{client_id}.yaml")
    try:
        return _portal_en(ruta, os.stat(ruta).st_mtime_ns)
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("portal de %s no legible (%s); se usa %s", client_id, exc, PORTAL_ID_DEFAULT)
        return PORTAL_ID_DEFAULT


def portales_en_orden() -> list[Portal]:
    """Los portales en el orden del registro (COMPRASAL primero): es el orden de ejecución (D10)."""
    return list(PORTALES.values())


def _importar(portal: Portal, ruta: str, que: str):
    """Importa `"paquete.modulo:atributo"`; cualquier fallo es un `ErrorPortal` de ese portal."""
    modulo, sep, atributo = ruta.partition(":")
    if not sep or not modulo.strip() or not atributo.strip():
        raise ErrorPortal(portal.portal_id, f"{que} mal declarado: {ruta!r} (se espera 'modulo:atributo')")
    try:
        objeto = getattr(importlib.import_module(modulo.strip()), atributo.strip())
    except (ImportError, AttributeError) as exc:
        raise ErrorPortal(
            portal.portal_id, f"{que} {ruta!r} no se puede importar", detalle=f"{type(exc).__name__}: {exc}",
        ) from exc
    if not callable(objeto):
        raise ErrorPortal(portal.portal_id, f"{que} {ruta!r} no es invocable")
    return objeto


def cargar_extractor(portal: Portal) -> ExtractorFn:
    """La función `async extraer(config, portal, desde)` del portal; `ErrorPortal` si no se puede importar."""
    if not portal.extractor:
        raise ErrorPortal(portal.portal_id, "el portal no declara extractor")
    return _importar(portal, portal.extractor, "extractor")


def cargar_localizador(portal: Portal) -> type[LocalizadorPliego] | None:
    """La clase localizadora de pliegos del portal, o None si el portal no tiene drill-down."""
    if not portal.localizador:
        return None
    return _importar(portal, portal.localizador, "localizador")
