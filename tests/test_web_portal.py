"""Tests de la vista de país del portal: flag, render, CSP y degradación ante ADB caída."""
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.web import graficos, portal
from src.web.app import crear_app


def _cfg(**c):
    base = dict(enable_portal=True, portal_cliente_id="cli", enable_feedback=False, daily_budget_usd=5.0)
    base.update(c)
    return SimpleNamespace(**base)


_SERIE_LLM = [
    {"dia": datetime(2026, 10, 21), "llamadas": 5, "p50_ms": 800, "p95_ms": 2100, "tasa_error": 0.0},
    {"dia": datetime(2026, 10, 22), "llamadas": 4, "p50_ms": 900, "p95_ms": 3000, "tasa_error": 25.0},
]


def _parchear_kpis(monkeypatch, severidad=None, corrida=None, serie=None, conflictos=None):
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
    monkeypatch.setattr(portal.mercado_repo, "listar_conflictos_oportunidad_v2",
                        lambda c, cl: conflictos or [])
    monkeypatch.setattr(portal.operativo, "estado_ultima_corrida", lambda c: corrida or {
        "estado": "OK", "hace_minutos": 125, "procesos_revisados": 42,
        "version": "abc123def4567", "error_msg": None})
    monkeypatch.setattr(portal.operativo, "gasto_llm_hoy", lambda c, tope: {
        "gasto_usd": 1.5, "tope_usd": tope, "pct": 30.0, "llamadas": 9})
    monkeypatch.setattr(portal.operativo, "latencia_y_errores_llm",
                        lambda c, dias: _SERIE_LLM if serie is None else serie)


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
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
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


def test_conflictos_se_muestran_y_sin_conflictos_no_hay_tarjeta(monkeypatch):
    from datetime import datetime
    _parchear_kpis(monkeypatch)
    assert "Conflictos con COMPRASAL" not in TestClient(crear_app(_cfg())).get("/portal").text
    _parchear_kpis(monkeypatch, conflictos=[{
        "proceso_gid": "comprasal_sv:96-2026-P0011<x>", "estado_manual": "DESCARTADO",
        "resultado_mercado": "GANADO", "responsable_usuario_id": None,
        "actualizado_en": datetime(2026, 10, 20)}])
    r = TestClient(crear_app(_cfg())).get("/portal")
    assert r.status_code == 200 and "Conflictos con COMPRASAL" in r.text
    assert "96-2026-P0011&lt;x&gt;" in r.text and "capturado como descartado" in r.text
    assert "COMPRASAL publica ganado" in r.text and "20/10/2026" in r.text


def test_falla_de_conflictos_no_tumba_el_portal(monkeypatch):
    _parchear_kpis(monkeypatch)

    def boom(c, cl):
        raise RuntimeError("tabla ausente")

    monkeypatch.setattr(portal.mercado_repo, "listar_conflictos_oportunidad_v2", boom)
    r = TestClient(crear_app(_cfg())).get("/portal")
    assert r.status_code == 200 and "Conflictos con COMPRASAL" not in r.text


def test_operativo_apagado_es_404(monkeypatch):
    _parchear_kpis(monkeypatch)
    assert TestClient(crear_app(_cfg(enable_portal=False))).get("/portal/operativo").status_code == 404


def test_operativo_render(monkeypatch):
    _parchear_kpis(monkeypatch)
    r = TestClient(crear_app(_cfg())).get("/portal/operativo")
    assert r.status_code == 200
    assert 'badge ok">OK' in r.text and "hace 2 h" in r.text and "42 procesos" in r.text
    assert "$1.50" in r.text and "$5.00" in r.text and "medidor" in r.text
    assert "p95 3.000 ms" in r.text and "25.0% de error" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_operativo_error_y_estados_vacios(monkeypatch):
    _parchear_kpis(monkeypatch, serie=[], corrida={
        "estado": "ERROR", "hace_minutos": 5, "procesos_revisados": 0,
        "version": None, "error_msg": "<boom>"})
    monkeypatch.setattr(portal.operativo, "gasto_llm_hoy", lambda c, tope: {
        "gasto_usd": 0.0, "tope_usd": tope, "pct": 0.0, "llamadas": 0})
    r = TestClient(crear_app(_cfg())).get("/portal/operativo")
    assert 'badge error">ERROR' in r.text and "&lt;boom&gt;" in r.text and "hace 5 min" in r.text
    assert "Sin llamadas al LLM hoy" in r.text and "Sin datos suficientes" in r.text


def test_operativo_sin_corridas(monkeypatch):
    _parchear_kpis(monkeypatch, corrida={"estado": None})
    r = TestClient(crear_app(_cfg())).get("/portal/operativo")
    assert "Sin corridas registradas" in r.text


def test_operativo_adb_caida_da_503_sin_traceback(monkeypatch):
    @contextmanager
    def conexion(cfg):
        raise RuntimeError("adb caída")
        yield

    monkeypatch.setattr(portal.db, "connection", conexion)
    r = TestClient(crear_app(_cfg())).get("/portal/operativo")
    assert r.status_code == 503 and "adb" not in r.text.lower() and "Traceback" not in r.text


def test_graficos_operativos_vacios_y_escape():
    assert graficos.medidor(1, 0) == "" and graficos.linea([]) == ""
    assert graficos.linea([("a", 1.0, "t"), ("b", None, "")]) == ""
    assert "&lt;b&gt;" in graficos.linea([("<b>", 1.0, "<b>"), ("c", 2.0, "x")])
    assert "var(--critico)" in graficos.medidor(6, 5) and "var(--bueno)" in graficos.medidor(1, 5)
    assert "var(--aviso)" in graficos.medidor(4, 5)
