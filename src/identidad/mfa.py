"""src.identidad.mfa — segundo factor TOTP (P4: admin_plataforma y admin_organizacion).

Piezas puras, sin base de datos:

- El secreto TOTP (base32, 160 bits) vive CIFRADO en `usuario.mfa_totp_secret`: con la tabla
  filtrada no se pueden generar códigos. La clave sale de ``IDENTIDAD_SECRET``. `cryptography` no es
  dependencia base del proyecto, así que se usa una construcción estándar sobre la biblioteca
  estándar: cifrado de flujo HMAC-SHA256 en modo contador + etiqueta HMAC (encrypt-then-MAC), con
  subclaves separadas para cifrar y autenticar y un nonce aleatorio por mensaje.
- La verificación acepta ±1 paso de 30 s y devuelve el paso; quien llama lo guarda para que el mismo
  código no sirva dos veces (anti-repetición).
- Los códigos de recuperación son de un solo uso y en la base solo vive su HMAC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

import pyotp

from src.identidad import codigos

PASO_SEG = 30
EMISOR = "AMALIA"
N_RECUPERACION = 8
_NONCE = 16
_TAG = 16


def nuevo_secreto() -> str:
    return pyotp.random_base32()


def agrupar(secreto: str) -> str:
    """El secreto en bloques de 4 para teclearlo a mano en la app (no hay QR: la CSP no admite imágenes)."""
    return " ".join(secreto[i:i + 4] for i in range(0, len(secreto), 4))


def uri_aprovisionamiento(secreto: str, email: str) -> str:
    return pyotp.TOTP(secreto).provisioning_uri(name=email, issuer_name=EMISOR)


# ── Cifrado del secreto en reposo ─────────────────────────────────────────────

def _subclave(identidad_secret: str, uso: bytes) -> bytes:
    if not identidad_secret:
        raise ValueError("IDENTIDAD_SECRET vacío: no se puede cifrar el secreto TOTP.")
    return hmac.new(identidad_secret.encode("utf-8"), b"amalia/mfa/v1/" + uso, hashlib.sha256).digest()


def _flujo(clave: bytes, nonce: bytes, largo: int) -> bytes:
    salida = b""
    contador = 0
    while len(salida) < largo:
        salida += hmac.new(clave, nonce + contador.to_bytes(4, "big"), hashlib.sha256).digest()
        contador += 1
    return salida[:largo]


def cifrar(identidad_secret: str, secreto: str) -> str:
    """base64url(nonce ∥ cifrado ∥ etiqueta). Un mensaje de 32 caracteres da 86: cabe en VARCHAR2(200)."""
    claro = secreto.encode("ascii")
    nonce = secrets.token_bytes(_NONCE)
    cifrado = bytes(a ^ b for a, b in zip(claro, _flujo(_subclave(identidad_secret, b"enc"), nonce, len(claro))))
    etiqueta = hmac.new(_subclave(identidad_secret, b"mac"), nonce + cifrado, hashlib.sha256).digest()[:_TAG]
    return base64.urlsafe_b64encode(nonce + cifrado + etiqueta).decode("ascii")


def descifrar(identidad_secret: str, guardado: str) -> str | None:
    """None si fue alterado o si la clave no es la que lo cifró (nunca levanta por datos malos)."""
    try:
        crudo = base64.urlsafe_b64decode(guardado.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None
    if len(crudo) <= _NONCE + _TAG:
        return None
    nonce, cifrado, etiqueta = crudo[:_NONCE], crudo[_NONCE:-_TAG], crudo[-_TAG:]
    esperada = hmac.new(_subclave(identidad_secret, b"mac"), nonce + cifrado, hashlib.sha256).digest()[:_TAG]
    if not hmac.compare_digest(etiqueta, esperada):
        return None
    claro = bytes(a ^ b for a, b in zip(cifrado, _flujo(_subclave(identidad_secret, b"enc"), nonce, len(cifrado))))
    try:
        return claro.decode("ascii")
    except UnicodeDecodeError:
        return None


# ── Verificación del código ───────────────────────────────────────────────────

def normalizar(codigo: str) -> str:
    return "".join(c for c in (codigo or "") if c.isdigit())


def verificar(secreto: str, codigo: str, ahora: float | None = None) -> int | None:
    """El paso (``unix // 30``) en que el código es válido, con tolerancia de ±1 paso; None si no lo es."""
    digitos = normalizar(codigo)
    if len(digitos) != 6:
        return None
    paso_actual = int((time.time() if ahora is None else ahora) // PASO_SEG)
    totp = pyotp.TOTP(secreto)
    for paso in (paso_actual - 1, paso_actual, paso_actual + 1):
        if hmac.compare_digest(totp.at(paso * PASO_SEG), digitos):
            return paso
    return None


# ── Códigos de recuperación ───────────────────────────────────────────────────

def generar_recuperacion(n: int = N_RECUPERACION) -> list[str]:
    """``XXXXX-XXXXX`` con el alfabeto de 32 sin ambiguos (50 bits cada uno)."""
    return ["".join(secrets.choice(codigos.ALFABETO) for _ in range(5)) + "-"
            + "".join(secrets.choice(codigos.ALFABETO) for _ in range(5)) for _ in range(n)]


def es_recuperacion(codigo: str) -> bool:
    """Un código de recuperación trae letras; un TOTP son solo 6 dígitos."""
    limpio = codigos.normalizar_codigo(codigo)
    return len(limpio) == 10 and not limpio.isdigit()


def hash_recuperacion(identidad_secret: str, usuario_id: str, codigo: str) -> str:
    if not identidad_secret:
        raise ValueError("IDENTIDAD_SECRET vacío: no se pueden emitir códigos de recuperación.")
    mensaje = f"mfa-recuperacion\x1f{usuario_id}\x1f{codigos.normalizar_codigo(codigo)}".encode("utf-8")
    return hmac.new(identidad_secret.encode("utf-8"), mensaje, hashlib.sha256).hexdigest()
