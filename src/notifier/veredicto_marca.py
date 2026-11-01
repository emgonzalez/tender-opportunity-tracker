"""Veredicto puro por marca; no activa capas ni modifica procesos o análisis.

``terminos`` recibe filas de marca_termino (o tuplas marca, término, tipo).
``verificacion`` es un dict con respuesta sí/no/incierta, cita, evidencia_valida,
fuente_texto y segunda_fuente. La validación puede venir del código anterior
(evidencia_valida is True), o hacerse aquí contra ``textos_pliego``: textos
originales de páginas, nunca la respuesta del LLM. Lo mismo vale para las citas
estructuradas del análisis. Sin validación, una afirmación no descarta.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

UMBRAL_CONFIRMADA = 0.75
UMBRAL_POSIBLE = 0.40

# Amplía la equivalencia de prefijo Oracle/Oracle NetSuite de memoria.relevancia.
EQUIVALENCIAS = {
    "oracle": (
        "oracle", "oracle netsuite", "netsuite", "mysql", "java", "weblogic",
        "solaris", "sun", "sparc", "peoplesoft", "jd edwards",
    ),
    "ibm": ("ibm", "cognos", "spss", "db2", "informix", "websphere", "maximo", "qradar"),
}


@dataclass(frozen=True)
class Veredicto:
    nivel: str
    por_que: str
    evidencia: str
    fuente: str
    competible: bool | None


def _get(obj, clave, default=None):
    return obj.get(clave, default) if isinstance(obj, dict) else getattr(obj, clave, default)


def _normal(texto):
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "").casefold())
                   if unicodedata.category(c) != "Mn")


def _contiene(texto, termino):
    return bool(termino and re.search(rf"(?<!\w){re.escape(_normal(termino))}(?!\w)", _normal(texto)))


def _propia(nombre, marca):
    return any(_contiene(nombre, alias) for alias in EQUIVALENCIAS.get(marca, (marca,)))


def _numero(valor):
    if isinstance(valor, bool):
        return None
    try:
        n = float(valor)
        return n if math.isfinite(n) and 0 <= n <= 1 else None
    except (ValueError, TypeError):
        return None


def _filas(terminos):
    for fila in terminos or ():
        if isinstance(fila, tuple) and len(fila) == 3:
            yield dict(zip(("marca_id", "termino", "tipo"), fila))
        elif isinstance(fila, dict) and fila.get("activo", True) and fila.get("estado", "activo") in ("semilla", "activo"):
            yield fila


def veredicto_marca(proc, ra, marca_id, *, terminos=(), calibracion=None,
                     verificacion=None, textos_pliego=()) -> Veredicto:
    """Aplica la primera regla concluyente; score/kNN nunca descartan por sí solos.

    ``calibracion`` admite dict/dataclass; solo se usa con estado='activa'.
    ``textos_pliego`` es un iterable de strings de fuentes originales. Para una
    negativa vision_llm, segunda_fuente debe ser True (corroboración previa por
    código) o contener una cita validada de una fuente distinta de visión.
    """
    marca = _normal(marca_id)
    titulo = _get(proc, "descripcion", "") or _get(proc, "titulo", "")
    filas = list(_filas(terminos))
    fuertes = list(EQUIVALENCIAS.get(marca, (marca,)))
    fuertes += [f["termino"] for f in filas if _propia(f.get("marca_id"), marca) and f.get("tipo") == "fuerte"]
    debiles = [f["termino"] for f in filas if _propia(f.get("marca_id"), marca) and f.get("tipo") == "debil"]
    excluye = [f["termino"] for f in filas if _propia(f.get("marca_id"), marca) and f.get("tipo") == "excluye"]
    entradas = [m for m in (_get(proc, "marcas_recomendadas", ()) or ()) if _propia(_get(m, "marca", m if isinstance(m, str) else ""), marca)]
    fuertes_titulo = [t for t in fuertes if _contiene(titulo, t)]
    debil = any(_contiene(titulo, t) for t in debiles)
    migracion = any(_contiene(titulo, t) for t in excluye) or any(
        re.search(rf"\b(?:migracion|migrar)\s+fuera\s+de\s+{re.escape(_normal(t))}(?!\w)", _normal(titulo))
        for t in fuertes
    ) or any(
        re.search(rf"\b(?:migracion|migrar)\s+(?:fuera\s+de\s+|de\s+)?{re.escape(_normal(t))}\s*(?:→|->|\ba\b|\bhacia\b)", _normal(titulo))
        for t in fuertes
    )
    textos = tuple(textos_pliego or ())
    if isinstance(textos_pliego, str):
        textos = (textos_pliego,)

    def cita(d):
        return _get(d, "cita", "") or _get(d, "evidencia", "") or ""

    def valida(d):
        c = cita(d)
        return bool(c and (_get(d, "evidencia_valida") is True or any(c in t for t in textos)))

    def solo_vision(d):
        if _get(d, "fuente_texto") != "vision_llm":
            return False
        segunda = _get(d, "segunda_fuente")
        return not (segunda is True or (segunda and _get(segunda, "fuente_texto") != "vision_llm" and valida(segunda)))

    competible = _get(ra, "competible")

    def resultado(nivel, motivo, evidencia="", fuente="llm", soporte=None):
        if nivel in ("fuera", "no_competible") and soporte is not None and solo_vision(soporte):
            return Veredicto("por_confirmar", f"{motivo}; visión sin segunda fuente", evidencia, fuente, None)
        return Veredicto(nivel, motivo, evidencia, fuente, competible)

    # 1. Ausencia y exclusiones deterministas o verificadas preceden al bloqueo.
    if not entradas and not fuertes_titulo and not debil and not migracion:
        return resultado("fuera", "Sin entrada ni término de la marca", fuente="diccionario")
    ajenas = [f["termino"] for f in filas if f.get("tipo") == "ajena" and not _propia(f.get("marca_id"), marca)]
    ajenas += [a for m, aliases in EQUIVALENCIAS.items() if m != marca for a in aliases]
    ajenas += [_get(m, "marca", "") for m in (_get(proc, "marcas_ajenas", ()) or ()) if not _propia(_get(m, "marca", ""), marca)]
    ajena_titulo = next((a for a in ajenas if _contiene(titulo, a)), None)
    if ajena_titulo and not fuertes_titulo:
        return resultado("fuera", "DESCARTADA: marca ajena en el título sin término fuerte propio", titulo, "título")
    v = verificacion or {}
    respuesta = _normal(_get(v, "respuesta", "incierta"))
    if respuesta == "no" and valida(v):
        return resultado("fuera", "Verificación negativa con cita válida", cita(v), "pliego", v)
    exigida = _get(ra, "marca_exigida") or {}
    nombre = _get(exigida, "marca", "")
    es_ajena = any(_contiene(nombre, a) for a in ajenas)
    if es_ajena and not _propia(nombre, marca) and _get(ra, "equivalente_admitido") is not True and valida(exigida):
        return resultado("fuera", f"Marca ajena exigida: {nombre}", cita(exigida), "pliego", exigida)

    # 2. Conservar motivo y cita de la evaluación de competibilidad.
    if competible is False:
        bloqueo = (_get(ra, "anexos", {}) or {}).get("competibilidad", {})
        evidencia = bloqueo if isinstance(bloqueo, str) else cita(bloqueo)
        return resultado("no_competible", _get(ra, "motivo_bloqueo", "") or "No competible", evidencia, "pliego", bloqueo)

    # El contrapatrón impide confirmar incluso con score alto o título explícito.
    if migracion:
        return resultado("por_confirmar", "Migración fuera de la marca; confirmar participación", titulo, "título")
    # 3. Confirmaciones por título, pliego verificado o score con evidencia fuerte.
    if fuertes_titulo:
        return resultado("confirmada", "Término fuerte de la marca en el título", titulo, "título")
    if respuesta == "si" and valida(v):
        return resultado("confirmada", "Verificación afirmativa con cita válida", cita(v), "pliego")
    confirmada, posible = UMBRAL_CONFIRMADA, UMBRAL_POSIBLE
    if _get(calibracion, "estado") == "activa" and _get(calibracion, "marca_id") in (None, marca):
        c = _numero(_get(calibracion, "umbral_confirmada"))
        p = _numero(_get(calibracion, "umbral_posible"))
        if c is not None and p is not None and p <= c:
            confirmada, posible = c, p
    for m in entradas:
        score = _numero(_get(m, "score"))
        if score is not None and score >= confirmada and _get(m, "role") == "Recomendada" and valida(m) and any(_contiene(cita(m), t) for t in fuertes):
            fuente = _get(m, "origen", "llm")
            return resultado("confirmada", "Score y evidencia fuerte validada", cita(m), fuente if fuente in ("pliego", "diccionario", "knn") else "llm")
    # 4. Lo antiguo, dudoso y lo aprendido sin confirmación nunca se ocultan.
    if debil:
        return resultado("por_confirmar", "Solo términos débiles del diccionario", titulo, "diccionario")
    if entradas or verificacion:
        return resultado("por_confirmar", "Score posible" if any((_numero(_get(m, "score")) or 0) >= posible for m in entradas) else "Verificación pendiente o evidencia insuficiente")
    return resultado("fuera", "Sin señales de la marca", fuente="diccionario")


def veredictos_de_proceso(proc, ra, marcas, *, terminos=(), calibracion=None, **kwargs):
    """Evalúa cada id; materializa el diccionario para admitir generadores."""
    filas = tuple(terminos or ())
    return {m: veredicto_marca(proc, ra, m, terminos=filas, calibracion=calibracion, **kwargs) for m in marcas}


def a_json(veredicto: Veredicto) -> dict:
    """Campos que se fusionan en cliente_proceso.marcas[i], sin mutar la fila."""
    return {"veredicto": veredicto.nivel, "por_que": veredicto.por_que,
            "evidencia": veredicto.evidencia, "origen": veredicto.fuente}
