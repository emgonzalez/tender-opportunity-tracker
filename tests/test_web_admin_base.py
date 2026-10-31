"""Fase G0: permisos de administración (`Alcance`/`politica`) y la base web `/portal/admin`.

Sin base real: la conexión, la sesión y la auditoría son dobles, como en `test_web_ver_como.py`. La ruta POST de
escritura es SOLO de prueba (en producción G1-G3 registran las suyas sobre `escritura_admin`).
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import Form, Request
from fastapi.testclient import TestClient

from src.identidad import politica, sesiones
from src.identidad.alcance import Alcance
from src.web import acceso, admin, csrf, portal, rutas, sesion
from src.web.app import crear_app

BASE = "https://dev.amalia.test"
SECRETO = "secreto-de-prueba"
ORIGEN = {"origin": BASE}
RUTA_PRUEBA = "/portal/admin/_prueba"


def _alc(roles, simulado=None, clientes=()):
    return Alcance(usuario_id="u1", org_id="org", roles=frozenset(roles), clientes=frozenset(clientes),
                   cambia_estado=frozenset(), ve_operativo=False, simulado=simulado)


ADMIN_PLATAFORMA = _alc(("admin_plataforma",))
ADMIN_ORG = _alc(("admin_organizacion",))
GERENTE = _alc(("gerente_pais",), clientes=("sv",))
SIMULADO_ADMIN = _alc(("admin_organizacion",), simulado="admin_organizacion")


def _cfg():
    return SimpleNamespace(enable_portal=True, portal_cliente_id="cli", enable_feedback=False, daily_budget_usd=5.0,
                           identidad_secret=SECRETO, feedback_base_url=BASE, clients_dir="clients",
                           sesion_ttl_horas=12, sesion_max_dias=7)


# ── Alcance y política ─────────────────────────────────────────────────────────

def test_roles_admin_son_los_dos_con_mfa():
    assert politica.ROLES_ADMIN == {"admin_organizacion", "admin_plataforma"}
    assert politica.ROLES_ADMIN <= politica.ROLES_MFA


@pytest.mark.parametrize("alcance, administra, plataforma", [
    (ADMIN_PLATAFORMA, True, True),
    (ADMIN_ORG, True, False),
    (GERENTE, False, False),
    (_alc(("operador",)), False, False),
    (_alc(("admin_plataforma", "ceo")), True, True),
    (SIMULADO_ADMIN, False, False),                                  # «ver como» nunca administra
    (_alc(("admin_plataforma",), simulado="admin_plataforma"), False, False),
])
def test_permisos_de_administracion(alcance, administra, plataforma):
    assert alcance.puede_administrar is administra
    assert alcance.es_plataforma is plataforma


def test_acciones_nuevas_estan_auditadas():
    nuevas = {"USUARIO_REACTIVADO", "INVITACION_CREADA", "INVITACION_REVOCADA", "INVITACION_USADA",
              "MFA_REINICIADO", "PERFIL_VERSION_CREADA", "PERFIL_VERSION_REVERTIDA", "CLIENTE_CREADO",
              "CLIENTE_ACTIVADO", "CLIENTE_DESACTIVADO", "PORTAL_EDITADO", "CUOTA_EDITADA",
              "ORGANIZACION_EDITADA", "SESION_REVOCADA_POR_ADMIN"}
    assert nuevas <= politica.ACCIONES_AUDITADAS


# ── Web ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mundo(monkeypatch):
    m = SimpleNamespace(auditados=[], escrituras=[], resultado=admin.OK, conexiones=0)

    @contextmanager
    def conexion(cfg):
        m.conexiones += 1
        yield object()

    for modulo in (admin, portal, acceso):
        monkeypatch.setattr(modulo.db, "connection", conexion)
    monkeypatch.setattr(sesion, "_pendiente", lambda c, token: False)
    monkeypatch.setattr(admin.identidad_repo, "registrar_auditoria",
                        lambda conn, org, accion, **kw: m.auditados.append((accion, kw)))
    monkeypatch.setattr(portal.ver_como, "opciones", lambda conn, real: [])
    return m


def _cliente(monkeypatch, mundo, alcance, real=None):
    """App con una ruta POST de prueba sobre `escritura_admin`; la cookie `t` resuelve a `alcance`."""
    res = sesion.Resolucion(alcance, real or alcance)
    monkeypatch.setattr(sesion, "_resolver", lambda c, token: res if token == "t" else None)
    app = crear_app(_cfg())

    @app.post(RUTA_PRUEBA)
    def escribir_prueba(request: Request, csrf: str = Form(""), plataforma: str = Form("")):
        def escribir(conn, alc, ip_hash):
            mundo.escrituras.append(alc.usuario_id)
            return mundo.resultado

        return admin.escritura_admin(request, csrf, "prueba", "CLIENTE_CREADO", "cliente=demo", escribir,
                                     plataforma=bool(plataforma))

    c = TestClient(app, base_url=BASE, follow_redirects=False)
    c.cookies.set(sesion.COOKIE, "t")
    return c


def _token():
    return csrf.emitir(SECRETO, sesiones.hash_token("t"))


def _no_existe():
    return TestClient(crear_app(_cfg()), base_url=BASE).get("/no-hay-ruta").text


class TestIndice:
    def test_sin_sesion_va_a_login(self, monkeypatch, mundo):
        monkeypatch.setattr(sesion, "_resolver", lambda c, token: None)
        r = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False).get(rutas.ADMIN)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?")

    @pytest.mark.parametrize("alcance", [GERENTE, _alc(("operador",)), _alc(("ceo",), clientes=("sv",))])
    def test_sin_rol_admin_es_404_uniforme(self, monkeypatch, mundo, alcance):
        r = _cliente(monkeypatch, mundo, alcance).get(rutas.ADMIN)
        assert r.status_code == 404 and r.text == _no_existe()

    @pytest.mark.parametrize("alcance", [ADMIN_PLATAFORMA, ADMIN_ORG])
    def test_admin_real_ve_el_indice(self, monkeypatch, mundo, alcance):
        r = _cliente(monkeypatch, mundo, alcance).get(rutas.ADMIN)
        assert r.status_code == 200 and "Administración" in r.text

    def test_simulado_con_rol_admin_es_404_en_get(self, monkeypatch, mundo):
        c = _cliente(monkeypatch, mundo, SIMULADO_ADMIN, real=ADMIN_PLATAFORMA)
        r = c.get(rutas.ADMIN)
        assert r.status_code == 404 and r.text == _no_existe()

    def test_portal_apagado_es_404(self, monkeypatch, mundo):
        monkeypatch.setattr(sesion, "_resolver", lambda c, token: None)
        cfg = _cfg()
        cfg.enable_portal = False
        assert TestClient(crear_app(cfg), base_url=BASE, follow_redirects=False).get(rutas.ADMIN).status_code == 404

    def test_la_navegacion_muestra_el_enlace_solo_a_admins(self, monkeypatch, mundo):
        admin_html = _cliente(monkeypatch, mundo, ADMIN_ORG).get(rutas.ADMIN).text
        assert f'href="{rutas.ADMIN}"' in admin_html and ">Administración</a>" in admin_html
        # La plantilla de navegación sin el permiso no dibuja el enlace.
        entorno = crear_app(_cfg()).state.plantillas
        html = entorno.get_template("portal_nav.html").render(nav=[], csrf="x", puede_administrar=False)
        assert "Administración" not in html
        html = entorno.get_template("portal_nav.html").render(nav=[], csrf="x", puede_administrar=True)
        assert "Administración" in html


class TestEscritura:
    def _post(self, c, token=None, headers=ORIGEN, **extra):
        return c.post(RUTA_PRUEBA, data={"csrf": _token() if token is None else token, **extra}, headers=headers)

    def test_admin_escribe_audita_y_redirige(self, monkeypatch, mundo):
        r = self._post(_cliente(monkeypatch, mundo, ADMIN_ORG))
        assert r.status_code == 303 and r.headers["location"] == f"{rutas.ADMIN}?notice=admin_ok"
        assert mundo.escrituras == ["u1"]
        assert [a for a, _ in mundo.auditados] == ["CLIENTE_CREADO"]
        assert mundo.auditados[0][1]["recurso"] == "cliente=demo"
        assert mundo.conexiones == 1                                   # servicio y auditoría, una transacción

    def test_sin_csrf_se_rechaza(self, monkeypatch, mundo):
        r = self._post(_cliente(monkeypatch, mundo, ADMIN_ORG), token="")
        assert r.status_code == 403 and mundo.escrituras == [] and mundo.auditados == []

    def test_con_origen_ajeno_se_rechaza(self, monkeypatch, mundo):
        r = self._post(_cliente(monkeypatch, mundo, ADMIN_ORG), headers={"origin": "https://evil.example.com"})
        assert r.status_code == 403 and mundo.escrituras == [] and mundo.auditados == []

    def test_sin_sesion_va_a_login(self, monkeypatch, mundo):
        monkeypatch.setattr(sesion, "_resolver", lambda c, token: None)
        c = _cliente(monkeypatch, mundo, ADMIN_ORG)
        monkeypatch.setattr(sesion, "_resolver", lambda c, token: None)
        r = self._post(c)
        assert r.status_code == 303 and r.headers["location"].startswith("/login") and mundo.escrituras == []

    def test_simulado_con_rol_admin_se_rechaza_sin_tocar_la_base(self, monkeypatch, mundo):
        r = self._post(_cliente(monkeypatch, mundo, SIMULADO_ADMIN, real=ADMIN_PLATAFORMA))
        assert r.status_code == 303 and "notice=simulacion" in r.headers["location"]
        assert mundo.escrituras == [] and mundo.auditados == [] and mundo.conexiones == 0

    def test_sin_rol_admin_es_404_y_queda_auditado_como_abuso(self, monkeypatch, mundo):
        r = self._post(_cliente(monkeypatch, mundo, GERENTE))
        assert r.status_code == 404 and r.text == _no_existe()
        assert mundo.escrituras == []
        assert [a for a, _ in mundo.auditados] == ["ACCESO_FUERA_DE_ALCANCE"]

    def test_lo_global_exige_admin_plataforma(self, monkeypatch, mundo):
        assert self._post(_cliente(monkeypatch, mundo, ADMIN_ORG), plataforma="1").status_code == 404
        assert mundo.escrituras == []
        assert self._post(_cliente(monkeypatch, mundo, ADMIN_PLATAFORMA), plataforma="1").status_code == 303
        assert mundo.escrituras == ["u1"]

    @pytest.mark.parametrize("resultado, esperado", [
        (admin.INVALIDO, "admin_invalido"), (admin.CONFLICTO, "admin_conflicto")])
    def test_si_el_servicio_no_da_ok_no_audita(self, monkeypatch, mundo, resultado, esperado):
        mundo.resultado = resultado
        r = self._post(_cliente(monkeypatch, mundo, ADMIN_ORG))
        assert r.status_code == 303 and r.headers["location"].endswith(f"notice={esperado}")
        assert mundo.auditados == []

    def test_lo_ajeno_es_el_404_uniforme(self, monkeypatch, mundo):
        mundo.resultado = admin.NO_ENCONTRADO
        r = self._post(_cliente(monkeypatch, mundo, ADMIN_ORG))
        assert r.status_code == 404 and r.text == _no_existe() and mundo.auditados == []

    def test_una_falla_de_la_base_es_503_sin_auditar(self, monkeypatch, mundo):
        @contextmanager
        def caida(cfg):
            raise RuntimeError("ADB caída")
            yield  # pragma: no cover

        c = _cliente(monkeypatch, mundo, ADMIN_ORG)
        monkeypatch.setattr(admin.db, "connection", caida)
        r = self._post(c)
        assert r.status_code == 503 and mundo.auditados == []
