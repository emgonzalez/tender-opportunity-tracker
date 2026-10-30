"""src.feedback.tokens — enlaces firmados del correo.

Cada destinatario recibe enlaces propios, así el voto queda atribuido sin pedirle
que inicie sesión. El token NO es una credencial fuerte: es una capacidad ("quien
tiene este enlace puede votar este proceso como este destinatario"). Un correo
reenviado atribuye el voto al destinatario original; se acepta y se documenta.

    token = b64url(json{p: proceso, c: cliente, d: destinatario_id}) "." firma

`firma` es HMAC-SHA256 del payload, truncado a 22 caracteres (~132 bits). Es
determinista: reenviar un reporte produce los mismos enlaces, y un voto dado desde
el correo de ayer cae en la misma fila que el de hoy.

`destinatario_id` es un hash corto del correo: la URL no lleva la dirección en
claro (los enlaces terminan en logs de proxies y en historiales de navegación).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from src.web.rutas import ruta_voto

VOTOS = ("relevante", "irrelevante")

_LARGO_FIRMA = 22
_MAX_TOKEN = 600  # un token legítimo mide ~150; esto corta basura antes de decodificar


@dataclass(frozen=True)
class Payload:
    proceso_id: str
    cliente_id: str
    destinatario_id: str


def destinatario_id(email: str) -> str:
    """Identificador estable y no reversible a simple vista de un correo."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:12]


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _des_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _firma(secreto: str, payload_b64: str) -> str:
    digest = hmac.new(secreto.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)[:_LARGO_FIRMA]


def firmar(secreto: str, proceso_id: str, cliente_id: str, dest_id: str) -> str:
    """Token para (proceso, cliente, destinatario). Sin secreto no se firma nada."""
    if not secreto:
        raise ValueError("FEEDBACK_HMAC_SECRET vacío: no se pueden firmar enlaces.")
    if not proceso_id or not cliente_id or not dest_id:
        raise ValueError("proceso, cliente y destinatario son obligatorios.")
    cuerpo = json.dumps(
        {"p": proceso_id, "c": cliente_id, "d": dest_id},
        separators=(",", ":"), ensure_ascii=False, sort_keys=True,
    )
    payload_b64 = _b64(cuerpo.encode("utf-8"))
    return f"{payload_b64}.{_firma(secreto, payload_b64)}"


def verificar(secreto: str, token: str) -> Payload | None:
    """Devuelve el payload si la firma es válida; `None` ante CUALQUIER problema.

    Un solo resultado para todo fallo (mal formado, firma incorrecta, campos
    ausentes) a propósito: quien sondea no debe poder distinguir los casos.
    """
    if not secreto or not token or len(token) > _MAX_TOKEN or token.count(".") != 1:
        return None
    payload_b64, firma = token.split(".")
    if not hmac.compare_digest(firma, _firma(secreto, payload_b64)):
        return None
    try:
        datos = json.loads(_des_b64(payload_b64).decode("utf-8"))
        p, c, d = datos["p"], datos["c"], datos["d"]
    except (ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(x, str) and x for x in (p, c, d)):
        return None
    return Payload(proceso_id=p, cliente_id=c, destinatario_id=d)


def url(base_url: str, token: str, voto: str) -> str:
    """`<base>/f/<token>/<relevant|irrelevant>`. `voto` ∈ `VOTOS`. Los enlaces viejos (`…/relevante`) de correos
    ya enviados siguen vivos para siempre (ver `src/web/rutas.py`)."""
    if voto not in VOTOS:
        raise ValueError(f"voto inválido: {voto!r}")
    return f"{base_url.rstrip('/')}/f/{token}/{ruta_voto(voto)}"
