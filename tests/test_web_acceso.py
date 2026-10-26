"""Entrega 3 de identidad: login passwordless por HTTP, con el servicio de identidad simulado."""
from __future__ import annotations

import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.identidad import service
from src.web import acceso, csrf, sesion
from src.web.app import crear_app

BASE = "https://dev.amalia.test"
SECRETO = "secreto-de-prueba"


def _cfg(**c):
    base = dict(
        enable_portal=True, identidad_secret=SECRETO, feedback_base_url=BASE, portal_cliente_id="cli",
        enable_feedback=False, daily_budget_usd=5.0, codigo_ttl_min=10, sesion_ttl_horas=12,
        sesion_max_dias=7, notification_from="AMALIA <x@y.z>", app_name="AMALIA",
    )
    base.update(c)
    return SimpleNamespace(**base)


@pytest.fixture
def mundo(monkeypatch):
    """Servicio de identidad en memoria: un usuario `ana@test.com`; registra lo enviado."""
    m = SimpleNamespace(enviados=[], emitidos=[], canjes=[], cerradas=[], alcance=None)

    @contextmanager
    def conexion(cfg):
        yield object()

    monkeypatch.setattr(acceso.db, "connection", conexion)
    monkeypatch.setattr(sesion.db, "connection", conexion)

    def emitir(conn, cfg, email):
        m.emitidos.append(email)
        return (email.lower(), "K7M2QX9P") if email.lower() == "ana@test.com" else None

    def verificar(conn, cfg, email, codigo, ip_hash=None, user_agent=None):
        m.canjes.append((email, codigo, ip_hash))
        return "token-de-sesion" if (email.lower(), codigo.upper()) == ("ana@test.com", "K7M2QX9P") else None

    monkeypatch.setattr(service, "emitir_codigo", emitir)
    monkeypatch.setattr(service, "verificar_codigo", verificar)
    monkeypatch.setattr(service, "cerrar_sesion", lambda conn, token: m.cerradas.append(token) or True)
    monkeypatch.setattr(acceso.email_sender, "send_access_code",
                        lambda cfg, email, codigo, ttl: m.enviados.append((email, codigo, ttl)))
    # La cookie se resuelve a un alcance fijo cuando es la del usuario de prueba.
    monkeypatch.setattr(sesion, "_resolver",
                        lambda cfg, token: m.alcance if token == "token-de-sesion" else None)
    return m


def _cliente(cfg=None, **kw):
    return TestClient(crear_app(cfg or _cfg()), base_url=BASE, follow_redirects=False, **kw)


def _csrf(contexto="anon"):
    return csrf.emitir(SECRETO, contexto)


ORIGEN = {"origin": BASE}


def _post(c, ruta, datos, headers=ORIGEN):
    return c.post(ruta, data=datos, headers=headers)


class TestCompuerta:
    @pytest.mark.parametrize("cambio", [{"enable_portal": False}, {"identidad_secret": ""}, {"feedback_base_url": ""}])
    def test_apagado_es_404_identico_al_de_una_ruta_inexistente(self, mundo, cambio):
        c = _cliente(_cfg(**cambio))
        inexistente = c.get("/no-existe")
        for ruta in ("/acceso", "/salir"):
            r = c.get(ruta) if ruta == "/acceso" else c.post(ruta, data={})
            assert r.status_code == 404 and r.text == inexistente.text
        assert c.post("/acceso", data={"email": "a@b.c"}).status_code == 404

    def test_el_feedback_y_healthz_siguen_sin_sesion(self, mundo):
        c = _cliente()
        assert c.get("/healthz").text == "ok"
        assert c.get("/f/token-invalido/relevante").status_code == 404


class TestFormulario:
    def test_get_no_escribe_y_trae_csrf(self, mundo):
        r = _cliente().get("/acceso")
        assert r.status_code == 200 and 'name="csrf"' in r.text and mundo.emitidos == []
        assert "script-src" not in r.headers["content-security-policy"]
        assert "no-store" in r.headers["cache-control"]

    def test_con_sesion_redirige(self, mundo):
        mundo.alcance = SimpleNamespace(usuario_id="u")
        r = _cliente().get("/acceso", cookies={sesion.COOKIE: "token-de-sesion"})
        assert r.status_code == 303 and r.headers["location"] == "/portal"


class TestPedirCodigo:
    def test_misma_respuesta_con_y_sin_cuenta(self, mundo):
        c = _cliente()
        con = _post(c, "/acceso", {"email": "ana@test.com", "csrf": _csrf()})
        sin = _post(c, "/acceso", {"email": "nadie@test.com", "csrf": _csrf()})
        assert con.status_code == sin.status_code == 200
        # La página cambia solo en el correo que se escribió: ni más ni menos información.
        norm = lambda t: re.sub(r'value="[^"]*"', 'value=""', t)  # noqa: E731
        assert norm(con.text) == norm(sin.text)
        assert "Si el correo tiene acceso" in con.text
        # Solo la cuenta real recibió un código.
        assert mundo.enviados == [("ana@test.com", "K7M2QX9P", 10)]
        assert mundo.emitidos == ["ana@test.com", "nadie@test.com"]

    def test_el_codigo_no_aparece_en_la_respuesta(self, mundo):
        r = _post(_cliente(), "/acceso", {"email": "ana@test.com", "csrf": _csrf()})
        assert "K7M2QX9P" not in r.text

    @pytest.mark.parametrize("datos,headers", [
        ({"email": "ana@test.com"}, ORIGEN),                                  # sin token
        ({"email": "ana@test.com", "csrf": "1.deadbeef"}, ORIGEN),            # token falso
        ({"email": "ana@test.com", "csrf": "tok"}, {"origin": "https://evil.test"}),
        ({"email": "ana@test.com", "csrf": "tok"}, {}),                       # sin Origin ni Referer
    ])
    def test_sin_csrf_valido_es_403_y_no_emite(self, mundo, datos, headers):
        if datos.get("csrf") == "tok":
            datos = {**datos, "csrf": _csrf()}  # token bueno: el rechazo es por el origen
        r = _post(_cliente(), "/acceso", datos, headers)
        assert r.status_code == 403 and mundo.emitidos == []

    def test_acepta_referer_si_no_hay_origin(self, mundo):
        r = _post(_cliente(), "/acceso", {"email": "ana@test.com", "csrf": _csrf()}, {"referer": BASE + "/acceso"})
        assert r.status_code == 200

    def test_token_csrf_vencido(self, mundo):
        viejo = csrf.emitir(SECRETO, "anon", ahora=1_000_000)
        assert _post(_cliente(), "/acceso", {"email": "a@b.c", "csrf": viejo}).status_code == 403

    def test_limite_por_ip(self, mundo):
        c = _cliente()
        codigos = [_post(c, "/acceso", {"email": "x@test.com", "csrf": _csrf()}).status_code for _ in range(12)]
        assert codigos[:10] == [200] * 10 and codigos[10:] == [429, 429]


class TestCanjear:
    def _canjear(self, c, codigo="K7M2QX9P", email="ana@test.com", **extra):
        return _post(c, "/acceso/codigo", {"email": email, "codigo": codigo, "csrf": _csrf(), **extra})

    def test_login_ok_pone_la_cookie_con_los_cuatro_atributos(self, mundo):
        r = self._canjear(_cliente())
        assert r.status_code == 303 and r.headers["location"] == "/portal"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{sesion.COOKIE}=token-de-sesion")
        for atributo in ("Secure", "HttpOnly", "Path=/", "SameSite=lax"):
            assert atributo.lower() in cookie.lower()
        assert "domain" not in cookie.lower() and "max-age=604800" in cookie.lower()

    def test_el_hash_de_ip_no_es_la_ip(self, mundo):
        self._canjear(_cliente())
        ip_hash = mundo.canjes[0][2]
        assert re.fullmatch(r"[0-9a-f]{64}", ip_hash) and "testclient" not in ip_hash

    @pytest.mark.parametrize("email,codigo", [("ana@test.com", "AAAAAAAA"), ("nadie@test.com", "K7M2QX9P")])
    def test_fallos_son_indistinguibles(self, mundo, email, codigo):
        r = self._canjear(_cliente(), codigo=codigo, email=email)
        assert r.status_code == 401 and "set-cookie" not in r.headers
        assert "no es válido o ya venció" in r.text

    def test_sin_csrf_es_403_y_no_canjea(self, mundo):
        r = _post(_cliente(), "/acceso/codigo", {"email": "ana@test.com", "codigo": "K7M2QX9P"})
        assert r.status_code == 403 and mundo.canjes == []

    @pytest.mark.parametrize("siguiente,esperado", [
        ("/portal/operativo", "/portal/operativo"),
        ("https://evil.test", "/portal"),
        ("//evil.test/portal", "/portal"),
        ("/otra-cosa", "/portal"),
        ("/portal\r\nSet-Cookie: x=1", "/portal"),
        ("/portal\\evil", "/portal"),
        ("", "/portal"),
    ])
    def test_siguiente_solo_rutas_internas(self, mundo, siguiente, esperado):
        r = self._canjear(_cliente(), siguiente=siguiente)
        assert r.headers["location"] == esperado

    def test_adb_caida_es_503_sin_traceback(self, mundo, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("adb caída")

        monkeypatch.setattr(service, "verificar_codigo", boom)
        r = self._canjear(_cliente())
        assert r.status_code == 503 and "adb" not in r.text.lower()


class TestSalir:
    def test_cierra_la_sesion_y_borra_la_cookie(self, mundo):
        from src.identidad import sesiones
        mundo.alcance = SimpleNamespace(usuario_id="u")
        token_csrf = csrf.emitir(SECRETO, sesiones.hash_token("token-de-sesion"))
        r = _cliente().post("/salir", data={"csrf": token_csrf}, headers=ORIGEN,
                            cookies={sesion.COOKIE: "token-de-sesion"})
        assert r.status_code == 303 and r.headers["location"] == "/acceso"
        assert mundo.cerradas == ["token-de-sesion"]
        assert "max-age=0" in r.headers["set-cookie"].lower()

    def test_un_token_csrf_anonimo_no_sirve_con_sesion(self, mundo):
        mundo.alcance = SimpleNamespace(usuario_id="u")
        r = _cliente().post("/salir", data={"csrf": _csrf("anon")}, headers=ORIGEN,
                            cookies={sesion.COOKIE: "token-de-sesion"})
        assert r.status_code == 403 and mundo.cerradas == []

    def test_sin_sesion_redirige_al_acceso(self, mundo):
        r = _post(_cliente(), "/salir", {"csrf": _csrf()})
        assert r.status_code == 303 and r.headers["location"] == "/acceso" and mundo.cerradas == []


class TestSesionMiddleware:
    def test_una_cookie_desconocida_no_da_alcance(self, mundo):
        mundo.alcance = SimpleNamespace(usuario_id="u")
        r = _cliente().get("/acceso", cookies={sesion.COOKIE: "otro-token"})
        assert r.status_code == 200  # sigue viendo el formulario

    def test_no_toca_la_base_fuera_de_las_rutas_del_portal(self, mundo, monkeypatch):
        def explota(cfg, token):
            raise AssertionError("no debía resolver la sesión")

        monkeypatch.setattr(sesion, "_resolver", explota)
        c = _cliente()
        assert c.get("/healthz", cookies={sesion.COOKIE: "x"}).status_code == 200
        assert c.get("/f/abc/relevante", cookies={sesion.COOKIE: "x"}).status_code == 404

    def test_la_adb_caida_deja_sin_sesion_pero_no_rompe(self, mundo, monkeypatch):
        def boom(cfg, token):
            raise RuntimeError("adb caída")

        monkeypatch.setattr(sesion, "_resolver", boom)
        assert _cliente().get("/acceso", cookies={sesion.COOKIE: "token-de-sesion"}).status_code == 200

    def test_cookie_mal_formada(self, mundo):
        assert _cliente().get("/acceso", headers={"cookie": "\x00;;=="}).status_code == 200


class TestPiezas:
    def test_csrf_ida_y_vuelta(self):
        t = csrf.emitir("s", "ctx")
        assert csrf.verificar("s", "ctx", t)
        assert not csrf.verificar("s", "otro", t) and not csrf.verificar("otro", "ctx", t)
        assert not csrf.verificar("s", "ctx", "") and not csrf.verificar("", "ctx", t)
        with pytest.raises(ValueError):
            csrf.emitir("", "ctx")

    def test_limitador_ventana_deslizante(self):
        from src.web.limitador import Limitador
        lim = Limitador(maximo=2, ventana_seg=10)
        assert lim.permitir("k", 0) and lim.permitir("k", 1) and not lim.permitir("k", 2)
        assert lim.permitir("otra", 2) and lim.permitir("k", 11.5)

    def test_envio_del_codigo_va_al_destinatario_real_aun_en_validacion(self, monkeypatch):
        # Regresión de P7: el correo de acceso no pasa por NOTIFICATION_OVERRIDE_TO.
        from unittest.mock import MagicMock, patch

        from src.notifier.email_sender import send_access_code
        cfg = _cfg(override_recipients=["otro@x.com"], smtp_host="h", smtp_port=1, smtp_user="u", smtp_password="p")
        servidor = MagicMock()
        with patch("src.notifier.email_sender.smtplib.SMTP") as smtp:
            smtp.return_value.__enter__.return_value = servidor
            send_access_code(cfg, "ana@test.com", "K7M2QX9P", 10)
        assert servidor.sendmail.call_args[0][1] == ["ana@test.com"]
