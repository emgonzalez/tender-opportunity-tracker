"""Cassettes públicos sintéticos: ninguna llamada real a LLM o ADB."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.classifier import engine
from src.classifier.client_profile import (
    Brand,
    Catalog,
    CatalogCategory,
    ClientProfile,
    Intelligence,
    Rules,
)
from src.llm.contrato import LLMError, Respuesta, Uso
from src.scraper.models import Proceso


@pytest.fixture
def perfil():
    return ClientProfile(
        client_id="ejemplo", client_name="Ejemplo", client_description="Integrador",
        active=True, catalog=Catalog(
            [CatalogCategory("software", "Software", ["licencias"], [Brand("oracle", "Oracle", [])])],
            ["licencias"], [], ["Software"]),
        rules=Rules("", "software, otro"), notification=None,
        intelligence=Intelligence(None, 0, None))


def cassette(titulo, **campos):
    p = Proceso(institucion="Institución de ejemplo", descripcion=titulo,
                fecha_publicacion=datetime(2026, 1, 1), url_directa="https://example.com/1")
    c = dict(indice=0, es_relevante=True, categoria_detectada="software",
             match_tecnico=.95, match_categoria=.95, factibilidad=.95, tags=["IBM", "Software"],
             marcas_recomendadas=[dict(marca="Oracle", score=.99, role="Recomendada", evidencia="Oracle")])
    c.update(campos)
    return p, json.dumps({"clasificaciones": [c]})


@pytest.mark.parametrize("titulo,veredicto,relevante", [
    ("Renovación y soporte de licencias COGNOS", "fuera", False),
    ("Renovación licenciamiento de productos Oracle", "confirmada", True),
    ("Compra de nóminas", "por_confirmar", False),
    ("Claude Pro", "por_confirmar", False),
    ("ArcGIS", "por_confirmar", False),
    ("servidores Sun V880", "confirmada", True),
    ("migración de Oracle a PostgreSQL", "por_confirmar", False),
    ("Crystal Ball", "confirmada", True),
    ("Crystal Reports", "fuera", False),
])
def test_cassettes(perfil, titulo, veredicto, relevante):
    p, texto = cassette(titulo)
    terminos = engine._terminos_v2(SimpleNamespace(), perfil)
    pc = engine._parsear_v2(texto, [p], perfil, terminos)[0]
    assert pc.marcas_recomendadas[0].veredicto == veredicto
    assert pc.es_relevante is relevante
    assert pc.tags == ["Software"]
    if "COGNOS" in titulo:
        assert any(m["marca"] == "ibm" and m["evidencia"].lower() == "cognos" for m in pc.marcas_ajenas)
        assert not pc.marcas_recomendadas[0].evidencia_valida


def test_umbrales_y_categoria(perfil):
    p, texto = cassette("Compra de licencias", evidencia_categoria="licencias", marcas_recomendadas=[])
    ts = engine._terminos_v2(SimpleNamespace(), perfil)
    assert engine._parsear_v2(texto, [p], perfil, ts)[0].es_relevante
    perfil.confidence_threshold = .99
    assert not engine._parsear_v2(texto, [p], perfil, ts)[0].es_relevante
    perfil.confidence_threshold = .7
    perfil.relevance_threshold = .99
    assert not engine._parsear_v2(texto, [p], perfil, ts)[0].es_relevante


@pytest.mark.parametrize("modo,llamadas", [("off", 1), ("sombra", 2), ("activo", 1)])
def test_flags(perfil, monkeypatch, modo, llamadas):
    from src.drilldown import quota

    p, texto = cassette("Claude Pro")
    pedidos = []
    def completar(cfg, pedido, **kw):
        pedidos.append(pedido)
        return Respuesta(texto=texto, modelo="claude-haiku-4-5", proveedor="anthropic", uso=Uso(10, 5))
    monkeypatch.setattr(engine.llm, "completar", completar)
    monkeypatch.setattr(quota, "cobrar", lambda *a: None)
    cfg = SimpleNamespace(intel_clasif=modo, app_name="Ejemplo", llm_provider="anthropic")
    resultado = engine.classify_procesos([p], cfg, perfil)
    assert len(pedidos) == llamadas
    assert resultado[0].es_relevante is (modo != "activo")
    if modo != "off":
        assert "tipo_actividad" in pedidos[-1].contenido[0].texto
        assert "PROHIBIDO" in pedidos[-1].sistema


def test_llm_caido_activo_conserva_diccionario(perfil, monkeypatch):
    from src.drilldown import quota

    def caer(*a, **kw):
        raise LLMError("Fallo simulado")
    monkeypatch.setattr(engine.llm, "completar", caer)
    monkeypatch.setattr(quota, "cobrar", lambda *a: None)
    p, _ = cassette("Oracle")
    salida = engine.classify_procesos([p], SimpleNamespace(intel_clasif="activo", llm_provider="anthropic"), perfil)
    assert salida[0].marcas_recomendadas[0].veredicto == "confirmada"


def test_adb_caida_con_dsn_invalido(perfil, monkeypatch):
    from contextlib import contextmanager

    from src.memoria import db

    @contextmanager
    def caida(cfg):
        assert cfg.adb_dsn == "dsn-invalido-de-prueba"
        raise RuntimeError("ADB simulada caída")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "connection", caida)
    cfg = SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, adb_dsn="dsn-invalido-de-prueba")
    # La semilla pública conserva las reglas aunque falte la memoria.
    assert any(t["termino"] == "Oracle" for t in engine._terminos_v2(cfg, perfil))


def test_vecinos_llegan_al_prompt(perfil, monkeypatch):
    from src.aprendizaje import service, vecinos

    monkeypatch.setattr(engine.llm, "soporta_esquema", lambda modelo: True)
    monkeypatch.setattr(service, "_tolerante", lambda nombre, cfg, conectar, vacio, operacion: operacion(None))
    monkeypatch.setattr(service, "terminos_activos", lambda *a: [])
    monkeypatch.setattr(vecinos, "consultar_vecinos", lambda *a: [
        {"titulo_texto": "Licencias Oracle", "etiqueta": "positivo", "evidencia": "Oracle", "distancia": .1}])
    p, texto = cassette("Oracle")
    pedidos = []
    def completar(cfg, pedido):
        pedidos.append(pedido)
        assert "Licencias Oracle" in pedido.sistema
        assert "score_knn" in pedido.sistema
        assert pedido.esquema["properties"]["clasificaciones"]["items"]["properties"]["marcas_recomendadas"]
        return Respuesta(texto=texto, modelo="claude-haiku-4-5", proveedor="anthropic", uso=Uso(10, 5))
    monkeypatch.setattr(engine.llm, "completar", completar)
    engine._clasificar_v2([p], SimpleNamespace(llm_provider="anthropic"), perfil)
    assert "Licencias Oracle" in pedidos[0].sistema
    assert pedidos[0].esquema["properties"]["clasificaciones"]["items"]["properties"]["marcas_recomendadas"]


def test_migracion_sin_sugerencia_llm(perfil):
    p, texto = cassette("migración de Oracle a PostgreSQL", marcas_recomendadas=[])
    pc = engine._parsear_v2(texto, [p], perfil, engine._terminos_v2(SimpleNamespace(), perfil))[0]
    assert pc.marcas_recomendadas[0].veredicto == "por_confirmar"
    assert "Migración" in pc.marcas_recomendadas[0].por_que
