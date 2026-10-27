"""E10 «ver como»: resolución efectiva/real, `POST /portal/ver-como` y solo lectura (capa web).

La lógica de dominio (`opciones`, `validar`, `iniciar`...) vive en `src.identidad.ver_como` y tiene sus
propios tests; acá se simula y se prueba lo que la web hace con ella: quién puede pedirla, que nada escriba
bajo simulación, y que una falla al leerla nunca rompa el portal.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_web_portal import _pagina_stub, _parchear_e11

from src.identidad import alcance as alcance_mod
from src.identidad import politica, repo, service, sesiones
from src.identidad.alcance import Alcance
from src.oportunidades import service as oportunidades_service
from src.web import acceso, csrf, portal, sesion
from src.web.app import crear_app
from src.web.limitador import Limitador

_A = Alcance

BASE = "https://dev.amalia.test"
SECRETO = "secreto-de-prueba"
ORIGEN = {"origin": BASE}


@pytest.fixture(autouse=True)
def _politica(monkeypatch):
    """Constantes de E10 (stub hasta reconciliar; con el valor real, setattr no cambia nada)."""
    monkeypatch.setattr(politica, "ROLES_VER_COMO", frozenset({"admin_plataforma"}), raising=False)
    monkeypatch.setattr(politica, "TTL_VER_COMO_MIN", 60, raising=False)


def _cfg(**c):
    base = dict(enable_portal=True, portal_cliente_id="cli", enable_feedback=False, daily_budget_usd=5.0,
                identidad_secret=SECRETO, feedback_base_url=BASE, clients_dir="clients",
                sesion_ttl_horas=12, sesion_max_dias=7)
    base.update(c)
    return SimpleNamespace(**base)


def _alc(roles, clientes=(), cambia=(), operativo=False, simulado=None):
    return _A(usuario_id="u-admin" if "admin_plataforma" in roles or simulado else "u1", org_id="org",
              roles=frozenset(roles), clientes=frozenset(clientes), cambia_estado=frozenset(cambia),
              ve_operativo=operativo, simulado=simulado)


ADMIN = _alc(("admin_plataforma",), operativo=True)
GERENTE_SV = _alc(("gerente_pais",), ("sv",), ("sv",))


def _simulado(rol, clientes=(), cambia=(), operativo=False):
    """El alcance que `alcance_simulado` le daría al admin: usuario real, rol simulado."""
    return _alc((rol,), clientes, cambia, operativo, simulado=rol)


@contextmanager
def _conexion(cfg):
    yield object()


@pytest.fixture
def mundo(monkeypatch):
    """KPIs, auditoría y conexión simulados; registra lo que se auditó y lo que se llamó."""
    m = SimpleNamespace(auditados=[], iniciar=[], terminar=[], opciones=0, cerradas=[], conexiones=0)

    @contextmanager
    def conexion(cfg):
        m.conexiones += 1
        yield object()

    monkeypatch.setattr(portal.db, "connection", conexion)
    monkeypatch.setattr(acceso.db, "connection", conexion)
    monkeypatch.setattr(sesion, "_pendiente", lambda c, token: False)
    monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria",
                        lambda conn, org, accion, **kw: m.auditados.append((accion, kw)))
    monkeypatch.setattr(portal.pais, "oportunidades_del_mes", lambda c, cl: {"valor": 1, "delta_pct": None,
                                                                             "serie": [1, 2]})
    monkeypatch.setattr(portal.pais, "pct_relevancia_clasificador",
                        lambda c, cl, dias: {"valor": 0.0, "total": 0, "relevantes": 0})
    monkeypatch.setattr(portal.pais, "instituciones_top", lambda c, cl, dias: [])
    monkeypatch.setattr(portal.pais, "embudo_pipeline", lambda c, cl, dias: {"camino": [], "salidas": []})
    monkeypatch.setattr(portal.pais, "severidad_riesgos", lambda c, cl, dias: [])
    monkeypatch.setattr(portal.mercado_repo, "listar_conflictos_oportunidad_v2", lambda c, cl: [])
    monkeypatch.setattr(portal.pais, "conversion_comercial", lambda c, cl, dias: {
        "detectadas": 0, "etapas": [], "perdido": 0, "descartado": 0,
        "conv_ofertado_pct": None, "conv_ganado_pct": None})
    monkeypatch.setattr(portal.pais, "tiempo_por_etapa", lambda c, cl, dias: [])
    # E11: la vista de país lee `analytics.tablero` y se pinta con el stub del contrato.
    _parchear_e11(monkeypatch)
    _pagina_stub(monkeypatch)
    monkeypatch.setattr(portal.regional, "nombres_clientes",
                        lambda c, cls: {x: {"sv": "El Salvador", "gt": "Guatemala"}.get(x, x) for x in cls})
    monkeypatch.setattr(portal.operativo, "estado_ultima_corrida", lambda c: {"estado": None})
    monkeypatch.setattr(portal.operativo, "gasto_llm_hoy", lambda c, tope: {
        "gasto_usd": 0.0, "tope_usd": tope, "pct": 0.0, "llamadas": 0})
    monkeypatch.setattr(portal.operativo, "latencia_y_errores_llm", lambda c, dias: [])

    def opciones(conn, real):
        m.opciones += 1
        return [SimpleNamespace(valor="ceo", etiqueta="CEO", grupo="Roles de organización"),
                SimpleNamespace(valor="gerente_pais|sv", etiqueta="Gerente de país · El Salvador",
                                grupo="Roles de país")]

    monkeypatch.setattr(portal.ver_como, "opciones", opciones)
    monkeypatch.setattr(portal.ver_como, "etiqueta",
                        lambda rol, cliente_nombre=None: rol + (f" · {cliente_nombre}" if cliente_nombre else ""))

    def iniciar(conn, real, token_hash, vista, ip_hash):
        m.iniciar.append((real, token_hash, vista, ip_hash))
        return vista in {"ceo", "gerente_pais|sv"}

    def terminar(conn, real, token_hash, ip_hash):
        m.terminar.append((real, token_hash, ip_hash))
        return True

    monkeypatch.setattr(portal.ver_como, "iniciar", iniciar)
    monkeypatch.setattr(portal.ver_como, "terminar", terminar)
    monkeypatch.setattr(service, "cerrar_sesion", lambda conn, token: m.cerradas.append(token) or True)
    return m


def _cli(monkeypatch, real, efectivo=None, cliente=None, desde=None, cfg=None):
    """Cliente con la cookie `t` resuelta a (efectivo, real) sin tocar la base."""
    res = sesion.Resolucion(efectivo or real, real, cliente, desde)
    monkeypatch.setattr(sesion, "_resolver", lambda c, token: res if token == "t" else None)
    c = TestClient(crear_app(cfg or _cfg()), base_url=BASE, follow_redirects=False)
    c.cookies.set(sesion.COOKIE, "t")
    return c


def _csrf():
    return csrf.emitir(SECRETO, sesiones.hash_token("t"))


def _no_existe(monkeypatch):
    return TestClient(crear_app(_cfg()), base_url=BASE).get("/no-hay-ruta").text


# ── Middleware: alcance efectivo vs real ───────────────────────────────────────

class TestResolucion:
    @pytest.fixture
    def base(self, monkeypatch):
        """`_resolver` real con la base simulada: usuario `u-admin`, sesión viva."""
        m = SimpleNamespace(real=ADMIN, vigente=None, simulado=None, consultas=[], simulaciones=[])
        monkeypatch.setattr(sesion.db, "connection", _conexion)
        monkeypatch.setattr(sesion.service, "usuario_de_sesion", lambda conn, token: "u-admin")
        monkeypatch.setattr(sesion.repo, "renovar_sesion", lambda *a: None)
        monkeypatch.setattr(sesion.alcance_mod, "resolver_alcance", lambda conn, u: m.real)

        def obtener(conn, token_hash, ttl_min):
            m.consultas.append((token_hash, ttl_min))
            if isinstance(m.vigente, Exception):
                raise m.vigente
            return m.vigente

        def simular(conn, real, rol, cliente):
            m.simulaciones.append((real, rol, cliente))
            return m.simulado

        monkeypatch.setattr(repo, "obtener_ver_como", obtener, raising=False)
        monkeypatch.setattr(alcance_mod, "alcance_simulado", simular, raising=False)
        return m

    def test_admin_con_simulacion_vigente_ve_el_alcance_simulado(self, base):
        desde = datetime(2026, 10, 26, 12, 0)
        base.vigente = ("gerente_pais", "sv", desde)
        base.simulado = _simulado("gerente_pais", ("sv",), ("sv",))
        r = sesion._resolver(_cfg(), "t")
        assert r == sesion.Resolucion(base.simulado, ADMIN, "sv", desde)
        assert base.consultas == [(sesiones.hash_token("t"), 60)]
        assert base.simulaciones == [(ADMIN, "gerente_pais", "sv")]

    def test_admin_sin_simulacion_ve_lo_real(self, base):
        assert sesion._resolver(_cfg(), "t") == sesion.Resolucion(ADMIN, ADMIN)
        assert base.simulaciones == []

    def test_simulacion_invalida_se_ignora(self, base):
        # Dejó de ser admin a mitad, el país se desactivó, combinación inválida...: la vista real.
        base.vigente = ("gerente_pais", "gt", datetime(2026, 10, 26))
        base.simulado = None
        assert sesion._resolver(_cfg(), "t") == sesion.Resolucion(ADMIN, ADMIN)

    @pytest.mark.parametrize("falla", ["obtener", "simular"])
    def test_cualquier_falla_cae_a_la_vista_real(self, base, monkeypatch, falla):
        """Imagen nueva con el DDL de E10 sin aplicar (ORA-00904): el admin entra igual."""
        if falla == "obtener":
            base.vigente = RuntimeError("ORA-00904: VER_COMO_ROL: identificador no válido")
        else:
            base.vigente = ("ceo", None, datetime(2026, 10, 26))
            monkeypatch.setattr(alcance_mod, "alcance_simulado",
                                lambda *a: (_ for _ in ()).throw(RuntimeError("boom")), raising=False)
        assert sesion._resolver(_cfg(), "t") == sesion.Resolucion(ADMIN, ADMIN)

    def test_quien_no_es_admin_ni_se_consulta_aunque_haya_columnas_pobladas(self, base):
        base.real = GERENTE_SV
        base.vigente = ("ceo", None, datetime(2026, 10, 26))  # escrito a mano en la base
        base.simulado = _simulado("ceo", ("sv", "gt"))
        assert sesion._resolver(_cfg(), "t") == sesion.Resolucion(GERENTE_SV, GERENTE_SV)
        assert base.consultas == [] and base.simulaciones == []

    def test_admin_con_otro_rol_la_simulacion_reemplaza_todo(self, base):
        base.real = _alc(("admin_plataforma", "ceo"), ("sv", "gt"), operativo=True)
        base.vigente = ("equipo_ventas", "sv", datetime(2026, 10, 26))
        base.simulado = _simulado("equipo_ventas", ("sv",), ("sv",))
        assert sesion._resolver(_cfg(), "t").efectivo is base.simulado

    def test_sin_db_de_e10_el_admin_ve_operativo_sin_500(self, base, mundo, monkeypatch):
        base.vigente = RuntimeError("ORA-00904")
        c = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False)
        c.cookies.set(sesion.COOKIE, "t")
        assert c.get("/portal/operativo").status_code == 200

    def test_el_middleware_deja_efectivo_real_desde_y_token(self, monkeypatch):
        desde = datetime(2026, 10, 26, 12, 0)
        sim = _simulado("ceo", ("sv",))
        monkeypatch.setattr(sesion, "_resolver", lambda c, t: sesion.Resolucion(sim, ADMIN, None, desde))
        monkeypatch.setattr(sesion, "_pendiente", lambda c, t: False)
        app = crear_app(_cfg())

        @app.get("/portal/_eco")
        def eco(request: portal.Request):
            e = request.scope["state"]
            return {"efectivo": e["alcance"].simulado, "real": sorted(e["alcance_real"].roles),
                    "desde": e["ver_como_desde"].isoformat(), "token": e["sesion_token"],
                    "real_de": sorted(sesion.alcance_real_de(request).roles)}

        c = TestClient(app, base_url=BASE)
        c.cookies.set(sesion.COOKIE, "t")
        assert c.get("/portal/_eco").json() == {"efectivo": "ceo", "real": ["admin_plataforma"],
                                                "desde": desde.isoformat(), "token": "t",
                                                "real_de": ["admin_plataforma"]}


# ── Las vistas bajo simulación son las de esa persona ──────────────────────────

class TestVistas:
    def test_admin_simulando_un_gerente_ve_su_pais(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv")
        r = c.get("/portal")
        assert r.status_code == 200 and "96-2026-P0011" in r.text  # la vista de país de `sv`
        # ...y sin simulación lo suyo es lo operativo.
        assert _cli(monkeypatch, ADMIN).get("/portal").headers["location"] == "/portal/operativo"

    def test_operativo_bajo_simulacion_de_ceo_es_404(self, mundo, monkeypatch):
        r = _cli(monkeypatch, ADMIN, _simulado("ceo", ("sv", "gt"))).get("/portal/operativo")
        assert r.status_code == 404 and r.text == _no_existe(monkeypatch)

    def test_operador_simulado_solo_ve_operativo(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN, _simulado("operador", operativo=True))
        assert c.get("/portal").headers["location"] == "/portal/operativo"
        assert c.get("/portal?cliente=sv").status_code == 404

    def test_cliente_ajeno_bajo_simulacion_es_404_y_la_auditoria_lleva_la_marca(self, mundo, monkeypatch):
        r = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv").get("/portal?cliente=gt")
        assert r.status_code == 404 and r.text == _no_existe(monkeypatch)
        assert mundo.auditados == [("ACCESO_FUERA_DE_ALCANCE",
                                    {"usuario_id": "u-admin", "recurso": "cliente=gt [ver_como=gerente_pais]"})]

    def test_contexto_de_la_navegacion(self, mundo, monkeypatch):
        capturado = {}
        real_pagina = portal.pagina

        def pagina(request, plantilla, **ctx):
            capturado.update(ctx)
            return real_pagina(request, plantilla, **ctx)

        monkeypatch.setattr(portal, "pagina", pagina)
        desde = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10, seconds=5)
        r = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv", desde).get("/portal")
        assert r.status_code == 200
        vc = capturado["ver_como"]
        assert vc["puede"] is True and [o.valor for o in vc["opciones"]] == ["ceo", "gerente_pais|sv"]
        assert vc["simulando"] == "gerente_pais · El Salvador" and vc["restante_min"] == 49
        assert capturado["solo_vista"] is True and capturado["puede_cambiar"] is True
        assert capturado["csrf"]

    def test_quien_no_es_admin_no_recibe_selector_ni_se_calculan_opciones(self, mundo, monkeypatch):
        capturado = {}
        real_pagina = portal.pagina
        monkeypatch.setattr(portal, "pagina", lambda req, pl, **ctx: capturado.update(ctx) or real_pagina(req, pl, **ctx))
        assert _cli(monkeypatch, GERENTE_SV).get("/portal").status_code == 200
        assert capturado["ver_como"] == {"puede": False, "opciones": [], "simulando": None, "valor": None, "restante_min": None}
        assert capturado["solo_vista"] is False and mundo.opciones == 0

    def test_si_las_opciones_fallan_la_pagina_sale_igual(self, mundo, monkeypatch):
        def boom(conn, real):
            raise RuntimeError("ORA-00904")

        monkeypatch.setattr(portal.ver_como, "opciones", boom)
        assert _cli(monkeypatch, ADMIN).get("/portal/operativo").status_code == 200

    def test_restante_vencido_y_con_zona(self):
        assert portal._restante_min(None) is None
        assert portal._restante_min(datetime.now(timezone.utc) - timedelta(minutes=90)) == 0
        assert portal._restante_min(datetime.now(timezone.utc) + timedelta(minutes=5)) == 60

    def test_el_aviso_de_simulacion_nombra_la_vista(self, mundo, monkeypatch):
        r = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv").get(
            "/portal?cliente=sv&aviso=simulacion")
        assert "Estás viendo como gerente_pais · El Salvador: la vista es de solo lectura." in r.text

    def test_el_aviso_sobrevive_a_la_redireccion_de_portal(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN)
        assert c.get("/portal?aviso=ver_como_fin").headers["location"] == "/portal/operativo?aviso=ver_como_fin"
        assert c.get("/portal?aviso=<x>").headers["location"] == "/portal/operativo"


# ── POST /portal/ver-como ──────────────────────────────────────────────────────

class TestElegirVista:
    RUTA = "/portal/ver-como"

    def _post(self, c, vista, csrf_token=None, headers=ORIGEN):
        return c.post(self.RUTA, data={"vista": vista, "csrf": csrf_token or _csrf()}, headers=headers)

    def test_exito_redirige_a_portal_y_pasa_el_real_y_los_hashes(self, mundo, monkeypatch):
        r = self._post(_cli(monkeypatch, ADMIN), "gerente_pais|sv")
        assert r.status_code == 303 and r.headers["location"] == "/portal?aviso=ver_como_ok"
        real, token_hash, vista, ip_hash = mundo.iniciar[0]
        assert real is ADMIN and token_hash == sesiones.hash_token("t") and vista == "gerente_pais|sv"
        assert ip_hash and ip_hash != "testclient" and len(ip_hash) == 64

    def test_se_autoriza_con_el_real_aunque_ya_este_simulando(self, mundo, monkeypatch):
        r = self._post(_cli(monkeypatch, ADMIN, _simulado("equipo_ventas", ("sv",), ("sv",)), "sv"), "ceo")
        assert r.headers["location"] == "/portal?aviso=ver_como_ok" and mundo.iniciar[0][0] is ADMIN

    @pytest.mark.parametrize("vista", ["admin_plataforma", "gerente_pais", "ceo|sv", "gerente_pais|sv|x",
                                       "x" * 5000, "gerente_pais|gt", " ceo"])
    def test_vista_invalida_no_cambia_nada(self, mundo, monkeypatch, vista):
        r = self._post(_cli(monkeypatch, ADMIN), vista)
        assert r.status_code == 303 and r.headers["location"] == "/portal?aviso=ver_como_invalido"
        assert mundo.iniciar[0][2] == vista and mundo.terminar == []  # se valida en el dominio, sin recortar

    def test_vista_vacia_termina(self, mundo, monkeypatch):
        r = self._post(_cli(monkeypatch, ADMIN, _simulado("ceo", ("sv",))), "")
        assert r.headers["location"] == "/portal?aviso=ver_como_fin"
        assert mundo.terminar == [(ADMIN, sesiones.hash_token("t"), mundo.terminar[0][2])] and mundo.iniciar == []

    @pytest.mark.parametrize("caso", ["sin_csrf", "csrf_ajeno", "sin_origen", "otro_origen"])
    def test_csrf_u_origen_ausentes_es_403(self, mundo, monkeypatch, caso):
        c = _cli(monkeypatch, ADMIN)
        if caso == "sin_csrf":
            r = c.post(self.RUTA, data={"vista": "ceo"}, headers=ORIGEN)
        elif caso == "csrf_ajeno":
            r = self._post(c, "ceo", csrf.emitir(SECRETO, "anon"))
        elif caso == "sin_origen":
            r = self._post(c, "ceo", headers={})
        else:
            r = self._post(c, "ceo", headers={"origin": "https://evil.test"})
        assert r.status_code == 403 and mundo.iniciar == [] and mundo.terminar == []

    @pytest.mark.parametrize("real", [GERENTE_SV, _alc(("ceo",), ("sv", "gt")), _alc(("operador",), operativo=True),
                                      _alc(("admin_organizacion",), ("sv",))])
    def test_quien_no_puede_simular_recibe_el_404_de_siempre_y_se_audita(self, mundo, monkeypatch, real):
        r = self._post(_cli(monkeypatch, real), "ceo")
        assert r.status_code == 404 and r.text == _no_existe(monkeypatch)
        assert mundo.auditados == [("ACCESO_FUERA_DE_ALCANCE", {"usuario_id": "u1", "recurso": "ver-como"})]
        assert mundo.iniciar == [] and mundo.terminar == []

    def test_sin_sesion_va_al_acceso(self, mundo, monkeypatch):
        monkeypatch.setattr(sesion, "_resolver", lambda c, t: None)
        c = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False)
        r = c.post(self.RUTA, data={"vista": "ceo"}, headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"].startswith("/acceso") and mundo.iniciar == []

    def test_limite_de_tasa(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN)
        c.app.state.limitador_estado = Limitador(maximo=2, ventana_seg=600)
        assert [self._post(c, "ceo").status_code for _ in range(3)] == [303, 303, 429]
        assert len(mundo.iniciar) == 2

    def test_adb_caida_es_503(self, mundo, monkeypatch):
        def boom(*a):
            raise RuntimeError("adb caída")

        monkeypatch.setattr(portal.ver_como, "iniciar", boom)
        r = self._post(_cli(monkeypatch, ADMIN), "ceo")
        assert r.status_code == 503 and "adb" not in r.text.lower()


# ── Solo lectura ───────────────────────────────────────────────────────────────

class TestSoloLectura:
    RUTA = "/portal/oportunidad/comprasal_sv%3A96-2026-P0011/estado"

    def test_el_post_de_estado_bajo_simulacion_no_llega_al_servicio_ni_a_la_base(self, mundo, monkeypatch):
        llamadas = []
        monkeypatch.setattr(portal.oportunidades, "cambiar_estado", lambda *a, **k: llamadas.append(a) or "ok")
        c = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv")
        antes = mundo.conexiones
        r = c.post(self.RUTA, data={"cliente": "sv", "estado": "EVALUANDO", "actualizado_en": "", "dias": "30",
                                    "csrf": _csrf()}, headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"] == "/portal?cliente=sv&dias=30&aviso=simulacion"
        assert llamadas == [] and mundo.conexiones == antes and mundo.auditados == []

    @pytest.mark.parametrize("accion,servicio,datos", [
        ("responsable", "asignar_responsable", {"responsable": "u2"}),
        ("recuperar", "recuperar_descartado", {}),
    ])
    def test_las_escrituras_de_e11_bajo_simulacion_no_llegan_al_servicio_ni_a_la_base(
            self, mundo, monkeypatch, accion, servicio, datos):
        llamadas = []
        monkeypatch.setattr(portal.oportunidades, servicio, lambda *a, **k: llamadas.append(a) or "ok",
                            raising=False)
        c = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv")
        antes = mundo.conexiones
        r = c.post(f"/portal/oportunidad/comprasal_sv%3A96-2026-P0011/{accion}",
                   data={"cliente": "sv", "actualizado_en": "", "dias": "30", "csrf": _csrf(), **datos},
                   headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"] == "/portal?cliente=sv&dias=30&aviso=simulacion"
        assert llamadas == [] and mundo.conexiones == antes and mundo.auditados == []

    def test_bajo_simulacion_sin_csrf_sigue_siendo_403(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN, _simulado("gerente_pais", ("sv",), ("sv",)), "sv")
        assert c.post(self.RUTA, data={"cliente": "sv", "csrf": "1.malo"}, headers=ORIGEN).status_code == 403

    def test_el_servicio_devuelve_simulacion_sin_tocar_la_conexion(self, monkeypatch):
        class Intocable:
            def __getattr__(self, nombre):
                raise AssertionError(f"tocó la conexión: {nombre}")

        def prohibido(*a, **k):
            raise AssertionError("consultó la base")

        monkeypatch.setattr(oportunidades_service.repo, "proceso_visible", prohibido)
        monkeypatch.setattr(oportunidades_service.repo, "obtener", prohibido)
        sim = _simulado("director_comercial", ("sv",), ("sv",))
        assert oportunidades_service.cambiar_estado(
            Intocable(), sim, "sv", "comprasal_sv:1", "EVALUANDO", "") == oportunidades_service.SIMULACION


# ── Lo que no cambia ───────────────────────────────────────────────────────────

class TestSesionBajoSimulacion:
    def test_acceso_con_alcance_sigue_redirigiendo(self, mundo, monkeypatch):
        r = _cli(monkeypatch, ADMIN, _simulado("ceo", ("sv",))).get("/acceso?siguiente=/portal/regional")
        assert r.status_code == 303 and r.headers["location"] == "/portal/regional"

    def test_salir_bajo_simulacion_revoca_la_sesion_real(self, mundo, monkeypatch):
        c = _cli(monkeypatch, ADMIN, _simulado("ceo", ("sv",)))
        r = c.post("/salir", data={"csrf": _csrf()}, headers=ORIGEN)
        assert r.status_code == 303 and r.headers["location"] == "/acceso"
        assert mundo.cerradas == ["t"] and sesion.COOKIE in r.headers.get("set-cookie", "")


def test_guardian_toda_escritura_del_portal_rechaza_la_simulacion():
    """Guardián de la regla de E10. Si este test falla porque agregaste un POST bajo `/portal`:
    toda escritura debe rechazar `alcance.simulado` («ver como» es de solo lectura) — en la ruta Y en el
    servicio —, con su test; recién entonces súmala a este conjunto."""
    def aplanar(rutas):
        # FastAPI reciente envuelve cada `include_router` en un `_IncludedRouter` (con `original_router`).
        for r in rutas:
            sub = getattr(r, "original_router", None)
            yield from aplanar(sub.routes) if sub is not None else (r,)

    rutas = [r for r in aplanar(crear_app(_cfg()).routes) if (getattr(r, "path", "") or "").startswith("/portal")]
    assert any(r.path == "/portal" for r in rutas), "el aplanado no encontró las rutas del portal"
    posts = {r.path for r in rutas if "POST" in (getattr(r, "methods", None) or set())}
    assert posts == {"/portal/oportunidad/{proceso_gid}/estado", "/portal/ver-como",
                     "/portal/oportunidad/{proceso_gid}/responsable",
                     "/portal/oportunidad/{proceso_gid}/recuperar"}, (
        "Cambió el conjunto de rutas POST bajo /portal: toda escritura debe rechazar alcance.simulado "
        f"(«ver como» es de solo lectura). Encontradas: {sorted(posts)}")
    # Y también los demás verbos que escriben.
    otros = {r.path for r in rutas if (getattr(r, "methods", None) or set()) & {"PUT", "PATCH", "DELETE"}}
    assert otros == set(), "toda escritura debe rechazar alcance.simulado"
