"""Los consumos C2 respetan el subtope diario sin duplicar el gasto total."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from src.config import Config
from src.drilldown import quota
from src.scraper.models import UsoModelo


@pytest.mark.parametrize("sombra", [False, True])
def test_subtope_configurable_y_default(tmp_path, monkeypatch, sombra):
    monkeypatch.setenv("COMPRASAL_EMAIL", "test@example.com")
    monkeypatch.setenv("COMPRASAL_PASSWORD", "test")
    monkeypatch.delenv("SUBTOPE_LECTURA_USD", raising=False)
    cfg = Config(data_dir=str(tmp_path), daily_budget_usd=20)
    assert cfg.lectura_subtope_usd == .30
    assert not quota.cabe_lectura(cfg, 1, sombra=sombra)

    monkeypatch.setenv("SUBTOPE_LECTURA_USD", "10")
    cfg = Config(data_dir=str(tmp_path), daily_budget_usd=20)
    assert cfg.lectura_subtope_usd == 10
    assert quota.cabe_lectura(cfg, 1, sombra=sombra)
    assert not quota.cabe_lectura(cfg, 10.01, sombra=sombra)
    cfg.daily_budget_usd = .50
    assert not quota.cabe_lectura(cfg, 1, sombra=sombra)


@pytest.mark.parametrize("sombra", [False, True])
def test_lectura_cobra_total_y_subtope_una_sola_vez(tmp_path, sombra):
    cfg = SimpleNamespace(budget_path=str(tmp_path / "gasto.json"), daily_budget_usd=5)
    uso = UsoModelo(modelo="claude-haiku-4-5", input_tokens=290_000, llamadas=1)
    assert quota.cabe_lectura(cfg, .29, sombra=sombra)
    assert quota.cobrar_lectura(cfg, uso, sombra=sombra) == pytest.approx(.29)
    libro = json.loads((tmp_path / "gasto.json").read_text())
    assert libro["gasto_usd"] == pytest.approx(.29)
    assert libro["subtopes"]["lectura"] == pytest.approx(.29)
    assert ("sombra" in libro["subtopes"]) == sombra
    assert not quota.cabe_lectura(cfg, .02, sombra=sombra)
    assert quota.cabe(cfg, .02)


def test_sombra_aplica_su_propio_subtope_y_lectura_respeta_global(tmp_path):
    ruta = tmp_path / "gasto.json"
    cfg = SimpleNamespace(budget_path=str(ruta), daily_budget_usd=5)
    ruta.write_text(json.dumps({"fecha": date.today().isoformat(), "gasto_usd": .29,
                               "subtopes": {"lectura": 0, "sombra": .29}}))
    assert not quota.cabe_lectura(cfg, .02, sombra=True)
    assert quota.cabe_lectura(cfg, .02)
    cfg.daily_budget_usd = .30
    assert not quota.cabe_lectura(cfg, .02)
