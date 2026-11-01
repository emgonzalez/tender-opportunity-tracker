"""Estadística pura para evaluar marcas; no importa configuración ni servicios."""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import NormalDist

# Umbrales del plan C8; Wilson bilateral al 95 %, ECE con diez bins iguales.
RECALL_MINIMO = 0.98
PRECISION_MINIMA = 0.95
WILSON_ORACLE_MINIMO = 0.85
ECE_MAXIMO = 0.05
CONFIANZA = 0.95
BINS_ECE = 10


def wilson(aciertos, total, confianza=CONFIANZA):
    """Intervalo binomial; sin observaciones devuelve [0, 1]."""
    if not 0 <= aciertos <= total or not 0 < confianza < 1:
        raise ValueError("Conteos o confianza inválidos")
    if total == 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf((1 + confianza) / 2)
    p = aciertos / total
    d = 1 + z * z / total
    centro = (p + z * z / (2 * total)) / d
    radio = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return max(0.0, centro - radio), min(1.0, centro + radio)


def matriz_confusion(reales, predichos, pesos=None):
    """Matriz binaria, con pesos opcionales (IPS); positivo significa enviado)."""
    reales, predichos = list(reales), list(predichos)
    pesos = [1] * len(reales) if pesos is None else list(pesos)
    if len(reales) != len(predichos) or len(pesos) != len(reales):
        raise ValueError("Longitudes distintas")
    matriz = dict(tp=0, fp=0, fn=0, tn=0)
    for real, pred, peso in zip(reales, predichos, pesos):
        if not math.isfinite(peso) or peso < 0:
            raise ValueError("Peso inválido")
        matriz[("tp" if real else "fp") if pred else ("fn" if real else "tn")] += peso
    return matriz


def precision_recall_f1(matriz):
    tp, fp, fn = (matriz[k] for k in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "wilson": wilson(tp, tp + fp), "matriz": matriz}


def kappa(uno, dos):
    """κ de Cohen sin ponderación; indefinido se representa con None."""
    uno, dos = list(uno), list(dos)
    if len(uno) != len(dos):
        raise ValueError("Longitudes distintas")
    n = len(uno)
    if not n:
        return None
    acuerdo = sum(a == b for a, b in zip(uno, dos)) / n
    azar = sum(uno.count(v) * dos.count(v) for v in set(uno + dos)) / n**2
    return (acuerdo - azar) / (1 - azar) if azar < 1 else None


def ece(probabilidades, reales, bins=BINS_ECE):
    """Calibración binaria P(positivo), bins [a,b), último cerrado en 1."""
    probabilidades, reales = list(probabilidades), list(reales)
    if len(probabilidades) != len(reales) or bins < 1:
        raise ValueError("Longitudes o bins inválidos")
    if not probabilidades:
        return None
    grupos = defaultdict(list)
    for p, y in zip(probabilidades, reales):
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("Probabilidad inválida")
        grupos[min(int(p * bins), bins - 1)].append((p, bool(y)))
    return sum(abs(sum(p - y for p, y in g)) for g in grupos.values()) / len(reales)


def mcnemar(activa_correcta, sombra_correcta):
    """Test binomial exacto bilateral de McNemar sobre los discordantes."""
    a, s = list(activa_correcta), list(sombra_correcta)
    if len(a) != len(s):
        raise ValueError("Longitudes distintas")
    b = sum(bool(x) and not y for x, y in zip(a, s))
    c = sum(not x and bool(y) for x, y in zip(a, s))
    n = b + c
    # lgamma evita enteros enormes y desbordamientos con muestras grandes.
    p = min(1.0, 2 * sum(math.exp(math.lgamma(n + 1) - math.lgamma(i + 1)
                                - math.lgamma(n - i + 1) - n * math.log(2))
                         for i in range(min(b, c) + 1))) if n else 1.0
    return {"activa_mejor": b, "sombra_mejor": c, "p": p}


def ips(reales, predichos, propensiones):
    """P/R/F1 auto normalizados por 1/P(selección); sin clipping implícito.

    Wilson no es válido para pseudo-conteos ponderados, por eso se omite.
    """
    props = list(propensiones)
    if any(not math.isfinite(p) or not 0 < p <= 1 for p in props):
        raise ValueError("Propensión fuera de (0,1]")
    resultado = precision_recall_f1(matriz_confusion(reales, predichos, [1 / p for p in props]))
    resultado.pop("wilson")
    return resultado


def evaluar(filas):
    """Filas: marca, estrato, real(bool), veredicto y probabilidad opcional [0,1].

    Enviado = confirmada o por_confirmar; faltantes jamás cuentan como aciertos.
    """
    grupos = defaultdict(list)
    for f in filas:
        grupos[(f["marca"].lower(), f.get("estrato", "prueba"))].append(f)
    salida = {}
    for (marca, estrato), grupo in sorted(grupos.items()):
        reales = [f["real"] for f in grupo]
        enviados = [f.get("veredicto") in {"confirmada", "por_confirmar"} for f in grupo]
        confirmados = [f.get("veredicto") == "confirmada" for f in grupo]
        m = precision_recall_f1(matriz_confusion(reales, enviados))
        confirmada = precision_recall_f1(matriz_confusion(reales, confirmados))
        calibrables = [f for f in grupo if f.get("probabilidad") is not None]
        m.update(n=len(grupo), precision_confirmada=confirmada["precision"],
                 wilson_confirmada=confirmada["wilson"],
                 ece=ece([f["probabilidad"] for f in calibrables], [f["real"] for f in calibrables]),
                 n_calibracion=len(calibrables),
                 faltantes=sum(f.get("veredicto") is None for f in grupo))
        salida[f"{marca}/{estrato}"] = m
    return salida


def puede_promover(metricas_candidata, metricas_activa, criticos):
    """Puerta conservadora: exige prueba, críticos completos y misma cobertura.

    criticos es una secuencia de booleanos (decisión exacta correcta por caso).
    Compara P/R/F1, precisión confirmada y ECE por cada marca/estrato de prueba.
    """
    motivos = []
    criticos = list(criticos)
    if not criticos or not all(criticos):
        motivos.append("Casos críticos ausentes o con fallos")
    candidatas = {k: v for k, v in metricas_candidata.items() if k.endswith("/prueba")}
    activas = {k: v for k, v in metricas_activa.items() if k.endswith("/prueba")}
    if not candidatas or candidatas.keys() != activas.keys():
        motivos.append("Cobertura de prueba insuficiente o distinta")
    for clave, m in candidatas.items():
        a = activas.get(clave, {})
        campos = ("precision", "recall", "f1", "precision_confirmada", "ece")
        if any(not isinstance(datos.get(k), (int, float)) or not math.isfinite(datos[k])
               or not 0 <= datos[k] <= 1 for datos in (m, a) for k in campos):
            motivos.append(f"{clave}: métricas ausentes o inválidas")
            continue
        if not m.get("n") or m.get("faltantes", 0) or m.get("n") != a.get("n"):
            motivos.append(f"{clave}: cobertura incompleta")
        for campo, umbral in (("recall", RECALL_MINIMO), ("precision_confirmada", PRECISION_MINIMA)):
            if m.get(campo, -1) < umbral:
                motivos.append(f"{clave}: {campo} insuficiente")
        if clave.startswith("oracle/") and m.get("wilson_confirmada", (0, 1))[0] < WILSON_ORACLE_MINIMO:
            motivos.append(f"{clave}: Wilson Oracle insuficiente")
        if (m.get("ece") is None or m["ece"] > ECE_MAXIMO
                or m.get("n_calibracion") != m.get("n")):
            motivos.append(f"{clave}: calibración insuficiente")
        if any(m.get(k, -1) < a.get(k, math.inf)
               for k in ("precision", "recall", "f1", "precision_confirmada")):
            motivos.append(f"{clave}: regresión frente a activa")
        if a.get("ece") is None or m.get("ece") is None or m["ece"] > a["ece"]:
            motivos.append(f"{clave}: ECE empeora o falta referencia")
    return {"puede_promover": not motivos, "motivos": motivos}
