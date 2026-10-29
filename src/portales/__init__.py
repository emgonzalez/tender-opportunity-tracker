"""src.portales — registro de los portales de compras públicas que AMALIA conoce.

Un cliente declara en su YAML (`client.portal`) de qué portal lee; sin ese campo es COMPRASAL
(El Salvador), de modo que los YAML existentes no cambian. El registro dice si el portal ya tiene
extractor (`integrado`): el pipeline agrupa a los clientes por portal, extrae una vez por portal
integrado y omite con aviso a los clientes de uno que todavía no lo está (ver
`docs/planes/multicomputos-costa-rica.md`).

Sin dependencias de `src.*`: lo importan memoria, feedback, mercado y el pipeline sin ciclos. Es la
única fuente del `portal_id` por defecto, que antes estaba duplicado en cinco módulos.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import yaml

from src.classifier.alias_clientes import id_canonico

logger = logging.getLogger(__name__)

PORTAL_ID_DEFAULT = "comprasal_sv"


@dataclass(frozen=True)
class Portal:
    portal_id: str
    nombre: str
    pais: str                 # ISO 3166-1 alpha-2
    zona_horaria: str
    moneda: str
    base_url: str
    idioma: str = "es"
    integrado: bool = False   # True solo si existe su extractor en `src/scraper/`


PORTALES: dict[str, Portal] = {
    "comprasal_sv": Portal(
        portal_id="comprasal_sv", nombre="COMPRASAL", pais="SV",
        zona_horaria="America/El_Salvador", moneda="USD",
        base_url="https://www.comprasal.gob.sv", integrado=True,
    ),
    # Costa Rica: declarado, sin extractor todavía (paso 2 del plan).
    "sicop_cr": Portal(
        portal_id="sicop_cr", nombre="SICOP", pais="CR",
        zona_horaria="America/Costa_Rica", moneda="CRC",
        base_url="https://www.sicop.go.cr", integrado=False,
    ),
}


class PortalDesconocido(ValueError):
    """`client.portal` apunta a un portal que no está en `PORTALES`."""


def obtener_portal(portal_id: str) -> Portal:
    try:
        return PORTALES[portal_id]
    except KeyError:
        raise PortalDesconocido(
            f"portal desconocido: {portal_id!r} (conocidos: {', '.join(sorted(PORTALES))})"
        ) from None


@lru_cache(maxsize=64)
def _portal_en(ruta: str, mtime_ns: int) -> str:
    # `mtime_ns` solo existe para invalidar la caché cuando el YAML cambia.
    with open(ruta, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return str((data.get("client") or {}).get("portal") or PORTAL_ID_DEFAULT).strip()


def portal_de_cliente(clients_dir: str, client_id: str) -> str:
    """`portal_id` del cliente según su YAML; COMPRASAL si no declara uno o no se puede leer.

    No levanta: se llama desde los servicios de memoria y feedback, que nunca deben caer por esto.
    """
    client_id = id_canonico(client_id)
    ruta = os.path.join(clients_dir or "", f"{client_id}.yaml")
    try:
        return _portal_en(ruta, os.stat(ruta).st_mtime_ns)
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("portal de %s no legible (%s); se usa %s", client_id, exc, PORTAL_ID_DEFAULT)
        return PORTAL_ID_DEFAULT
