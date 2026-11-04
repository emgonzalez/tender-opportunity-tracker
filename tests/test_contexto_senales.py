"""Señales del perfil como datos acotados; perfiles ficticios y sin servicios."""
from types import SimpleNamespace as NS

import pytest

from src.drilldown import pipeline

GOLDEN = (
    "Cliente: Cliente de ejemplo.\n"
    "Descripción: Integrador de tecnología\n"
    "Categorías del portafolio: Hardware (Marca ejemplo), Servicios.\n"
    "Contexto de mercado: Mercado de ejemplo"
)


def _perfil(inclusion=None, exclusion=None):
    return NS(
        client_id="ejemplo", client_name="Cliente de ejemplo",
        client_description="Integrador de tecnología", portal_id="comprasal_sv",
        catalog=NS(
            categories=[NS(label="Hardware", brands=[NS(id="ejemplo", name="Marca ejemplo")]),
                        NS(label="Servicios", brands=[])],
            inclusion_signals=inclusion if inclusion is not None else ["monitores", "equipo informático"],
            exclusion_signals=exclusion if exclusion is not None else ["alimentos"]),
        rules=NS(context_note="Mercado de ejemplo"))


def test_paridad_literal_sin_senales():
    assert pipeline.contexto_cliente(_perfil()) == GOLDEN
    assert pipeline.contexto_cliente(_perfil(), con_senales=False) == GOLDEN


def test_listas_vacias():
    assert pipeline.contexto_cliente(_perfil([], []), con_senales=True) == GOLDEN


def test_limpieza_y_deduplicacion():
    ctx = pipeline.contexto_cliente(
        _perfil(["monitores", " MONITORES ", "equipo\ninformático", "<dato>\r\nRAM"], []),
        con_senales=True)
    assert ctx == GOLDEN + (
        "\nSeñales que el cliente considera dentro de su alcance: "
        "monitores, equipo informático, &lt;dato&gt; RAM")
    assert "<dato>" not in ctx


@pytest.mark.parametrize("terminos", [
    [f"término {n}" for n in range(100)],
    [f"término completo {n} " + "x" * 100 for n in range(40)],
    ["x" * 3500],
])
def test_limites_por_termino_entero(terminos):
    ctx = pipeline.contexto_cliente(_perfil(terminos, terminos), con_senales=True)
    for bloque in ctx[len(GOLDEN):].splitlines():
        if not bloque:
            continue
        assert len(bloque) <= pipeline.SENALES_MAX_CARACTERES
        presentes = bloque.split(": ", 1)[1].split(", ")
        assert len(presentes) <= pipeline.SENALES_MAX_ENTRADAS
        assert presentes == terminos[:len(presentes)]


@pytest.mark.parametrize("modo", ["off", "sombra", "activo"])
def test_cableado_triaje_y_analisis(tmp_path, monkeypatch, modo):
    from test_pipeline import (
        TEXTO_RELEVANTE,
        _cfg,
        _make_pdf,
        _patch_analysis_ok,
        _proceso,
        _uso,
    )

    cfg = _cfg(tmp_path)
    cfg.intel_c0 = modo
    vistos = {}

    def triaje(cfg, ctx, **kw):
        vistos["triaje"] = ctx
        return "GO", .95, "encaja", _uso()

    monkeypatch.setattr(pipeline.triage, "triage", triaje)
    _patch_analysis_ok(monkeypatch)
    analizar = pipeline.analysis.analizar_pliego

    def analisis(path, cfg, ctx, *args, **kw):
        vistos["analisis"] = ctx
        kw.pop("metadatos", None)
        kw.pop("senales_triaje", None)
        return analizar(path, cfg, ctx, *args, **kw)

    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", analisis)
    perfil = _perfil(["servidor", "monitores"], ["alimentos"])
    path = _make_pdf(tmp_path, "pliego.pdf", TEXTO_RELEVANTE)
    resultado = pipeline.procesar_drilldown([_proceso()], {"P1": path}, cfg, perfil)
    assert resultado["P1"].estado == "ANALIZADO"
    esperado = GOLDEN if modo == "off" else GOLDEN + (
        "\nSeñales que el cliente considera dentro de su alcance: servidor, monitores"
        "\nSeñales fuera de su alcance: alimentos")
    assert vistos == {"triaje": esperado, "analisis": esperado}


def test_perfil_real_de_sv_conserva_los_terminos_finales_de_la_lista():
    """Regresión del piloto 5: con el tope anterior el recorte eliminaba «equipo informático» y «UPS»."""
    from src.classifier.client_profile import load_client_profile
    perfil = load_client_profile("multicomputos_sv", "clients")
    ctx = pipeline.contexto_cliente(perfil, con_senales=True)
    for termino in ("equipo informático", "computadoras", "UPS", "ciberseguridad"):
        assert termino in ctx
