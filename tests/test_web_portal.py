"""Tests de la vista de país del portal: flag, render, CSP y degradación ante ADB caída."""
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.web import graficos, portal
from src.web.app import crear_app

BASE = "https://dev.amalia.test"


def _cfg(**c):
    base = dict(enable_portal=True, portal_cliente_id="cli", enable_feedback=False, daily_budget_usd=5.0,
                identidad_secret="secreto-de-prueba", feedback_base_url=BASE, clients_dir="clients")
    base.update(c)
    return SimpleNamespace(**base)


def _alcance(roles=("gerente_pais",), clientes=("cli",), cambia=(), operativo=False):
    from src.identidad.alcance import Alcance
    return Alcance(usuario_id="u1", org_id="org", roles=frozenset(roles), clientes=frozenset(clientes),
                   cambia_estado=frozenset(cambia), ve_operativo=operativo)


def _cli(monkeypatch, alcance="default", cfg=None):
    """Cliente HTTP con (o sin) sesión: la cookie se resuelve al `Alcance` dado, sin tocar la base."""
    from src.web import sesion
    if alcance == "default":
        alcance = _alcance(operativo=True)
    monkeypatch.setattr(sesion, "_resolver", lambda c, token: alcance if token == "t" else None)
    monkeypatch.setattr(sesion, "_pendiente", lambda c, token: False)
    cliente = TestClient(crear_app(cfg or _cfg()), base_url=BASE, follow_redirects=False)
    if alcance is not None:
        cliente.cookies.set(sesion.COOKIE, "t")
    return cliente


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
    monkeypatch.setattr(portal.pais, "conversion_comercial", lambda c, cl, dias: {
        "detectadas": 20, "etapas": [{"estado": "EVALUANDO", "conteo": 8}, {"estado": "OFERTADO", "conteo": 4},
                                     {"estado": "GANADO", "conteo": 1}],
        "perdido": 2, "descartado": 3, "conv_ofertado_pct": 20.0, "conv_ganado_pct": 25.0})
    monkeypatch.setattr(portal.pais, "tiempo_por_etapa", lambda c, cl, dias: [
        {"etapa": "EVALUANDO", "dias_promedio": 3.5, "muestra": 6}])
    monkeypatch.setattr(portal.oportunidades_repo, "listar", lambda c, cl, dias=90, limite=50: [
        {"proceso_gid": "comprasal_sv:96-2026-P0011", "numero_referencia": "96-2026-P0011", "titulo": "Servidores <x>",
         "institucion": "Min Salud", "fecha_limite": datetime(2026, 11, 5), "estado": "NUEVO", "origen": None, "token": ""},
        {"proceso_gid": "comprasal_sv:96-2026-P0012", "numero_referencia": "96-2026-P0012", "titulo": "Licencias",
         "institucion": "BCR", "fecha_limite": None, "estado": "GANADO", "origen": "AUTOMATICO_MERCADO",
         "token": "2026-10-20T10:00:00.123456"}])
    monkeypatch.setattr(portal.regional, "nombres_clientes",
                        lambda c, cls: {x: {"sv": "El Salvador", "gt": "Guatemala"}.get(x, x) for x in cls})
    monkeypatch.setattr(portal.regional, "oportunidades_por_pais", lambda c, cls, dias: [
        {"cliente_id": x, "conteo": 12 if x == "sv" else 0} for x in cls])
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
    r = _cli(monkeypatch).get("/portal?dias=30")
    assert r.status_code == 200
    assert "Min &lt;Salud&gt;" in r.text and "Min <Salud>" not in r.text  # autoescape
    assert "+20.0%" in r.text and "40.0%" in r.text and "svg" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert 'aria-current="true">30 días' in r.text


def test_rango_invalido_cae_a_90(monkeypatch):
    _parchear_kpis(monkeypatch)
    r = _cli(monkeypatch).get("/portal?dias=7")
    assert 'aria-current="true">90 días' in r.text


def test_adb_caida_da_503_sin_traceback(monkeypatch):
    @contextmanager
    def conexion(cfg):
        raise RuntimeError("adb caída")
        yield

    monkeypatch.setattr(portal.db, "connection", conexion)
    r = _cli(monkeypatch).get("/portal")
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
    assert "Conflictos con COMPRASAL" not in _cli(monkeypatch).get("/portal").text
    _parchear_kpis(monkeypatch, conflictos=[{
        "proceso_gid": "comprasal_sv:96-2026-P0011<x>", "estado_manual": "DESCARTADO",
        "resultado_mercado": "GANADO", "responsable_usuario_id": None,
        "actualizado_en": datetime(2026, 10, 20)}])
    r = _cli(monkeypatch).get("/portal")
    assert r.status_code == 200 and "Conflictos con COMPRASAL" in r.text
    assert "96-2026-P0011&lt;x&gt;" in r.text and "capturado como descartado" in r.text
    assert "COMPRASAL publica ganado" in r.text and "20/10/2026" in r.text


def test_falla_de_conflictos_no_tumba_el_portal(monkeypatch):
    _parchear_kpis(monkeypatch)

    def boom(c, cl):
        raise RuntimeError("tabla ausente")

    monkeypatch.setattr(portal.mercado_repo, "listar_conflictos_oportunidad_v2", boom)
    r = _cli(monkeypatch).get("/portal")
    assert r.status_code == 200 and "Conflictos con COMPRASAL" not in r.text


def test_operativo_apagado_es_404(monkeypatch):
    _parchear_kpis(monkeypatch)
    assert _cli(monkeypatch, cfg=_cfg(enable_portal=False)).get("/portal/operativo").status_code == 404


def test_operativo_render(monkeypatch):
    _parchear_kpis(monkeypatch)
    r = _cli(monkeypatch).get("/portal/operativo")
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
    r = _cli(monkeypatch).get("/portal/operativo")
    assert 'badge error">ERROR' in r.text and "&lt;boom&gt;" in r.text and "hace 5 min" in r.text
    assert "Sin llamadas al LLM hoy" in r.text and "Sin datos suficientes" in r.text


def test_operativo_sin_corridas(monkeypatch):
    _parchear_kpis(monkeypatch, corrida={"estado": None})
    r = _cli(monkeypatch).get("/portal/operativo")
    assert "Sin corridas registradas" in r.text


def test_operativo_adb_caida_da_503_sin_traceback(monkeypatch):
    @contextmanager
    def conexion(cfg):
        raise RuntimeError("adb caída")
        yield

    monkeypatch.setattr(portal.db, "connection", conexion)
    r = _cli(monkeypatch).get("/portal/operativo")
    assert r.status_code == 503 and "adb" not in r.text.lower() and "Traceback" not in r.text


def test_graficos_operativos_vacios_y_escape():
    assert graficos.medidor(1, 0) == "" and graficos.linea([]) == ""
    assert graficos.linea([("a", 1.0, "t"), ("b", None, "")]) == ""
    assert "&lt;b&gt;" in graficos.linea([("<b>", 1.0, "<b>"), ("c", 2.0, "x")])
    assert "var(--critico)" in graficos.medidor(6, 5) and "var(--bueno)" in graficos.medidor(1, 5)
    assert "var(--aviso)" in graficos.medidor(4, 5)


# ── Entrega 4: autorización por rol y por país ─────────────────────────────────

class TestAcceso:
    def test_sin_sesion_redirige_al_acceso_con_el_destino(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        c = _cli(monkeypatch, alcance=None)
        r = c.get("/portal?dias=30")
        assert r.status_code == 303 and r.headers["location"] == "/acceso?siguiente=%2Fportal%3Fdias%3D30"
        assert c.get("/portal/operativo").headers["location"].startswith("/acceso?siguiente=")
        assert c.get("/portal/regional").status_code == 303

    @pytest.mark.parametrize("cambio", [{"identidad_secret": ""}, {"feedback_base_url": ""}, {"enable_portal": False}])
    def test_sin_identidad_configurada_falla_cerrado(self, monkeypatch, cambio):
        _parchear_kpis(monkeypatch)
        c = _cli(monkeypatch, cfg=_cfg(**cambio))
        assert c.get("/portal").status_code == 404 and c.get("/portal/operativo").status_code == 404
        assert c.get("/portal/regional").status_code == 404

    def test_gerente_de_pais_ve_su_pais_sin_selector_ni_regional(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        c = _cli(monkeypatch, _alcance(clientes=("sv",)))
        r = c.get("/portal")
        assert r.status_code == 200 and "El Salvador" not in r.text.split("<nav")[0]
        assert 'href="/portal/regional"' not in r.text and 'href="/portal?cliente=' not in r.text.split("</nav>")[0]
        assert 'action="/salir"' in r.text and 'name="csrf"' in r.text

    def test_pais_ajeno_es_el_mismo_404_que_una_ruta_inexistente_y_se_audita(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        auditados = []
        monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria",
                            lambda conn, org, accion, **kw: auditados.append((org, accion, kw)))
        c = _cli(monkeypatch, _alcance(clientes=("sv",)))
        ajeno = c.get("/portal?cliente=gt")
        inexistente = c.get("/portal?cliente=no-existe")
        ruta = c.get("/no-hay-ruta")
        assert ajeno.status_code == inexistente.status_code == ruta.status_code == 404
        assert ajeno.text == inexistente.text == ruta.text
        assert [a[1] for a in auditados] == ["ACCESO_FUERA_DE_ALCANCE"] * 2
        assert auditados[0][2]["usuario_id"] == "u1" and auditados[0][2]["recurso"] == "cliente=gt"

    def test_una_falla_al_auditar_no_cambia_la_respuesta(self, monkeypatch):
        _parchear_kpis(monkeypatch)

        def boom(*a, **kw):
            raise RuntimeError("adb caída")

        monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria", boom)
        assert _cli(monkeypatch, _alcance(clientes=("sv",))).get("/portal?cliente=gt").status_code == 404

    def test_el_pais_pedido_dentro_del_alcance_se_muestra_y_el_rango_lo_conserva(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        c = _cli(monkeypatch, _alcance(roles=("ceo",), clientes=("sv", "gt")))
        r = c.get("/portal?cliente=gt&dias=30")
        assert r.status_code == 200 and "<h1>Guatemala</h1>" in r.text
        assert "/portal?cliente=gt&amp;dias=90" in r.text or "/portal?cliente=gt&dias=90" in r.text

    def test_regional_sin_cliente_va_a_la_vista_regional(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        r = _cli(monkeypatch, _alcance(roles=("ceo",), clientes=("sv", "gt"))).get("/portal")
        assert r.status_code == 303 and r.headers["location"] == "/portal/regional"

    def test_tecnico_sin_negocio_va_a_lo_operativo(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        r = _cli(monkeypatch, _alcance(roles=("operador",), clientes=(), operativo=True)).get("/portal")
        assert r.status_code == 303 and r.headers["location"] == "/portal/operativo"
        # ...y no ve ningún país aunque lo pida.
        assert _cli(monkeypatch, _alcance(roles=("operador",), clientes=(), operativo=True)).get(
            "/portal?cliente=sv").status_code == 404

    def test_sin_paises_ni_operativo_es_404(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        assert _cli(monkeypatch, _alcance(roles=("gerente_pais",), clientes=())).get("/portal").status_code == 404


class TestOperativo:
    @pytest.mark.parametrize("roles,operativo,esperado", [
        (("equipo_ventas",), False, 404),
        (("gerente_pais",), False, 404),
        (("ceo",), False, 404),
        (("operador",), True, 200),
        (("admin_plataforma",), True, 200),
    ])
    def test_solo_operador_y_admin_plataforma(self, monkeypatch, roles, operativo, esperado):
        _parchear_kpis(monkeypatch)
        r = _cli(monkeypatch, _alcance(roles=roles, clientes=("sv",), operativo=operativo)).get("/portal/operativo")
        assert r.status_code == esperado
        if esperado == 404:
            assert r.text == _cli(monkeypatch).get("/no-hay-ruta").text  # idéntico al de cualquier ruta inexistente


class TestRegional:
    def test_solo_roles_regionales(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        assert _cli(monkeypatch, _alcance(roles=("gerente_pais",), clientes=("sv",))).get(
            "/portal/regional").status_code == 404
        assert _cli(monkeypatch, _alcance(roles=("equipo_ventas",), clientes=("sv",))).get(
            "/portal/regional").status_code == 404

    def test_render_con_selector_y_paises_sin_integracion(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        monkeypatch.setattr(portal.portales, "portal_de_cliente",
                            lambda d, c: "sicop_cr" if c == "gt" else "comprasal_sv")
        r = _cli(monkeypatch, _alcance(roles=("director_estrategia",), clientes=("sv", "gt"))).get("/portal/regional")
        assert r.status_code == 200
        assert 'href="/portal?cliente=sv"' in r.text and 'href="/portal?cliente=gt"' in r.text
        assert "12 oportunidades" in r.text and "sin datos — portal de compras no integrado todavía" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]

    def test_adb_caida_da_503(self, monkeypatch):
        @contextmanager
        def conexion(cfg):
            raise RuntimeError("adb caída")
            yield

        monkeypatch.setattr(portal.db, "connection", conexion)
        r = _cli(monkeypatch, _alcance(roles=("ceo",), clientes=("sv",))).get("/portal/regional")
        assert r.status_code == 503 and "adb" not in r.text.lower()


def test_navegacion_segun_el_rol(monkeypatch):
    _parchear_kpis(monkeypatch)
    r = _cli(monkeypatch, _alcance(roles=("ceo", "admin_plataforma"), clientes=("sv", "gt"), operativo=True)
             ).get("/portal/regional")
    nav = r.text.split('<nav class="sesion"')[1].split("</nav>")[0]
    assert "/portal/regional" in nav and "/portal/operativo" in nav and "Guatemala" in nav and "Salir" in nav


# ── Entrega 5: captura manual del pipeline comercial ───────────────────────────

class TestPipelineEnLaVista:
    def test_quien_puede_cambiar_ve_el_control_con_solo_las_transiciones_validas(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        r = _cli(monkeypatch, _alcance(clientes=("cli",), cambia=("cli",))).get("/portal")
        assert r.status_code == 200 and "Servidores &lt;x&gt;" in r.text
        assert 'action="/portal/oportunidad/comprasal_sv%3A96-2026-P0011/estado"' in r.text
        filas = {f.split("</strong>")[0].split("<strong>")[-1]: f for f in r.text.split("<tr>") if "<strong>" in f}
        nuevo = filas["96-2026-P0011"]
        assert 'value="EVALUANDO"' in nuevo and 'value="DESCARTADO"' in nuevo and 'value="GANADO"' not in nuevo
        ganado = filas["96-2026-P0012"]
        assert 'value="PERDIDO"' in ganado and "(COMPRASAL)" in ganado
        assert "Conversión comercial" in r.text and "20.0%" in r.text and "Tiempo en cada etapa" in r.text

    def test_solo_lectura_no_ve_el_control(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        r = _cli(monkeypatch, _alcance(roles=("ceo",), clientes=("cli",), cambia=())).get("/portal?cliente=cli")
        assert r.status_code == 200 and "96-2026-P0011" in r.text and "solo lectura" in r.text
        assert "/estado" not in r.text

    @pytest.mark.parametrize("codigo,texto", [("ok", "Estado actualizado."), ("conflicto", "Alguien cambió"),
                                              ("invalido", "no es válido")])
    def test_avisos(self, monkeypatch, codigo, texto):
        _parchear_kpis(monkeypatch)
        r = _cli(monkeypatch).get(f"/portal?aviso={codigo}")
        assert texto in r.text
        assert "aviso-estado" not in _cli(monkeypatch).get("/portal?aviso=<script>").text.split("</style>")[1]


class TestCambiarEstado:
    GID = "comprasal_sv:96-2026-P0011"
    RUTA = "/portal/oportunidad/comprasal_sv%3A96-2026-P0011/estado"

    @pytest.fixture
    def llamadas(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        reg = SimpleNamespace(args=[], resultado="ok")

        def cambiar(conn, alcance, cliente, gid, nuevo, token, motivo=None, monto=None, competidor=None):
            reg.args.append(dict(cliente=cliente, gid=gid, nuevo=nuevo, token=token, motivo=motivo,
                                 monto=monto, competidor=competidor))
            return reg.resultado

        monkeypatch.setattr(portal.oportunidades, "cambiar_estado", cambiar)
        return reg

    def _post(self, monkeypatch, alcance=None, **datos):
        from src.identidad import sesiones as ses
        from src.web import csrf as csrf_mod
        c = _cli(monkeypatch, alcance or _alcance(clientes=("cli",), cambia=("cli",)))
        base = dict(cliente="cli", estado="EVALUANDO", actualizado_en="", dias="30",
                    csrf=csrf_mod.emitir("secreto-de-prueba", ses.hash_token("t")))
        base.update(datos)
        # El token CSRF de una sesión resuelta se liga al hash de su cookie.
        return c.post(self.RUTA, data=base, headers={"origin": BASE})

    def test_ok_redirige_a_la_vista_y_pasa_los_datos(self, monkeypatch, llamadas):
        monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria", lambda *a, **k: None)
        r = self._post(monkeypatch, motivo=" ya cotizado ", monto_ofertado="1,250.50", competidor="ACME")
        assert r.status_code == 303
        assert r.headers["location"] == "/portal?cliente=cli&dias=30&aviso=ok"
        assert llamadas.args[0] == dict(cliente="cli", gid=self.GID, nuevo="EVALUANDO", token="",
                                        motivo=" ya cotizado ", monto=__import__("decimal").Decimal("1250.50"),
                                        competidor="ACME")

    @pytest.mark.parametrize("resultado,aviso", [("conflicto", "conflicto"), ("invalido", "invalido")])
    def test_conflicto_e_invalido_vuelven_con_aviso(self, monkeypatch, llamadas, resultado, aviso):
        llamadas.resultado = resultado
        r = self._post(monkeypatch)
        assert r.status_code == 303 and r.headers["location"].endswith(f"aviso={aviso}")

    @pytest.mark.parametrize("monto", ["abc", "-5", "1e15", "10.123", "NaN"])
    def test_monto_invalido_no_llega_al_servicio(self, monkeypatch, llamadas, monto):
        r = self._post(monkeypatch, monto_ofertado=monto)
        assert r.status_code == 303 and r.headers["location"].endswith("aviso=invalido") and llamadas.args == []

    def test_estado_desconocido_no_llega_al_servicio(self, monkeypatch, llamadas):
        r = self._post(monkeypatch, estado="HACKEADO")
        assert r.headers["location"].endswith("aviso=invalido") and llamadas.args == []

    def test_no_encontrado_es_el_404_de_siempre(self, monkeypatch, llamadas):
        llamadas.resultado = "no_encontrado"
        r = self._post(monkeypatch)
        assert r.status_code == 404 and r.text == _cli(monkeypatch).get("/no-hay-ruta").text

    def test_sin_csrf_es_403_y_no_escribe(self, monkeypatch, llamadas):
        r = self._post(monkeypatch, csrf="1.malo")
        assert r.status_code == 403 and llamadas.args == []

    def test_sin_sesion_redirige_al_acceso(self, monkeypatch, llamadas):
        r = _cli(monkeypatch, alcance=None).post(self.RUTA, data={"cliente": "cli"}, headers={"origin": BASE})
        assert r.status_code == 303 and r.headers["location"].startswith("/acceso") and llamadas.args == []

    def test_solo_lectura_y_pais_ajeno_son_404_y_el_ajeno_se_audita(self, monkeypatch, llamadas):
        auditados = []
        monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria",
                            lambda conn, org, accion, **kw: auditados.append(accion))
        solo_lectura = _alcance(roles=("ceo",), clientes=("cli",), cambia=())
        assert self._post(monkeypatch, alcance=solo_lectura).status_code == 404
        assert auditados == []  # lo ve, solo que no puede cambiarlo: no es una señal de abuso
        assert self._post(monkeypatch, cliente="otro-pais").status_code == 404
        assert auditados == ["ACCESO_FUERA_DE_ALCANCE"] and llamadas.args == []

    def test_adb_caida_es_503(self, monkeypatch, llamadas):
        def boom(*a, **k):
            raise RuntimeError("adb caída")

        monkeypatch.setattr(portal.oportunidades, "cambiar_estado", boom)
        r = self._post(monkeypatch)
        assert r.status_code == 503 and "adb" not in r.text.lower()


def test_un_post_sin_sesion_vuelve_a_la_vista_y_no_a_la_ruta_del_post(monkeypatch):
    _parchear_kpis(monkeypatch)
    r = _cli(monkeypatch, alcance=None).post(
        "/portal/oportunidad/x/estado", data={"cliente": "cli"}, headers={"origin": BASE})
    assert r.status_code == 303 and r.headers["location"] == "/acceso?siguiente=%2Fportal"
