"""Fase G2: administración de clientes y perfiles en el portal (`/portal/admin/clients`).

Sin Oracle: la ADB es la `FakeADB` en memoria de `test_clientes_cli.py` (ampliada con las consultas nuevas), la
sesión y la auditoría son dobles, como en `test_web_admin_base.py`. Los perfiles son dicts inline.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from test_clientes_cli import FakeADB, FakeCursor

from src.clientes import repo, servicio
from src.identidad import sesiones
from src.identidad.alcance import Alcance
from src.web import admin, admin_clientes, csrf, portal, rutas, sesion
from src.web.app import crear_app

BASE = "https://dev.amalia.test"
SECRETO = "secreto-de-prueba"
ORIGEN = {"origin": BASE}
CLI = rutas.ADMIN_CLIENTS


def _alc(roles, org="org", simulado=None, clientes=()):
    return Alcance(usuario_id="u1", org_id=org, roles=frozenset(roles), clientes=frozenset(clientes),
                   cambia_estado=frozenset(), ve_operativo=False, simulado=simulado)


PLATAFORMA = _alc(("admin_plataforma",))
ADMIN_ORG = _alc(("admin_organizacion",))
GERENTE = _alc(("gerente_pais",), clientes=("acme",))
SIMULADO = _alc(("admin_organizacion",), simulado="admin_organizacion")


def _perfil(cid="acme", nombre="Acme", **extra):
    base = {
        "client": {"id": cid, "name": nombre, "active": True, "portal": "comprasal_sv"},
        "catalog": {"categories": [{"id": "infra", "label": "Infraestructura", "items": ["Servidores"],
                                    "brands": [{"id": "dell", "name": "Dell", "products": ["PowerEdge"]}]}],
                    "inclusion_signals": ["servidor"], "exclusion_signals": ["obra civil"], "tag_taxonomy": ["Servidores"]},
        "rules": {"context_note": "nota", "categories": "hardware, software"},
        "classification": {"relevance_threshold": 0.6, "confidence_threshold": 0.7},
        "notification": {"email_to": ["a@example.com"], "system_admins_email": [], "report_title": "Reporte",
                         "subject_prefix": "[ACME]", "intro_message": ""},
        "intelligence": {"generated_at": "2026-01-01", "processes_analyzed": 3, "profile": "perfil entrenado"},
    }
    base.update(extra)
    return base


class Cur2(FakeCursor):
    """Consultas que el portal agrega a la FakeADB de la CLI."""

    def execute(self, sql, *args, **kw):
        s = " ".join(sql.split())
        db = self.db
        if s.startswith("SELECT cliente_id, org_id, nombre, activo, locale, zona_horaria FROM cliente"):
            v = db.clientes.get(kw["c"])
            self._res = [(kw["c"], v["org_id"], v["nombre"], v["activo"], v.get("locale", "es-SV"),
                          v.get("zona", "America/El_Salvador"))] if v else []
        elif s.startswith("SELECT cliente_id FROM cliente WHERE cliente_id = :c FOR UPDATE"):
            db.bloqueos.append(kw["c"])
            self._res = [(kw["c"],)]
        elif s.startswith("SELECT org_id, nombre FROM organizacion"):
            self._res = [(o, o) for o in sorted(db.orgs)]
        elif s.startswith("SELECT portal_id, activo, credencial_ref"):
            self._res = list(db.cliente_portal.get(kw["c"], []))
        elif s.startswith("UPDATE cliente SET nombre"):
            db.clientes[kw["c"]].update(nombre=kw["n"], locale=kw["l"], zona=kw["z"])
            self._res = []
        elif s.startswith("SELECT c.cliente_id") and "WHERE c.org_id" in s:
            self._res = [(k, v["org_id"], v["nombre"], v["activo"],
                          max((f["version"] for f in db.activos(k)), default=None))
                         for k, v in sorted(db.clientes.items()) if v["org_id"] == kw["o"]]
        else:
            super().execute(sql, *args, **kw)


class ADB(FakeADB):
    def __init__(self):
        super().__init__()
        self.bloqueos: list[str] = []
        self.cliente_portal: dict[str, list] = {}

    def cursor(self):
        return Cur2(self)

    def sembrar(self, cid="acme", org="org", nombre="Acme", perfil=None):
        self.orgs.add(org)
        self.clientes[cid] = {"org_id": org, "nombre": nombre, "activo": 1}
        self.config.append({"cliente_id": cid, "version": 1, "contenido": perfil or _perfil(cid, nombre),
                            "activa": 1, "creado_por": "sembrado"})

    def activa(self, cid="acme"):
        return self.activos(cid)[0]


@pytest.fixture
def mundo(monkeypatch):
    m = SimpleNamespace(adb=ADB(), auditados=[], conexiones=0)
    m.adb.sembrar()

    @contextmanager
    def conexion(cfg):
        m.conexiones += 1
        # Como `db.connection`: si la ruta falla, se descarta; acá basta con no auditar fuera de la transacción.
        yield m.adb

    monkeypatch.setattr(admin.db, "connection", conexion)       # `db` es el mismo módulo en toda la web
    monkeypatch.setattr(sesion, "_pendiente", lambda c, token: False)
    monkeypatch.setattr(admin.identidad_repo, "registrar_auditoria",
                        lambda conn, org, accion, **kw: m.auditados.append((accion, kw)))
    monkeypatch.setattr(servicio, "creado_por", lambda conn, alcance: "admin@example.com")
    monkeypatch.setattr(portal.ver_como, "opciones", lambda conn, real: [])
    return m


def _cfg():
    return SimpleNamespace(enable_portal=True, portal_cliente_id="cli", enable_feedback=False, daily_budget_usd=5.0,
                           identidad_secret=SECRETO, feedback_base_url=BASE, clients_dir="clients",
                           sesion_ttl_horas=12, sesion_max_dias=7)


def _web(monkeypatch, alcance, real=None, con_sesion=True):
    res = sesion.Resolucion(alcance, real or alcance)
    monkeypatch.setattr(sesion, "_resolver", lambda c, token: res if (con_sesion and token == "t") else None)
    c = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False)
    c.cookies.set(sesion.COOKIE, "t")
    return c


def _token():
    return csrf.emitir(SECRETO, sesiones.hash_token("t"))


def _post(c, ruta, headers=ORIGEN, token=None, **datos):
    return c.post(ruta, data={"csrf": _token() if token is None else token, **datos}, headers=headers)


def _aviso(r):
    return parse_qs(urlparse(r.headers["location"]).query).get("notice", [""])[0]


def _no_existe():
    return TestClient(crear_app(_cfg()), base_url=BASE).get("/no-hay-ruta").text


def _ajeno(mundo):
    mundo.adb.sembrar("otro", org="otra_org", nombre="Otro", perfil=_perfil("otro", "Otro"))


# ── Acceso ──────────────────────────────────────────────────────────────────────

class TestAcceso:
    def test_sin_sesion_va_a_login(self, monkeypatch, mundo):
        r = _web(monkeypatch, ADMIN_ORG, con_sesion=False).get(CLI)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?")

    @pytest.mark.parametrize("ruta", ["", "/acme", "/acme/profile/rules", "/acme/versions", "/acme/profile/intelligence"])
    def test_sin_rol_admin_es_404_uniforme(self, monkeypatch, mundo, ruta):
        r = _web(monkeypatch, GERENTE).get(CLI + ruta)
        assert r.status_code == 404 and r.text == _no_existe()

    def test_simulado_es_404_en_get(self, monkeypatch, mundo):
        r = _web(monkeypatch, SIMULADO, real=PLATAFORMA).get(CLI)
        assert r.status_code == 404 and r.text == _no_existe()

    def test_admin_organizacion_solo_ve_su_organizacion(self, monkeypatch, mundo):
        _ajeno(mundo)
        html = _web(monkeypatch, ADMIN_ORG).get(CLI).text
        assert "acme" in html and "otro" not in html and "Crear cliente" not in html

    def test_plataforma_ve_todo_y_el_alta(self, monkeypatch, mundo):
        _ajeno(mundo)
        html = _web(monkeypatch, PLATAFORMA).get(CLI).text
        assert "acme" in html and "otro" in html and "Crear cliente" in html

    @pytest.mark.parametrize("ruta", ["/otro", "/otro/profile/rules", "/otro/versions", "/no_existe"])
    def test_lo_ajeno_o_inexistente_es_404_uniforme(self, monkeypatch, mundo, ruta):
        _ajeno(mundo)
        r = _web(monkeypatch, ADMIN_ORG).get(CLI + ruta)
        assert r.status_code == 404 and r.text == _no_existe()

    def test_seccion_desconocida_es_404(self, monkeypatch, mundo):
        assert _web(monkeypatch, ADMIN_ORG).get(CLI + "/acme/profile/otra").status_code == 404

    def test_la_inteligencia_es_de_solo_lectura(self, monkeypatch, mundo):
        c = _web(monkeypatch, ADMIN_ORG)
        html = c.get(CLI + "/acme/profile/intelligence").text
        assert "perfil entrenado" in html and "Guardar como versión nueva" not in html
        assert _post(c, CLI + "/acme/profile/intelligence", version_base="1").status_code in (303, 404)
        assert mundo.adb.activa()["version"] == 1

    def test_cliente_portal_es_de_solo_lectura(self, monkeypatch, mundo):
        mundo.adb.cliente_portal["acme"] = [("comprasal_sv", 1, "ref-secreta", 2, "0 7 * * *")]
        c = _web(monkeypatch, ADMIN_ORG)
        html = c.get(CLI + "/acme").text
        assert "ref-secreta" in html and "Solo lectura" in html
        assert c.post(CLI + "/acme/portals", data={"csrf": _token()}, headers=ORIGEN).status_code in (404, 405)


# ── CSRF, simulado, permiso ─────────────────────────────────────────────────────

RUTAS_ESCRITURA = [
    ("/acme/profile/rules", {"context_note": "x", "categories": "otro", "version_base": "1"}),
    ("/acme/general", {"nombre": "N", "locale": "es-SV", "zona_horaria": "America/El_Salvador", "version_base": "1"}),
    ("/acme/versions/1/revert", {"version_base": "1"}),
    ("/acme/deactivate", {"version_base": "1"}),
    ("/create", {"cliente_id": "nuevo_pa", "nombre": "N", "portal": "comprasal_sv", "org_id": "org"}),
]


class TestGuardas:
    @pytest.mark.parametrize("ruta, datos", RUTAS_ESCRITURA)
    def test_sin_csrf_403(self, monkeypatch, mundo, ruta, datos):
        r = _post(_web(monkeypatch, PLATAFORMA), CLI + ruta, token="", **datos)
        assert r.status_code == 403 and mundo.auditados == [] and len(mundo.adb.config) == 1

    @pytest.mark.parametrize("ruta, datos", RUTAS_ESCRITURA)
    def test_origen_ajeno_403(self, monkeypatch, mundo, ruta, datos):
        r = _post(_web(monkeypatch, PLATAFORMA), CLI + ruta, headers={"origin": "https://evil.example.com"}, **datos)
        assert r.status_code == 403 and mundo.auditados == [] and len(mundo.adb.config) == 1

    @pytest.mark.parametrize("ruta, datos", RUTAS_ESCRITURA)
    def test_simulado_se_rechaza_sin_tocar_la_base(self, monkeypatch, mundo, ruta, datos):
        r = _post(_web(monkeypatch, SIMULADO, real=PLATAFORMA), CLI + ruta, **datos)
        assert r.status_code == 303 and _aviso(r) == "simulacion"
        assert mundo.conexiones == 0 and mundo.auditados == []

    @pytest.mark.parametrize("ruta, datos", RUTAS_ESCRITURA)
    def test_sin_rol_admin_404_y_queda_como_abuso(self, monkeypatch, mundo, ruta, datos):
        r = _post(_web(monkeypatch, GERENTE), CLI + ruta, **datos)
        assert r.status_code == 404 and [a for a, _ in mundo.auditados] == ["ACCESO_FUERA_DE_ALCANCE"]
        assert len(mundo.adb.config) == 1

    @pytest.mark.parametrize("ruta", ["/create", "/acme/activate", "/acme/deactivate"])
    def test_crear_y_activar_exigen_admin_plataforma(self, monkeypatch, mundo, ruta):
        datos = {"cliente_id": "nuevo_pa", "nombre": "N", "portal": "comprasal_sv", "org_id": "org", "version_base": "1"}
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + ruta, **datos)
        assert r.status_code == 404 and len(mundo.adb.config) == 1 and "nuevo_pa" not in mundo.adb.clientes

    @pytest.mark.parametrize("ruta, datos", [r for r in RUTAS_ESCRITURA if r[0] != "/create"])
    def test_lo_ajeno_no_se_escribe(self, monkeypatch, mundo, ruta, datos):
        _ajeno(mundo)
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + ruta.replace("acme", "otro"), **datos)
        assert r.status_code == 404 and r.text == _no_existe()
        assert len(mundo.adb.config) == 2 and mundo.adb.clientes["otro"]["activo"] == 1
        assert {a for a, _ in mundo.auditados} <= {"ACCESO_FUERA_DE_ALCANCE"}      # nunca una acción de escritura


# ── Perfil: éxito, auditoría, sin cambios ───────────────────────────────────────

class TestPerfil:
    def test_edita_reglas_versiona_audita_y_conserva_el_resto(self, monkeypatch, mundo):
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/profile/rules",
                  context_note="Nuevo contexto", categories="hardware, redes", version_base="1")
        assert r.status_code == 303 and _aviso(r) == "admin_ok"
        assert r.headers["location"].startswith(CLI + "/acme/profile/rules")
        nueva = mundo.adb.activa()
        assert nueva["version"] == 2 and nueva["creado_por"] == "admin@example.com"
        assert nueva["contenido"]["rules"] == {"context_note": "Nuevo contexto", "categories": "hardware, redes"}
        previo = _perfil()
        for clave in ("client", "catalog", "classification", "notification", "intelligence"):
            assert nueva["contenido"][clave] == previo[clave]
        assert [a for a, _ in mundo.auditados] == ["PERFIL_VERSION_CREADA"]
        assert "cliente=acme" in mundo.auditados[0][1]["recurso"]
        assert mundo.conexiones == 1 and "acme" in mundo.adb.bloqueos       # fila bloqueada, una transacción

    def test_umbrales_y_correo_y_motivos(self, monkeypatch, mundo):
        c = _web(monkeypatch, ADMIN_ORG)
        _post(c, CLI + "/acme/profile/thresholds", relevance_threshold="0,55", confidence_threshold="0.8", version_base="1")
        _post(c, CLI + "/acme/profile/email", report_title="T", subject_prefix="[X]", intro_message="Hola", version_base="2")
        _post(c, CLI + "/acme/profile/feedback", motivos="caro | Es muy caro\nlejos | Queda lejos", version_base="3")
        datos = mundo.adb.activa()["contenido"]
        assert mundo.adb.activa()["version"] == 4
        assert datos["classification"] == {"relevance_threshold": 0.55, "confidence_threshold": 0.8}
        assert datos["notification"]["subject_prefix"] == "[X]" and datos["notification"]["email_to"] == ["a@example.com"]
        assert datos["feedback"]["motivos"] == [{"id": "caro", "label": "Es muy caro"}, {"id": "lejos", "label": "Queda lejos"}]
        _post(c, CLI + "/acme/profile/feedback", motivos="", version_base="4")
        assert "feedback" not in mundo.adb.activa()["contenido"]

    def test_catalogo(self, monkeypatch, mundo):
        cats = json.dumps([{"id": "redes", "label": "Redes", "items": ["Switches"],
                            "brands": [{"id": "cisco", "name": "Cisco", "products": ["Catalyst"]}]}])
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/profile/catalog", categories=cats,
                  inclusion_signals="switch\n\nswitch\nrouter", exclusion_signals="", tag_taxonomy="Redes\nSwitches",
                  version_base="1")
        assert _aviso(r) == "admin_ok"
        cat = mundo.adb.activa()["contenido"]["catalog"]
        assert cat["categories"][0]["brands"][0]["name"] == "Cisco"
        assert cat["inclusion_signals"] == ["switch", "router"] and cat["exclusion_signals"] == []
        assert cat["tag_taxonomy"] == ["Redes", "Switches"]

    def test_sin_cambios_no_crea_version_ni_audita(self, monkeypatch, mundo):
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/profile/rules",
                  context_note="nota", categories="hardware, software", version_base="1")
        assert r.status_code == 303 and _aviso(r) == "admin_sin_cambios"
        assert len(mundo.adb.config) == 1 and mundo.auditados == []

    def test_conflicto_si_cambio_la_version_activa(self, monkeypatch, mundo):
        c = _web(monkeypatch, ADMIN_ORG)
        # Alguien más guarda (v2) mientras esta persona editaba sobre la v1.
        assert _aviso(_post(c, CLI + "/acme/profile/rules", context_note="de la otra persona",
                            categories="otro", version_base="1")) == "admin_ok"
        mundo.auditados.clear()
        r = _post(c, CLI + "/acme/profile/rules", context_note="mi cambio", categories="otro", version_base="1")
        assert r.status_code == 303 and _aviso(r) == "admin_conflicto"
        assert mundo.adb.activa()["version"] == 2
        assert mundo.adb.activa()["contenido"]["rules"]["context_note"] == "de la otra persona"
        assert mundo.auditados == []

    @pytest.mark.parametrize("seccion, campos", [
        ("rules", {"context_note": "x", "categories": "a\nb"}),                            # salto de línea
        ("rules", {"context_note": "x", "categories": ""}),
        ("rules", {"context_note": "x" * 5001, "categories": "otro"}),
        ("thresholds", {"relevance_threshold": "1.5", "confidence_threshold": "0.5"}),
        ("thresholds", {"relevance_threshold": "nan", "confidence_threshold": "0.5"}),
        ("thresholds", {"relevance_threshold": "abc", "confidence_threshold": "0.5"}),
        ("email", {"report_title": "T", "subject_prefix": "[X]\nBcc: a@b", "intro_message": ""}),    # cabecera
        ("email", {"report_title": "", "subject_prefix": "[X]", "intro_message": ""}),
        ("feedback", {"motivos": "sin separador"}),
        ("feedback", {"motivos": "Malo Id | x"}),
        ("feedback", {"motivos": "a_b | uno\na_b | dos"}),
        ("catalog", {"categories": "{no json", "inclusion_signals": "", "exclusion_signals": "", "tag_taxonomy": ""}),
        ("catalog", {"categories": '{"id": "x"}', "inclusion_signals": "", "exclusion_signals": "", "tag_taxonomy": ""}),
        ("catalog", {"categories": '[{"label": "sin id"}]', "inclusion_signals": "", "exclusion_signals": "", "tag_taxonomy": ""}),
        ("catalog", {"categories": '[{"id": "a", "label": "A", "items": "no-lista"}]', "inclusion_signals": "",
                     "exclusion_signals": "", "tag_taxonomy": ""}),
        ("catalog", {"categories": '[{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]', "inclusion_signals": "",
                     "exclusion_signals": "", "tag_taxonomy": ""}),
        ("catalog", {"categories": '[{"id": "a", "label": "A", "brands": [{"id": "m"}]}]', "inclusion_signals": "",
                     "exclusion_signals": "", "tag_taxonomy": ""}),
    ])
    def test_validacion_rechazada_no_escribe(self, monkeypatch, mundo, seccion, campos):
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + f"/acme/profile/{seccion}", version_base="1", **campos)
        assert r.status_code == 303 and _aviso(r) == "admin_invalido"
        assert len(mundo.adb.config) == 1 and mundo.adb.activa()["contenido"] == _perfil()
        assert mundo.auditados == []

    def test_perfil_que_rechaza_el_parser_no_se_guarda(self, monkeypatch, mundo):
        # El perfil activo está dañado para el parser (sin `catalog`): ninguna edición lo "arregla" a medias.
        mundo.adb.config[0]["contenido"] = {"client": {"id": "acme", "name": "Acme"}}
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/profile/rules", context_note="x", categories="otro",
                  version_base="1")
        assert _aviso(r) == "admin_invalido" and len(mundo.adb.config) == 1

    @pytest.mark.parametrize("base", ["", "abc", "0", "-1", "99999999"])
    def test_version_base_invalida(self, monkeypatch, mundo, base):
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/profile/rules", context_note="x", categories="otro",
                  version_base=base)
        assert _aviso(r) in ("admin_invalido", "admin_conflicto") and len(mundo.adb.config) == 1

    def test_el_texto_libre_se_muestra_escapado(self, monkeypatch, mundo):
        mundo.adb.config[0]["contenido"]["rules"]["context_note"] = "<script>alert(1)</script>"
        mundo.adb.config[0]["contenido"]["intelligence"]["profile"] = "<img src=x onerror=alert(2)>"
        c = _web(monkeypatch, ADMIN_ORG)
        html = c.get(CLI + "/acme/profile/rules").text
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        html = c.get(CLI + "/acme/profile/intelligence").text
        assert "<img src=x" not in html and "&lt;img src=x" in html

    def test_el_formulario_trae_la_version_base(self, monkeypatch, mundo):
        html = _web(monkeypatch, ADMIN_ORG).get(CLI + "/acme/profile/thresholds").text
        assert 'name="version_base" value="1"' in html and 'name="csrf"' in html


# ── Datos generales ─────────────────────────────────────────────────────────────

class TestGeneral:
    def test_edita_nombre_locale_y_zona_en_columna_y_json(self, monkeypatch, mundo):
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/general", nombre="Acme Costa Rica",
                  locale="es-CR", zona_horaria="America/Costa_Rica", version_base="1")
        assert _aviso(r) == "admin_ok"
        assert mundo.adb.clientes["acme"] == {"org_id": "org", "nombre": "Acme Costa Rica", "activo": 1,
                                              "locale": "es-CR", "zona": "America/Costa_Rica"}
        assert mundo.adb.activa()["contenido"]["client"]["name"] == "Acme Costa Rica"
        assert [a for a, _ in mundo.auditados] == ["PERFIL_VERSION_CREADA"]

    @pytest.mark.parametrize("nombre, locale, zona", [
        ("", "es-SV", "America/El_Salvador"), ("N", "es_SV", "America/El_Salvador"), ("N", "es-SV", "Marte/Olimpo"),
        ("N", "es-SV", "../etc/passwd"), ("N\nX", "es-SV", "America/El_Salvador"),
    ])
    def test_invalido(self, monkeypatch, mundo, nombre, locale, zona):
        r = _post(_web(monkeypatch, ADMIN_ORG), CLI + "/acme/general", nombre=nombre, locale=locale,
                  zona_horaria=zona, version_base="1")
        assert _aviso(r) == "admin_invalido" and mundo.adb.clientes["acme"]["nombre"] == "Acme"
        assert mundo.auditados == []

    def test_conflicto(self, monkeypatch, mundo):
        c = _web(monkeypatch, ADMIN_ORG)
        _post(c, CLI + "/acme/profile/rules", context_note="otro", categories="otro", version_base="1")
        r = _post(c, CLI + "/acme/general", nombre="N", locale="es-SV", zona_horaria="America/El_Salvador",
                  version_base="1")
        assert _aviso(r) == "admin_conflicto" and mundo.adb.clientes["acme"]["nombre"] == "Acme"


# ── Alta, activar y desactivar ──────────────────────────────────────────────────

class TestClientes:
    def test_plataforma_crea_cliente_con_version_1_y_audita(self, monkeypatch, mundo):
        r = _post(_web(monkeypatch, PLATAFORMA), CLI + "/create", cliente_id="multicomputos_pa", nombre="Panamá",
                  portal="comprasal_sv", org_id="org")
        assert _aviso(r) == "admin_ok" and r.headers["location"].startswith(CLI)
        assert mundo.adb.clientes["multicomputos_pa"]["org_id"] == "org"
        f = mundo.adb.activa("multicomputos_pa")
        assert f["version"] == 1 and f["creado_por"] == "admin@example.com"
        assert f["contenido"]["client"]["name"] == "Panamá"
        assert [a for a, _ in mundo.auditados] == ["CLIENTE_CREADO"]
        assert mundo.auditados[0][1]["recurso"] == "cliente=multicomputos_pa"

    @pytest.mark.parametrize("datos", [
        {"cliente_id": "acme", "nombre": "Dup", "portal": "comprasal_sv", "org_id": "org"},          # existe
        {"cliente_id": "multicomputos", "nombre": "Alias", "portal": "comprasal_sv", "org_id": "org"},   # alias viejo
        {"cliente_id": "Mal Id", "nombre": "N", "portal": "comprasal_sv", "org_id": "org"},
        {"cliente_id": "../x", "nombre": "N", "portal": "comprasal_sv", "org_id": "org"},
        {"cliente_id": "nuevo_pa", "nombre": "", "portal": "comprasal_sv", "org_id": "org"},
        {"cliente_id": "nuevo_pa", "nombre": "N", "portal": "no_existe", "org_id": "org"},
        {"cliente_id": "nuevo_pa", "nombre": "N", "portal": "comprasal_sv", "org_id": "no_existe"},
    ])
    def test_alta_invalida(self, monkeypatch, mundo, datos):
        r = _post(_web(monkeypatch, PLATAFORMA), CLI + "/create", **datos)
        assert _aviso(r) == "admin_invalido" and set(mundo.adb.clientes) == {"acme"} and mundo.auditados == []

    def test_desactivar_y_activar_mantienen_columna_y_json_juntos(self, monkeypatch, mundo):
        c = _web(monkeypatch, PLATAFORMA)
        r = _post(c, CLI + "/acme/deactivate", version_base="1")
        assert _aviso(r) == "admin_ok"
        assert mundo.adb.clientes["acme"]["activo"] == 0 and mundo.adb.activa()["contenido"]["client"]["active"] is False
        assert mundo.adb.activa()["version"] == 2
        _post(c, CLI + "/acme/activate", version_base="2")
        assert mundo.adb.clientes["acme"]["activo"] == 1 and mundo.adb.activa()["contenido"]["client"]["active"] is True
        assert [a for a, _ in mundo.auditados] == ["CLIENTE_DESACTIVADO", "CLIENTE_ACTIVADO"]

    def test_activar_con_version_vieja_es_conflicto(self, monkeypatch, mundo):
        c = _web(monkeypatch, PLATAFORMA)
        _post(c, CLI + "/acme/profile/rules", context_note="x", categories="otro", version_base="1")
        r = _post(c, CLI + "/acme/deactivate", version_base="1")
        assert _aviso(r) == "admin_conflicto" and mundo.adb.clientes["acme"]["activo"] == 1

    def test_la_ficha_ofrece_el_boton_segun_el_estado_y_el_rol(self, monkeypatch, mundo):
        assert "/acme/deactivate" in _web(monkeypatch, PLATAFORMA).get(CLI + "/acme").text
        assert "/acme/deactivate" not in _web(monkeypatch, ADMIN_ORG).get(CLI + "/acme").text


# ── Versiones: historial, comparar, revertir ────────────────────────────────────

class TestVersiones:
    def _dos_versiones(self, monkeypatch, mundo):
        c = _web(monkeypatch, ADMIN_ORG)
        _post(c, CLI + "/acme/profile/rules", context_note="segunda", categories="otro", version_base="1")
        mundo.auditados.clear()
        return c

    def test_historial_y_comparacion(self, monkeypatch, mundo):
        c = self._dos_versiones(monkeypatch, mundo)
        html = c.get(CLI + "/acme/versions").text
        assert "ACTIVA" in html and "Revertir a esta" in html and "admin@example.com" in html
        diff = c.get(CLI + "/acme/versions?a=1&b=2").text
        assert "-    &#34;context_note&#34;: &#34;nota&#34;," in diff or "context_note" in diff
        assert "segunda" in diff and "perfil entrenado" not in diff            # la inteligencia no entra en el diff

    def test_comparar_inexistente_o_basura(self, monkeypatch, mundo):
        c = self._dos_versiones(monkeypatch, mundo)
        assert c.get(CLI + "/acme/versions?a=1&b=99").status_code == 404
        assert c.get(CLI + "/acme/versions?a=x&b=y").status_code == 200       # se ignora, solo el historial

    def test_revertir_crea_version_nueva_con_el_contenido_antiguo(self, monkeypatch, mundo):
        c = self._dos_versiones(monkeypatch, mundo)
        r = _post(c, CLI + "/acme/versions/1/revert", version_base="2")
        assert r.status_code == 303 and _aviso(r) == "admin_ok" and r.headers["location"].startswith(CLI + "/acme/versions")
        activa = mundo.adb.activa()
        assert activa["version"] == 3 and activa["contenido"] == _perfil()
        assert [f["version"] for f in mundo.adb.config] == [1, 2, 3]            # nunca borra
        assert [a for a, _ in mundo.auditados] == ["PERFIL_VERSION_REVERTIDA"]
        assert "version=1" in mundo.auditados[0][1]["recurso"]

    def test_revertir_a_lo_mismo_no_hace_nada(self, monkeypatch, mundo):
        c = _web(monkeypatch, ADMIN_ORG)
        mundo.adb.config.append({"cliente_id": "acme", "version": 2, "contenido": _perfil(), "activa": 0, "creado_por": "x"})
        mundo.adb.config[0]["activa"] = 0
        mundo.adb.config[1]["activa"] = 1
        r = _post(c, CLI + "/acme/versions/1/revert", version_base="2")
        assert _aviso(r) == "admin_sin_cambios" and len(mundo.adb.config) == 2 and mundo.auditados == []

    def test_revertir_con_version_base_vieja_es_conflicto(self, monkeypatch, mundo):
        c = self._dos_versiones(monkeypatch, mundo)
        r = _post(c, CLI + "/acme/versions/1/revert", version_base="1")
        assert _aviso(r) == "admin_conflicto" and mundo.adb.activa()["version"] == 2 and mundo.auditados == []

    @pytest.mark.parametrize("numero", ["99", "0", "abc"])
    def test_revertir_a_una_version_que_no_existe(self, monkeypatch, mundo, numero):
        c = self._dos_versiones(monkeypatch, mundo)
        r = _post(c, CLI + f"/acme/versions/{numero}/revert", version_base="2")
        assert r.status_code in (404, 303) and mundo.adb.activa()["version"] == 2 and mundo.auditados == []

    def test_el_historial_escapa_creado_por(self, monkeypatch, mundo):
        mundo.adb.config[0]["creado_por"] = "<b>x</b>"
        html = _web(monkeypatch, ADMIN_ORG).get(CLI + "/acme/versions").text
        assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_servicio_trunca_creado_por_a_64_bytes(monkeypatch):
    from src.identidad import repo as identidad_repo
    monkeypatch.setattr(identidad_repo, "obtener_usuario", lambda conn, uid: {"email": "ñ" * 60 + "@example.com"})
    texto = servicio.creado_por(object(), PLATAFORMA)
    assert len(texto.encode("utf-8")) <= 64


def test_repo_listar_filtra_por_organizacion():
    adb = ADB()
    adb.sembrar("a", org="o1")
    adb.sembrar("b", org="o2")
    assert [c["cliente_id"] for c in repo.listar(adb)] == ["a", "b"]
    assert [c["cliente_id"] for c in repo.listar(adb, "o2")] == ["b"]


def test_el_aviso_sin_cambios_esta_registrado():
    assert admin_clientes.admin.AVISOS_ADMIN["sin_cambios"] == "admin_sin_cambios"
