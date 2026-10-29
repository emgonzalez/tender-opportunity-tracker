"""Fase U, paso A: las URLs en inglés y las españolas responden igual; lo que la app emite sigue en español."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.web import rutas
from src.web.app import crear_app
from tests.test_web_acceso import (  # noqa: F401 — fixture
    BASE,
    ORIGEN,
    SECRETO,
    _cfg,
    _cliente,
    _csrf,
    mundo,
)

# ── traducción pura ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("nueva,vigente", [
    ("/login", "/acceso"), ("/login/code", "/acceso/codigo"), ("/login/mfa", "/acceso/mfa"),
    ("/logout", "/salir"), ("/portal/operations", "/portal/operativo"), ("/portal/theme", "/portal/tema"),
    ("/portal/view-as", "/portal/ver-como"),
    ("/portal/opportunity/abc-1/status", "/portal/oportunidad/abc-1/estado"),
    ("/portal/opportunity/abc-1/owner", "/portal/oportunidad/abc-1/responsable"),
    ("/portal/opportunity/abc-1/recover", "/portal/oportunidad/abc-1/recuperar"),
    ("/f/TOKEN.firma/relevant", "/f/TOKEN.firma/relevante"),
    ("/f/TOKEN.firma/irrelevant", "/f/TOKEN.firma/irrelevante"),
])
def test_ruta_nueva_traduce_a_la_vigente(nueva, vigente):
    assert rutas.traducir_ruta(nueva) == vigente


@pytest.mark.parametrize("ruta", [
    "/acceso", "/salir", "/portal", "/portal/regional", "/portal/operativo", "/healthz", "/f/x/relevante",
    "/portal/static/charts/bar.js", "/portal/oportunidad/a/estado", "/otra/cosa", "/login/otra", "/logout/x",
    "/portal/opportunity/a/otra", "/f/x/relevantes",
])
def test_las_demas_rutas_no_se_tocan(ruta):
    assert rutas.traducir_ruta(ruta) == ruta


def test_query_nueva_traduce_y_la_vieja_sigue():
    assert rutas.traducir_query("client=sv&days=30&day=2026-10-28&brand=Oracle&notice=ok&next=%2Fportal") == \
        "cliente=sv&dias=30&dia=2026-10-28&marca=Oracle&aviso=ok&siguiente=%2Fportal"
    assert rutas.traducir_query("cliente=sv&dias=30") == "cliente=sv&dias=30"
    assert rutas.traducir_query("") == ""


def test_query_si_vienen_ambas_manda_la_nueva():
    assert rutas.traducir_query("dias=7&days=30") == "dias=30"
    assert rutas.traducir_query("days=30&dias=7") == "dias=30"


def test_owner_none_es_responsable_sin():
    assert rutas.traducir_query("owner=none") == "responsable=sin"
    assert rutas.traducir_query("owner=ana") == "responsable=ana"


def test_query_conserva_lo_que_no_conoce_y_los_vacios():
    assert rutas.traducir_query("x=1&days=&y=%20") == "x=1&dias=&y=+"


# ── de punta a punta ──────────────────────────────────────────────────────────

def test_login_y_acceso_sirven_el_mismo_formulario(mundo):  # noqa: F811
    c = _cliente()
    nuevo, viejo = c.get("/login"), c.get("/acceso")
    assert nuevo.status_code == viejo.status_code == 200
    assert 'name="email"' in nuevo.text and 'name="email"' in viejo.text


def test_post_login_y_post_acceso_emiten_el_mismo_codigo(mundo):  # noqa: F811
    c = _cliente()
    for ruta in ("/login", "/acceso"):
        r = c.post(ruta, data={"email": "ana@test.com", "csrf": _csrf()}, headers=ORIGEN)
        assert r.status_code == 200, ruta
    assert mundo.emitidos == ["ana@test.com", "ana@test.com"]


def test_login_code_canjea_igual_que_acceso_codigo(mundo):  # noqa: F811
    c = _cliente()
    for ruta in ("/login/code", "/acceso/codigo"):
        r = c.post(ruta, data={"email": "ana@test.com", "codigo": "K7M2QX9P", "csrf": _csrf(), "siguiente": ""},
                   headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"] == "/portal", ruta
        assert "__Host-amalia_sesion" in r.headers.get("set-cookie", ""), ruta


def test_logout_responde_igual_que_salir(mundo):  # noqa: F811
    estados = {}
    for ruta in ("/logout", "/salir"):
        c = _cliente()
        c.cookies.set("__Host-amalia_sesion", "token-de-sesion")
        r = c.post(ruta, data={"csrf": _csrf("token-de-sesion")}, headers=ORIGEN)
        estados[ruta] = (r.status_code, r.headers.get("location"))
    assert estados["/logout"] == estados["/salir"], estados


def test_lo_que_la_app_emite_sigue_en_espanol(mundo):  # noqa: F811
    """Paso A: no se emite ningún enlace nuevo; un rollback a la imagen anterior no deja enlaces rotos."""
    r = _cliente().get("/login")
    assert 'action="/acceso"' in r.text and "/login" not in r.text


def test_ruta_inexistente_sigue_siendo_404(mundo):  # noqa: F811
    assert _cliente().get("/login/otra").status_code == 404
    assert _cliente().get("/logout").status_code in (404, 405)


def test_sin_portal_las_rutas_nuevas_dan_404_como_las_viejas():
    c = TestClient(crear_app(_cfg(enable_portal=False)), base_url=BASE, follow_redirects=False)
    assert c.get("/login").status_code == c.get("/acceso").status_code == 404


def test_healthz_no_cambia():
    assert TestClient(crear_app(_cfg())).get("/healthz").text == "ok"


# ── votos de correo: verbos en inglés y en español, mismo registro ────────────

from tests.test_web_feedback import PROCESO, _token, perfil, servicio  # noqa: E402,F401,F811
from tests.test_web_feedback import _cfg as _cfg_feedback  # noqa: E402


@pytest.fixture
def cliente_feedback(servicio, perfil):  # noqa: F811
    return TestClient(crear_app(_cfg_feedback()))


@pytest.mark.parametrize("verbo,esperado", [
    ("relevant", "RELEVANTE"), ("relevante", "RELEVANTE"),
])
def test_voto_relevante_con_verbo_ingles_o_espanol(cliente_feedback, servicio, verbo, esperado):  # noqa: F811
    r = cliente_feedback.post(f"/f/{_token()}/{verbo}")
    assert r.status_code == 200
    assert [v["voto"] for v in servicio.votos] == [esperado] and servicio.votos[0]["proceso_id"] == PROCESO


@pytest.mark.parametrize("verbo", ["irrelevant", "irrelevante"])
def test_voto_irrelevante_con_verbo_ingles_o_espanol(cliente_feedback, servicio, verbo):  # noqa: F811
    assert cliente_feedback.get(f"/f/{_token()}/{verbo}").status_code == 200
    r = cliente_feedback.post(f"/f/{_token()}/{verbo}", data={"motivos": [], "comentario": "x"})
    assert r.status_code == 200 and [v["voto"] for v in servicio.votos] == ["IRRELEVANTE"]
