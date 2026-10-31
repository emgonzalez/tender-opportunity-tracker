"""Correo por marca (E14): qué parte del reporte ve un destinatario con rol restringido a un fabricante.

El portal filtra en SQL con `memoria.relevancia.sql_marca`; el correo filtra en memoria los
`ProcesoClasificado` del día. Los dos criterios tienen que dar lo mismo: una oportunidad es «de la marca»
si alguna marca recomendada empieza por ella (`es_de_marca`) o si el título la nombra como palabra completa.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from src.memoria.relevancia import MARCAS_RESTRINGIBLES, es_de_marca


def _restringibles(solo_marcas: Iterable[str]) -> frozenset[str]:
    """Solo las claves conocidas: una marca fuera de la lista cerrada no restringe ni lanza KeyError."""
    return frozenset(m for m in (solo_marcas or ()) if m in MARCAS_RESTRINGIBLES)


def _en_titulo(texto: str, solo_marcas: Iterable[str]) -> bool:
    """La palabra de la marca como palabra completa en el texto (mismo patrón que `sql_marca`)."""
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(MARCAS_RESTRINGIBLES[m])}(?![A-Za-z0-9_])", texto or "", re.I)
        for m in solo_marcas
    )


def es_de_marca_proceso(proc, solo_marcas: Iterable[str]) -> bool:
    """True si ``proc`` involucra alguna de ``solo_marcas``; sin restricción (vacío) todo pasa."""
    if not solo_marcas:
        return True
    marcas = _restringibles(solo_marcas)
    if not marcas:
        return False  # restricción que no sabemos evaluar: nunca se abre de más
    if any(es_de_marca(m.marca or "", marcas) for m in (getattr(proc, "marcas_recomendadas", None) or [])):
        return True
    return _en_titulo(getattr(proc, "descripcion", ""), marcas)


def marcas_visibles(marcas: Iterable[str], solo_marcas: Iterable[str]) -> list[str]:
    """Nombres de marca que puede ver un perfil restringido (sin co-marcas); sin restricción, todos."""
    marcas = list(marcas)
    if not solo_marcas:
        return marcas
    restringibles = _restringibles(solo_marcas)
    return [m for m in marcas if restringibles and es_de_marca(m, restringibles)]
