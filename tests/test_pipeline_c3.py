"""Verificación con LLM y cuota falsos; sin proveedores externos."""
from types import SimpleNamespace

import pytest

from src import llm
from src.drilldown import pipeline, quota, verificacion
from src.scraper.models import ResultadoAnalisis


@pytest.mark.parametrize("modo,soporta,vision,competible", [
    ("sombra", "insuficiente", False, False),
    ("activo", "insuficiente", False, None),
    ("activo", "no", False, None),
    ("activo", "si", True, None),
    ("activo", "si", False, False)])
def test_verificacion_c3(monkeypatch, modo, soporta, vision, competible):
    cfg = SimpleNamespace(intel_c3=modo, enable_memoria=False)
    proceso = SimpleNamespace(numero_referencia="P1", descripcion="Oracle", match_tecnico=.9,
                              marcas_recomendadas=[])
    res = ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO", formato="NATIVO",
                            competible=False, modelo_analisis="analista")
    cobrados, decisiones = [], []
    monkeypatch.setattr(pipeline, "_paginas_verificables", lambda *a: {
        1: {"texto": "El contrato exige una marca específica", "fuente_texto": "capa_texto"}})
    monkeypatch.setattr(llm, "modelo_de", lambda *a: "verificador")
    monkeypatch.setattr(quota, "cabe", lambda *a: True)
    monkeypatch.setattr(quota, "costo_verificacion_estimado_usd", lambda *a, **k: 1)
    monkeypatch.setattr(quota, "cobrar", lambda cfg, uso: cobrados.append(uso))
    uso = llm.Uso(input_tokens=12, output_tokens=4)
    monkeypatch.setattr(verificacion, "verificar_afirmacion", lambda *a, **k: verificacion.Verificacion(
        soporta=soporta, cita="El contrato exige una marca específica", pagina=1,
        solo_vision=vision, uso=uso))
    from src.aprendizaje import service
    monkeypatch.setattr(service, "guardar_decision", lambda *a, **k: decisiones.append(k))
    pipeline._verificar_c3(cfg, proceso, res, "", [], ["Oracle"], "cliente")
    assert res.competible is competible
    assert cobrados[0].input_tokens == 12
    assert cobrados[0].llamadas == 1
    assert decisiones[0]["variante"] == ("sombra" if modo == "sombra" else "activa")
    assert ("verificacion_c3" in res.anexos) == (modo == "activo")
    if modo == "activo" and soporta == "no":
        assert res.anexos["verificacion_c3"]["oracle"]["respuesta"] == "incierta"


def test_estimacion_verificador(monkeypatch):
    monkeypatch.setattr(llm, "max_tokens_de", lambda *a: 123)
    monkeypatch.setattr(quota, "costo_usd", lambda m, i, o: (m, i, o))
    assert quota.costo_verificacion_estimado_usd(None, "datos", modelo="m") == (
        "m", quota._tokens_de_texto("datos") + quota._OVERHEAD_PROMPT_TOKENS, 123)


def test_pliego_pagina_y_decision_atomicas(monkeypatch):
    from contextlib import nullcontext

    from src.aprendizaje import paginas, repo, service
    from src.memoria import db

    cfg = SimpleNamespace()
    conexion = object()
    guardadas, decisiones = [], []
    cita = "Se requiere renovación y soporte de IBM Cognos"
    res = ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO", marca_exigida={
        "marca": "IBM Cognos", "cita": cita, "pagina": 2})
    monkeypatch.setattr(service, "habilitado", lambda cfg: True)
    monkeypatch.setattr(service, "terminos_activos", lambda *a: [])
    monkeypatch.setattr(db, "connection", lambda cfg: nullcontext(conexion))
    monkeypatch.setattr(paginas, "guardar_paginas", lambda conn, gid, filas: (
        guardadas.append((conn, gid, filas)) or 1))
    monkeypatch.setattr(repo, "guardar_decision", lambda *a, **k: decisiones.append((a, k)))
    pipeline._aprender_pliego(cfg, "cliente", "P1", res, {
        2: {"texto": cita, "fuente_texto": "capa_texto"}})
    assert guardadas[0][0] is decisiones[0][0][0] is conexion
    assert guardadas[0][1] == decisiones[0][0][2]
    assert decisiones[0][1]["salida"]["marca_exigida"]["marca"] == "ibm"
    assert decisiones[0][1]["score"] == 1


def test_pliego_no_aprende_pagina_ausente(monkeypatch):
    from src.aprendizaje import service
    monkeypatch.setattr(service, "habilitado", lambda cfg: True)
    res = ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO", marca_exigida={
        "marca": "IBM", "cita": "La marca IBM es obligatoria para este proceso", "pagina": 8})
    pipeline._aprender_pliego(SimpleNamespace(), "cliente", "P1", res, {})


def test_c3_sin_cuota_no_llama_ni_cobra(monkeypatch):
    from src.aprendizaje import service
    cfg = SimpleNamespace(intel_c3="activo", enable_memoria=False)
    proceso = SimpleNamespace(numero_referencia="P1", descripcion="Oracle", match_tecnico=.9,
                              marcas_recomendadas=[])
    res = ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO", formato="NATIVO")
    monkeypatch.setattr(pipeline, "_paginas_verificables", lambda *a: {
        1: {"texto": "El proceso exige Oracle Database", "fuente_texto": "capa_texto"}})
    monkeypatch.setattr(llm, "modelo_de", lambda *a: "verificador")
    monkeypatch.setattr(quota, "cabe", lambda *a: False)
    monkeypatch.setattr(quota, "costo_verificacion_estimado_usd", lambda *a, **k: 1)
    monkeypatch.setattr(service, "habilitado", lambda *a: False)
    monkeypatch.setattr(verificacion, "verificar_afirmacion", lambda *a, **k: pytest.fail("LLM sin cuota"))
    monkeypatch.setattr(quota, "cobrar", lambda *a: pytest.fail("cobro sin llamada"))
    pipeline._verificar_c3(cfg, proceso, res, "", [], ["Oracle"], "cliente")


@pytest.mark.parametrize("nombre,marca_id,exigida,equivalente", [
    ("Oracle", "oracle", "Java", None),
    ("IBM", "ibm", "Cognos", None),
    ("Marca de ejemplo", "marca_ejemplo", "Marca de ejemplo", None),
    ("Oracle", "oracle", "IBM", True),
])
def test_c3_ids_catalogo_y_aliases_propios(monkeypatch, nombre, marca_id, exigida, equivalente):
    from src.aprendizaje import service

    cfg = SimpleNamespace(intel_c3="activo", enable_memoria=False)
    proceso = SimpleNamespace(numero_referencia="P1", descripcion=nombre, match_tecnico=.9,
                              marcas_recomendadas=[])
    res = ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO", formato="NATIVO",
                            marca_exigida={"marca": exigida}, equivalente_admitido=equivalente)
    afirmaciones = []
    monkeypatch.setattr(pipeline, "_paginas_verificables", lambda *a: {
        1: {"texto": "Texto de ejemplo para verificar la afirmación", "fuente_texto": "capa_texto"}})
    monkeypatch.setattr(llm, "modelo_de", lambda *a: "verificador")
    monkeypatch.setattr(quota, "cabe", lambda *a: True)
    monkeypatch.setattr(quota, "costo_verificacion_estimado_usd", lambda *a, **k: 0)
    monkeypatch.setattr(quota, "cobrar", lambda *a: None)
    monkeypatch.setattr(service, "guardar_decision", lambda *a, **k: None)
    monkeypatch.setattr(verificacion, "verificar_afirmacion", lambda a, *args, **k: (
        afirmaciones.append(a) or verificacion.Verificacion()))
    pipeline._verificar_c3(cfg, proceso, res, "", [], [nombre], "cliente", [marca_id])
    assert not afirmaciones[0]["negativa"]
    assert set(res.anexos["verificacion_c3"]) == {marca_id}
