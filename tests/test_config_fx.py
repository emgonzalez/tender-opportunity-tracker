"""Configuración del tipo de cambio automático desde el entorno."""

import logging

import pytest

from src.config import Config


@pytest.fixture(autouse=True)
def limpiar_entorno_fx(monkeypatch):
    # `Config()` exige SMTP_USER/SMTP_PASSWORD. En local `python-dotenv` los encuentra en el `.env` del repo
    # (sube por los directorios padre, también desde un worktree); en CI no hay `.env`: se fijan aquí.
    for variable in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(variable, "x")
    for variable in ("FX_AUTO", "FX_API_URL", "FX_ALERTA_DIAS", "FX_REPETIR_ALERTA_DIAS"):
        monkeypatch.delenv(variable, raising=False)


def test_defaults_fx(caplog):
    config = Config()

    assert config.fx_auto is False
    assert config.fx_api_url == "https://api.hacienda.go.cr/indicadores/tc/dolar"
    assert config.fx_alerta_dias == 5
    assert config.fx_repetir_alerta_dias == 3
    assert not caplog.records


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("true", True), ("TRUE", True), (" true ", True), ("false", False), ("otro", False)],
)
def test_fx_auto(monkeypatch, valor, esperado):
    monkeypatch.setenv("FX_AUTO", valor)

    assert Config().fx_auto is esperado


def test_fx_api_url(monkeypatch):
    url = "https://hacienda.example/tipo-de-cambio"
    monkeypatch.setenv("FX_API_URL", url)

    assert Config().fx_api_url == url


@pytest.mark.parametrize(("alerta", "repetir"), [(8, 2), (2, 8)])
def test_umbrales_fx_independientes(monkeypatch, alerta, repetir):
    monkeypatch.setenv("FX_ALERTA_DIAS", str(alerta))
    monkeypatch.setenv("FX_REPETIR_ALERTA_DIAS", str(repetir))

    config = Config()

    assert config.fx_alerta_dias == alerta
    assert config.fx_repetir_alerta_dias == repetir


@pytest.mark.parametrize(
    ("valor", "dias", "repetir", "avisa"),
    [("1", 1, 1, False), (" 8 ", 8, 8, False), ("", 5, 3, False), ("  ", 5, 3, False),
     ("otro", 5, 3, True), ("0", 5, 3, True), ("-2", 5, 3, True)],
)
def test_dias_fx(monkeypatch, caplog, valor, dias, repetir, avisa):
    monkeypatch.setenv("FX_ALERTA_DIAS", valor)
    monkeypatch.setenv("FX_REPETIR_ALERTA_DIAS", valor)

    config = Config()

    assert config.fx_alerta_dias == dias
    assert config.fx_repetir_alerta_dias == repetir
    avisos = [registro for registro in caplog.records if registro.levelno == logging.WARNING]
    assert len(avisos) == (2 if avisa else 0)
    if avisa:
        assert "FX_ALERTA_DIAS" in avisos[0].message
        assert "FX_REPETIR_ALERTA_DIAS" in avisos[1].message
