"""Reglas puras para el tipo de cambio diario de Costa Rica."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from math import isfinite

MAX_DIAS_ATRASO = 7


@dataclass(frozen=True)
class TasaDia:
    moneda: str
    dia: date
    venta: float
    compra: float | None
    fuente: str

    def __post_init__(self) -> None:
        if not isfinite(self.venta) or self.venta <= 0:
            raise ValueError("la tasa de venta debe ser finita y positiva")


def tasa_vigente(
    tasas: Iterable[TasaDia] | Mapping[date, TasaDia],
    dia: date,
    max_dias: int = MAX_DIAS_ATRASO,
) -> TasaDia | None:
    """Elige la tasa más reciente a la fecha dada, dentro de la ventana inclusiva."""
    if max_dias < 0:
        raise ValueError("max_dias debe ser mayor o igual a cero")
    disponibles = tasas.values() if isinstance(tasas, Mapping) else tasas
    return max(
        (tasa for tasa in disponibles if 0 <= (dia - tasa.dia).days <= max_dias),
        key=lambda tasa: tasa.dia,
        default=None,
    )


def convertir_a_usd(monto: float, venta: float) -> float:
    """Divide por la tasa de venta y redondea a centavos como Oracle ROUND."""
    try:
        importe, tasa = Decimal(str(monto)), Decimal(str(venta))
    except InvalidOperation:
        raise ValueError("el monto y la tasa de venta deben ser numéricos") from None
    if not importe.is_finite():
        raise ValueError("el monto debe ser finito")
    if not tasa.is_finite() or tasa <= 0:
        raise ValueError("la tasa de venta debe ser finita y positiva")

    with localcontext() as contexto:
        # Conserva los dígitos de la parte entera y precisión de sobra para los centavos.
        contexto.prec = max(0, importe.adjusted() - tasa.adjusted()) + 40
        resultado = float((importe / tasa).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if not isfinite(resultado):
        raise ValueError("el monto convertido excede el rango de float")
    return resultado


def dia_local_cr(dt: datetime) -> date:
    """Devuelve la fecha de Costa Rica (UTC−6), interpretando un datetime sin zona como UTC."""
    utc = dt.replace(tzinfo=timezone.utc) if dt.utcoffset() is None else dt.astimezone(timezone.utc)
    return (utc - timedelta(hours=6)).date()
