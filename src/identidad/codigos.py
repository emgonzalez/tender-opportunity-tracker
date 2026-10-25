"""src.identidad.codigos — código de acceso de un solo uso (acceso sin contraseña).

8 caracteres de un alfabeto de 32 sin ambiguos (40 bits). En la base solo vive
``HMAC-SHA256(IDENTIDAD_SECRET, usuario_id ∥ código)``: la tabla filtrada no sirve para
entrar ni se invierte por fuerza bruta sin el secreto. Mismo patrón que `src.feedback.tokens`.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# A-Z sin I ni O, y 2-9: no hay 0/O ni 1/I que confundir. 24 + 8 = 32 símbolos = 5 bits cada uno.
ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LARGO = 8


def generar_codigo() -> str:
    return "".join(secrets.choice(ALFABETO) for _ in range(LARGO))


def normalizar_codigo(texto: str) -> str:
    """Tolera minúsculas, espacios y guiones al teclear («k7m2-qx9p»)."""
    return "".join(c for c in (texto or "").upper() if c not in " -\t\n")


def hashear_codigo(secreto: str, usuario_id: str, codigo: str) -> str:
    if not secreto:
        raise ValueError("IDENTIDAD_SECRET vacío: no se pueden emitir códigos de acceso.")
    mensaje = f"{usuario_id}\x1f{normalizar_codigo(codigo)}".encode("utf-8")
    return hmac.new(secreto.encode("utf-8"), mensaje, hashlib.sha256).hexdigest()


def verificar_codigo(secreto: str, usuario_id: str, codigo: str, hash_guardado: str) -> bool:
    if not secreto or not codigo:
        return False
    return hmac.compare_digest(hashear_codigo(secreto, usuario_id, codigo), hash_guardado or "")
