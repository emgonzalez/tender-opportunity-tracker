"""Observabilidad: unión sin duplicación, ventanas y degradación de CLI."""
import pytest

from scripts import metricas_capas as m


def test_deriva():
    assert m.deriva(["Oracle licencias"], ["Oracle licencias"]) == pytest.approx(0)
    assert m.deriva(["sillas"], ["licencias"]) == 1
    assert m.deriva([], ["sillas"]) is None


def test_consumo_no_multiplica_y_variantes():
    ctx = dict(cliente_id="sintetico", proceso_gid="sintetico:1", capa="C2", variante="activa")
    llamada = ("triage", 0.01, 100, 1, None, {"contexto": ctx, "prompt_hash": "hash"})
    activa = ("sintetico", "sintetico:1", "C2", "activa", {"veredicto": "confirmada"}, "hash")
    sombra = ("sintetico", "sintetico:1", "C2", "sombra", {"veredicto": "por_confirmar"}, "hash")
    resultado = m.resumir([llamada], [activa, activa, sombra])
    assert resultado["consumo"][0]["costo_usd"] == 0.01
    assert resultado["consumo"][0]["enlazadas"] == 1
    assert resultado["tasa_cambio"] == 1
    assert resultado["porcentaje_incierto"] == 50
    r = m.resumir([("triage", 0, 1, 1, "parse_error", {})], [])
    assert r["consumo"][0]["tasa_error"] == 1
    assert r["tasa_cambio"] is None


def test_cli_sin_red(monkeypatch, capsys):
    def falla():
        raise RuntimeError("sensible")
    monkeypatch.setattr(m, "configuracion", falla)
    assert m.main(["--dry-run"]) == 0
    assert m.main([]) == 2
    assert "sensible" not in capsys.readouterr().err
