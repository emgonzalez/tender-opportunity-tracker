"""Contrato reproducible y evidencia del vector puro C6."""
from dataclasses import dataclass

import pytest

from src.aprendizaje.features import NOMBRES_FEATURES, extraer_features

DIC = [
    {"marca_id": "oracle", "tipo": "fuerte", "termino": "MySQL"},
    {"marca_id": "oracle", "tipo": "debil", "termino": "base de datos"},
    {"marca_id": "ibm", "tipo": "ajena", "termino": "Cognos"},
]


def extraer(proc=None, **kwargs):
    entradas = dict(dic=DIC, knn=None, llm=None, pliego=None, mercado=None,
                    verificador=None, forma=None)
    entradas.update(kwargs)
    return extraer_features(proc or {}, "oracle", **entradas)


def test_ausencias_neutras_y_orden_versionado():
    vector = extraer()
    assert tuple(vector) == NOMBRES_FEATURES
    assert len(vector) == 13
    assert all(k.startswith("v1_") for k in vector)
    assert vector["v1_score_knn"] == vector["v1_afinidad"] == 0.5
    assert vector["v1_equivalente_admitido"] == 0.5
    assert vector["v1_logprob_triaje"] == vector["v1_score_llm"] == 0


def test_terminos_no_confunden_ajena_con_fuerte_y_respetan_limites():
    vector = extraer({"descripcion": "Cognos con base de datos MySQL"}, knn=0.99)
    assert vector["v1_termino_fuerte"] == vector["v1_termino_debil"] == 1
    assert vector["v1_termino_ajena"] == 1
    assert extraer({"descripcion": "MySQLX"})["v1_termino_fuerte"] == 0


def test_llm_y_verificador_requieren_cita_real():
    llm = dict(score=0.9, evidencia="MySQL", evidencia_valida=True)
    vector = extraer({"descripcion": "Renovación MySQL"}, llm=llm,
                     verificador={**llm, "voto": -1})
    assert vector["v1_score_llm"] == 0.9
    assert vector["v1_voto_verificador"] == -1
    assert extraer(llm=llm)["v1_score_llm"] == 0
    assert extraer({"descripcion": "MySQL"}, llm={**llm, "evidencia_valida": False})["v1_score_llm"] == 0


def test_pliego_exige_fuente_y_alias_propio():
    cita = {"marca": "Oracle", "cita": "Licencias MySQL"}
    pliego = dict(texto="Licencias MySQL obligatorias", marca_exigida=cita,
                  marcas_involucradas=[cita], equivalente_admitido=False)
    vector = extraer(pliego=pliego)
    assert vector["v1_marca_exigida"] == vector["v1_mencion_pliego"] == 1
    assert vector["v1_equivalente_admitido"] == 0
    assert extraer(pliego={**pliego, "texto": "otra cosa"})["v1_marca_exigida"] == 0


def test_dataclass_y_senales_acotadas_sin_mutacion():
    @dataclass
    class Proceso:
        descripcion: str = "MySQL"
        forma_contratacion: str = "Contratación directa"

    mercado = {"afinidad": 1.8, "proveedor_unico_hecho": 0.8}
    vector = extraer(Proceso(), mercado=mercado, forma={"logprob_triaje": -0.7})
    assert vector["v1_afinidad"] == vector["v1_contratacion_directa"] == 1
    assert vector["v1_proveedor_unico_hecho"] == 0.8
    assert vector["v1_logprob_triaje"] == -0.7
    assert mercado["afinidad"] == 1.8


@pytest.mark.parametrize("valor", [float("nan"), float("inf"), "invalido"])
def test_entradas_numericas_no_finitas_son_neutras(valor):
    assert extraer(knn=valor)["v1_score_knn"] == 0.5
