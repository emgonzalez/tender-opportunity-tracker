"""Caché local mínima: escritura atómica (tmp + os.replace) y lectura que nunca levanta por un archivo dañado."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def escribir_atomico(ruta: str | Path, contenido: str | bytes, modo: int = 0o644) -> None:
    """Un corte a mitad de escritura deja el archivo anterior intacto, nunca uno truncado."""
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    binario = isinstance(contenido, bytes)
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if binario else "w", **({} if binario else {"encoding": "utf-8"})) as f:
            f.write(contenido)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, modo)
        os.replace(tmp, ruta)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def escribir_json(ruta: str | Path, dato, modo: int = 0o644) -> None:
    escribir_atomico(ruta, json.dumps(dato, ensure_ascii=False, sort_keys=True, default=str), modo)


def leer_json(ruta: str | Path, por_defecto=None):
    """`por_defecto` si el archivo no existe o no es JSON válido (se avisa, no se propaga)."""
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return por_defecto
    except (OSError, ValueError):
        logger.warning("caché ilegible: %s", ruta, exc_info=True)
        return por_defecto
