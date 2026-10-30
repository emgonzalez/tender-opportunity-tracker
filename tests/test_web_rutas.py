"""Fase U, paso B: la app registra y emite las URLs en inglés; las españolas son alias (308 en GET, mismo handler en POST)."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from src.web import csrf, rutas, sesion
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

# ── tabla de compatibilidad ───────────────────────────────────────────────────

_GID = "comprasal_sv%3A96-2026-P0011"
VIEJAS = [
    ("/acceso", "/login"), ("/acceso/codigo", "/login/code"), ("/acceso/mfa", "/login/mfa"),
    ("/salir", "/logout"), ("/portal/operativo", "/portal/operations"), ("/portal/tema", "/portal/theme"),
    ("/portal/ver-como", "/portal/view-as"),
    (f"/portal/oportunidad/{_GID}/estado", f"/portal/opportunity/{_GID}/status"),
    (f"/portal/oportunidad/{_GID}/responsable", f"/portal/opportunity/{_GID}/owner"),
    (f"/portal/oportunidad/{_GID}/recuperar", f"/portal/opportunity/{_GID}/recover"),
]
# vieja → nueva de los GET que redirigen (los /f/ viejos se sirven directo: ver más abajo)
_QUERY_VIEJA = "cliente=sv&dias=30&dia=2026-10-28&responsable=sin&marca=Oracle&aviso=ok&siguiente=%2Fportal"
_QUERY_NUEVA = "client=sv&days=30&day=2026-10-28&owner=none&brand=Oracle&notice=ok&next=%2Fportal"


def test_la_tabla_cubre_todo_el_mapa_legado():
    assert set(rutas.LEGADO) <= {v for v, _ in VIEJAS}
    assert rutas.QUERY_LEGADO == {"cliente": "client", "dias": "days", "dia": "day", "responsable": "owner",
                                  "marca": "brand", "aviso": "notice", "siguiente": "next"}


@pytest.mark.parametrize("vieja,nueva", VIEJAS)
def test_get_viejo_redirige_308_a_la_nueva_traduciendo_la_query(mundo, vieja, nueva):  # noqa: F811
    r = _cliente().get(f"{vieja}?{_QUERY_VIEJA}")
    assert r.status_code == 308, vieja
    assert unquote(r.headers["location"]) == unquote(f"{nueva}?{_QUERY_NUEVA}")
    sin_query = _cliente().get(vieja)
    assert sin_query.status_code == 308 and unquote(sin_query.headers["location"]) == unquote(nueva)


def test_get_con_query_vieja_en_una_ruta_nueva_tambien_redirige(mundo):  # noqa: F811
    r = _cliente().get("/portal?cliente=sv&dias=7")
    assert r.status_code == 308 and r.headers["location"] == "/portal?client=sv&days=7"


def test_query_si_vienen_ambas_manda_la_nueva():
    assert rutas.traducir_query("dias=7&days=30") == "days=30"
    assert rutas.traducir_query("days=30&dias=7") == "days=30"
    assert rutas.traducir_query("responsable=sin") == "owner=none"
    assert rutas.traducir_query("responsable=ana") == "owner=ana"
    assert rutas.traducir_query("x=1&dias=&y=%20") == "x=1&days=&y=+"
    assert rutas.traducir_query("") == ""


@pytest.mark.parametrize("ruta", [
    "/login", "/login/code", "/login/mfa", "/logout", "/portal", "/portal/regional", "/portal/operations",
    "/portal/static/charts/bar.js", "/healthz", "/otra/cosa", "/login/otra", "/f/x/relevant", "/f/x/relevantes",
])
def test_las_rutas_nuevas_y_ajenas_no_son_alias(ruta):
    assert rutas.nueva_de_legada(ruta) is None


def test_solo_los_votos_son_permanentes():
    assert rutas.es_permanente("/f/TOK/relevante") and rutas.es_permanente("/f/TOK/irrelevante")
    assert not any(rutas.es_permanente(v) for v, _ in VIEJAS)
    assert rutas.VENTANA_ALIAS_DIAS == 60


def test_post_viejo_sirve_el_mismo_handler_que_el_nuevo_sin_redirigir(mundo):  # noqa: F811
    """Un 3xx perdería el cuerpo del formulario: el POST viejo corre el handler nuevo."""
    c = _cliente()
    for ruta in ("/login", "/acceso"):
        r = c.post(ruta, data={"email": "ana@test.com", "csrf": _csrf()}, headers=ORIGEN)
        assert r.status_code == 200, ruta
    assert mundo.emitidos == ["ana@test.com", "ana@test.com"]


def test_canje_de_codigo_igual_en_la_ruta_vieja_y_la_nueva(mundo):  # noqa: F811
    for ruta in ("/login/code", "/acceso/codigo"):
        r = _cliente().post(ruta, data={"email": "ana@test.com", "codigo": "K7M2QX9P", "csrf": _csrf(), "siguiente": ""},
                            headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"] == "/portal", ruta
        assert "__Host-amalia_sesion" in r.headers.get("set-cookie", ""), ruta


def test_post_viejo_conserva_la_query_traducida(mundo):  # noqa: F811
    """`?siguiente=` en un POST viejo llega como `?next=` al handler (aquí: el GET del mfa de otra ruta no aplica)."""
    r = _cliente().post("/salir?siguiente=%2Fportal", data={"csrf": _csrf()}, headers=ORIGEN)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("ruta", ["/logout", "/salir"])
def test_logout_cierra_la_sesion_con_la_cookie_en_ambas_rutas(mundo, ruta):  # noqa: F811
    from src.identidad import sesiones
    mundo.alcance = SimpleNamespace(usuario_id="u")
    c = _cliente()
    r = c.post(ruta, data={"csrf": csrf.emitir(SECRETO, sesiones.hash_token("token-de-sesion"))}, headers=ORIGEN,
               cookies={sesion.COOKIE: "token-de-sesion"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert mundo.cerradas == ["token-de-sesion"]


def test_la_cookie_se_resuelve_en_login_y_logout(mundo):  # noqa: F811
    assert sesion._PREFIJOS == ("/portal", "/login", "/logout")
    mundo.alcance = SimpleNamespace(usuario_id="u")
    r = _cliente().get("/login", cookies={sesion.COOKIE: "token-de-sesion"})
    assert r.status_code == 303 and r.headers["location"] == "/portal"  # con sesión, /login manda al portal


def test_lo_que_la_app_emite_es_ingles(mundo):  # noqa: F811
    html = _cliente().get("/login").text
    assert 'action="/login"' in html and "/acceso" not in html


def test_redirect_a_login_lleva_next(mundo):  # noqa: F811
    r = _cliente().get("/portal/operations?x=1")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_ruta_inexistente_sigue_siendo_404(mundo):  # noqa: F811
    assert _cliente().get("/login/otra").status_code == 404
    assert _cliente().get("/logout").status_code in (404, 405)


def test_sin_portal_las_rutas_nuevas_dan_404():
    c = TestClient(crear_app(_cfg(enable_portal=False)), base_url=BASE, follow_redirects=False)
    assert c.get("/login").status_code == 404


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
    r = cliente_feedback.post(f"/f/{_token()}/{verbo}")  # el español sigue vivo: está en correos ya enviados
    assert r.status_code == 200
    assert [v["voto"] for v in servicio.votos] == [esperado] and servicio.votos[0]["proceso_id"] == PROCESO


@pytest.mark.parametrize("verbo", ["irrelevant", "irrelevante"])
def test_voto_irrelevante_con_verbo_ingles_o_espanol(cliente_feedback, servicio, verbo):  # noqa: F811
    assert cliente_feedback.get(f"/f/{_token()}/{verbo}").status_code == 200
    r = cliente_feedback.post(f"/f/{_token()}/{verbo}", data={"motivos": [], "comentario": "x"})
    assert r.status_code == 200 and [v["voto"] for v in servicio.votos] == ["IRRELEVANTE"]


def test_token_viejo_y_nuevo_escriben_el_mismo_registro(cliente_feedback, servicio):  # noqa: F811
    """Mismo token, enlace de correo viejo (`relevante`) y nuevo (`relevant`): el mismo voto, sin redirigir."""
    t = _token()
    for verbo in ("relevante", "relevant"):
        r = cliente_feedback.post(f"/f/{t}/{verbo}")
        assert r.status_code == 200, verbo
    assert len(servicio.votos) == 2 and servicio.votos[0] == servicio.votos[1]


def test_get_de_voto_viejo_se_sirve_directo_y_permanente(cliente_feedback):  # noqa: F811
    for verbo in ("relevante", "irrelevante"):
        r = cliente_feedback.get(f"/f/{_token()}/{verbo}", follow_redirects=False)
        assert r.status_code == 200, verbo


def test_los_correos_nuevos_emiten_solo_ingles():
    assert rutas.ruta_voto("relevante") == "relevant" and rutas.ruta_voto("irrelevante") == "irrelevant"
    from src.feedback import tokens as t
    assert t.url("https://f.example.com/", "TOK", "irrelevante") == "https://f.example.com/f/TOK/irrelevant"
