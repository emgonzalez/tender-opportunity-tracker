"""Validación local y conservadora de citas literales del pliego."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from src.llm.parseo import normalizar_texto_modelo


@dataclass(frozen=True)
class CitaValida:
    ok: bool
    puntuacion: float
    fuente_texto: str


def normalizar(texto: str) -> str:
    """Repara mojibake reconocible y OCR conocido; elimina tildes y blancos extra."""
    texto = normalizar_texto_modelo(texto or "")
    # UTF-8 interpretado como Latin-1/Windows-1252. Solo se adopta una
    # conversión reversible y completa: nunca se pierden caracteres.
    for _ in range(2):
        if not any(marca in texto for marca in ("Ã", "Â", "â€")):
            break
        for codificacion in ("latin1", "cp1252"):
            try:
                reparado = texto.encode(codificacion).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            texto = reparado
            break
        else:
            break
    texto = normalizar_texto_modelo(texto)
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def validar_cita(cita, texto_pagina, umbral=0.9) -> CitaValida:
    """Busca una ventana literal; la similitud nunca autoriza cambiar palabras.

    Una página puede ser texto o ``{texto, fuente_texto}``. Sin metadatos su
    procedencia es ``desconocida``: no se inventa una segunda fuente nativa.
    El umbral tolera puntuación/OCR ya normalizado, nunca paráfrasis ni una
    negación insertada o suprimida aunque el cociente de similitud sea alto.
    """
    fuente = "desconocida"
    if isinstance(texto_pagina, dict):
        fuente = str(texto_pagina.get("fuente_texto") or fuente)
        texto_pagina = texto_pagina.get("texto", "")
    if not isinstance(cita, str) or not isinstance(texto_pagina, str):
        return CitaValida(False, 0.0, fuente)
    cita, texto = normalizar(cita), normalizar(texto_pagina)
    palabras = re.findall(r"\w+", cita)
    if len(cita) < 24 or len(palabras) < 4 or not 0 <= umbral <= 1:
        return CitaValida(False, 0.0, fuente)
    if cita in texto:
        # Los bordes también deben ser de palabra: no vale citar «competible»
        # dentro de «incompetible».
        if re.search(r"(?<!\w)" + re.escape(cita) + r"(?!\w)", texto):
            return CitaValida(True, 1.0, fuente)
    tokens = list(re.finditer(r"\w+", texto))
    mejor = 0.0
    literal = False
    n = len(palabras)
    for i in range(len(tokens) - n + 1):
        ventana = texto[tokens[i].start():tokens[i + n - 1].end()]
        puntuacion = SequenceMatcher(None, cita, ventana, autojunk=False).ratio()
        mejor = max(mejor, puntuacion)
        if [t.group() for t in tokens[i:i + n]] == palabras and puntuacion >= umbral:
            literal = True
    return CitaValida(literal, mejor, fuente)
