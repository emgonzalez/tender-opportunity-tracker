"""src.web.limitador — tope de intentos por clave, en memoria (plan de identidad, T1).

Vale porque el servicio corre con UN solo worker (`src/web/__main__.py`); se pierde al reiniciar,
y es aceptable: el control fuerte contra la fuerza bruta es el de la base (5 intentos por código,
3 códigos por hora y usuario). Esto solo frena a una misma IP que tantea muchos correos.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class Limitador:
    def __init__(self, maximo: int = 10, ventana_seg: int = 600, max_claves: int = 10_000):
        self.maximo, self.ventana, self.max_claves = maximo, ventana_seg, max_claves
        self._eventos: dict[str, deque[float]] = defaultdict(deque)

    def permitir(self, clave: str, ahora: float | None = None) -> bool:
        """Registra un intento y dice si cabe. Un intento rechazado no cuenta de nuevo."""
        ahora = time.monotonic() if ahora is None else ahora
        if len(self._eventos) > self.max_claves:
            self._purgar(ahora)
        cola = self._eventos[clave]
        while cola and ahora - cola[0] > self.ventana:
            cola.popleft()
        if len(cola) >= self.maximo:
            return False
        cola.append(ahora)
        return True

    def _purgar(self, ahora: float) -> None:
        for clave in [k for k, c in self._eventos.items() if not c or ahora - c[-1] > self.ventana]:
            del self._eventos[clave]
