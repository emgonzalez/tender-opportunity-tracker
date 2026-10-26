"""src.web.csrf — protección CSRF de los formularios del portal (plan de identidad, T3).

`SameSite=Lax` solo no alcanza (no cubre subdominios del mismo sitio), así que cada POST exige
DOS cosas: un token firmado y un `Origin`/`Referer` que coincida con `FEEDBACK_BASE_URL`.

El token es ``"<ts>.<hmac>"`` con HMAC-SHA256 sobre ``contexto ∥ ts`` y ``IDENTIDAD_SECRET``. El
contexto es el hash de la sesión (formularios con sesión) o ``"anon"`` (el formulario de acceso,
que existe antes de tener sesión). No guarda estado en el servidor y caduca a las 2 horas.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

MAX_EDAD_SEG = 2 * 3600


def _firma(secreto: str, contexto: str, ts: int) -> str:
    return hmac.new(secreto.encode("utf-8"), f"{contexto}\x1f{ts}".encode("utf-8"), hashlib.sha256).hexdigest()


def emitir(secreto: str, contexto: str, ahora: float | None = None) -> str:
    if not secreto:
        raise ValueError("IDENTIDAD_SECRET vacío: no se pueden emitir tokens CSRF.")
    ts = int(time.time() if ahora is None else ahora)
    return f"{ts}.{_firma(secreto, contexto, ts)}"


def verificar(secreto: str, contexto: str, token: str, ahora: float | None = None) -> bool:
    if not secreto or not token or "." not in token:
        return False
    ts_txt, _, firma = token.partition(".")
    if not ts_txt.isdigit():
        return False
    ts, ahora = int(ts_txt), (time.time() if ahora is None else ahora)
    if ts > ahora + 60 or ahora - ts > MAX_EDAD_SEG:
        return False
    return hmac.compare_digest(_firma(secreto, contexto, ts), firma)


def origen_valido(base_url: str, cabeceras: Mapping[str, str]) -> bool:
    """`Origin` si viene; si no, `Referer`. Sin ninguno de los dos, se rechaza."""
    base = (base_url or "").rstrip("/")
    if not base:
        return False
    origen = cabeceras.get("origin")
    if origen:
        return origen.rstrip("/") == base
    referer = cabeceras.get("referer")
    if referer:
        return referer == base or referer.startswith(base + "/")
    return False
