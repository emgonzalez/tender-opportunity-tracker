"""Señales deterministas de instrucciones hostiles dentro de datos del pliego."""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.drilldown.citas import normalizar


@dataclass(frozen=True)
class Hallazgo:
    patron: str
    fragmento: str


_PATRONES = {
    "ignorar_instrucciones": r"\b(?:ignora|ignore|ignorar|disregard)\s+(?:(?:todas|all|las|the|any|previous|prior|anteriores)\s+)*(?:instrucciones|instructions)\b",
    "olvidar_contexto": r"\b(?:olvida(?:r)?\s+(?:todo\s+)?lo\s+anterior|forget\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|context))\b",
    "respuesta_dirigida": r"\b(?:responde|responder|respond|reply|answer)\s+(?:(?:solo|only|with|con)\s+)*(?:go|no-go)\b",
    "nuevo_rol": r"\byou\s+are\s+now\b|\bahora\s+eres\b",
    "rol": r"(?:^|\s)(?:system|assistant|developer)\s*:|<\|(?:im_start|im_end|system|assistant|user|start_header_id|end_header_id)\|>|\[/?inst\]|</?(?:system|assistant|developer)>|###\s*(?:system|assistant|developer)\b",
}


def detectar_inyeccion(texto) -> list[Hallazgo]:
    """Devuelve fragmentos normalizados y acotados; no ejecuta instrucciones."""
    if not isinstance(texto, str):
        return []
    texto = normalizar(texto)
    hallazgos = []
    for nombre, patron in _PATRONES.items():
        for match in re.finditer(patron, texto):
            hallazgos.append(Hallazgo(nombre, texto[max(0, match.start() - 35):match.end() + 70]))
    return hallazgos
