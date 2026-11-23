"""Rotulado de montos: siempre en la moneda original, con un equivalente aproximado opcional.

Diseño en `docs/arquitectura/portales.md` §7 (D9) y decisión A-2 de §12:

- Se rotula la moneda **original** con su código ISO (`USD 45,000`, `CRC 12,500,000`). COMPRASAL queda
  idéntico al texto que ya mostraba el correo.
- Un monto sin moneda conocida **jamás** se rotula «USD»: sale como `12,500,000 (moneda sin confirmar)`.
- El equivalente se muestra entre paréntesis y siempre marcado con «≈»: `CRC 12,500,000 (≈ USD 24,000)`.
  Este módulo **no convierte**: la tasa la aplica la ADB (`fx_tasa` → `proceso.monto_usd`). Sin
  equivalente (sin tasa) solo se muestra el original.

Depende solo de `src.portales`.
"""
from __future__ import annotations

import math

from src.portales import PORTAL_ID_DEFAULT, PORTALES

SIN_CONFIRMAR = "(moneda sin confirmar)"


def _codigo(moneda) -> str:
    return str(moneda or "").strip().upper()


def _numero(monto) -> float | None:
    """Número positivo y finito, o None (bool no es un número; 0 o negativo es «no se estimó»)."""
    if monto is None or isinstance(monto, bool):
        return None
    try:
        valor = float(monto)
    except (TypeError, ValueError):
        return None
    return valor if math.isfinite(valor) and valor > 0 else None


def moneda_de(proceso) -> str:
    """Moneda ISO del monto del proceso, o "" si no se conoce.

    `proceso.moneda` si viene; si no y el portal es de moneda única, la del portal; si no, "".
    Una línea vieja (sin `portal_id` ni `moneda`) da "USD", que es lo correcto para COMPRASAL.
    """
    propia = _codigo(getattr(proceso, "moneda", ""))
    if propia:
        return propia
    portal = PORTALES.get(getattr(proceso, "portal_id", "") or PORTAL_ID_DEFAULT)
    if portal is not None and portal.moneda_unica:
        return portal.moneda
    return ""


def formatear_monto(monto: float | None, moneda: str, *, miles: str = ",") -> str:
    """`"USD 45,000"`; "" si no hay monto (None, 0 o negativo: nunca «USD 0»).

    Sin moneda conocida: `"45,000 (moneda sin confirmar)"`.
    """
    valor = _numero(monto)
    if valor is None:
        return ""
    cifra = f"{valor:,.0f}"
    if miles != ",":
        cifra = cifra.replace(",", miles)
    codigo = _codigo(moneda)
    return f"{codigo} {cifra}" if codigo else f"{cifra} {SIN_CONFIRMAR}"


def texto_monto(
    proceso, *, miles: str = ",", equivalente: float | None = None, moneda_equivalente: str = "USD",
    con_equivalente: bool = True,
) -> str:
    """Lo que se muestra del monto de un proceso, en su moneda original.

    - Un solo monto: `formatear_monto(monto, moneda_de(p))`.
    - Mezcla (`montos_por_moneda`): `"CRC 12,000,000 + USD 35,000"`, ordenado por código. Nunca se suman
      monedas distintas.
    - Equivalente: `equivalente` (o, si no se pasa y `moneda_equivalente` es USD, `proceso.monto_usd`, que
      solo llena la ADB con una tasa real) se agrega como `" (≈ USD 24,000)"`. No se agrega si el original no tiene moneda conocida, si
      ya está en `moneda_equivalente`, o si no hay equivalente: nunca se inventa una tasa.
      `con_equivalente=False` lo suprime.
    """
    mezcla = getattr(proceso, "montos_por_moneda", None) or {}
    if isinstance(mezcla, dict) and mezcla:
        partes = [(_codigo(k), formatear_monto(v, k, miles=miles)) for k, v in sorted(mezcla.items())]
        partes = [(k, t) for k, t in partes if t]
        texto = " + ".join(t for _, t in partes)
        monedas = {k for k, _ in partes}
    else:
        moneda = moneda_de(proceso)
        texto = formatear_monto(getattr(proceso, "monto", None), moneda, miles=miles)
        monedas = {moneda}
    if not texto or not con_equivalente:
        return texto

    destino = _codigo(moneda_equivalente)
    valor_eq = equivalente
    if valor_eq is None and destino == "USD":  # `monto_usd` está en USD: no sirve para otra moneda
        valor_eq = getattr(proceso, "monto_usd", None)
    if not destino or "" in monedas or monedas == {destino}:
        return texto
    eq = formatear_monto(valor_eq, destino, miles=miles)
    return f"{texto} (≈ {eq})" if eq else texto
