"""Selector de fuente por dato: el corte de archivo a ADB se decide con una variable de entorno, no con código."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ARCHIVO, ADB, SOMBRA = "archivo", "adb", "sombra"
VALIDAS = (ARCHIVO, ADB, SOMBRA)


def fuente(dato: str, env: dict | None = None) -> str:
    """Fuente vigente de `dato` (`CLIENTES`, `ESTADO`, `CUOTA`, `SESION`), leída de `<DATO>_FUENTE`.

    Sin variable, o con un valor desconocido, vale `archivo`: el comportamiento de siempre. Un error de
    tipeo en el entorno nunca debe mover el estado de lugar.
    """
    env = os.environ if env is None else env
    clave = f"{dato.upper()}_FUENTE"
    valor = (env.get(clave) or ARCHIVO).strip().lower()
    if valor not in VALIDAS:
        logger.warning("%s=%r no es válido (%s): se usa %r", clave, valor, "|".join(VALIDAS), ARCHIVO)
        return ARCHIVO
    return valor


def lee_adb(dato: str, env: dict | None = None) -> bool:
    """¿Hay que consultar la ADB para `dato`? Sí en `adb` y en `sombra` (donde solo se compara)."""
    return fuente(dato, env) != ARCHIVO


def manda_adb(dato: str, env: dict | None = None) -> bool:
    """¿El valor que se usa es el de la ADB? Solo en `adb`; en `sombra` sigue mandando el archivo."""
    return fuente(dato, env) == ADB
