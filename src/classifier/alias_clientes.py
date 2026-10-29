"""Ids de cliente que cambiaron de nombre: el id viejo sigue resolviendo al nuevo.

Convención: `multicomputos_<país>` (el cliente es la filial, no la empresa). El primero, `multicomputos`, pasó a
`multicomputos_sv`. Los enlaces `/f/{token}` de correos ya enviados llevan el id viejo dentro del token firmado y
seguirán en bandejas de entrada, así que este alias **no tiene plazo de retiro**. Un solo diccionario, sin imports.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ALIAS_CLIENTES: dict[str, str] = {"multicomputos": "multicomputos_sv"}
_LEGADO_DE: dict[str, str] = {nuevo: viejo for viejo, nuevo in ALIAS_CLIENTES.items()}


def id_canonico(client_id: str) -> str:
    """El id vigente de `client_id`; los que no tienen alias vuelven tal cual."""
    return ALIAS_CLIENTES.get(client_id, client_id)


def ruta_con_legado(ruta: str, client_id: str) -> str:
    """`ruta` (con el id canónico en el nombre del archivo) o, si todavía no existe, la del nombre viejo.

    Tolera el tramo entre desplegar el código y renombrar `data/history/<id>.*` en la VM: sin esto el historial
    parecería vacío y se reabriría la deduplicación de lo ya notificado. Si ninguno existe devuelve `ruta`
    (es donde se creará). Quitar cuando los archivos de DEV y PROD estén renombrados y estables.
    """
    if os.path.exists(ruta):
        return ruta
    viejo = _LEGADO_DE.get(client_id)
    if not viejo:
        return ruta
    carpeta, nombre = os.path.split(ruta)
    alt = os.path.join(carpeta, nombre.replace(client_id, viejo, 1))
    if os.path.exists(alt):
        logger.warning("usando %s: renombrarlo a %s", alt, nombre)
        return alt
    return ruta
