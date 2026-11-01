"""Reconciliación por marca con persistencias falsas y sin conexiones externas."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.drilldown import reconciliar as modulo
from src.scraper.models import MarcaRecomendada, ProcesoClasificado, ResultadoAnalisis


@pytest.fixture
def entorno(monkeypatch):
    perfil = SimpleNamespace(client_id="ejemplo", catalog=SimpleNamespace(categories=[
        SimpleNamespace(brands=[SimpleNamespace(id="oracle", name="Oracle"),
                                SimpleNamespace(id="cisco", name="Cisco")])]))
    for nombre, retorno in (("terminos_activos", []), ("calibracion_activa", None),
                            ("guardar_decision", None)):
        monkeypatch.setattr(modulo.aprendizaje, nombre, Mock(return_value=retorno))
    monkeypatch.setattr(modulo.memoria, "actualizar_marcas", Mock(return_value=True))
    monkeypatch.setattr(modulo.memoria, "_portal", Mock(return_value="portal_ejemplo"))
    return perfil


def proceso(titulo):
    return ProcesoClasificado(
        institucion="Institución de ejemplo", descripcion=titulo,
        fecha_publicacion=datetime(2026, 1, 1), url_directa="", numero_referencia="EJEMPLO-1",
        es_relevante=True, match_tecnico=0.86,
        marcas_recomendadas=[MarcaRecomendada(marca="Oracle", score=0.86, role="Recomendada")])


def test_off_es_noop_total(entorno):
    p = proceso("Licencias COGNOS")
    antes = deepcopy(p)
    modulo.reconciliar([p], {}, SimpleNamespace(marca_veredicto="off"), entorno)
    assert p == antes
    modulo.aprendizaje.terminos_activos.assert_not_called()
    modulo.memoria.actualizar_marcas.assert_not_called()


@pytest.mark.parametrize("modo", ["sombra", "activo"])
@pytest.mark.parametrize("titulo,nivel", [
    ("Renovación y soporte de licencias COGNOS", "fuera"),
    ("Renovación licenciamiento de productos Oracle", "confirmada"),
    ("Migración fuera de Oracle", "por_confirmar"),
])
def test_casos_por_marca_y_objeto_actualizado(entorno, modo, titulo, nivel):
    p = proceso(titulo)
    ra = ResultadoAnalisis(proceso_uuid=p.numero_referencia, estado="ANALIZADO")
    if "COGNOS" in titulo:
        ra.marca_exigida = {"marca": "IBM/Cognos", "cita": "Exclusivo IBM Cognos"}
        ra.competible = False
    modulo.reconciliar([p], {p.numero_referencia: ra}, SimpleNamespace(marca_veredicto=modo), entorno)
    marca = p.marcas_recomendadas[0]
    assert marca.veredicto == nivel
    assert marca.por_que
    assert marca.score == 0.86
    assert marca.role == "Recomendada" and len(p.marcas_recomendadas) == 1
    assert p.es_relevante and p.match_tecnico == 0.86
    if "COGNOS" in titulo:
        assert p.marcas_ajenas[0]["marca"] == "ibm"
    llamada = modulo.aprendizaje.guardar_decision.call_args
    assert llamada.args[2] == "portal_ejemplo:EJEMPLO-1"
    assert llamada.args[3] == "veredicto"
    assert set(llamada.kwargs["salida"]["marcas"]) == {"oracle", "cisco"}
    modulo.memoria.actualizar_marcas.assert_called_once_with(
        SimpleNamespace(marca_veredicto=modo), "ejemplo", "EJEMPLO-1", p.marcas_recomendadas)


def test_fallos_persistencia_no_pierden_memoria(entorno):
    modulo.memoria.actualizar_marcas.side_effect = RuntimeError("ADB no disponible")
    modulo.aprendizaje.guardar_decision.side_effect = RuntimeError("ADB no disponible")
    p = proceso("Oracle")
    modulo.reconciliar([p], {}, SimpleNamespace(marca_veredicto="sombra"), entorno)
    assert p.marcas_recomendadas[0].veredicto == "confirmada"
    modulo.aprendizaje.guardar_decision.assert_called_once()


def test_no_relevantes_no_se_mutan(entorno):
    p = proceso("Oracle")
    p.es_relevante = False
    antes = deepcopy(p)
    modulo.reconciliar([p], {}, SimpleNamespace(marca_veredicto="activo"), entorno)
    assert p == antes


@pytest.mark.parametrize("modo,nivel", [("off", "confirmada"), ("sombra", "confirmada"),
                                        ("activo", "fuera")])
def test_c3_solo_consume_activo(entorno, modo, nivel):
    p = proceso("Oracle")
    ra = ResultadoAnalisis(proceso_uuid=p.numero_referencia, estado="ANALIZADO", anexos={
        "verificacion_c3": {"oracle": {"respuesta": "no", "cita": "No se requiere Oracle",
                                        "evidencia_valida": True, "fuente_texto": "nativo"}}})
    modulo.reconciliar([p], {p.numero_referencia: ra},
                      SimpleNamespace(marca_veredicto="sombra", intel_c3=modo), entorno)
    assert p.marcas_recomendadas[0].veredicto == nivel


def test_adb_caida_con_dsn_invalido_sin_red(monkeypatch):
    from src.memoria import db

    conectar = Mock(side_effect=ValueError("DSN inválido"))
    monkeypatch.setattr(db, "connection", conectar)
    monkeypatch.setattr(modulo.memoria, "_portal", lambda *args: "portal_ejemplo")
    perfil = SimpleNamespace(client_id="ejemplo", catalog=SimpleNamespace(categories=[
        SimpleNamespace(brands=[SimpleNamespace(id="oracle", name="Oracle")])]))
    cfg = SimpleNamespace(marca_veredicto="sombra", enable_memoria=True,
                          enable_esquema_v2=True, adb_dsn="dsn-invalido")
    p = proceso("Licencias Oracle")
    modulo.reconciliar([p], {}, cfg, perfil)
    assert p.marcas_recomendadas[0].veredicto == "confirmada"
    assert conectar.call_count >= 4


@pytest.mark.parametrize("fuente,pagina_citada,nivel", [
    ("capa_texto", 2, "fuera"),
    ("vision_llm", 2, "por_confirmar"),
    ("capa_texto", 1, "por_confirmar"),
    ("capa_texto", None, "por_confirmar"),
])
def test_exigida_valida_pagina_exacta_y_preserva_fuente(entorno, monkeypatch,
                                                      fuente, pagina_citada, nivel):
    p = proceso("Renovación de plataforma analítica")
    cita = "Se exige exclusivamente licenciamiento IBM Cognos del fabricante"
    ra = ResultadoAnalisis(proceso_uuid=p.numero_referencia, estado="ANALIZADO",
                          marca_exigida={"marca": "IBM Cognos", "cita": cita,
                                         "pagina": pagina_citada, "evidencia_valida": True,
                                         "segunda_fuente": True})
    antes = deepcopy(ra)
    monkeypatch.setattr(modulo.aprendizaje, "paginas_pliego", Mock(return_value=[
        {"pagina": 1, "texto": "Página introductoria sin marca exigida", "fuente": "capa_texto"},
        {"pagina": 2, "texto": cita, "fuente": fuente}]))
    modulo.reconciliar([p], {p.numero_referencia: ra},
                      SimpleNamespace(marca_veredicto="sombra"), entorno)
    assert p.marcas_recomendadas[0].veredicto == nivel
    assert ra == antes
    modulo.aprendizaje.paginas_pliego.assert_called_once_with(
        SimpleNamespace(marca_veredicto="sombra"), "portal_ejemplo:EJEMPLO-1")


def test_paginas_pliego_apagado_no_conecta(monkeypatch):
    from src.memoria import db

    conectar = Mock(side_effect=AssertionError("no debe abrir conexión"))
    monkeypatch.setattr(db, "connection", conectar)
    assert modulo.aprendizaje.paginas_pliego(SimpleNamespace(enable_memoria=False), "portal:ref") == []
    conectar.assert_not_called()
