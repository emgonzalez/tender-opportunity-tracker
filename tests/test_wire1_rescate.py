"""Cableado C5: transacción falsa y ninguna publicación de marca implícita."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import main
from src.aprendizaje import service
from src.classifier import rescate
from src.memoria import db


@pytest.mark.parametrize("modo", ["off", "sombra", "activo"])
def test_modos_y_commit(monkeypatch, modo):
    cfg = SimpleNamespace(intel_c5=modo, enable_memoria=True,
                          enable_esquema_v2=True, adb_dsn="dsn-falso")
    profile = SimpleNamespace(client_id="demo")
    proc = SimpleNamespace(es_relevante=False, numero_referencia="REF-1", razon="Dudoso",
                           marcas_recomendadas=[])
    relevante = SimpleNamespace(es_relevante=True)
    eventos = []
    conn = object()

    @contextmanager
    def conectar(_):
        eventos.append("abrir")
        yield conn
        eventos.append("commit")

    candidato = {"proceso": proc, "motivo": "Afinidad sin evidencia de marca"}
    llamada = Mock(side_effect=lambda *args: eventos.append("rescatar") or [candidato])
    guardar = Mock()
    monkeypatch.setattr(db, "connection", conectar)
    monkeypatch.setattr(rescate, "rescatar", llamada)
    monkeypatch.setattr(service, "guardar_decision", guardar)
    salida = main._rescatar_semanticos(cfg, profile, [proc, relevante])
    assert salida == ([candidato] if modo == "activo" else [])
    assert eventos == ([] if modo == "off" else ["abrir", "rescatar", "commit"])
    if modo != "off":
        llamada.assert_called_once_with(conn, cfg, [proc], "demo", 5)
    if modo == "sombra":
        assert guardar.call_args.args[3] == "C5"
        assert guardar.call_args.kwargs["variante"] == "sombra"
        with guardar.call_args.kwargs["conectar"]() as misma:
            assert misma is conn
    else:
        guardar.assert_not_called()
    assert vars(proc) == dict(es_relevante=False, numero_referencia="REF-1", razon="Dudoso",
                             marcas_recomendadas=[])


@pytest.mark.parametrize("fallo_commit", [False, True])
def test_adb_caida_o_commit_fallido_no_publica(monkeypatch, fallo_commit):
    cfg = SimpleNamespace(intel_c5="activo", enable_memoria=True,
                          enable_esquema_v2=True, adb_dsn="no-es-un-dsn")
    proc = SimpleNamespace(es_relevante=False)

    @contextmanager
    def conectar(_):
        if fallo_commit:
            yield object()
        raise RuntimeError("DSN inválido")

    monkeypatch.setattr(db, "connection", conectar)
    monkeypatch.setattr(rescate, "rescatar", lambda *args: [{"proceso": proc}])
    assert main._rescatar_semanticos(cfg, SimpleNamespace(client_id="demo"), [proc]) == []
    assert proc.es_relevante is False


def test_sin_memoria_no_conecta(monkeypatch):
    conectar = Mock(side_effect=AssertionError("No debe conectar"))
    monkeypatch.setattr(db, "connection", conectar)
    cfg = SimpleNamespace(intel_c5="activo", enable_memoria=False)
    assert main._rescatar_semanticos(cfg, SimpleNamespace(client_id="demo"), []) == []
    conectar.assert_not_called()
