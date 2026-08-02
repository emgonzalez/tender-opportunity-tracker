"""Tests de la guardia de cuota de tokens del drill-down (Fase 2, MVP Track A)."""
import json
import types

from src.drilldown.quota import check_and_reserve, _load_budget, _save_budget


class _FakeCount:
    def __init__(self, n):
        self.input_tokens = n


class _FakeMessages:
    def __init__(self, n):
        self._n = n

    def count_tokens(self, model, messages):
        return _FakeCount(self._n)


class _FakeClient:
    def __init__(self, n):
        self.messages = _FakeMessages(n)


def _cfg(tmp_path, budget):
    return types.SimpleNamespace(
        daily_token_budget=budget,
        token_budget_path=str(tmp_path / "tb.json"),
    )


def test_reserva_hasta_agotar_presupuesto(tmp_path):
    cfg = _cfg(tmp_path, 1000)
    client = _FakeClient(600)
    msgs = [{"role": "user", "content": "x"}]

    assert check_and_reserve(client, "m", msgs, cfg) is True   # 0 + 600 <= 1000
    assert check_and_reserve(client, "m", msgs, cfg) is False  # 600 + 600 > 1000

    data = json.loads((tmp_path / "tb.json").read_text())
    assert data["consumido"] == 600


def test_load_budget_resetea_por_fecha_vieja(tmp_path):
    path = str(tmp_path / "tb.json")
    _save_budget(path, {"fecha": "2000-01-01", "consumido": 999})
    data = _load_budget(path)
    assert data["consumido"] == 0
    assert data["fecha"] != "2000-01-01"


def test_save_y_load_roundtrip(tmp_path):
    path = str(tmp_path / "tb.json")
    from datetime import date
    hoy = date.today().isoformat()
    _save_budget(path, {"fecha": hoy, "consumido": 123})
    data = _load_budget(path)
    assert data["consumido"] == 123 and data["fecha"] == hoy
