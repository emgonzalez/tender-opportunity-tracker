"""Reglas puras de marca: el diccionario y las citas prevalecen sobre el modelo."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

from src.llm.parseo import normalizar_texto_modelo

CONFIRMADA = "CONFIRMADA"
DESCARTADA = "DESCARTADA"
DUDOSA = "DUDOSA"


def normalizar(texto: str) -> str:
    """Texto comparable sin tildes, con las reparaciones medidas del modelo."""
    texto = normalizar_texto_modelo(texto or "")
    texto = unicodedata.normalize("NFKD", texto)
    return " ".join("".join(c for c in texto if not unicodedata.combining(c)).lower().split())


def _vigentes(terminos: Iterable[dict]) -> list[dict]:
    return [t for t in terminos if isinstance(t, Mapping)
            and t.get("activo", True) in (True, 1)
            and t.get("estado", "activo") in ("semilla", "activo")
            and normalizar(t.get("termino", "")) not in ("", "crystal")]


def contiene(texto: str, termino: str) -> bool:
    """Alias completo: las letras y los dígitos adyacentes impiden el match."""
    termino = normalizar(termino)
    return bool(termino and re.search(r"(?<!\w)" + re.escape(termino) + r"(?!\w)", normalizar(texto)))


def detectar_por_diccionario(texto: str, terminos: Iterable[dict], marca_id: str) -> str:
    """Confirma lo explícito; una salida de la marca conserva la duda."""
    filas = _vigentes(terminos)
    propias = [t for t in filas if normalizar(t.get("marca_id", "")) == normalizar(marca_id)]
    fuerte = any(t.get("tipo") == "fuerte" and contiene(texto, t["termino"]) for t in propias)
    excluye = any(t.get("tipo") == "excluye" and contiene(texto, t["termino"]) for t in propias)
    if excluye:
        return DUDOSA
    if fuerte:
        return CONFIRMADA
    if any(t.get("tipo") == "ajena" and contiene(texto, t["termino"])
           and normalizar(t.get("marca_id", "")) != normalizar(marca_id) for t in filas):
        return DESCARTADA
    return DUDOSA


def validar_evidencia(cita: str, fuentes, terminos_fuertes: Iterable) -> bool:
    """La cita debe existir en una fuente individual y contener un alias fuerte."""
    if not isinstance(cita, str) or not normalizar(cita):
        return False
    if isinstance(fuentes, Mapping):
        fuentes = [fuentes.get(k, "") for k in ("titulo", "descripcion", "institucion", "tipo_actividad")]
    elif isinstance(fuentes, str):
        fuentes = [fuentes]
    if not any(normalizar(cita) in normalizar(f) for f in fuentes if isinstance(f, str)):
        return False
    fuertes = []
    for termino in terminos_fuertes:
        if isinstance(termino, str):
            if normalizar(termino) != "crystal":
                fuertes.append(termino)
        elif _vigentes([termino]) and termino.get("tipo") == "fuerte":
            fuertes.append(termino["termino"])
    return any(contiene(cita, termino) for termino in fuertes)


def marcas_ajenas_en(texto: str, terminos: Iterable[dict]) -> list[dict]:
    """Marcas ajenas explícitas, conservando el alias como evidencia verificable."""
    salida = []
    vistas = set()
    for termino in _vigentes(terminos):
        marca = termino.get("marca_id", "")
        if termino.get("tipo") != "ajena" or marca in vistas or not contiene(texto, termino["termino"]):
            continue
        salida.append({"marca": marca, "evidencia": termino["termino"],
                       "evidencia_valida": True, "origen": "diccionario"})
        vistas.add(marca)
    return salida
