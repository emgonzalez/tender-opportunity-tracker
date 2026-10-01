"""Tests de la vista de país del portal: flag, render, CSP y degradación ante ADB caída."""
from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.web import graficos, portal
from src.web.app import crear_app


def _cfg(**c):
    base = dict(enable_portal=True, portal_cliente_id="cli", enable_feedback=False)
    base.update(c)
    return SimpleNamespace(**base)


def _parchear_kpis(monkeypatch, severidad=None):
    @contextmanager
    def conexion(cfg):
        yield object()

    monkeypatch.setattr(portal.db, "connection", conexion)
    monkeypatch.setattr(portal.pais, "oportunidades_del_mes",
                        lambda c, cl: {"valor": 12, "delta_pct": 20.0, "serie": [1, 3, 2, 5]})
    monkeypatch.setattr(portal.pais, "pct_relevancia_clasificador",
                        lambda c, cl, dias: {"valor": 40.0, "total": 10, "relevantes": 4})
    monkeypatch.setattr(portal.pais, "instituciones_top",
                        lambda c, cl, dias: [{"institucion": "Min <Salud>", "conteo": 7}])
    monkeypatch.setattr(portal.pais, "embudo_pipeline", lambda c, cl, dias: {
        "camino": [{"estado": "DESCUBIERTO", "conteo": 10}, {"estado": "NOTIFICADO", "conteo": 4}],
        "salidas": [{"estado": "ERROR_LLM", "conteo": 1}]})
    monkeypatch.setattr(portal.pais, "severidad_riesgos",
                        lambda c, cl, dias: severidad if severidad is not None else [])


def test_apagado_es_404(monkeypatch):
    _parchear_kpis(monkeypatch)
    cliente = TestClient(crear_app(_cfg(enable_portal=False)))
    assert cliente.get("/portal").status_code == 404
    assert cliente.get("/portal/static/charts/bar.js").status_code == 404


def test_render_y_csp(monkeypatch):
    _parchear_kpis(monkeypatch, [{"severidad": "alta", "conteo": 3}])
    r = TestClient(crear_app(_cfg())).get("/portal?dias=30")
    assert r.status_code == 200
    assert "Min &lt;Salud&gt;" in r.text and "Min <Salud>" not in r.text  # autoescape
    assert "+20.0%" in r.text and "40.0%" in r.text and "svg" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert 'aria-current="true">30 días' in r.text


def test_rango_invalido_cae_a_90(monkeypatch):
    _parchear_kpis(monkeypatch)
    r = TestClient(crear_app(_cfg())).get("/portal?dias=7")
    assert 'aria-current="true">90 días' in r.text


def test_adb_caida_da_503_sin_traceback(monkeypatch):
    @contextmanager
    def conexion(cfg):
        raise RuntimeError("adb caída")
        yield

    monkeypatch.setattr(portal.db, "connection", conexion)
    r = TestClient(crear_app(_cfg())).get("/portal")
    assert r.status_code == 503 and "adb" not in r.text.lower()


def test_estaticos_se_sirven_y_el_resto_sigue_sin_scripts():
    c = TestClient(crear_app(_cfg()))
    assert c.get("/portal/static/charts/tooltip.js").status_code == 200
    assert "script-src" not in c.get("/healthz").headers["content-security-policy"]


def test_graficos_vacios_y_escape():
    assert graficos.barras([]) == "" and graficos.embudo([("A", 0)]) == "" and graficos.sparkline([1]) == ""
    assert "&lt;b&gt;" in graficos.barras([("<b>", 1)])
    assert "50%" in graficos.embudo([("A", 10), ("B", 5)])
