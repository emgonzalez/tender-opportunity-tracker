"""Transiciones válidas del pipeline comercial. Viven acá y no en la base: cambiarlas no exige DDL."""
from __future__ import annotations

ESTADOS = ("NUEVO", "EVALUANDO", "OFERTADO", "GANADO", "PERDIDO", "DESCARTADO")
FINALES = frozenset({"GANADO", "PERDIDO", "DESCARTADO"})
ESTADO_INICIAL = "NUEVO"   # estado implícito de un proceso que nadie ha tocado todavía

# El camino es NUEVO → EVALUANDO → OFERTADO → GANADO|PERDIDO y DESCARTADO desde cualquier etapa
# activa. Un estado final admite CORREGIRSE (siempre con motivo): hace falta para resolver un
# conflicto con COMPRASAL (P8: la persona dijo una cosa y el portal publicó otra) o un error de
# captura. Un descartado puede reabrirse a EVALUANDO.
TRANSICIONES: dict[str, frozenset[str]] = {
    "NUEVO": frozenset({"EVALUANDO", "DESCARTADO"}),
    "EVALUANDO": frozenset({"OFERTADO", "DESCARTADO"}),
    "OFERTADO": frozenset({"GANADO", "PERDIDO", "DESCARTADO"}),
    "GANADO": frozenset({"PERDIDO", "DESCARTADO"}),
    "PERDIDO": frozenset({"GANADO", "DESCARTADO"}),
    "DESCARTADO": frozenset({"EVALUANDO"}),
}


class TransicionInvalida(ValueError):
    """Un cambio de estado que el pipeline no permite, o al que le falta el motivo."""


def siguientes(anterior: str) -> list[str]:
    """Estados a los que se puede pasar desde ``anterior``, en el orden del pipeline."""
    return [e for e in ESTADOS if e in TRANSICIONES.get(anterior, frozenset())]


def exige_motivo(anterior: str, nuevo: str) -> bool:
    """Descartar y corregir un estado final se justifican por escrito."""
    return nuevo == "DESCARTADO" or anterior in FINALES


def validar(anterior: str, nuevo: str, motivo: str | None) -> None:
    if nuevo not in TRANSICIONES.get(anterior, frozenset()):
        raise TransicionInvalida(f"{anterior} → {nuevo} no es una transición válida")
    if exige_motivo(anterior, nuevo) and not (motivo or "").strip():
        raise TransicionInvalida(f"{anterior} → {nuevo} exige un motivo")
