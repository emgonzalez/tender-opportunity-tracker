"""Tests de integración de la Fase B del drill-down con mocks (Fase 2, MVP Track A).

No hace red: triage.triage y analysis.analizar_pliego se parchean; los PDFs son
reales (generados con fitz) para ejercitar pdf.py y prefilter.py de verdad.
"""
import json
import types

import fitz  # pymupdf
import pytest

from src import history
from src.drilldown import pipeline, quota
from src.scraper.models import (
    Proceso, ResultadoAnalisis, MatrizRiesgos, RiesgoItem, UsoModelo,
)

INCLUSION = ["servidor", "firewall"]
EXCLUSION = ["alimentos"]


def _uso(inp=12_660, out=48):
    """Uso de tokens simulado del triaje (5 páginas rasterizadas ≈ 12.6k)."""
    return UsoModelo(modelo="claude-haiku-4-5", input_tokens=inp,
                     output_tokens=out, llamadas=1)

TEXTO_RELEVANTE = (
    "Contratación de servidor rack con firewall y almacenamiento para el "
    "data center institucional, incluye licenciamiento y soporte."
)
TEXTO_IRRELEVANTE = (
    "Suministro de alimentos y bebidas para el comedor institucional durante "
    "el ejercicio fiscal en curso, entrega mensual."
)


def _make_pdf(tmp_path, name, texto):
    path = tmp_path / name
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), texto)
    doc.save(str(path))
    doc.close()
    return str(path)


def _cfg(tmp_path, daily_budget_usd=5.00):
    return types.SimpleNamespace(
        data_dir=str(tmp_path),
        budget_path=str(tmp_path / "gasto_diario.json"),
        pliego_max_pages=100,
        triage_pages=15,
        triage_raster_pages=5,
        daily_budget_usd=daily_budget_usd,
        analisis_tokens_por_pagina=3000,
        drilldown_tmp_dir=str(tmp_path / "tmp"),
        model_triage="claude-haiku-4-5",
        model_analysis="claude-sonnet-5",
        anthropic_api_key="x",
    )


def _proceso(uuid="P1"):
    return Proceso(
        institucion="Ministerio X",
        descripcion="descripcion",
        fecha_publicacion=None,
        url_directa="https://comprasal/pac/1",
        numero_referencia=uuid,
    )


def _uso_analisis(inp=60_000, out=1_200):
    """Uso simulado del análisis (pliego de 20 páginas como bloque `document`)."""
    return UsoModelo(modelo="claude-sonnet-5", input_tokens=inp,
                     output_tokens=out, llamadas=1)


def _patch_analysis_ok(monkeypatch):
    def fake_analysis(pdf_path, cfg, ctx, uuid, formato="", client=None):
        return ResultadoAnalisis(
            proceso_uuid=uuid, estado="ANALIZADO", formato=formato,
            matriz=MatrizRiesgos(items=[RiesgoItem("financiero", "multa", "alta", "5.1")], resumen="ok"),
            modelo_analisis="test", uso_analisis=_uso_analisis(),
        )
    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", fake_analysis)


def test_ruta_relevante_analizado(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pipeline.triage, "triage", lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P1"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "ANALIZADO"
    assert res.matriz and res.matriz.items[0].categoria == "financiero"
    assert history.analisis_estado("cli", "P1", cfg.data_dir) == "ANALIZADO"


def test_descartado_regex_no_llama_triaje(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    llamadas = []
    monkeypatch.setattr(pipeline.triage, "triage",
                        lambda cfg, ctx, **kw: llamadas.append(1) or ("GO", 0.9, "x", _uso()))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "irrelevante.pdf", TEXTO_IRRELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P2"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "DESCARTADO_REGEX"
    assert llamadas == []  # el triaje no debe llamarse si no pasa el pre-filtro
    assert history.analisis_estado("cli", "P2", cfg.data_dir) == "DESCARTADO_REGEX"


def test_cuota_excedida_revision_manual(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, daily_budget_usd=0.0)  # nada cabe
    monkeypatch.setattr(pipeline.triage, "triage", lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P3"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "REQUIERE_REVISION_MANUAL"
    assert history.analisis_estado("cli", "P3", cfg.data_dir) == "REQUIERE_REVISION_MANUAL"


def test_descartado_triaje_nogo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pipeline.triage, "triage", lambda cfg, ctx, **kw: ("NO-GO", 0.9, "irrelevante", _uso()))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P4"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "DESCARTADO_TRIAJE"


def test_idempotencia_salta_estado_terminal(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    # Sembrar un estado terminal previo.
    history.save_analisis("cli", ResultadoAnalisis(proceso_uuid="P5", estado="NOTIFICADO"), cfg.data_dir)
    called = []
    monkeypatch.setattr(pipeline.triage, "triage",
                        lambda cfg, ctx, **kw: called.append(1) or ("GO", 0.9, "x", _uso()))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P5"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "NOTIFICADO"
    assert called == []  # no reprocesa


class TestInstrumentacionDeCosto:
    """Sin estos campos la heurística de cuota no se puede calibrar.

    La corrida del 2026-08-03 gastó el presupuesto entero sin dejar registro de
    cuántas páginas tenía cada pliego ni cuántos tokens costó realmente: el
    historial solo guardaba el estado. Estos tests impiden que vuelva a pasar.
    """

    def test_analizado_registra_paginas_tamano_y_uso(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        res = pipeline.analizar_pliego_proceso(
            _proceso("I1"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        assert res.n_paginas == 1
        assert res.tamano_bytes > 0
        assert res.uso_triaje.input_tokens == 12_660
        assert res.uso_triaje.modelo == "claude-haiku-4-5"

    def test_descartes_tambien_registran_el_tamano(self, tmp_path, monkeypatch):
        """Un pliego descartado sigue siendo un dato: cuántos y de qué tamaño."""
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "irrelevante.pdf", TEXTO_IRRELEVANTE)

        res = pipeline.analizar_pliego_proceso(
            _proceso("I2"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        assert res.estado == "DESCARTADO_REGEX"
        assert res.n_paginas == 1
        assert res.tamano_bytes > 0

    def test_nogo_conserva_el_uso_del_triaje(self, tmp_path, monkeypatch):
        """El triaje se pagó aunque el veredicto sea NO-GO."""
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("NO-GO", 0.9, "no aplica", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        res = pipeline.analizar_pliego_proceso(
            _proceso("I3"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        assert res.estado == "DESCARTADO_TRIAJE"
        assert res.uso_triaje.input_tokens == 12_660

    def test_la_instrumentacion_llega_al_jsonl(self, tmp_path, monkeypatch):
        """asdict() debe serializar los campos nuevos, no solo tenerlos en RAM."""
        import json

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        pipeline.analizar_pliego_proceso(
            _proceso("I4"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        linea = (tmp_path / "history" / "cli.analisis.jsonl").read_text().strip()
        d = json.loads(linea)
        assert d["n_paginas"] == 1
        assert d["tamano_bytes"] > 0
        assert d["uso_triaje"]["input_tokens"] == 12_660


class TestGuardiaDeGasto:
    """La guardia vieja estimaba antes del triaje pero solo reservaba tras un GO.

    Consecuencia: el gasto de triaje —el mayor del pipeline en un corpus
    mayoritariamente escaneado— nunca entraba al libro, así que el presupuesto
    diario se podía exceder sin que la guardia se enterara. Estos tests fijan
    que ahora se cobra por etapa y con el consumo REAL, no con la estimación.
    """

    def test_el_triaje_se_cobra_aunque_sea_nogo(self, tmp_path, monkeypatch):
        """LA fuga: un NO-GO gastó tokens de Haiku que antes no se anotaban."""
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("NO-GO", 0.9, "no aplica", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        pipeline.analizar_pliego_proceso(
            _proceso("G1"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        # 12.660 in x $1/MTok + 48 out x $5/MTok
        assert quota.gasto_del_dia(cfg) == pytest.approx(0.01290)

    def test_el_analisis_se_cobra_ademas_del_triaje(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        pipeline.analizar_pliego_proceso(
            _proceso("G2"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        libro = json.loads((tmp_path / "gasto_diario.json").read_text())
        assert set(libro["por_modelo"]) == {"claude-haiku-4-5", "claude-sonnet-5"}
        # 60.000 in x $3/MTok + 1.200 out x $15/MTok = $0.198
        assert libro["por_modelo"]["claude-sonnet-5"]["gasto_usd"] == pytest.approx(0.198)
        assert libro["gasto_usd"] == pytest.approx(0.198 + 0.01290)

    def test_sin_presupuesto_para_el_analisis_no_se_llama_a_sonnet(self, tmp_path, monkeypatch):
        """El triaje cabe y se cobra; el análisis ya no. Debe frenarse ANTES de gastar."""
        cfg = _cfg(tmp_path, daily_budget_usd=0.02)  # alcanza para el triaje, no más
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
        llamadas = []

        def fake_analysis(*a, **kw):
            llamadas.append(1)
            raise AssertionError("no debió llamarse al análisis sin presupuesto")

        monkeypatch.setattr(pipeline.analysis, "analizar_pliego", fake_analysis)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        res = pipeline.analizar_pliego_proceso(
            _proceso("G3"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        assert llamadas == []
        assert res.estado == "REQUIERE_REVISION_MANUAL"
        # el triaje ya ocurrió: su gasto queda anotado y su uso, persistido
        assert res.uso_triaje.input_tokens == 12_660
        assert quota.gasto_del_dia(cfg) == pytest.approx(0.01290)

    def test_el_descarte_por_regex_no_gasta_nada(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "irrelevante.pdf", TEXTO_IRRELEVANTE)

        pipeline.analizar_pliego_proceso(
            _proceso("G4"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        assert quota.gasto_del_dia(cfg) == 0.0

    def test_el_gasto_se_acumula_entre_pliegos(self, tmp_path, monkeypatch):
        """El libro es diario, no por pliego: si no acumulara, no frenaría nada."""
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(pipeline.triage, "triage",
                            lambda cfg, ctx, **kw: ("NO-GO", 0.9, "x", _uso()))
        _patch_analysis_ok(monkeypatch)
        pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

        for uuid in ("G5", "G6", "G7"):
            pipeline.analizar_pliego_proceso(
                _proceso(uuid), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

        assert quota.gasto_del_dia(cfg) == pytest.approx(3 * 0.01290)


def test_limpiar_pliegos_borra_archivos(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 dummy")
    pipeline.limpiar_pliegos({"P1": str(p)})
    assert not p.exists()
