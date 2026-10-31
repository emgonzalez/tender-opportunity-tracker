"""Fase G1 — pantallas `/portal/admin/users` e `/portal/admin/invitations`.

Sin base real (conexión, sesión y auditoría son dobles, como en `test_web_admin_base.py`); el servicio se reemplaza
por un registro de llamadas para probar SOLO la capa web: acceso, CSRF, «ver como», 404 uniforme, auditoría y
redirección. Las reglas del servicio las cubre `test_admin_usuarios_service.py`.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.identidad import repo as identidad_repo
from src.identidad import service, sesiones
from src.identidad.alcance import Alcance
from src.web import acceso, admin, admin_usuarios, csrf, portal, rutas, sesion
from src.web.app import crear_app

BASE = "https://dev.amalia.test"
SECRETO = "secreto-de-prueba"
ORIGEN = {"origin": BASE}
USERS = rutas.ADMIN_USERS
INV = rutas.ADMIN_INVITATIONS


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(var, "x")


def _alc(roles, uid="u1", simulado=None, clientes=()):
    return Alcance(usuario_id=uid, org_id="org", roles=frozenset(roles), clientes=frozenset(clientes),
                   cambia_estado=frozenset(), ve_operativo=False, simulado=simulado)


ADMIN_PLATAFORMA = _alc(("admin_plataforma",))
ADMIN_ORG = _alc(("admin_organizacion",))
GERENTE = _alc(("gerente_pais",), clientes=("sv",))
SIMULADO = _alc(("admin_organizacion",), simulado="admin_organizacion")


def _cfg():
    return SimpleNamespace(enable_portal=True, portal_cliente_id="cli", enable_feedback=False, daily_budget_usd=5.0,
                           identidad_secret=SECRETO, feedback_base_url=BASE, clients_dir="clients",
                           sesion_ttl_horas=12, sesion_max_dias=7)


@pytest.fixture
def mundo(monkeypatch):
    m = SimpleNamespace(auditados=[], llamadas=[], resultado=service.OK, conexiones=0)

    @contextmanager
    def conexion(cfg):
        m.conexiones += 1
        yield object()

    for modulo in (admin, admin_usuarios, portal, acceso):
        monkeypatch.setattr(modulo.db, "connection", conexion)
    monkeypatch.setattr(sesion, "_pendiente", lambda c, token: False)
    monkeypatch.setattr(admin.identidad_repo, "registrar_auditoria",
                        lambda conn, org, accion, **kw: m.auditados.append((accion, kw)))
    monkeypatch.setattr(portal.ver_como, "opciones", lambda conn, real: [])

    # Servicios: registran (nombre, argumentos) y devuelven `m.resultado`.
    def falso(nombre):
        def f(conn, actor, *args):
            m.llamadas.append((nombre, actor.usuario_id, *args))
            return m.resultado
        return f

    for nombre in ("admin_crear_usuario", "admin_editar_nombre", "admin_baja_usuario", "admin_reactivar_usuario",
                   "admin_cambiar_rol", "admin_agregar_pais", "admin_quitar_pais", "admin_agregar_rol_org",
                   "admin_quitar_rol_org", "admin_revocar_sesion", "admin_cerrar_sesiones", "admin_reiniciar_mfa",
                   "admin_invitar", "admin_revocar_invitacion"):
        monkeypatch.setattr(service, nombre, falso(nombre))
    return m


def _cliente(monkeypatch, alcance, real=None):
    res = sesion.Resolucion(alcance, real or alcance)
    monkeypatch.setattr(sesion, "_resolver", lambda c, token: res if token == "t" else None)
    c = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False)
    c.cookies.set(sesion.COOKIE, "t")
    return c


def _token():
    return csrf.emitir(SECRETO, sesiones.hash_token("t"))


def _no_existe():
    return TestClient(crear_app(_cfg()), base_url=BASE).get("/no-hay-ruta").text


# (ruta, campos, servicio, argumentos esperados tras el actor, acción auditada, destino, solo plataforma)
UID = "abc123"
FICHA = f"{USERS}/{UID}"
ESCRITURAS = [
    (f"{USERS}/create", {"email": "Ana@Example.com", "nombre": "Ana", "rol": "ceo", "cliente": "", "org": ""},
     "admin_crear_usuario", ("Ana@Example.com", "Ana", "ceo", "", ""), "USUARIO_ALTA", USERS, False),
    (f"{FICHA}/name", {"nombre": "Eva"}, "admin_editar_nombre", (UID, "Eva"),
     admin_usuarios.ACCION_EDITAR_NOMBRE, FICHA, False),
    (f"{FICHA}/deactivate", {}, "admin_baja_usuario", (UID,), "USUARIO_BAJA", FICHA, False),
    (f"{FICHA}/reactivate", {}, "admin_reactivar_usuario", (UID,), "USUARIO_REACTIVADO", FICHA, False),
    (f"{FICHA}/role", {"rol": "gerente_pais", "cliente": "sv"}, "admin_cambiar_rol",
     (UID, "sv", "gerente_pais"), "ROL_CAMBIADO", FICHA, False),
    (f"{FICHA}/country/add", {"rol": "equipo_ventas", "cliente": "cr"}, "admin_agregar_pais",
     (UID, "cr", "equipo_ventas"), "MEMBRESIA_ALTA", FICHA, False),
    (f"{FICHA}/country/remove", {"cliente": "cr"}, "admin_quitar_pais", (UID, "cr"), "MEMBRESIA_BAJA", FICHA, False),
    (f"{FICHA}/org-role/add", {"rol": "ceo"}, "admin_agregar_rol_org", (UID, "ceo"), "MEMBRESIA_ALTA", FICHA, False),
    (f"{FICHA}/org-role/remove", {}, "admin_quitar_rol_org", (UID,), "MEMBRESIA_BAJA", FICHA, False),
    (f"{FICHA}/sessions/revoke", {"sesion": "a" * 16}, "admin_revocar_sesion", (UID, "a" * 16),
     "SESION_REVOCADA_POR_ADMIN", FICHA, False),
    (f"{FICHA}/sessions/revoke-all", {}, "admin_cerrar_sesiones", (UID,), "SESION_REVOCADA_POR_ADMIN", FICHA, False),
    (f"{FICHA}/mfa/reset", {}, "admin_reiniciar_mfa", (UID,), "MFA_REINICIADO", FICHA, True),
    (f"{INV}/create", {"email": "n@example.com", "rol": "equipo_ventas", "cliente": "sv", "org": ""},
     "admin_invitar", ("n@example.com", "equipo_ventas", "sv", ""), "INVITACION_CREADA", INV, False),
    (f"{INV}/inv1/revoke", {}, "admin_revocar_invitacion", ("inv1",), "INVITACION_REVOCADA", INV, False),
]
IDS = [e[0].replace("/portal/admin/", "") for e in ESCRITURAS]


def _post(c, ruta, campos, token=None, headers=ORIGEN):
    return c.post(ruta, data={"csrf": _token() if token is None else token, **campos}, headers=headers)


@pytest.mark.parametrize("ruta, campos, servicio, args, accion, destino, plataforma", ESCRITURAS, ids=IDS)
class TestEscrituras:
    def test_exito_llama_al_servicio_audita_y_redirige(self, monkeypatch, mundo, ruta, campos, servicio, args,
                                                       accion, destino, plataforma):
        r = _post(_cliente(monkeypatch, ADMIN_PLATAFORMA), ruta, campos)
        assert r.status_code == 303 and r.headers["location"] == f"{destino}?notice=admin_ok"
        assert mundo.llamadas == [(servicio, "u1", *args)]
        assert [a for a, _ in mundo.auditados] == [accion]
        assert mundo.auditados[0][1]["usuario_id"] == "u1" and mundo.auditados[0][1]["recurso"]
        assert mundo.conexiones == 1                                      # servicio y auditoría, una transacción

    def test_sin_csrf_o_con_origen_ajeno_403(self, monkeypatch, mundo, ruta, campos, servicio, args, accion,
                                              destino, plataforma):
        c = _cliente(monkeypatch, ADMIN_PLATAFORMA)
        assert _post(c, ruta, campos, token="").status_code == 403
        assert _post(c, ruta, campos, headers={"origin": "https://evil.example.com"}).status_code == 403
        assert mundo.llamadas == [] and mundo.auditados == []

    def test_sin_sesion_va_a_login(self, monkeypatch, mundo, ruta, campos, servicio, args, accion, destino,
                                   plataforma):
        c = _cliente(monkeypatch, ADMIN_ORG)
        monkeypatch.setattr(sesion, "_resolver", lambda cc, token: None)
        r = _post(c, ruta, campos)
        assert r.status_code == 303 and r.headers["location"].startswith("/login") and mundo.llamadas == []

    def test_simulado_se_rechaza_sin_tocar_la_base(self, monkeypatch, mundo, ruta, campos, servicio, args, accion,
                                                    destino, plataforma):
        r = _post(_cliente(monkeypatch, SIMULADO, real=ADMIN_PLATAFORMA), ruta, campos)
        assert r.status_code == 303 and "notice=simulacion" in r.headers["location"]
        assert mundo.llamadas == [] and mundo.auditados == [] and mundo.conexiones == 0

    def test_sin_rol_admin_es_404_y_queda_auditado_como_abuso(self, monkeypatch, mundo, ruta, campos, servicio, args,
                                                               accion, destino, plataforma):
        r = _post(_cliente(monkeypatch, GERENTE), ruta, campos)
        assert r.status_code == 404 and r.text == _no_existe()
        assert mundo.llamadas == [] and [a for a, _ in mundo.auditados] == ["ACCESO_FUERA_DE_ALCANCE"]

    def test_lo_ajeno_es_el_404_uniforme_y_no_audita(self, monkeypatch, mundo, ruta, campos, servicio, args, accion,
                                                     destino, plataforma):
        mundo.resultado = service.NO_ENCONTRADO
        r = _post(_cliente(monkeypatch, ADMIN_PLATAFORMA), ruta, campos)
        assert r.status_code == 404 and r.text == _no_existe() and mundo.auditados == []

    @pytest.mark.parametrize("resultado, aviso", [(service.INVALIDO, "admin_invalido"),
                                                  (service.CONFLICTO, "admin_conflicto")])
    def test_si_el_servicio_no_da_ok_no_audita(self, monkeypatch, mundo, ruta, campos, servicio, args, accion,
                                               destino, plataforma, resultado, aviso):
        mundo.resultado = resultado
        r = _post(_cliente(monkeypatch, ADMIN_PLATAFORMA), ruta, campos)
        assert r.status_code == 303 and r.headers["location"] == f"{destino}?notice={aviso}"
        assert mundo.auditados == []

    def test_un_admin_organizacion_solo_pasa_si_la_ruta_no_es_de_plataforma(
            self, monkeypatch, mundo, ruta, campos, servicio, args, accion, destino, plataforma):
        r = _post(_cliente(monkeypatch, ADMIN_ORG), ruta, campos)
        assert r.status_code == (404 if plataforma else 303)
        assert (mundo.llamadas == []) is plataforma


# ── Pantallas (GET) ────────────────────────────────────────────────────────────

FECHA = datetime(2026, 10, 31, 12, 0)


@pytest.fixture
def datos(monkeypatch):
    usuarios = [
        {"usuario_id": "u1", "org_id": "org", "email": "yo@example.com", "nombre": "Yo", "activo": True,
         "mfa": True, "creado_en": FECHA},
        {"usuario_id": UID, "org_id": "org", "email": "ana@example.com", "nombre": "Ana <b>", "activo": False,
         "mfa": False, "creado_en": FECHA},
        {"usuario_id": "plat", "org_id": "org", "email": "plat@example.com", "nombre": "", "activo": True,
         "mfa": True, "creado_en": FECHA},
    ]
    roles = {"u1": {"org": ["admin_organizacion"], "pais": []},
             UID: {"org": ["ceo"], "pais": [("sv", "equipo_ventas")]},
             "plat": {"org": ["admin_plataforma"], "pais": []}}
    monkeypatch.setattr(identidad_repo, "listar_usuarios", lambda c, org: list(usuarios))
    monkeypatch.setattr(identidad_repo, "roles_de_usuarios", lambda c, org: roles)
    monkeypatch.setattr(identidad_repo, "listar_clientes", lambda c, org: ["cr", "sv"])
    monkeypatch.setattr(identidad_repo, "listar_organizaciones", lambda c: [{"org_id": "org", "nombre": "Org"}])
    monkeypatch.setattr(identidad_repo, "listar_sesiones_vigentes", lambda c, u: [
        {"id": "a" * 16, "creado_en": FECHA, "expira_en": FECHA, "mfa_ok": True, "user_agent": "Firefox"}])
    monkeypatch.setattr(identidad_repo, "listar_invitaciones", lambda c, org: [
        {"invitacion_id": "inv1", "org_id": "org", "email": "n@example.com", "rol_propuesto": "equipo_ventas:sv",
         "invitado_por": "u1", "estado": "PENDIENTE", "creado_en": FECHA, "expira_en": FECHA},
        {"invitacion_id": "inv2", "org_id": "org", "email": "m@example.com", "rol_propuesto": "ceo",
         "invitado_por": "u1", "estado": "REVOCADA", "creado_en": FECHA, "expira_en": FECHA}])
    por_id = {u["usuario_id"]: u for u in usuarios}

    def visible(conn, actor, uid):
        u = por_id.get(uid)
        if not u or (uid == "plat" and not actor.es_plataforma):
            return None
        return u

    monkeypatch.setattr(service, "usuario_visible", visible)
    monkeypatch.setattr(service, "roles_de_usuario", lambda c, uid: (
        roles[uid]["org"], roles[uid]["pais"]))


class TestPantallas:
    @pytest.mark.parametrize("ruta", [USERS, FICHA, INV])
    def test_sin_rol_admin_404_uniforme(self, monkeypatch, mundo, datos, ruta):
        for alcance in (GERENTE, _alc(("operador",))):
            r = _cliente(monkeypatch, alcance).get(ruta)
            assert r.status_code == 404 and r.text == _no_existe()

    @pytest.mark.parametrize("ruta", [USERS, FICHA, INV])
    def test_simulado_es_404_y_sin_sesion_va_a_login(self, monkeypatch, mundo, datos, ruta):
        assert _cliente(monkeypatch, SIMULADO, real=ADMIN_PLATAFORMA).get(ruta).status_code == 404
        monkeypatch.setattr(sesion, "_resolver", lambda c, token: None)
        r = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False).get(ruta)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?")

    def test_lista_muestra_roles_estado_y_escapa_el_html(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_PLATAFORMA).get(USERS).text
        assert "ana@example.com" in html and "equipo_ventas · sv" in html and "De baja" in html
        assert "Ana &lt;b&gt;" in html and "Ana <b>" not in html
        assert 'name="csrf"' in html and f'action="{USERS}/create"' in html
        assert "plat@example.com" in html                                 # plataforma sí ve a los suyos

    def test_org_admin_no_ve_a_los_admin_plataforma_ni_el_selector_de_organizacion(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_ORG).get(USERS).text
        assert "plat@example.com" not in html and "yo@example.com" in html
        assert 'name="org"' not in html
        assert 'name="org"' in _cliente(monkeypatch, ADMIN_PLATAFORMA).get(USERS).text

    def test_org_admin_no_puede_elegir_admin_plataforma_en_el_alta(self, monkeypatch, mundo, datos):
        assert '<option value="admin_plataforma">' not in _cliente(monkeypatch, ADMIN_ORG).get(USERS).text
        assert '<option value="admin_plataforma">' in _cliente(monkeypatch, ADMIN_PLATAFORMA).get(USERS).text

    def test_ficha(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_PLATAFORMA).get(FICHA).text
        assert "ana@example.com" in html and "Reactivar" in html and "Dar de baja" not in html
        assert f'action="{FICHA}/role"' in html and f'action="{FICHA}/country/remove"' in html
        assert "aaaaaaaaaaaaaaaa" in html and f'action="{FICHA}/sessions/revoke-all"' in html

    def test_ficha_propia_no_ofrece_baja_ni_reinicio_de_mfa(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_ORG).get(f"{USERS}/u1").text
        assert "Dar de baja" not in html and "/mfa/reset" not in html

    def test_reinicio_de_mfa_solo_lo_ofrece_a_plataforma_sobre_otro_admin(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_PLATAFORMA).get(f"{USERS}/u1").text      # u1 es el alcance de plataforma
        assert "/mfa/reset" not in html                                              # él mismo (usuario_id u1)
        otro = _alc(("admin_plataforma",), uid="otro")
        assert f'action="{USERS}/u1/mfa/reset"' in _cliente(monkeypatch, otro).get(f"{USERS}/u1").text
        org = _alc(("admin_organizacion",), uid="otro")
        assert "/mfa/reset" not in _cliente(monkeypatch, org).get(f"{USERS}/u1").text

    def test_ficha_inexistente_o_ajena_404(self, monkeypatch, mundo, datos):
        assert _cliente(monkeypatch, ADMIN_ORG).get(f"{USERS}/no-existe").status_code == 404
        assert _cliente(monkeypatch, ADMIN_ORG).get(f"{USERS}/plat").status_code == 404
        assert _cliente(monkeypatch, ADMIN_ORG).get(f"{USERS}/create").status_code == 404

    def test_invitaciones(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_ORG).get(INV).text
        assert "n@example.com" in html and "equipo_ventas · sv" in html and "pendiente" in html
        assert f'action="{INV}/inv1/revoke"' in html and f'action="{INV}/inv2/revoke"' not in html

    def test_el_indice_enlaza_a_usuarios(self, monkeypatch, mundo, datos):
        html = _cliente(monkeypatch, ADMIN_ORG).get(rutas.ADMIN).text
        assert f'href="{USERS}"' in html and f'href="{INV}"' in html

    def test_una_falla_de_la_base_es_503(self, monkeypatch, mundo, datos):
        @contextmanager
        def caida(cfg):
            raise RuntimeError("ADB caída")
            yield  # pragma: no cover

        c = _cliente(monkeypatch, ADMIN_ORG)
        monkeypatch.setattr(admin_usuarios.db, "connection", caida)
        assert c.get(USERS).status_code == 503
