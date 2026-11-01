"""`src.aprendizaje.service`: la corrida diaria nunca se cae por la memoria de aprendizaje."""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.aprendizaje import repo, service

CFG_ON = SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, adb_dsn="dsn")


def _caida():
    raise ConnectionError("ORA-12541: no listener")


@pytest.mark.parametrize("cfg,esperado", [
    (CFG_ON, True),
    (SimpleNamespace(enable_memoria=True, enable_esquema_v2=False, adb_dsn="dsn"), False),
    (SimpleNamespace(enable_memoria=False, enable_esquema_v2=True, adb_dsn="dsn"), False),
    (SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, adb_dsn=""), False),
])
def test_habilitado(cfg, esperado):
    assert service.habilitado(cfg) is esperado


def test_apagado_no_conecta(monkeypatch):
    from src.memoria import db
    monkeypatch.setattr(db, "connection", MagicMock(side_effect=AssertionError("no debía conectar")))
    apagado = SimpleNamespace(enable_memoria=False)
    assert service.terminos_activos(apagado, "cli") == []
    assert service.calibracion_activa(apagado, "cli") is None
    assert service.buscar_decision(apagado, "h", "triaje") is None
    assert service.guardar_decision(apagado, "cli", "sv:1", "triaje") is None


@pytest.mark.parametrize("llamada,vacio", [
    (lambda c: service.terminos_activos(CFG_ON, "cli", conectar=c), []),
    (lambda c: service.calibracion_activa(CFG_ON, "cli", "oracle", conectar=c), None),
    (lambda c: service.buscar_decision(CFG_ON, "h", "triaje", conectar=c), None),
    (lambda c: service.guardar_decision(CFG_ON, "cli", "sv:1", "triaje", salida={}, conectar=c), None),
    (lambda c: service.listar_ejemplos(CFG_ON, "cli", conectar=c), []),
])
def test_adb_caida_devuelve_vacio_y_avisa(llamada, vacio, caplog):
    assert llamada(_caida) == vacio
    assert "ConnectionError" in caplog.text


def test_error_dentro_de_la_consulta_tambien_degrada(monkeypatch, caplog):
    monkeypatch.setattr(repo, "listar_terminos_activos", MagicMock(side_effect=RuntimeError("ORA-00942")))
    assert service.terminos_activos(CFG_ON, "cli", conectar=lambda: contextlib.nullcontext(MagicMock())) == []
    assert "RuntimeError" in caplog.text


def test_con_adb_devuelve_lo_del_repo(monkeypatch):
    monkeypatch.setattr(repo, "listar_terminos_activos", lambda conn, cid, marca: [{"termino": "Exadata"}])
    assert service.terminos_activos(CFG_ON, "cli", "oracle", conectar=lambda: contextlib.nullcontext(MagicMock())) \
        == [{"termino": "Exadata"}]
