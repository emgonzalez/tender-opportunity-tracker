"""Cifrado de blobs en reposo con la clave de `IDENTIDAD_SECRET` (sin dependencias nuevas).

Misma construcción que `src/identidad/mfa.py` (flujo HMAC-SHA256 + etiqueta de integridad, nonce aleatorio por
mensaje) pero con otro dominio de claves, para que un blob de aquí jamás se descifre como un secreto TOTP, y para
bytes arbitrarios (la sesión de COMPRASAL es JSON de varios KB). Si el blob fue alterado o la clave no es la que
lo cifró, `descifrar` devuelve None: nunca levanta por datos malos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_NONCE, _TAG = 16, 16
_DOMINIO = b"amalia/persistencia/v1/"


def _subclave(clave_maestra: str, uso: bytes) -> bytes:
    if not clave_maestra:
        raise ValueError("IDENTIDAD_SECRET vacío: no se puede cifrar.")
    return hmac.new(clave_maestra.encode("utf-8"), _DOMINIO + uso, hashlib.sha256).digest()


def _flujo(clave: bytes, nonce: bytes, largo: int) -> bytes:
    salida = bytearray()
    contador = 0
    while len(salida) < largo:
        salida += hmac.new(clave, nonce + contador.to_bytes(4, "big"), hashlib.sha256).digest()
        contador += 1
    return bytes(salida[:largo])


def cifrar(clave_maestra: str, claro: bytes, contexto: str = "") -> str:
    """base64url(nonce ∥ cifrado ∥ etiqueta). `contexto` (p. ej. el id de la fila) queda atado a la etiqueta."""
    nonce = secrets.token_bytes(_NONCE)
    flujo = _flujo(_subclave(clave_maestra, b"enc"), nonce, len(claro))
    cifrado = bytes(a ^ b for a, b in zip(claro, flujo))
    etiqueta = hmac.new(_subclave(clave_maestra, b"mac"), contexto.encode() + b"\0" + nonce + cifrado, hashlib.sha256).digest()[:_TAG]
    return base64.urlsafe_b64encode(nonce + cifrado + etiqueta).decode("ascii")


def descifrar(clave_maestra: str, guardado: str, contexto: str = "") -> bytes | None:
    try:
        crudo = base64.urlsafe_b64decode(guardado.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None
    if len(crudo) < _NONCE + _TAG:
        return None
    nonce, cifrado, etiqueta = crudo[:_NONCE], crudo[_NONCE:-_TAG], crudo[-_TAG:]
    esperada = hmac.new(_subclave(clave_maestra, b"mac"), contexto.encode() + b"\0" + nonce + cifrado, hashlib.sha256).digest()[:_TAG]
    if not hmac.compare_digest(etiqueta, esperada):
        return None
    flujo = _flujo(_subclave(clave_maestra, b"enc"), nonce, len(cifrado))
    return bytes(a ^ b for a, b in zip(cifrado, flujo))
