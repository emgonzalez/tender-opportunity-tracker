"""Datos sintéticos; C6 no requiere dependencias numéricas ni servicios."""
import json
import math
import random

import pytest

from src.aprendizaje.modelo import Modelo, calibrar_platt, entrenar, peso_muestra


def datos(n=240, ruido=False):
    rng = random.Random(42)
    filas = []
    for _ in range(n):
        x = rng.uniform(-2, 2)
        y = int(rng.random() < 1/(1+math.exp(-2*x))) if ruido else int(x > 0)
        filas.append(dict(features={'v1_senal': x}, etiqueta=y, marca_id='marca_a'))
    return filas


@pytest.mark.parametrize('ruido', [False, True])
def test_convergencia(ruido):
    filas = datos(ruido=ruido)
    modelo = entrenar(filas)
    assert modelo.listo and not modelo.solo_sombra
    assert modelo.predecir_proba({'v1_senal': 1.5}) > .8
    assert modelo.predecir_proba({'v1_senal': -1.5}) < .2
    aciertos = sum((modelo.predecir_proba(f['features']) >= .5) == f['etiqueta']
                   for f in filas)/len(filas)
    assert aciertos > (.7 if ruido else .95)
    assert modelo.parametros == entrenar(filas).parametros


def test_arranque_y_sombra():
    modelo = entrenar(datos(99))
    assert not modelo.listo and modelo.solo_sombra
    assert modelo.predecir_proba({'v1_senal': 20}) == .5
    assert entrenar(datos(120)).solo_sombra
    assert not entrenar([dict(features={'x': 1}, etiqueta=1)]*200).listo
    escasos = [dict(features={'x': i % 2}, etiqueta=int(i < 20)) for i in range(200)]
    assert entrenar(escasos).solo_sombra


def test_interceptos_encogidos_y_marca_escasa():
    filas = []
    for marca, n, positivos in [('a', 80, 70), ('b', 80, 10), ('escasa', 20, 20)]:
        filas += [dict(features={'x': 0}, etiqueta=int(i < positivos), marca_id=marca)
                  for i in range(n)]
    modelo = entrenar(filas, calibracion=[])
    offsets = modelo.parametros['interceptos_marca']
    assert set(offsets) == {'a', 'b'}
    assert 0 < offsets['a'] < math.log(7)
    assert -math.log(7) < offsets['b'] < 0
    assert modelo.predecir_proba({'x': 0}, marca_id='a') > modelo.predecir_proba(
        {'x': 0}, marca_id='b')
    assert modelo.predecir_proba({'x': 0}, marca_id='escasa') == modelo.predecir_proba({'x': 0})


def test_serializacion_y_pesos():
    modelo = entrenar(datos())
    copia = Modelo.desde_parametros(json.loads(json.dumps(modelo.parametros, allow_nan=False)))
    assert modelo.predecir_proba({'v1_senal': 1}) == copia.predecir_proba({'v1_senal': 1})
    assert peso_muestra({'fuente': 'humana', 'antiguedad_dias': 365}) == .5
    assert peso_muestra({'fuente': 'semilla'}) == .3
    with pytest.raises(ValueError):
        modelo.predecir_proba({'v1_senal': float('nan')})


def test_ajena_gana_a_knn_alto():
    filas = [dict(features={'v1_ajena': float(i % 2), 'v1_knn': .99}, etiqueta=1-i % 2)
             for i in range(240)]
    modelo = entrenar(filas)
    assert modelo.predecir_proba({'v1_ajena': 1, 'v1_knn': .99}) < .1


def test_platt_mejora_ece_en_reserva_independiente():
    # Logits exagerados: la frecuencia verdadera es 0.2/0.8 por grupo.
    logits = [-4.0]*100 + [4.0]*100
    etiquetas = [1]*20 + [0]*80 + [1]*80 + [0]*20
    a, b = calibrar_platt(logits, etiquetas)
    def ece(a, b):
        return sum(abs(1/(1+math.exp(-(a*x+b)))-real)
                   for x, real in [(-4, .2), (4, .8)])/2
    assert ece(a, b) < ece(1, 0)/10
    assert calibrar_platt([], []) == [1, 0]
    assert calibrar_platt([1, 2], [1, 1]) == [1, 0]


def test_desbalance_separacion_y_reserva_sin_fuga():
    filas = [dict(features={'x': int(i < 5)}, etiqueta=int(i < 5)) for i in range(200)]
    modelo = entrenar(filas, calibracion=[])
    assert math.isfinite(modelo.logit({'x': 1}))
    assert modelo.predecir_proba({'x': 0}) < .1
    reserva = [dict(features={'x': 1000}, etiqueta=i % 2, marca_id='reserva')
               for i in range(100)]
    otro = entrenar(filas, calibracion=reserva)
    assert otro.parametros['pesos'] == modelo.parametros['pesos']
    assert 'reserva' not in otro.parametros['interceptos_marca']
