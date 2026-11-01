"""Intentos de triaje por cliente con ADB sintética y respaldo local."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

from test_pipeline import EXCLUSION, INCLUSION, TEXTO_RELEVANTE, _cfg, _make_pdf, _proceso, _uso

from src.drilldown import pipeline
from src.drilldown.triage import ResultadoTriaje
from src.memoria import db, repo, service
from src.scraper.models import ResultadoAnalisis


def test_repo_intentos_claves_y_actualizacion_opcional():
    conn = Mock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (2,)
    assert repo.get_intentos_v2(conn, "cli", "P1", "portal_prueba") == 2
    sql, binds = cursor.execute.call_args.args
    assert "SELECT intentos FROM cliente_proceso" in sql
    assert binds == {"cliente_id": "cli", "proceso_gid": "portal_prueba:P1"}
    repo.set_estado_v2(conn, "P1", "cli", "ERROR_LLM", intentos=3)
    sql, binds = cursor.execute.call_args.args
    assert "intentos = :intentos" in sql
    assert binds["intentos"] == 3
    repo.set_estado_v2(conn, "P1", "cli", "ANALIZADO")
    assert "intentos" not in cursor.execute.call_args.args[1]


def test_service_intentos_y_guardado(tmp_path, monkeypatch):
    cfg = SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, data_dir=str(tmp_path))
    conn = Mock()

    @contextmanager
    def conexion(config):
        yield conn

    monkeypatch.setattr(db, "connection", conexion)
    monkeypatch.setattr(service, "_portal", lambda *a: "portal_prueba")
    leer = Mock(return_value=2)
    guardar = Mock()
    monkeypatch.setattr(repo, "get_intentos_v2", leer)
    monkeypatch.setattr(repo, "set_estado", Mock())
    monkeypatch.setattr(repo, "set_estado_v2", guardar)
    assert service.intentos_de(cfg, "cli", "P1") == 2
    leer.assert_called_once_with(conn, "cli", "P1", portal_id="portal_prueba")
    service.guardar_estado(cfg, "cli", ResultadoAnalisis("P1", "ERROR_LLM"), intentos=3)
    assert guardar.call_args.kwargs["intentos"] == 3
    cfg.enable_esquema_v2 = False
    assert service.intentos_de(cfg, "cli", "P1") is None


def test_pipeline_retoma_intentos_adb(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.intel_c0 = "activo"
    path = _make_pdf(tmp_path, "sintetico.pdf", TEXTO_RELEVANTE)
    monkeypatch.setattr(service, "intentos_de", lambda *a: 2)
    guardar = Mock()
    monkeypatch.setattr(service, "guardar_estado", guardar)
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k:
                        ResultadoTriaje("ERROR_LLM", 0.0, "parse_error", _uso()))
    res = pipeline.analizar_pliego_proceso(_proceso(), path, cfg, "ctx", "cli",
                                          INCLUSION, EXCLUSION)
    assert res.estado == "REQUIERE_REVISION_MANUAL"
    assert res.anexos["triaje_intentos"] == 3
    assert guardar.call_args.kwargs["intentos"] == 3


def test_pipeline_caida_adb_conserva_tope_local(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, enable_memoria=True)
    cfg.enable_esquema_v2 = True
    cfg.intel_c0 = "activo"
    cfg.adb_dsn = "dsn-invalido-sintetico"

    def caida(config):
        assert config.adb_dsn == "dsn-invalido-sintetico"
        raise RuntimeError("DSN inválido")

    monkeypatch.setattr(db, "connection", caida)
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k:
                        ResultadoTriaje("ERROR_LLM", 0.0, "parse_error", _uso()))
    path = _make_pdf(tmp_path, "sintetico.pdf", TEXTO_RELEVANTE)
    for intento in range(1, 4):
        res = pipeline.analizar_pliego_proceso(_proceso(), path, cfg, "ctx", "cli",
                                              INCLUSION, EXCLUSION)
        assert res.anexos["triaje_intentos"] == intento
    assert res.estado == "REQUIERE_REVISION_MANUAL"
