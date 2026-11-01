"""Stacker C6 puro: logística L2, offsets de marca y Platt sobre reserva separada.

Las filas son {features, etiqueta, marca_id?, fuente?, peso?, antiguedad_dias?}.
No consulta reloj, archivos, servicios ni configuración. No decide veredictos.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

PESOS_FUENTE = {
    'humana': 1.0, 'confirmacion': 1.0, 'resultado_comercial': 1.0,
    'pliego': 0.9, 'voto_usuario': 0.8, 'participacion': 0.7,
    'verificacion_llm': 0.4, 'semilla': 0.3,
}


def _numero(valor):
    numero = float(valor)
    if not math.isfinite(numero):
        raise ValueError('Se requieren valores finitos')
    return numero


def _sigmoide(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _etiqueta(fila):
    valor = fila['etiqueta']
    if valor in (1, 'positivo'):
        return 1.0
    if valor in (0, 'negativo', 'ajena'):
        return 0.0
    raise ValueError('Etiqueta desconocida')


def peso_muestra(fila):
    """Fiabilidad por fuente y semivida anual; edad explícita para reproducibilidad."""
    return (max(0.0, _numero(fila.get('peso', 1)))
            * PESOS_FUENTE.get(fila.get('fuente', 'humana'), 0.5)
            * 0.5 ** (max(0.0, _numero(fila.get('antiguedad_dias', 0))) / 365))


def calibrar_platt(logits, etiquetas, pesos=None):
    """Ajuste Newton de dos parámetros; pendiente positiva y L2 evitan separación.

    Debe recibir exclusivamente una reserva que no entrenó el clasificador.
    Reserva vacía o de una sola clase conserva la transformación identidad.
    """
    if len(logits) != len(etiquetas):
        raise ValueError('Longitudes incompatibles')
    pesos = [1.0] * len(logits) if pesos is None else list(pesos)
    if len(pesos) != len(logits):
        raise ValueError('Longitudes incompatibles')
    xs = [_numero(x) for x in logits]
    ys = [_etiqueta({'etiqueta': y}) for y in etiquetas]
    ws = [max(0.0, _numero(w)) for w in pesos]
    if not any(w for y, w in zip(ys, ws) if y == 0) or not any(
            w for y, w in zip(ys, ws) if y == 1):
        return [1.0, 0.0]
    a, b = 1.0, 0.0

    def perdida(a, b):
        return sum(w * (max(z := a*x+b, 0) + math.log1p(math.exp(-abs(z))) - y*z)
                   for x, y, w in zip(xs, ys, ws)) + .01 * ((a-1)**2 + b*b)

    for _ in range(100):
        ga, gb, aa, ab, bb = .02*(a-1), .02*b, .02, 0.0, .02
        for x, y, w in zip(xs, ys, ws):
            p = _sigmoide(a*x+b)
            d, h = w*(p-y), w*p*(1-p)
            ga += d*x
            gb += d
            aa += h*x*x
            ab += h*x
            bb += h
        det = aa*bb-ab*ab
        da, db = (bb*ga-ab*gb)/det, (aa*gb-ab*ga)/det
        paso, anterior = 1.0, perdida(a, b)
        while paso > 1e-8:
            na, nb = max(0.0, a-paso*da), b-paso*db
            if perdida(na, nb) <= anterior:
                break
            paso *= .5
        if paso <= 1e-8:
            break
        a, b = na, nb
        if paso * (abs(da)+abs(db)) < 1e-7:
            break
    return [a, b]


@dataclass(frozen=True)
class Modelo:
    """Parámetros JSON nativos; marca opcional nunca forma parte del vector numérico."""

    parametros: dict

    @property
    def listo(self):
        return self.parametros['listo']

    @property
    def solo_sombra(self):
        return self.parametros['solo_sombra']

    @classmethod
    def desde_parametros(cls, parametros):
        if parametros.get('version') != 'stacker_v1':
            raise ValueError('Versión de modelo desconocida')
        return cls(parametros)

    def logit(self, features, *, marca_id=None):
        p = self.parametros
        return (p['intercepto'] + p['interceptos_marca'].get(marca_id, 0.0)
                + sum(w * (_numero(features.get(k, m))-m)/s
                      for k, w, m, s in zip(p['nombres'], p['pesos'], p['medias'], p['escalas'])))

    def predecir_proba(self, features, *, marca_id=None, calibrada=True):
        if not self.listo:
            return 0.5
        z = self.logit(features, marca_id=marca_id)
        a, b = self.parametros['platt'] if calibrada else (1.0, 0.0)
        return _sigmoide(a*z+b)


def entrenar(ejemplos, *, calibracion=None, semilla=1729, l2=0.01,
             l2_marca=0.2, max_iter=800, tolerancia=1e-7):
    """Un modelo global; offsets con ≥30 etiquetas y penalización mayor al global.

    Sin reserva explícita se aparta 20% estratificado. No se balancean artificialmente
    las clases: se conserva el prior para probabilidades útiles con desbalance.
    <100 etiquetas o una sola clase da arranque frío; ≥150 y ≥30 positivos habilita
    salir de sombra (no equivale a promoción ni activa flags).
    """
    if l2 <= 0 or l2_marca < l2 or max_iter < 1:
        raise ValueError('Regularización o iteraciones inválidas')
    filas = [dict(f) for f in ejemplos if peso_muestra(f) > 0]
    ys = [_etiqueta(f) for f in filas]
    n, positivos = len(filas), sum(ys)
    nombres = sorted({k for f in filas for k in f['features']})
    p = dict(version='stacker_v1', listo=False, solo_sombra=True, n_etiquetas=n,
             n_positivos=int(positivos), nombres=nombres, medias=[0.0]*len(nombres),
             escalas=[1.0]*len(nombres), pesos=[0.0]*len(nombres), intercepto=0.0,
             interceptos_marca={}, platt=[1.0, 0.0], semilla=semilla)
    modelo = Modelo(p)
    if n < 100 or positivos in (0, n):
        return modelo
    if any(set(f['features']) != set(nombres) for f in filas):
        raise ValueError('Todas las filas deben compartir el esquema de features')
    reserva = list(calibracion) if calibracion is not None else []
    if calibracion is None:
        rng = random.Random(semilla)
        indices = set()
        for clase in (0, 1):
            grupo = [i for i, y in enumerate(ys) if y == clase]
            rng.shuffle(grupo)
            indices.update(grupo[:int(len(grupo)*.2)])
        reserva = [f for i, f in enumerate(filas) if i in indices]
        filas = [f for i, f in enumerate(filas) if i not in indices]
    # El conteo de offsets usa solo el subconjunto que entrena, nunca la reserva.
    marcas = sorted({f.get('marca_id') for f in filas if f.get('marca_id')})
    marcas = [m for m in marcas if sum(f.get('marca_id') == m for f in filas) >= 30]
    p['medias'] = [sum(_numero(f['features'][k]) for f in filas)/len(filas) for k in nombres]
    p['escalas'] = [max(.1, math.sqrt(sum((_numero(f['features'][k])-m)**2
                                         for f in filas)/len(filas)))
                    for k, m in zip(nombres, p['medias'])]
    xs = [[1.0] + [(_numero(f['features'][k])-m)/s
                   for k, m, s in zip(nombres, p['medias'], p['escalas'])]
          + [float(f.get('marca_id') == marca) for marca in marcas] for f in filas]
    ws, ys = [peso_muestra(f) for f in filas], [_etiqueta(f) for f in filas]
    total = sum(ws)
    prior = (sum(w*y for w, y in zip(ws, ys))+.5)/(total+1)
    beta = [math.log(prior/(1-prior))] + [0.0]*(len(xs[0])-1)
    penal = [0.0001] + [l2]*len(nombres) + [l2_marca]*len(marcas)
    # Cota de Lipschitz de la Hessiana: paso estable incluso con separación perfecta.
    paso = 1 / (.25*sum(w*sum(v*v for v in x) for w, x in zip(ws, xs))/total
                + max(penal))
    for _ in range(max_iter):
        grad = [r*b for r, b in zip(penal, beta)]
        for x, y, w in zip(xs, ys, ws):
            d = w*(_sigmoide(sum(b*v for b, v in zip(beta, x)))-y)/total
            for j, v in enumerate(x):
                grad[j] += d*v
        beta = [b-paso*g for b, g in zip(beta, grad)]
        if max(abs(g) for g in grad) < tolerancia:
            break
    p.update(listo=True, solo_sombra=n < 150 or positivos < 30,
             intercepto=beta[0], pesos=beta[1:1+len(nombres)],
             interceptos_marca=dict(zip(marcas, beta[1+len(nombres):])))
    p['platt'] = calibrar_platt([modelo.logit(f['features'], marca_id=f.get('marca_id'))
                               for f in reserva], [_etiqueta(f) for f in reserva],
                              [peso_muestra(f) for f in reserva])
    return modelo
