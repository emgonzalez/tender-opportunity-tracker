"""Detrás de Caddy (Entrega 7): la IP del cliente solo se toma de `X-Forwarded-For` si viene de un proxy confiable."""
from __future__ import annotations

import asyncio

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.web.__main__ import PROXIES_CONFIABLES


def _ip_vista(origen: str, forwarded: str | None) -> str:
    visto = {}

    async def app(scope, receive, send):
        visto["ip"] = scope["client"][0]

    cabeceras = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    scope = {"type": "http", "client": (origen, 4000), "headers": cabeceras, "scheme": "http"}
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=PROXIES_CONFIABLES)(scope, None, None))
    return visto["ip"]


def test_caddy_en_la_red_de_docker_reenvia_la_ip_real():
    for caddy in ("172.18.0.3", "172.17.0.2", "172.31.255.1"):
        assert _ip_vista(caddy, "203.0.113.9") == "203.0.113.9"


def test_una_cabecera_falsa_de_un_origen_no_confiable_se_ignora():
    assert _ip_vista("203.0.113.50", "1.2.3.4") == "203.0.113.50"
    assert _ip_vista("8.8.8.8", "10.0.0.1") == "8.8.8.8"


def test_sin_cabecera_queda_la_ip_de_la_conexion():
    assert _ip_vista("172.18.0.3", None) == "172.18.0.3"


def test_el_valor_por_defecto_cubre_el_host_y_la_red_privada_de_docker():
    assert PROXIES_CONFIABLES.split(",") == ["127.0.0.1", "172.16.0.0/12"]
