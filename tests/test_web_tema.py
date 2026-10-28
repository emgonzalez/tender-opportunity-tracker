"""E12 · selector de tema Auto · Claro · Oscuro: cookie, `POST /portal/tema` y contexto de `pagina()`.

La ruta guarda una PREFERENCIA (no datos de negocio): exige sesión, CSRF y Origin como las demás, pero
es la única escritura del portal que funciona bajo «ver como» (ver el guardián de `test_web_ver_como.py`).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader, Environment
from starlette.requests import Request
from test_web_ver_como import (
    ADMIN,
    BASE,
    GERENTE_SV,
    ORIGEN,
    SECRETO,
    _alc,
    _cfg,
    _cli,
    _csrf,
    _simulado,
)
from test_web_ver_como import mundo as _mundo_ver_como

from src.web import csrf, plantillas, sesion
from src.web.app import crear_app
from src.web.limitador import Limitador
from src.web.plantillas import COOKIE_TEMA, TEMAS, pagina, ruta_actual, tema_de, volver_seguro

RUTA = "/portal/tema"


@pytest.fixture
def mundo(monkeypatch):
    """El mismo mundo simulado que `test_web_ver_como.py` (base, auditoría, «ver como»)."""
    return _mundo_ver_como.__wrapped__(monkeypatch)


def _request(cookie: str | None = None, path: str = "/portal", query: str = "", plantillas_env=None) -> Request:
    headers = [(b"cookie", cookie.encode("latin-1"))] if cookie is not None else []
    app = SimpleNamespace(state=SimpleNamespace(plantillas=plantillas_env))
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode("latin-1"),
                    "headers": headers, "scheme": "https", "server": ("dev.amalia.test", 443), "app": app})


# ── tema_de ────────────────────────────────────────────────────────────────────

class TestTemaDe:
    def test_constantes(self):
        assert COOKIE_TEMA == "__Host-amalia_tema" and TEMAS == ("auto", "claro", "oscuro")

    def test_sin_cookie_es_auto(self):
        assert tema_de(_request()) == "auto"
        assert tema_de(_request("otra=1")) == "auto"

    @pytest.mark.parametrize("tema", TEMAS)
    def test_valores_validos(self, tema):
        assert tema_de(_request(f"{COOKIE_TEMA}={tema}")) == tema

    @pytest.mark.parametrize("valor", ["", "Oscuro", "OSCURO", "dark", "light", "oscuro_", "claro,x", "x" * 5000,
                                       "oscuro\x01", "%6Fscuro", '"oscuro"x', "<script>"])
    def test_basura_es_auto(self, valor):
        assert tema_de(_request(f"{COOKIE_TEMA}={valor}")) == "auto"

    def test_cookie_mal_formada_es_auto(self):
        assert tema_de(_request('a="sin cerrar; ' + COOKIE_TEMA + "=oscuro\x00")) in TEMAS


# ── volver_seguro y ruta_actual ────────────────────────────────────────────────

class TestVolverSeguro:
    @pytest.mark.parametrize("valor", ["/portal", "/portal/regional?dias=30", "/portal/operativo",
                                       "/portal?cliente=sv&dias=90&marca=Cisco+Meraki",
                                       "/portal?cliente=sv&dia=2026-10-27"])
    def test_validos_se_respetan(self, valor):
        assert volver_seguro(valor) == valor

    @pytest.mark.parametrize("valor", [
        "", None, 123, "https://evil.test/", "//evil.test", "/\\evil", "/portal//evil", "/portal\\evil",
        "javascript:alert(1)", "/portal\n", "/portal\r\nSet-Cookie: x=1", "/portal\t", "/portal%0d%0aSet-Cookie",
        "/portal/%2F%2Fevil.test", "/portal/%5Cevil", "/portal?x=%00", "/portal@evil.test", "/portal:80",
        "/portalevil", "/portal-x", "/acceso", "portal", " /portal", "/portal " , "/portal/" + "a" * 400,
        "/portal\x7f",
    ])
    def test_maliciosos_o_raros_van_a_portal(self, valor):
        assert volver_seguro(valor) == "/portal"

    def test_ruta_actual_del_portal_con_query_sin_aviso(self):
        r = _request(path="/portal", query="cliente=sv&dias=30&aviso=ok")
        assert ruta_actual(r) == "/portal?cliente=sv&dias=30"
        assert ruta_actual(_request(path="/portal/regional", query="dias=180")) == "/portal/regional?dias=180"
        assert ruta_actual(_request(path="/portal/operativo")) == "/portal/operativo"

    def test_ruta_actual_fuera_del_portal_o_rara_es_portal(self):
        assert ruta_actual(_request(path="/acceso", query="siguiente=/portal")) == "/portal"
        assert ruta_actual(_request(path="/f/abc/relevante")) == "/portal"
        assert ruta_actual(_request(path="/portal", query="x=" + "a" * 400)) == "/portal"

    def test_ruta_actual_codifica_lo_que_venga_en_la_query(self):
        r = _request(path="/portal", query="cliente=a%40b%3A%2F%2Fevil")
        assert ruta_actual(r) == "/portal?cliente=a%40b%3A%2F%2Fevil"
        assert ruta_actual(_request(path="/portal", query="marca=%0d%0a")) == "/portal"


# ── pagina(): contexto ─────────────────────────────────────────────────────────

class TestPaginaContexto:
    ENV = Environment(loader=DictLoader({
        "t.html": "{{ tema }}|{{ ruta_actual }}|{{ otra }}",
        "feedback_like.html": "<p>{{ mensaje }}</p>",
    }), autoescape=True)

    def test_expone_tema_y_ruta_actual(self):
        r = _request(f"{COOKIE_TEMA}=oscuro", "/portal/regional", "dias=30&aviso=ok", self.ENV)
        assert pagina(r, "t.html", otra="x").body.decode() == "oscuro|/portal/regional?dias=30|x"

    def test_sin_cookie_es_auto_y_fuera_del_portal_ruta_portal(self):
        r = _request(None, "/acceso", "", self.ENV)
        assert pagina(r, "t.html").body.decode() == "auto|/portal|"

    def test_quien_llama_tiene_prioridad(self):
        r = _request(f"{COOKIE_TEMA}=oscuro", "/portal", "", self.ENV)
        assert pagina(r, "t.html", tema="claro", ruta_actual="/portal/x").body.decode() == "claro|/portal/x|"

    def test_plantillas_que_no_lo_usan_no_cambian(self):
        r = _request(f"{COOKIE_TEMA}=claro", "/f/tok/relevante", "", self.ENV)
        resp = pagina(r, "feedback_like.html", status=404, mensaje="<b>")
        assert resp.status_code == 404 and resp.body.decode() == "<p>&lt;b&gt;</p>"

    def test_las_plantillas_reales_de_login_y_feedback_siguen_rindiendo(self):
        env = plantillas.crear_entorno()
        r = _request(f"{COOKIE_TEMA}=oscuro", "/acceso", "", env)
        assert pagina(r, "feedback_error.html", status=404, titulo="T", mensaje="M").status_code == 404
        assert pagina(r, "acceso_correo.html", csrf="c", siguiente="/portal").status_code == 200


# ── POST /portal/tema ──────────────────────────────────────────────────────────

def _post(c, tema="oscuro", csrf_token=None, volver="/portal", headers=ORIGEN):
    datos = {"tema": tema, "csrf": csrf_token if csrf_token is not None else _csrf(), "volver": volver}
    return c.post(RUTA, data=datos, headers=headers)


def _cookie_tema(r) -> str | None:
    return next((v for v in r.headers.get_list("set-cookie") if v.startswith(COOKIE_TEMA + "=")), None)


class TestPostTema:
    @pytest.mark.parametrize("tema", TEMAS)
    def test_feliz_fija_la_cookie_con_sus_atributos(self, mundo, monkeypatch, tema):
        antes = mundo.conexiones
        r = _post(_cli(monkeypatch, GERENTE_SV), tema)
        assert r.status_code == 303 and r.headers["location"] == "/portal"
        cookie = _cookie_tema(r)
        assert cookie is not None and cookie.startswith(f"{COOKIE_TEMA}={tema};")
        atributos = {a.strip().lower() for a in cookie.split(";")[1:]}
        assert {"secure", "httponly", "samesite=lax", "path=/", "max-age=31536000"} <= atributos
        assert not any(a.startswith("domain") for a in atributos)
        # Es una preferencia: ni base ni auditoría.
        assert mundo.conexiones == antes and mundo.auditados == []

    @pytest.mark.parametrize("tema", ["", "dark", "Oscuro", "oscuro;Domain=evil.test", "x" * 5000, "auto\r\nX: y"])
    def test_tema_invalido_redirige_sin_cookie(self, mundo, monkeypatch, tema):
        r = _post(_cli(monkeypatch, GERENTE_SV), tema, volver="/portal/regional")
        assert r.status_code == 303 and r.headers["location"] == "/portal/regional"
        assert _cookie_tema(r) is None

    def test_volver_valido_se_respeta(self, mundo, monkeypatch):
        r = _post(_cli(monkeypatch, GERENTE_SV), "claro", volver="/portal/regional?dias=30")
        assert r.status_code == 303 and r.headers["location"] == "/portal/regional?dias=30"

    @pytest.mark.parametrize("volver", ["https://evil.test/", "//evil.test", "/\\evil", "/portal//evil",
                                        "javascript:alert(1)", "/portal\n", "/portal%0d%0aSet-Cookie", "",
                                        "/portal/" + "a" * 5000, "/acceso", "/portal@evil.test"])
    def test_volver_malicioso_va_a_portal(self, mundo, monkeypatch, volver):
        r = _post(_cli(monkeypatch, GERENTE_SV), "claro", volver=volver)
        assert r.status_code == 303 and r.headers["location"] == "/portal"
        assert _cookie_tema(r) is not None

    @pytest.mark.parametrize("caso", ["sin_csrf", "csrf_malo", "csrf_ajeno", "sin_origen", "otro_origen"])
    def test_csrf_u_origen_invalidos_es_403_sin_cookie(self, mundo, monkeypatch, caso):
        c = _cli(monkeypatch, GERENTE_SV)
        if caso == "sin_csrf":
            r = c.post(RUTA, data={"tema": "oscuro", "volver": "/portal"}, headers=ORIGEN)
        elif caso == "csrf_malo":
            r = _post(c, csrf_token="1.malo")
        elif caso == "csrf_ajeno":
            r = _post(c, csrf_token=csrf.emitir(SECRETO, "anon"))
        elif caso == "sin_origen":
            r = _post(c, headers={})
        else:
            r = _post(c, headers={"origin": "https://evil.test"})
        assert r.status_code == 403 and _cookie_tema(r) is None

    def test_sin_sesion_va_al_acceso(self, mundo, monkeypatch):
        monkeypatch.setattr(sesion, "_resolver", lambda c, t: None)
        c = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False)
        r = c.post(RUTA, data={"tema": "oscuro", "csrf": "x", "volver": "/portal"}, headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"].startswith("/acceso") and _cookie_tema(r) is None

    def test_portal_apagado_es_404(self, mundo, monkeypatch):
        c = _cli(monkeypatch, GERENTE_SV, cfg=_cfg(enable_portal=False))
        r = _post(c)
        assert r.status_code == 404 and _cookie_tema(r) is None

    def test_funciona_bajo_ver_como(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv")
        antes = mundo.conexiones
        r = _post(c, "claro", volver="/portal?cliente=sv")
        assert r.status_code == 303 and r.headers["location"] == "/portal?cliente=sv"
        assert _cookie_tema(r).startswith(f"{COOKIE_TEMA}=claro;")
        assert mundo.conexiones == antes and mundo.auditados == [] and mundo.iniciar == mundo.terminar == []

    def test_no_exige_cambia_estado(self, mundo, monkeypatch):
        solo_lectura = _alc(("equipo_ventas",), ("sv",), ())
        r = _post(_cli(monkeypatch, solo_lectura), "oscuro")
        assert r.status_code == 303 and _cookie_tema(r) is not None

    def test_limite_de_tasa(self, mundo, monkeypatch):
        c = _cli(monkeypatch, GERENTE_SV)
        c.app.state.limitador_estado = Limitador(maximo=2, ventana_seg=600)
        assert [_post(c).status_code for _ in range(3)] == [303, 303, 429]

    def test_get_no_existe(self, mundo, monkeypatch):
        assert _cli(monkeypatch, GERENTE_SV).get(RUTA).status_code in (404, 405)

    def test_la_vista_lee_la_cookie(self, mundo, monkeypatch):
        """Tras el PRG, la siguiente vista del portal recibe el tema elegido en el contexto."""
        capturado = {}
        real = plantillas.pagina

        def espia(request, nombre, status=200, **ctx):
            capturado.update(tema=plantillas.tema_de(request), ruta=plantillas.ruta_actual(request))
            return real(request, nombre, status=status, **ctx)

        from src.web import portal
        monkeypatch.setattr(portal, "pagina", espia)
        c = _cli(monkeypatch, ADMIN)
        c.cookies.set(COOKIE_TEMA, "oscuro")
        r = c.get("/portal/operativo?aviso=ok")
        assert r.status_code == 200 and capturado == {"tema": "oscuro", "ruta": "/portal/operativo"}


    def test_la_pagina_real_llega_con_el_atributo_y_el_control(self, mundo, monkeypatch):
        """De punta a punta con las plantillas reales: cookie → `data-tema` en <html> y el selector con el
        botón activo, `csrf` y `volver` (sin `aviso`). Ninguna de las dos mitades lo comprobaba junta."""
        c = _cli(monkeypatch, ADMIN)
        c.cookies.set(COOKIE_TEMA, "claro")
        html = c.get("/portal/operativo?aviso=ok").text
        assert '<html lang="es" data-tema="claro">' in html
        assert 'action="/portal/tema"' in html
        assert 'name="volver" value="/portal/operativo"' in html and "aviso" not in html.split('name="volver"')[1][:60]
        assert html.count('aria-pressed="true"') == 1 and 'value="claro"' in html
        sin_cookie = _cli(monkeypatch, ADMIN).get("/portal/operativo").text
        assert 'data-tema="auto"' in sin_cookie
