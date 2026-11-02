"""Relevancia de compras y confirmación de marca son decisiones independientes."""
import json
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

import pytest

from scripts import replay_marca
from src.classifier import engine
from src.classifier.client_profile import _parse_profile
from src.llm.contrato import Respuesta, Uso
from src.scraper.models import Proceso


@pytest.fixture
def perfil():
    return _parse_profile({
        "client": {"id": "ejemplo", "name": "Ejemplo"},
        "catalog": {"categories": [{"id": "ti", "label": "TI", "brands": [
            {"id": "oracle", "name": "Oracle"}]}]},
        "classification": {"relevance_threshold": .6},
    }, "sintetico")


def proceso(titulo):
    return Proceso(institucion="Institución de ejemplo", descripcion=titulo,
                   fecha_publicacion=datetime(2026, 1, 1), url_directa="https://example.com")


def fila(relevante=True, scores=(.8, .3, .2), marcas=None):
    return dict(indice=0, es_relevante=relevante, match_tecnico=scores[0],
                match_categoria=scores[1], factibilidad=scores[2],
                marcas_recomendadas=marcas or [])


def parsear(perfil, titulo, **kw):
    return engine._parsear_v2(json.dumps({"clasificaciones": [fila(**kw)]}),
                             [proceso(titulo)], perfil, [])[0]


@pytest.mark.parametrize("titulo,relevante,scores,esperado", [
    ("Suministro de switches", True, (.8, .2, .1), True),
    ("Renovación de licenciamiento de firewall", True, (.2, .8, .1), True),
    ("Servicios de TI", True, (.2, .1, .8), True),
    ("Compra de nóminas", False, (.9, .9, .9), False),
    ("Suministro de switches", True, (.59, .4, .3), False),
    ("Suministro de switches", True, (.6, .1, .1), True),
])
def test_relevancia_sin_marca(perfil, titulo, relevante, scores, esperado):
    pc = parsear(perfil, titulo, relevante=relevante, scores=scores)
    assert pc.es_relevante is esperado
    assert pc.relevante_llm_crudo is relevante
    assert pc.relevante_con_umbral is esperado
    assert pc.marcas_recomendadas == []
    assert asdict(pc)["relevante_llm_crudo"] is relevante


def test_marca_sin_evidencia_no_cambia_relevancia(perfil):
    pc = parsear(perfil, "Suministro de switches", marcas=[
        dict(marca="Oracle", evidencia="Oracle", score=.99)])
    assert pc.es_relevante
    assert pc.marcas_recomendadas[0].veredicto == "por_confirmar"
    assert not pc.marcas_recomendadas[0].evidencia_valida


@pytest.mark.parametrize("umbral", [None, "inválido", float("nan"), -1, 2])
def test_umbral_invalido_default_seguro(perfil, umbral):
    perfil.relevance_threshold = umbral
    assert parsear(perfil, "Compra TI").es_relevante
    assert not parsear(perfil, "Compra TI", scores=(.59, .1, .1)).es_relevante


def test_perfil_sin_umbral(perfil):
    perfil = SimpleNamespace(catalog=perfil.catalog, rules=perfil.rules)
    assert parsear(perfil, "Compra TI").es_relevante
    assert not parsear(perfil, "Compra TI", scores=(.59, .1, .1)).es_relevante


def test_tasa_sintetica_y_replay(perfil, monkeypatch, tmp_path):
    from src.drilldown import quota

    titulos = ["Suministro de switches", "Licencias de firewall", "Soporte TI", "Compra de nóminas"]
    filas = [dict(fila(relevante=i < 3), indice=i) for i in range(4)]
    texto = json.dumps({"clasificaciones": filas})
    resultados = engine._parsear_v2(texto, [proceso(t) for t in titulos], perfil, [])
    assert sum(p.es_relevante for p in resultados) / 4 == .75
    assert sum(p.relevante_llm_crudo for p in resultados) / 4 == .75
    filas[2].update(match_tecnico=.3, match_categoria=.2, factibilidad=.1)
    texto = json.dumps({"clasificaciones": filas})
    monkeypatch.setattr(engine.llm, "completar", lambda *a, **kw: Respuesta(
        texto=texto, modelo="claude-haiku-4-5", proveedor="anthropic", uso=Uso(10, 5)))
    monkeypatch.setattr(quota, "costo_usd", lambda *a: .01)
    ruta = tmp_path / "checkpoint.json"
    historico = [dict(proceso_gid=f"sintetico:{i}", titulo=t, marcas=[], es_relevante=True)
                 for i, t in enumerate(titulos)]
    reporte = replay_marca.etapa_b(historico, [], SimpleNamespace(llm_provider="anthropic"),
                                   perfil, 1, checkpoint=ruta, decir=lambda *a: None)
    assert reporte["relevantes_por_criterio"] == dict(llm_crudo=3, con_umbral=2, final=2)
    assert reporte["diagnosticos_ausentes"] == 0
    assert reporte["resultados"]["sintetico:0"]["relevante_llm_crudo"] is True
    assert json.loads(ruta.read_text())["resultados"] == reporte["resultados"]


def test_v1_no_aplica_umbral_ni_diagnostico(perfil, monkeypatch):
    from src.drilldown import quota

    texto = json.dumps({"clasificaciones": [fila(scores=(.1, .1, .1))]})
    monkeypatch.setattr(engine.llm, "completar", lambda *a, **kw: Respuesta(
        texto=texto, modelo="claude-haiku-4-5", proveedor="anthropic", uso=Uso(10, 5)))
    monkeypatch.setattr(engine, "_clasificar_v2", lambda *a: pytest.fail("v2 apagada"))
    monkeypatch.setattr(quota, "cobrar", lambda *a: None)
    pc = engine.classify_procesos([proceso("Compra TI")], SimpleNamespace(
        intel_clasif="off", llm_provider="anthropic", app_name="Ejemplo"), perfil)[0]
    assert pc.es_relevante
    assert "relevante_llm_crudo" not in asdict(pc)
