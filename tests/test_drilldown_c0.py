"""Cascada C0 con persistencia local y proveedores sintéticos."""
import pytest
from test_pipeline import (
    EXCLUSION,
    INCLUSION,
    TEXTO_RELEVANTE,
    _cfg,
    _make_pdf,
    _proceso,
    _uso,
)

from src.drilldown import pipeline
from src.drilldown.triage import ResultadoTriaje


def test_tres_errores_persistidos_exigen_revision(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.intel_c0 = "activo"
    path = _make_pdf(tmp_path, "sintetico.pdf", TEXTO_RELEVANTE)
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k:
                        ResultadoTriaje("ERROR_LLM", 0.0, "parse_error", _uso()))
    estados = []
    for intento in range(1, 4):
        res = pipeline.analizar_pliego_proceso(_proceso(), path, cfg, "ctx", "cli",
                                              INCLUSION, EXCLUSION)
        estados.append(res.estado)
        assert res.anexos["triaje_intentos"] == intento
        assert res.triaje != "NO-GO"
    assert estados == ["ERROR_LLM", "ERROR_LLM", "REQUIERE_REVISION_MANUAL"]


@pytest.mark.parametrize("veredicto,motivo,datos", [
    ("GO", "INCIERTO: faltan datos", {}),
    ("NO-GO", "fuera de alcance", {"marcas_mencionadas": [
        {"marca": "Oracle", "cita": "Oracle", "evidencia_valida": True}]}),
])
def test_incierto_y_marca_citada_continuan(tmp_path, monkeypatch, veredicto, motivo, datos):
    cfg = _cfg(tmp_path)
    cfg.intel_c0 = "activo"
    path = _make_pdf(tmp_path, "sintetico.pdf", TEXTO_RELEVANTE)
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k:
                        ResultadoTriaje(veredicto, 0.1, motivo, _uso(), datos))
    from src.scraper.models import MatrizRiesgos, ResultadoAnalisis

    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", lambda *a, **k:
                        ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO",
                                          matriz=MatrizRiesgos(items=[], resumen="TI")))
    res = pipeline.analizar_pliego_proceso(_proceso(), path, cfg, "ctx", "cli",
                                          INCLUSION, EXCLUSION, marcas_cliente=("Oracle",))
    assert res.estado == "ANALIZADO"
    assert res.triaje == "GO"


def test_adb_caida_con_dsn_invalido_no_pierde_error(tmp_path, monkeypatch):
    from src.memoria import db

    cfg = _cfg(tmp_path, enable_memoria=True)
    cfg.intel_c0 = "activo"
    cfg.adb_dsn = "dsn-invalido-sintetico"
    cfg.enable_esquema_v2 = False
    path = _make_pdf(tmp_path, "sintetico.pdf", TEXTO_RELEVANTE)

    def caida(config):
        assert config.adb_dsn == "dsn-invalido-sintetico"
        raise RuntimeError("DSN inválido")

    monkeypatch.setattr(db, "connection", caida)
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k:
                        ResultadoTriaje("ERROR_LLM", 0.0, "parse_error", _uso()))
    res = pipeline.analizar_pliego_proceso(_proceso(), path, cfg, "ctx", "cli",
                                          INCLUSION, EXCLUSION)
    assert res.estado == "ERROR_LLM"
    assert res.anexos["triaje_intentos"] == 1
