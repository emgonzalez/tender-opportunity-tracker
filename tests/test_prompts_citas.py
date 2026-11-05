"""Contratos de citas nuevos: PDF, abstención y aislamiento del legado."""
import json
from types import SimpleNamespace

import pytest

from src import llm
from src.drilldown import analysis, triage, verificacion
from src.drilldown.citas import validar_cita
from src.drilldown.contexto import REGLA_CITAS, armar_contexto

CITA = "Se requiere soporte técnico para las licencias del fabricante."


@pytest.mark.parametrize("prompt", [
    triage.system_prompt(c0=True), analysis.system_prompt(senales=True),
])
def test_regla_exclusiva_del_pliego(prompt):
    assert REGLA_CITAS in prompt
    assert "24 caracteres normalizados y cuatro palabras" in prompt
    assert validar_cita("uno dos tres " + "x" * 10, "uno dos tres " + "x" * 10).ok is False
    assert validar_cita("uno dos tres " + "x" * 11, "uno dos tres " + "x" * 11).ok


def test_esquemas_citas_anulables():
    for esquema in (triage._CITA, triage._EXIGIDA, analysis._CITA_MARCA,
                    verificacion.ESQUEMA):
        propiedades = esquema["properties"]
        assert propiedades["cita"]["type"] == ["string", "null"]
        assert propiedades["pagina"]["type"] == ["integer", "null"]
    assert "motivo" in verificacion.ESQUEMA["required"]


@pytest.mark.parametrize("cita,pagina", [
    (None, None), ("", 0), ("No se encontró información en el pliego", 1),
    ("No se encontro ningúna cita literal", 1), ("Sin evidencia de la marca", None),
    ("No hay evidencia suficiente", 1), ("Evidencia insuficiente", 1),
])
def test_parseo_normaliza_abstenciones(cita, pagina):
    marca = dict(marca="Marca de ejemplo", cita=cita, pagina=pagina)
    datos = triage._datos_triaje(json.dumps(dict(
        viabilidad="GO", confianza=0.8, motivo="Objeto TI",
        marcas_mencionadas=[marca], marca_exigida=marca)))
    analisis = analysis._datos_analisis(json.dumps(dict(
        resumen="Sin respaldo", items=[], marca_exigida=marca,
        marcas_involucradas=[marca])))
    for salida in (datos["marca_exigida"], datos["marcas_mencionadas"][0],
                   analisis["marca_exigida"], analisis["marcas_involucradas"][0]):
        assert salida["cita"] is salida["pagina"] is None


@pytest.mark.parametrize("cita,pagina", [(None, None), ("", 0),
                                       ("No se encontró información", 1),
                                       ("Sin evidencia suficiente", 1)])
def test_c3_abstencion_conserva_motivo_y_consumo(monkeypatch, cita, pagina):
    uso = llm.Uso(20, 10)
    pedidos = []

    def completar(cfg, pedido):
        pedidos.append(pedido)
        return llm.Respuesta(json.dumps(dict(
            soporta="insuficiente", cita=cita, pagina=pagina, motivo="Sin respaldo explícito")),
            uso, verificacion.MAVERICK, "oci")

    monkeypatch.setattr(llm, "completar", completar)
    cfg = SimpleNamespace(llm_provider="oci", intel_c3="activo",
                          verificacion_presupuesto_disponible=True)
    salida = verificacion.verificar_afirmacion("Requiere soporte", {1: CITA}, cfg)
    assert salida.soporta == "insuficiente"
    assert salida.cita is salida.pagina is None
    assert salida.motivo == "Sin respaldo explícito" and salida.uso is uso
    assert REGLA_CITAS in pedidos[0].sistema


def test_c1_rotulado_con_datos_intactos():
    bloque = armar_contexto(
        {"titulo": "Proceso de ejemplo"}, {}, [{"marca": "Marca de ejemplo",
        "evidencia": "Indicio de ejemplo"}], [], [])
    assert "NO citable" in bloque
    assert "Título: Proceso de ejemplo" in bloque
    assert "Marca candidata: Marca de ejemplo; evidencia de apoyo: Indicio de ejemplo" in bloque


def test_triaje_abstencion_no_reintenta_ni_rescata(monkeypatch):
    pedidos = []

    def completar(cfg, pedido, **kwargs):
        pedidos.append(pedido)
        return llm.Respuesta(json.dumps(dict(
            viabilidad="GO", confianza=0.8, motivo="Objeto TI",
            marcas_mencionadas=[dict(marca="Ejemplo", cita=None, pagina=None)])),
            llm.Uso(1, 1), "ejemplo", "anthropic")

    monkeypatch.setattr(llm, "completar", completar)
    salida = triage.triage(SimpleNamespace(intel_c0="activo", llm_provider="anthropic"),
                          "Cliente de ejemplo", texto=CITA)
    assert salida[0] == "GO" and len(pedidos) == 1
    assert salida.marcas_mencionadas[0]["evidencia_valida"] is False


def test_legado_no_normaliza_citas_ni_agrega_regla():
    assert REGLA_CITAS not in triage.system_prompt()
    assert REGLA_CITAS not in analysis.system_prompt()
    datos = {"items": [], "cita": "No se encontró información", "pagina": 1}
    assert analysis._limpiar_texto_modelo(datos) == datos
