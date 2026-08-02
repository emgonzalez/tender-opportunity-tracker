"""Tests de integración de la Fase B del drill-down con mocks (Fase 2, MVP Track A).

No hace red: triage.triage y analysis.analizar_pliego se parchean; los PDFs son
reales (generados con fitz) para ejercitar pdf.py y prefilter.py de verdad.
"""
import types

import fitz  # pymupdf
import pytest

from src import history
from src.drilldown import pipeline
from src.scraper.models import Proceso, ResultadoAnalisis, MatrizRiesgos, RiesgoItem

INCLUSION = ["servidor", "firewall"]
EXCLUSION = ["alimentos"]

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


def _cfg(tmp_path, daily_budget=1_000_000):
    return types.SimpleNamespace(
        data_dir=str(tmp_path),
        token_budget_path=str(tmp_path / "token_budget.json"),
        pliego_max_pages=100,
        triage_pages=15,
        daily_token_budget=daily_budget,
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


def _patch_analysis_ok(monkeypatch):
    def fake_analysis(pdf_path, cfg, ctx, uuid, formato="", client=None):
        return ResultadoAnalisis(
            proceso_uuid=uuid, estado="ANALIZADO", formato=formato,
            matriz=MatrizRiesgos(items=[RiesgoItem("financiero", "multa", "alta", "5.1")], resumen="ok"),
            modelo_analisis="test",
        )
    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", fake_analysis)


def test_ruta_relevante_analizado(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pipeline.triage, "triage", lambda cfg, ctx, **kw: ("GO", 0.9, "ok"))
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
                        lambda cfg, ctx, **kw: llamadas.append(1) or ("GO", 0.9, "x"))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "irrelevante.pdf", TEXTO_IRRELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P2"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "DESCARTADO_REGEX"
    assert llamadas == []  # el triaje no debe llamarse si no pasa el pre-filtro
    assert history.analisis_estado("cli", "P2", cfg.data_dir) == "DESCARTADO_REGEX"


def test_cuota_excedida_revision_manual(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, daily_budget=10)  # estimado = 1*2000+5000 >> 10
    monkeypatch.setattr(pipeline.triage, "triage", lambda cfg, ctx, **kw: ("GO", 0.9, "ok"))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P3"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "REQUIERE_REVISION_MANUAL"
    assert history.analisis_estado("cli", "P3", cfg.data_dir) == "REQUIERE_REVISION_MANUAL"


def test_descartado_triaje_nogo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pipeline.triage, "triage", lambda cfg, ctx, **kw: ("NO-GO", 0.9, "irrelevante"))
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
                        lambda cfg, ctx, **kw: called.append(1) or ("GO", 0.9, "x"))
    _patch_analysis_ok(monkeypatch)
    pdf_path = _make_pdf(tmp_path, "relevante.pdf", TEXTO_RELEVANTE)

    res = pipeline.analizar_pliego_proceso(
        _proceso("P5"), pdf_path, cfg, "ctx", "cli", INCLUSION, EXCLUSION)

    assert res.estado == "NOTIFICADO"
    assert called == []  # no reprocesa


def test_limpiar_pliegos_borra_archivos(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 dummy")
    pipeline.limpiar_pliegos({"P1": str(p)})
    assert not p.exists()
