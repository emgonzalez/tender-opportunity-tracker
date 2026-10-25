"""src.identidad.sesiones — token opaco de sesión. En la base solo vive su sha256."""
from __future__ import annotations

import hashlib
import secrets


def nuevo_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """64 hex: cabe en ``sesion.sesion_id`` (VARCHAR2(64)). Un token filtrado de la tabla no existe."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
