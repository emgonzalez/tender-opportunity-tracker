"""`python -m src.web` — levanta el servicio web con uvicorn.

Escucha en 0.0.0.0 porque corre dentro de un contenedor; qué puertos se publican hacia
afuera (y detrás de qué proxy TLS) lo decide la infraestructura, no este módulo.
"""
from __future__ import annotations

import os

import uvicorn

from src.web.app import crear_app


def main() -> None:
    puerto = int(os.getenv("FEEDBACK_PORT", "8080"))
    # `access_log=False`: el log de acceso de uvicorn escribe la ruta completa, y la ruta
    # ES el token. Lo que sí interesa (proceso, cliente, voto) lo registra el router sin
    # el token. Un solo worker: la VM de 1 GB no da para más y el tráfico es bajo.
    uvicorn.run(crear_app(), host="0.0.0.0", port=puerto, access_log=False)


if __name__ == "__main__":
    main()
