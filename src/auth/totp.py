"""
Generación automática de TOTP con pyotp.

Se activa cuando COMPRASAL_TOTP_SECRET tiene un valor en el .env.
Para obtener el secret TOTP:
  1. Habilitar 2FA en la cuenta de COMPRASAL.
  2. Cuando COMPRASAL muestre el QR, escanear con una app de autenticador
     que permita exportar el secret (ej: Aegis en Android).
  3. Copiar el secret base32 al .env como COMPRASAL_TOTP_SECRET.
"""
import pyotp


def generate_totp(secret: str) -> str:
    """Genera el código TOTP actual dado el secret base32."""
    totp = pyotp.TOTP(secret)
    return totp.now()


def is_totp_valid(secret: str, code: str, valid_window: int = 1) -> bool:
    """Verifica si un código TOTP es válido (tolerancia ±1 intervalo de 30s)."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=valid_window)
