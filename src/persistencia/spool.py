"""Cola de salida local: lo que la ADB no alcanzó a recibir espera aquí y se reenvía en la corrida siguiente.

Un archivo JSONL por cola. `drenar` solo retira un registro cuando `enviar` lo aceptó, así que un corte a
mitad de camino reenvía de más (el destino debe ser idempotente) pero nunca pierde.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable

from src.persistencia.cache import escribir_atomico

logger = logging.getLogger(__name__)

_NOMBRE = re.compile(r"^[A-Za-z0-9_.-]+$")


class Spool:
    def __init__(self, directorio: str | Path):
        self.directorio = Path(directorio)

    def _ruta(self, cola: str) -> Path:
        if not _NOMBRE.match(cola):
            raise ValueError(f"nombre de cola no válido: {cola!r}")
        return self.directorio / f"{cola}.jsonl"

    def agregar(self, cola: str, registro: dict) -> None:
        ruta = self._ruta(cola)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def pendientes(self, cola: str) -> list[dict]:
        """Registros esperando envío; las líneas dañadas se omiten con un aviso."""
        ruta = self._ruta(cola)
        if not ruta.exists():
            return []
        salida = []
        with open(ruta, encoding="utf-8") as f:
            for n, linea in enumerate(f, 1):
                if not linea.strip():
                    continue
                try:
                    salida.append(json.loads(linea))
                except ValueError:
                    logger.warning("spool %s: línea %d dañada, se omite", ruta.name, n)
        return salida

    def drenar(self, cola: str, enviar: Callable[[dict], bool]) -> tuple[int, int]:
        """Reenvía en orden; se detiene en el primer fallo para conservar el orden. Devuelve (enviados, quedan)."""
        pend = self.pendientes(cola)
        enviados = 0
        for reg in pend:
            try:
                ok = bool(enviar(reg))
            except Exception:  # noqa: BLE001 — la ADB sigue caída: el resto espera
                logger.debug("spool %s: el envío falló", cola, exc_info=True)
                ok = False
            if not ok:
                break
            enviados += 1
        resto = pend[enviados:]
        ruta = self._ruta(cola)
        if not resto:
            ruta.unlink(missing_ok=True)
        elif enviados:
            escribir_atomico(ruta, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in resto))
        return enviados, len(resto)
