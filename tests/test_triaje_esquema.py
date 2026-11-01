"""Cassettes sintéticos de C0; no se construyen clientes de red."""
import json
from types import SimpleNamespace

import pytest

from src.drilldown import triage
from src.llm.contrato import Respuesta, Uso


def _ejecutar(monkeypatch, textos, modo="activo", texto="Oracle servidor"):
    pedidos = []
    respuestas = iter(textos)

    def completar(cfg, pedido, **kwargs):
        pedidos.append(pedido)
        return Respuesta(texto=next(respuestas), uso=Uso(input_tokens=7, output_tokens=3),
                         modelo="falso", proveedor="anthropic")

    monkeypatch.setattr(triage.llm, "completar", completar)
    cfg = SimpleNamespace(intel_c0=modo, llm_provider="anthropic", llm_temperatura=0.2)
    resultado = triage.triage(cfg, "contexto", texto=texto,
                              metadatos={"cliente_id": "sintetico", "texto": "no registrar"})
    return resultado, pedidos


@pytest.mark.parametrize("valor", ["sin JSON", "[]", '{"viabilidad":"DESCONOCIDO"}'])
def test_parse_error_no_descarta(monkeypatch, valor):
    res, pedidos = _ejecutar(monkeypatch, [valor, valor])
    assert res[:3] == ("ERROR_LLM", 0.0, "parse_error")
    assert res[3].llamadas == 2
    assert len(pedidos) == 2
    assert valor in pedidos[1].contenido[0].texto
    assert pedidos[0].esquema["additionalProperties"] is False
    assert pedidos[0].temperatura == 0.2
    assert pedidos[0].contexto == {"cliente_id": "sintetico", "capa": "C0", "variante": "activo"}


def test_reparacion_formato_anterior(monkeypatch):
    res, _ = _ejecutar(monkeypatch, ["roto", json.dumps({
        "viabilidad": "GO", "confianza": 0.9, "motivo": "TI"})])
    assert res[:3] == ("GO", 0.9, "TI")
    assert res[3].input_tokens == 14


def test_incierto_sigue_y_valida_citas(monkeypatch):
    res, _ = _ejecutar(monkeypatch, [json.dumps({
        "viabilidad": "INCIERTO", "confianza": 0.1, "motivo": "alcance ambiguo",
        "marcas_mencionadas": [
            {"marca": "Oracle", "cita": "Oracle servidor", "pagina": 1},
            {"marca": "IBM", "cita": "IBM inventado", "pagina": 2}]})])
    assert res[0] == "GO"
    assert res[2].startswith("INCIERTO:")
    assert [m["evidencia_valida"] for m in res.marcas_mencionadas] == [True, False]


def test_sombra_conserva_descarte_anterior(monkeypatch):
    res, _ = _ejecutar(monkeypatch, [
        '{"viabilidad":"NO-GO","confianza":0.8,"motivo":"anterior"}',
        '{"viabilidad":"INCIERTO","confianza":0.2,"motivo":"nuevo"}'], modo="sombra")
    assert res[:3] == ("NO-GO", 0.8, "anterior")
    assert res.datos["decision_sombra"][0] == "GO"
    assert res[3].llamadas == 2
