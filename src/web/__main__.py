"""`python -m src.web` — levanta el servicio web con uvicorn.

Escucha en 0.0.0.0 porque corre dentro de un contenedor; qué puertos se publican hacia
afuera (y detrás de qué proxy TLS) lo decide la infraestructura, no este módulo.
"""
from __future__ import annotations

import os

import uvicorn

from src.web.app import crear_app

# Detrás de Caddy, toda petición llega desde la red de compose: sin esto `request.client` sería SIEMPRE la
# IP de Caddy y el tope por IP del acceso limitaría a todos los usuarios a la vez. Solo se confía en
# `X-Forwarded-For` si la conexión viene de la red privada de Docker (o del propio host): el contenedor
# `web` no publica puertos, así que por ahí solo llega Caddy, que reescribe esa cabecera. Un CIDR y no
# la IP de Caddy: cambia al recrear el contenedor. `FORWARDED_ALLOW_IPS` lo reemplaza.
PROXIES_CONFIABLES = "127.0.0.1,172.16.0.0/12"


def main() -> None:
    puerto = int(os.getenv("FEEDBACK_PORT", "8080"))
    # `access_log=False`: el log de acceso de uvicorn escribe la ruta completa, y la ruta
    # ES el token. Lo que sí interesa (proceso, cliente, voto) lo registra el router sin
    # el token. Un solo worker: la VM de 1 GB no da para más y el tráfico es bajo.
    uvicorn.run(crear_app(), host="0.0.0.0", port=puerto, access_log=False,
                proxy_headers=True, forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS") or PROXIES_CONFIABLES)


if __name__ == "__main__":
    main()
