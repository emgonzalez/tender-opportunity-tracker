"""Selector C2: descarte por fila sin servicios externos."""
import json
from types import SimpleNamespace

import fitz
import pytest

from src.drilldown import lectura
from src.llm.contrato import Respuesta, Uso


@pytest.fixture
def selector(tmp_path, monkeypatch):
    ruta = tmp_path / "sintetico.pdf"
    with fitz.open() as doc:
        for _ in range(30):
            doc.new_page(width=30, height=40)
        doc.save(str(ruta))
    monkeypatch.setattr(lectura.llm, "modelo_de", lambda *a: "falso")
    monkeypatch.setattr(lectura.quota, "cabe_lectura", lambda *a, **kw: True)
    monkeypatch.setattr(lectura.quota, "cobrar_lectura", lambda *a, **kw: None)
    cfg = SimpleNamespace(intel_c2="activo", llm_temperatura=0, lectura_tiempo_max_s=120)

    def ejecutar(lotes, total=30):
        respuestas = iter(lotes)
        llamadas = []

        def completar(*args):
            llamadas.append(args[1])
            valor = next(respuestas)
            if isinstance(valor, Exception):
                raise valor
            if isinstance(valor, Respuesta):
                return valor
            return Respuesta(json.dumps({"paginas": valor}), Uso(1, 1), "falso", "falso")

        monkeypatch.setattr(lectura.llm, "completar", completar)
        diagnostico = {}
        salida = lectura.seleccionar_paginas(str(ruta), total, cfg, {}, diagnostico=diagnostico)
        return salida, diagnostico, llamadas

    return ejecutar


def test_filas_invalidas_no_pierden_validas_ni_lotes(selector, caplog):
    invalidas = [None, [], {"n": True, "tipo": "marca"}, {"n": "2", "tipo": "marca"},
                 {"n": 11, "tipo": "marca"}, {"n": 3, "tipo": "otro"},
                 {"n": 2.0, "tipo": "marca"}, {"tipo": "marca"}]
    salida, diag, llamadas = selector([
        [*invalidas, {"n": 5, "tipo": "carta"}],
        [{"n": 19, "tipo": "marca"}], [{"n": 23, "tipo": "fabricante"}]])
    assert salida == [1, 19, 23, 5]
    assert diag == {"c2_selector": "ok", "c2_filas_descartadas": 8}
    assert len(llamadas) == 3
    assert "8 filas descartadas" in caplog.text


def test_lote_invalido_no_impide_siguientes(selector):
    salida, diag, llamadas = selector([[False], [{"n": 12, "tipo": "objeto"}], [None]])
    assert salida == [1, 12]
    assert diag == {"c2_selector": "ok", "c2_filas_descartadas": 2}
    assert len(llamadas) == 3


def test_solo_invalidas_permite_fallback(selector):
    salida, diag, _ = selector([[{"n": True, "tipo": "marca"}]], total=10)
    assert salida == []
    assert diag == {"c2_selector": "vacio", "c2_filas_descartadas": 1}


def test_respuesta_vacia_conserva_portada(selector):
    salida, diag, _ = selector([[], [], []])
    assert salida == [1]
    assert diag == {"c2_selector": "ok", "c2_filas_descartadas": 0}


@pytest.mark.parametrize("fallo", [
    Respuesta('{"paginas":[{"n":12,"tipo":"marca"}]}', Uso(1, 1), "falso", "falso",
              truncada=True),
    Respuesta('{', Uso(1, 1), "falso", "falso"),
    Respuesta('{"paginas":{}}', Uso(1, 1), "falso", "falso"),
    lectura._TiempoAgotado("tiempo"), lectura._PresupuestoAgotado("presupuesto"),
])
@pytest.mark.parametrize("previas", [[], [{"n": 4, "tipo": "marca"}]])
def test_fallo_estricto_conserva_solo_resultados_previos(selector, fallo, previas):
    salida, diag, llamadas = selector([previas, fallo])
    assert salida == ([1, 4] if previas else [])
    assert diag["c2_selector"].startswith("error:")
    assert diag["c2_filas_descartadas"] == 0
    assert len(llamadas) == 2


def test_prioridad_duplicados_y_limite_paridad_valida(selector):
    salida, diag, _ = selector([
        [{"n": n, "tipo": "objeto"} for n in range(1, 11)],
        [{"n": 18, "tipo": "marca"}, {"n": 12, "tipo": "fabricante"},
         {"n": 11, "tipo": "carta"}, {"n": 12, "tipo": "objeto"}],
        [{"n": 30, "tipo": "admisibilidad"}, {"n": 21, "tipo": "especificaciones"}]])
    assert salida == [1, 18, 12, 11, 30, 21, 2, 3, 4, 5]
    assert diag == {"c2_selector": "ok", "c2_filas_descartadas": 0}
