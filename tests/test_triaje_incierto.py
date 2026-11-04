"""Contrato C0 y paridad del triaje con proveedor falso, sin red."""

import json
from types import SimpleNamespace

import pytest

from src.drilldown import triage
from src.llm.contrato import Respuesta, Uso


def datos(viabilidad="GO", **extra):
    return {"viabilidad": viabilidad, "confianza": 0.8, "motivo": "Objeto evaluado", **extra}


@pytest.fixture
def proveedor(monkeypatch):
    pedidos = []
    respuestas = []

    def completar(cfg, pedido, **kwargs):
        pedidos.append(pedido)
        valor = respuestas.pop(0)
        if isinstance(valor, Exception):
            raise valor
        return Respuesta(json.dumps(valor), Uso(10, 5), "falso", "falso")

    monkeypatch.setattr(triage.llm, "completar", completar)
    return pedidos, respuestas


def cfg(modo):
    return SimpleNamespace(intel_c0=modo, llm_provider="anthropic")


@pytest.mark.parametrize("viabilidad", ["GO", "NO-GO", "INCIERTO"])
def test_respuestas_anteriores_sin_causa(viabilidad):
    original = datos(viabilidad)
    assert triage._datos_triaje(json.dumps(original)) == original


@pytest.mark.parametrize("viabilidad", ["GO", "NO-GO", "INCIERTO"])
@pytest.mark.parametrize("causa", triage.ESQUEMA_TRIAJE["properties"]["causa_incierto"]["enum"])
def test_coherencia_de_causa(viabilidad, causa):
    texto = json.dumps(datos(viabilidad, causa_incierto=causa))
    if (viabilidad == "INCIERTO") == (causa != "ninguna"):
        assert triage._datos_triaje(texto)["causa_incierto"] == causa
    else:
        with pytest.raises(ValueError, match="incoherente"):
            triage._datos_triaje(texto)


@pytest.mark.parametrize("causa", [None, 1, [], {}, "desconocida"])
def test_causa_fuera_del_enum(causa):
    with pytest.raises(ValueError, match="causa_incierto inválida"):
        triage._datos_triaje(json.dumps(datos(causa_incierto=causa)))


@pytest.mark.parametrize("motivo", ["", " \n\t"])
def test_no_go_exige_motivo(motivo):
    with pytest.raises(ValueError, match="requiere motivo"):
        triage._datos_triaje(json.dumps(datos("NO-GO", motivo=motivo)))


@pytest.mark.parametrize("extra", [{}, {"causa_incierto": "extracto_ilegible"}])
def test_incierto_sigue_a_go_y_transporta_datos(proveedor, extra):
    _, respuestas = proveedor
    respuestas.append(datos("INCIERTO", motivo="Extracto ilegible", **extra))
    resultado = triage.triage(cfg("activo"), "Integrador TI", texto="?")
    assert resultado[:3] == ("GO", 0.8, "INCIERTO: Extracto ilegible")
    assert resultado.datos == datos("INCIERTO", motivo="Extracto ilegible", **extra)


def test_prompt_y_esquema_c0(proveedor):
    pedidos, respuestas = proveedor
    respuestas.append(datos(causa_incierto="ninguna"))
    triage.triage(cfg("activo"), "Integrador TI", texto="Software",
                  contexto_proceso="Objeto: adquisición de software")
    pedido = pedidos[0]
    for regla in ("INCIERTO solo si", "vacío o ilegible", "contradictoria real",
                  "sin una parte dominante", "no TI => NO-GO", "=> GO",
                  "prohibido usar INCIERTO por falta de datos del cliente",
                  "contexto del cliente", "título/objeto del proceso"):
        assert regla in pedido.sistema
    assert pedido.contenido[0].texto == "Objeto: adquisición de software"
    assert "causa_incierto" in pedido.esquema["required"]
    assert pedido.esquema["properties"]["causa_incierto"]["enum"] == [
        "ninguna", "extracto_ilegible", "objeto_ambiguo", "mixto_ti_no_ti", "falta_pliego",
    ]


def test_off_paridad_con_legado(proveedor):
    pedidos, respuestas = proveedor
    respuestas.extend([datos("NO-GO", motivo="Obra civil")] * 2)
    argumentos = dict(texto="Pavimentación", contexto_proceso="Objeto: obra civil")
    legado = triage._triage_legacy(cfg("off"), "Integrador TI", **argumentos)
    resultado = triage.triage(cfg("off"), "Integrador TI", **argumentos)
    assert resultado == legado
    assert pedidos[0] == pedidos[1]
    assert pedidos[1].sistema == triage._SYSTEM_PROMPT
    assert pedidos[1].esquema is None


@pytest.mark.parametrize("observacion", [
    datos("INCIERTO", causa_incierto="mixto_ti_no_ti"), RuntimeError("fallo simulado"),
])
def test_sombra_conserva_decision_legada(proveedor, observacion):
    pedidos, respuestas = proveedor
    respuestas.extend([datos("NO-GO", motivo="Obra civil"), observacion])
    resultado = triage.triage(cfg("sombra"), "Integrador TI", texto="Obra civil")
    assert resultado[:3] == ("NO-GO", 0.8, "Obra civil")
    assert pedidos[0].sistema == triage._SYSTEM_PROMPT
    assert len(pedidos) == 2
    assert resultado[3].llamadas == (1 if isinstance(observacion, Exception) else 2)


@pytest.mark.parametrize("respuestas_falsas", [
    [RuntimeError("fallo simulado")],
    [datos("NO-GO", motivo=""), datos("NO-GO", motivo="")],
    [datos("INCIERTO", causa_incierto="ninguna")] * 2,
])
def test_error_llm_no_descarta(proveedor, respuestas_falsas):
    pedidos, respuestas = proveedor
    respuestas.extend(respuestas_falsas)
    resultado = triage.triage(cfg("activo"), "Integrador TI", texto="Software")
    assert resultado[0] == "ERROR_LLM"
    assert len(pedidos) == len(respuestas_falsas)


def test_reintento_corrige_causa_incoherente(proveedor):
    pedidos, respuestas = proveedor
    respuestas.extend([datos("INCIERTO", causa_incierto="ninguna"),
                       datos("GO", causa_incierto="ninguna")])
    resultado = triage.triage(cfg("activo"), "Integrador TI", texto="Software")
    assert resultado[0] == "GO"
    assert resultado.datos["causa_incierto"] == "ninguna"
    assert len(pedidos) == 2
    assert resultado[3].llamadas == 2


def test_contenido_marca_paginas_fisicas_solo_si_se_pide(tmp_path):
    import fitz

    ruta = tmp_path / "sintetico.pdf"
    with fitz.open() as documento:
        documento.new_page().insert_text((72, 72), "Objeto de la contratacion de servidores")
        documento.new_page().insert_text((72, 72), "Segunda pagina del pliego")
        documento.save(ruta)
    marcado = triage._build_user_content("cliente", texto="x", pdf_path=str(ruta),
                                         scanned=False, marcar_paginas=True)
    assert "[[Página 2]]" in marcado[-1].texto
    plano = triage._build_user_content("cliente", texto="x", pdf_path=str(ruta), scanned=False)
    assert "[[Página" not in plano[-1].texto
    imagenes = triage._build_user_content("cliente", texto=None, pdf_path=str(ruta),
                                          scanned=True, raster_pages=2, marcar_paginas=True)
    rotulos = [b.texto for b in imagenes if hasattr(b, "texto") and b.texto.startswith("Página física")]
    assert rotulos == ["Página física 1 del PDF", "Página física 2 del PDF"]
