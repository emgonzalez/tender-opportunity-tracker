"""Pruebas sintéticas del diseño de muestreo C6."""
import math

import pytest

from src.aprendizaje.exploracion import seleccionar_exploracion


def test_cupo_propensiones_y_exclusion_determinista():
    candidatos = [{"id": i, "descartado_determinista": i == 29} for i in range(30)]
    filas = seleccionar_exploracion(candidatos, [.5] * 10 + [.1] * 20,
                                   [1.] * 30, 10, semilla=7)
    assert sum(f["mostrado"] for f in filas) == 10
    assert filas[29]["propension"] == 0
    assert not filas[29]["mostrado"]
    assert sum(f["origen"] == "aleatorio" for f in filas) == 2
    assert all(f["propension"] == 2 / 19 for f in filas[10:29])
    assert all(f["propension"] == 1 for f in filas[:8])
    assert candidatos[0] == {"id": 0, "descartado_determinista": False}


def test_distancia_prioriza_caso_no_etiquetado_y_semilla_reproducible():
    c = list(range(40))
    p = [.5] * 10 + [.1] * 30
    d = [0.] + [1.] * 39
    uno = seleccionar_exploracion(c, p, d, 10, semilla=4)
    assert not uno[0]["mostrado"]
    assert uno == seleccionar_exploracion(c, p, d, 10, semilla=4)
    assert uno != seleccionar_exploracion(c, p, d, 10, semilla=5)


def test_sin_bajos_completa_cupo_y_ausencias_neutras():
    filas = seleccionar_exploracion([{"id": i} for i in range(20)], {}, {}, 10)
    assert sum(f["mostrado"] for f in filas) == 10
    assert all(f["propension"] in (0, 1) for f in filas)
    assert all(f["puntaje"] == 1 for f in filas)
    assert seleccionar_exploracion([], [], [], 10) == []


def test_propension_es_frecuencia_marginal_del_diseno():
    conteo = 0
    for semilla in range(1000):
        filas = seleccionar_exploracion(list(range(20)), [.5] * 8 + [.1] * 12,
                                       [1.] * 20, 10, semilla=semilla)
        conteo += filas[10]["mostrado"]
    assert abs(conteo / 1000 - 2 / 12) < .04
    assert math.isclose(filas[10]["propension"], 2 / 12)


@pytest.mark.parametrize("kwargs", [{"cupo": -1}, {"aleatorio_pct": .5},
                                    {"probs": [float("nan")]},
                                    {"etiquetados_vecinos": [2]}])
def test_entradas_invalidas(kwargs):
    datos = dict(candidatos=["a"], probs=[.5], etiquetados_vecinos=[1], cupo=1)
    datos.update(kwargs)
    with pytest.raises(ValueError):
        seleccionar_exploracion(**datos)
