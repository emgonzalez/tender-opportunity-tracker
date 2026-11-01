"""Selección pura C6 y probabilidades de inclusión para evaluación IPS."""
from __future__ import annotations

import math
import random
from collections.abc import Mapping


def _id(candidato, indice):
    if isinstance(candidato, Mapping):
        return candidato.get("id", candidato.get("numero_referencia", indice))
    return candidato


def _valor(valores, clave, indice, defecto):
    if isinstance(valores, Mapping):
        return valores.get(clave, defecto)
    return valores[indice]


def _distancia(valor):
    if valor is None:
        return 1.0
    if isinstance(valor, (list, tuple)):
        valor = min(valor, default=1.0)
    valor = float(valor)
    if not math.isfinite(valor) or not 0 <= valor <= 1:
        raise ValueError("Distancia fuera de [0, 1]")
    return valor


def seleccionar_exploracion(candidatos, probs, etiquetados_vecinos, cupo,
                            aleatorio_pct=0.15, semilla=0):
    """Devuelve una fila por candidato, incluso los no mostrados, sin mutarlos.

    ``probs`` y ``etiquetados_vecinos`` son secuencias alineadas o mapas por
    ``id``/``numero_referencia``. Los vecinos expresan distancia normalizada al
    etiquetado más cercano (escalar o lista); ausencia significa distancia 1.
    Probabilidad ausente significa 0.5. Cada candidato puede declarar ``umbral``
    (0.5 por defecto) y ``descartado_determinista=True``: este último jamás entra.

    Se reserva aproximadamente 10–20 % del cupo (redondeo entero) para una muestra
    uniforme sin reemplazo de los casos bajo el umbral, fuera del prefijo de
    mayor incertidumbre × distancia. El resto es selección determinista, con
    desempate por posición. Si falta población aleatoria se completa por puntaje.
    La propensión es marginal bajo este diseño: 1 en el prefijo, r/N en la
    población aleatoria, 0 fuera. No es la probabilidad de la realización observada.
    Con cupos pequeños el redondeo puede dejar cero plazas aleatorias. IPS solo
    identifica la población con propensión positiva; no corrige los ceros.
    """
    candidatos = list(candidatos)
    if not isinstance(cupo, int) or isinstance(cupo, bool) or cupo < 0:
        raise ValueError("Cupo inválido")
    if not 0.10 <= aleatorio_pct <= 0.20:
        raise ValueError("La fracción aleatoria debe estar entre 0.10 y 0.20")
    for valores in (probs, etiquetados_vecinos):
        if not isinstance(valores, Mapping) and len(valores) != len(candidatos):
            raise ValueError("Longitudes distintas")
    filas = []
    for indice, candidato in enumerate(candidatos):
        clave = _id(candidato, indice)
        p = float(_valor(probs, clave, indice, 0.5))
        distancia = _distancia(_valor(etiquetados_vecinos, clave, indice, None))
        datos = candidato if isinstance(candidato, Mapping) else {}
        umbral = float(datos.get("umbral", 0.5))
        if not math.isfinite(p) or not 0 <= p <= 1 or not 0 < umbral < 1:
            raise ValueError("Probabilidad o umbral inválido")
        incertidumbre = 1 - abs(p - umbral) / (umbral if p < umbral else 1 - umbral)
        filas.append({"candidato": candidato, "mostrado": False, "propension": 0.0,
                      "puntaje": incertidumbre * distancia, "origen": "no_mostrado"})
    elegibles = [i for i, c in enumerate(candidatos)
                 if not (isinstance(c, Mapping) and c.get("descartado_determinista", False))]
    orden = sorted(elegibles, key=lambda i: (-filas[i]["puntaje"], i))
    total = min(cupo, len(orden))
    reserva = int(total * aleatorio_pct + 0.5)
    prefijo = orden[:total - reserva]
    restantes = orden[total - reserva:]
    bajos = []
    for i in restantes:
        c = candidatos[i]
        umbral = c.get("umbral", 0.5) if isinstance(c, Mapping) else 0.5
        if float(_valor(probs, _id(c, i), i, 0.5)) < umbral:
            bajos.append(i)
    plazas = min(reserva, len(bajos))
    # Completar antes del sorteo mantiene exacta y sencilla la propensión r/N.
    adicionales = [i for i in restantes if i not in bajos][:reserva - plazas]
    prefijo += adicionales
    for i in prefijo:
        filas[i].update(mostrado=True, propension=1.0, origen="incertidumbre")
    elegidos = set(random.Random(semilla).sample(bajos, plazas))
    for i in bajos:
        filas[i].update(mostrado=i in elegidos, propension=plazas / len(bajos),
                        origen="aleatorio" if i in elegidos else "no_mostrado")
    return filas
