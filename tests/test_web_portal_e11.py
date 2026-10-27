"""E11 — vista de país «qué hago hoy»: contexto del contrato, filtros, y las dos escrituras nuevas.

Lo que lee la base (`analytics.tablero`, `usuarios_asignables`) y los servicios de escritura se simulan;
`portal_pais.html` se sustituye por el render mínimo del contrato (`STUB_PAIS`). La prueba de humo del
final pinta la plantilla REAL cuando ya existen los parciales de E11.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from markupsafe import Markup
from test_web_portal import (
    BASE,
    _alcance,
    _cfg,
    _cli,
    _fila,
    _parchear_kpis,
    filas_base,
)

from src.identidad import sesiones
from src.web import csrf as csrf_mod
from src.web import portal
from src.web.app import crear_app

GERENTE = dict(roles=("gerente_pais",), clientes=("cli",), cambia=("cli",))


def _get(monkeypatch, url="/portal", alcance=None, **parche):
    ctx = _parchear_kpis(monkeypatch)
    if parche:
        from test_web_portal import _parchear_e11
        _parchear_e11(monkeypatch, **parche)
    r = _cli(monkeypatch, alcance or _alcance(**GERENTE)).get(url)
    return r, ctx


def _tarjetas(ctx):
    return {t["referencia"]: t for c in ctx["tablero"]["columnas"] for t in c["tarjetas"]}


# ── Contexto completo ──────────────────────────────────────────────────────────

class TestContexto:
    def test_todas_las_claves_del_contrato_con_sus_tipos(self, monkeypatch):
        r, ctx = _get(monkeypatch)
        assert r.status_code == 200
        for clave in ("pais_nombre", "cliente", "dias", "rangos", "aviso", "csrf", "puede_cambiar", "solo_vista",
                      "nav", "ver_como", "cabecera", "kpis", "cierres", "atencion", "tablero", "clasificacion",
                      "votos", "analisis"):
            assert clave in ctx, clave
        assert set(ctx["cabecera"]) >= {"veredicto", "fecha_larga", "corrida"}
        assert set(ctx["kpis"]) == {"decidir", "pipeline", "relevancia", "estancadas"}
        assert set(ctx["kpis"]["decidir"]) == {"n", "semana", "primera"}
        assert set(ctx["kpis"]["pipeline"]) == {"monto_usd", "activas", "con_monto"}
        assert set(ctx["kpis"]["relevancia"]) == {"valor", "relevantes", "total", "delta_pts", "spark"}
        assert set(ctx["kpis"]["estancadas"]) == {"n", "dias"}
        assert set(ctx["cierres"]) == {"svg", "dias", "dia"} and isinstance(ctx["cierres"]["svg"], Markup)
        assert all(set(d) == {"fecha", "n", "urgencia", "href"} for d in ctx["cierres"]["dias"])
        assert all(set(a) == {"chip", "clase", "titulo", "detalle", "gid"} for a in ctx["atencion"])
        assert set(ctx["tablero"]) == {"columnas", "asignables", "filtros", "truncado"}
        assert set(ctx["tablero"]["filtros"]) == {"responsable", "marca", "opciones"}
        assert set(ctx["tablero"]["filtros"]["opciones"]) == {"responsables", "marcas"}
        for c in ctx["tablero"]["columnas"]:
            assert set(c) == {"clave", "titulo", "n", "tarjetas"} and c["n"] == len(c["tarjetas"])
            for t in c["tarjetas"]:
                assert set(t) >= {"gid", "referencia", "titulo", "institucion", "url", "match", "cierre", "marcas",
                                  "monto_txt", "responsable", "responsable_id", "dias_en_estado", "voto",
                                  "siguientes", "exige_motivo", "token", "estado"}
        assert set(ctx["clasificacion"]) >= {"total", "grupos", "barra", "motivos", "descartados", "hay_mas",
                                             "total_descartados"}
        assert set(ctx["votos"]) == {"relevante", "irrelevante", "motivos", "desacuerdos", "pocos"}
        assert set(ctx["analisis"]) == {"g_tendencia", "g_marcas", "g_embudo", "g_instituciones", "g_severidad",
                                        "g_conversion", "g_tiempos", "conversion", "salidas", "hay_severidad",
                                        "tiempos", "conflictos"}

    def test_cabecera_veredicto_fecha_y_corrida(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        cab = ctx["cabecera"]
        assert cab["veredicto"] == "Hoy hay 1 oportunidad que cierra esta semana y 1 sin responsable."
        assert cab["fecha_larga"] == "martes 27 de octubre de 2026"
        # La última corrida fue hace 125 min (stub de operativo) → 07:55 en El Salvador.
        assert cab["corrida"] == {"hora": "07:55", "revisados": 42}

    @pytest.mark.parametrize("semana,sin,esperado", [
        (0, 0, "Nada urgente hoy."),
        (2, 3, "Hoy hay 2 oportunidades que cierran esta semana y 3 sin responsable."),
        (1, 0, "Hoy hay 1 oportunidad que cierra esta semana y 0 sin responsable."),
    ])
    def test_veredicto_singular_y_plural(self, semana, sin, esperado):
        assert portal._veredicto(semana, sin) == esperado

    def test_columnas_del_tablero(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        col = {c["clave"]: [t["referencia"] for t in c["tarjetas"]] for c in ctx["tablero"]["columnas"]}
        # Nuevo: sin estado y de un grupo del tablero, por cierre (hoy, 3 días, sin fecha); el descartado no está.
        assert col == {"nuevo": ["96-2026-P0016", "96-2026-P0011", "96-2026-P0013"],
                       "evaluando": ["96-2026-P0012"], "ofertado": [], "cerradas": ["96-2026-P0015"]}
        assert [c["titulo"] for c in ctx["tablero"]["columnas"]] == ["Nuevo", "Evaluando", "Ofertado", "Cerradas"]

    def test_tarjeta(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        t = _tarjetas(ctx)
        p11 = t["96-2026-P0011"]
        assert p11["cierre"] == {"texto": "Cierra vie 30 oct · 3 días", "urgencia": "rojo"}
        assert p11["monto_txt"] == "USD 12.500" and p11["marcas"] == ["Dell"] and p11["responsable"] is None
        assert p11["voto"] == {"n": 2, "motivo": "Fuera de nuestro rubro"} and p11["estado"] == "NUEVO"
        assert p11["dias_en_estado"] is None and p11["url"].startswith("https://")
        p12 = t["96-2026-P0012"]
        assert p12["monto_txt"] == "monto sin estimar" and p12["responsable"] == "Ana"
        assert p12["responsable_id"] == "u2" and p12["dias_en_estado"] == 12 and p12["voto"] is None
        assert t["96-2026-P0013"]["cierre"] is None

    def test_monto_cero_o_nulo_nunca_es_cero(self):
        assert portal._monto_txt(None) == portal._monto_txt(0) == "monto sin estimar"
        assert portal._monto_txt("x") == "monto sin estimar" and portal._monto_txt(1500.4) == "USD 1.500"

    def test_url_que_no_es_http_no_llega_al_href(self, monkeypatch):
        filas = filas_base()
        filas[0]["url"] = "javascript:alert(1)"
        _, ctx = _get(monkeypatch, filas=filas)
        assert _tarjetas(ctx)["96-2026-P0011"]["url"] is None

    def test_calendario(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        dias = ctx["cierres"]["dias"]
        assert len(dias) == 14 and dias[0]["fecha"] == date(2026, 10, 27) and dias[-1]["fecha"] == date(2026, 11, 9)
        por_fecha = {d["fecha"]: d for d in dias}
        # Solo lo que sigue en juego: P0011 (vie 30). El no competible de hoy y el de nov 20 no cuentan.
        assert por_fecha[date(2026, 10, 30)]["n"] == 1 and sum(d["n"] for d in dias) == 1
        assert por_fecha[date(2026, 10, 30)]["urgencia"] == "rojo"
        assert por_fecha[date(2026, 11, 9)]["urgencia"] == "ambar"
        assert por_fecha[date(2026, 10, 30)]["href"] == "/portal?cliente=cli&dias=90&dia=2026-10-30"
        assert ctx["cierres"]["dia"] is None

    def test_requiere_atencion(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        chips = [(a["chip"], a["gid"]) for a in ctx["atencion"]]
        assert chips == [("Cierra en 3 días", "comprasal_sv:96-2026-P0011"),
                         ("Sin moverse", "comprasal_sv:96-2026-P0012"),
                         ("Sin responsable", None)]
        assert ctx["atencion"][1]["detalle"] == "12 días en evaluación"
        assert ctx["atencion"][2]["titulo"] == "1 oportunidad sin responsable"

    def test_kpis(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        k = ctx["kpis"]
        # Para decidir: P0011 y P0012 (P0015 ya está GANADO); cierra esta semana solo P0011.
        assert k["decidir"]["n"] == 2 and k["decidir"]["semana"] == 1
        assert k["decidir"]["primera"]["gid"] == "comprasal_sv:96-2026-P0011"
        assert k["pipeline"] == {"monto_usd": 12500.0, "activas": 2, "con_monto": 1}
        assert k["estancadas"] == {"n": 1, "dias": 7}
        # Mismo stub para 30 y 60 días → sin ventana previa: sin delta.
        assert k["relevancia"]["valor"] == 40.0 and k["relevancia"]["delta_pts"] is None

    def test_delta_de_relevancia_contra_los_30_dias_previos(self, monkeypatch):
        _parchear_kpis(monkeypatch)
        datos = {30: {"valor": 40.0, "total": 10, "relevantes": 4}, 60: {"valor": 30.0, "total": 20, "relevantes": 6}}
        monkeypatch.setattr(portal.pais, "pct_relevancia_clasificador", lambda c, cl, dias: datos[dias])
        ctx = {}
        real = portal.pagina
        monkeypatch.setattr(portal, "pagina", lambda req, n, **k: ctx.update(k) or real(req, n, **k))
        _cli(monkeypatch, _alcance(**GERENTE)).get("/portal")
        assert ctx["kpis"]["relevancia"]["delta_pts"] == 20.0  # 40 % ahora vs 2/10 = 20 % antes

    def test_clasificacion(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        c = ctx["clasificacion"]
        assert c["total"] == 6
        assert c["grupos"] == {"decidir": 3, "no_competible": 1, "baja": 1, "descartados": 1}
        assert c["motivos"] == [("Fuera de rubro", 1)]
        assert c["descartados"] == [{"gid": "comprasal_sv:96-2026-P0014", "referencia": "96-2026-P0014",
                                     "titulo": "Mobiliario", "match": 70, "motivo": "Fuera de rubro", "token": ""}]
        assert c["hay_mas"] is False and c["total_descartados"] == 1 and isinstance(c["barra"], Markup)

    def test_descartados_hasta_diez_por_match(self, monkeypatch):
        filas = [_fila(f"D{i:02d}", "descartado", match=i) for i in range(12)]
        _, ctx = _get(monkeypatch, filas=filas)
        c = ctx["clasificacion"]
        assert [d["match"] for d in c["descartados"]] == list(range(11, 1, -1))
        assert c["hay_mas"] is True and c["total_descartados"] == 12

    def test_un_descartado_recuperado_esta_en_nuevo_y_no_en_descartados(self, monkeypatch):
        filas = [_fila("R1", "descartado", estado="NUEVO", token="2026-10-26T10:00:00.000000",
                       actualizado_en=datetime(2026, 10, 26, 10))]
        _, ctx = _get(monkeypatch, filas=filas, votos={"comprasal_sv:R1": {"relevante": 2, "irrelevante": 0}})
        assert [t["referencia"] for t in ctx["tablero"]["columnas"][0]["tarjetas"]] == ["R1"]
        assert ctx["clasificacion"]["descartados"] == [] and ctx["clasificacion"]["grupos"]["descartados"] == 1
        assert ctx["votos"]["desacuerdos"][0]["lado"] == "relevante"

    def test_votos(self, monkeypatch):
        r, ctx = _get(monkeypatch)
        v = ctx["votos"]
        assert v["relevante"] == 1 and v["irrelevante"] == 2 and v["pocos"] is True
        assert v["motivos"] == [("Fuera de nuestro rubro", 2)]
        assert [(d["gid"], d["lado"]) for d in v["desacuerdos"]] == [
            ("comprasal_sv:96-2026-P0011", "irrelevante"), ("comprasal_sv:96-2026-P0014", "relevante")]
        assert "Fuera de nuestro rubro" in v["desacuerdos"][0]["detalle"]

    def test_muchos_votos_no_es_pocos_y_motivos_como_dict(self, monkeypatch):
        _, ctx = _get(monkeypatch, resumen={"relevante": 7, "irrelevante": 3,
                                            "motivos": [{"motivo": "otro", "n": 3}]})
        assert ctx["votos"]["pocos"] is False and ctx["votos"]["motivos"] == [("Otro motivo (explicar abajo)", 3)]

    def test_analisis(self, monkeypatch):
        _, ctx = _get(monkeypatch)
        a = ctx["analisis"]
        assert isinstance(a["g_tendencia"], Markup) and a["g_tendencia"]
        assert "Dell" in a["g_marcas"] and "DESCUBIERTO" in a["g_embudo"]
        assert a["salidas"] == [{"estado": "ERROR_LLM", "conteo": 1}] and a["hay_severidad"] is False

    def test_una_sola_conexion_y_votos_solo_de_los_gids_leidos(self, monkeypatch):
        conexiones = []
        ctx = _parchear_kpis(monkeypatch)
        from test_web_portal import _parchear_e11
        reg = _parchear_e11(monkeypatch)

        @contextmanager
        def conexion(cfg):
            conexiones.append(1)
            yield object()

        monkeypatch.setattr(portal.db, "connection", conexion)
        assert _cli(monkeypatch, _alcance(**GERENTE)).get("/portal").status_code == 200
        assert len(conexiones) == 1 and ctx["cabecera"]
        (gids,) = [a[1] for n, a, _ in reg.llamadas if n == "votos_por_proceso"]
        assert set(gids) == {f["proceso_gid"] for f in filas_base()}
        # El cliente siempre es el del alcance, nunca otro.
        assert all(a[0] == "cli" for n, a, _ in reg.llamadas if n != "usuarios_asignables")


class TestDatosVacios:
    def test_sin_procesos_ni_votos(self, monkeypatch):
        r, ctx = _get(monkeypatch, filas=[], votos={}, resumen={}, asignables=[], estancadas=[],
                      pipeline={}, tendencia=[], marcas=[])
        assert r.status_code == 200
        assert ctx["cabecera"]["veredicto"] == "Nada urgente hoy."
        assert all(c["n"] == 0 for c in ctx["tablero"]["columnas"]) and ctx["atencion"] == []
        assert ctx["kpis"]["decidir"] == {"n": 0, "semana": 0, "primera": None}
        assert ctx["kpis"]["pipeline"] == {"monto_usd": None, "activas": 0, "con_monto": 0}
        assert ctx["kpis"]["estancadas"]["n"] == 0
        assert ctx["clasificacion"]["total"] == 0 and ctx["clasificacion"]["descartados"] == []
        assert ctx["votos"] == {"relevante": 0, "irrelevante": 0, "motivos": [], "desacuerdos": [], "pocos": True}
        assert sum(d["n"] for d in ctx["cierres"]["dias"]) == 0
        assert ctx["tablero"]["filtros"]["opciones"] == {
            "responsables": [{"valor": "sin", "etiqueta": "Sin responsable"}], "marcas": []}

    def test_truncado(self, monkeypatch):
        _, ctx = _get(monkeypatch, truncado=True)
        assert ctx["tablero"]["truncado"] is True

    def test_zona_invalida_cae_a_el_salvador(self, monkeypatch):
        monkeypatch.setattr(portal.portales, "portal_de_cliente", lambda d, c: "no-existe")
        assert portal._zona(_cfg(), "cli").key == "America/El_Salvador"


# ── Filtros por query ─────────────────────────────────────────────────────────

class TestFiltros:
    def test_filtro_por_dia(self, monkeypatch):
        _, ctx = _get(monkeypatch, "/portal?dia=2026-10-30")
        assert ctx["cierres"]["dia"] == date(2026, 10, 30)
        assert [t["referencia"] for c in ctx["tablero"]["columnas"] for t in c["tarjetas"]] == ["96-2026-P0011"]
        # Los KPIs no se filtran.
        assert ctx["kpis"]["decidir"]["n"] == 2

    def test_filtro_sin_responsable_y_por_responsable(self, monkeypatch):
        _, ctx = _get(monkeypatch, "/portal?responsable=sin")
        assert "96-2026-P0012" not in _tarjetas(ctx) and "96-2026-P0011" in _tarjetas(ctx)
        _, ctx = _get(monkeypatch, "/portal?responsable=u2")
        assert set(_tarjetas(ctx)) == {"96-2026-P0012", "96-2026-P0015"}
        assert ctx["tablero"]["filtros"]["responsable"] == "u2"

    def test_filtro_por_marca(self, monkeypatch):
        _, ctx = _get(monkeypatch, "/portal?marca=HPE")
        assert set(_tarjetas(ctx)) == {"96-2026-P0012"} and ctx["tablero"]["filtros"]["marca"] == "HPE"
        assert ctx["tablero"]["filtros"]["opciones"]["marcas"] == ["Dell", "HPE"]

    @pytest.mark.parametrize("query", [
        "dia=2026-13-45", "dia=2026-12-30", "dia=2026-10-26", "dia=%27%20OR%201%3D1",
        "responsable=%27%20OR%201%3D1", "responsable=u999", "marca=%3Cscript%3E", "marca=Cisco",
    ])
    def test_query_invalido_se_ignora(self, monkeypatch, query):
        r, ctx = _get(monkeypatch, f"/portal?{query}")
        assert r.status_code == 200
        assert ctx["cierres"]["dia"] is None
        assert ctx["tablero"]["filtros"]["responsable"] is None and ctx["tablero"]["filtros"]["marca"] is None
        assert len(_tarjetas(ctx)) == 5
        assert "<script>" not in r.text and "OR 1=1" not in r.text


# ── Escrituras: responsable y recuperar ────────────────────────────────────────

RUTAS = {
    "responsable": ("/portal/oportunidad/comprasal_sv%3A96-2026-P0011/responsable", "asignar_responsable",
                    "asignado"),
    "recuperar": ("/portal/oportunidad/comprasal_sv%3A96-2026-P0014/recuperar", "recuperar_descartado",
                  "recuperado"),
}


@pytest.fixture
def servicio(monkeypatch):
    _parchear_kpis(monkeypatch)
    reg = SimpleNamespace(args=[], resultado="ok", auditados=[])

    def asignar(conn, alcance, cliente, gid, responsable_id, token, ip_hash):
        reg.args.append(("responsable", cliente, gid, responsable_id, token, bool(ip_hash)))
        return reg.resultado

    def recuperar(conn, alcance, cliente, gid, token, ip_hash):
        reg.args.append(("recuperar", cliente, gid, token, bool(ip_hash)))
        return reg.resultado

    monkeypatch.setattr(portal.oportunidades, "asignar_responsable", asignar, raising=False)
    monkeypatch.setattr(portal.oportunidades, "recuperar_descartado", recuperar, raising=False)
    monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria",
                        lambda conn, org, accion, **kw: reg.auditados.append((accion, kw)))
    return reg


def _post(monkeypatch, accion, alcance=None, **datos):
    c = _cli(monkeypatch, alcance or _alcance(**GERENTE))
    base = dict(cliente="cli", actualizado_en="", dias="30",
                csrf=csrf_mod.emitir("secreto-de-prueba", sesiones.hash_token("t")))
    if accion == "responsable":
        base["responsable"] = "u2"
    base.update(datos)
    return c.post(RUTAS[accion][0], data=base, headers={"origin": BASE})


def _no_existe(monkeypatch):
    return _cli(monkeypatch).get("/no-hay-ruta").text


@pytest.mark.parametrize("accion", ["responsable", "recuperar"])
class TestEscrituras:
    def test_ok_redirige_con_su_aviso_y_pasa_los_datos(self, monkeypatch, servicio, accion):
        r = _post(monkeypatch, accion, actualizado_en="2026-10-20T10:00:00.123456")
        assert r.status_code == 303
        assert r.headers["location"] == f"/portal?cliente=cli&dias=30&aviso={RUTAS[accion][2]}"
        if accion == "responsable":
            assert servicio.args == [("responsable", "cli", "comprasal_sv:96-2026-P0011", "u2",
                                      "2026-10-20T10:00:00.123456", True)]
        else:
            assert servicio.args == [("recuperar", "cli", "comprasal_sv:96-2026-P0014",
                                      "2026-10-20T10:00:00.123456", True)]

    @pytest.mark.parametrize("resultado,aviso", [
        ("conflicto", "conflicto"), ("invalido", "invalido"), ("SIMULACION", "simulacion"),
        ("responsable_invalido", "responsable_invalido"), ("no_recuperable", "no_recuperable"),
        ("ya_recuperado", "no_recuperable"), ("algo-raro", "invalido"),
    ])
    def test_cada_resultado_su_aviso(self, monkeypatch, servicio, accion, resultado, aviso):
        constantes = {"responsable_invalido": portal.oportunidades.RESPONSABLE_INVALIDO,
                      "no_recuperable": portal.oportunidades.NO_RECUPERABLE,
                      "ya_recuperado": portal.oportunidades.YA_RECUPERADO,
                      "SIMULACION": portal.oportunidades.SIMULACION}
        servicio.resultado = constantes.get(resultado, resultado)
        r = _post(monkeypatch, accion)
        assert r.status_code == 303 and r.headers["location"].endswith(f"aviso={aviso}")

    def test_sin_sesion_redirige_al_acceso(self, monkeypatch, servicio, accion):
        r = _cli(monkeypatch, alcance=None).post(RUTAS[accion][0], data={"cliente": "cli"}, headers={"origin": BASE})
        assert r.status_code == 303 and r.headers["location"] == "/acceso?siguiente=%2Fportal"
        assert servicio.args == []

    @pytest.mark.parametrize("csrf,origen", [("1.malo", BASE), (None, "https://evil.test"), (None, None)])
    def test_csrf_u_origen_invalido_es_403(self, monkeypatch, servicio, accion, csrf, origen):
        c = _cli(monkeypatch, _alcance(**GERENTE))
        datos = {"cliente": "cli", "csrf": csrf or csrf_mod.emitir("secreto-de-prueba", sesiones.hash_token("t"))}
        r = c.post(RUTAS[accion][0], data=datos, headers={"origin": origen} if origen else {})
        assert r.status_code == 403 and servicio.args == []

    def test_simulacion_no_llama_al_servicio(self, monkeypatch, servicio, accion):
        from src.identidad.alcance import Alcance
        sim = Alcance(usuario_id="u-admin", org_id="org", roles=frozenset({"gerente_pais"}),
                      clientes=frozenset({"cli"}), cambia_estado=frozenset({"cli"}), ve_operativo=False,
                  simulado="gerente_pais")
        r = _post(monkeypatch, accion, alcance=sim)
        assert r.status_code == 303 and r.headers["location"] == "/portal?cliente=cli&dias=30&aviso=simulacion"
        assert servicio.args == [] and servicio.auditados == []

    def test_sin_permiso_es_404_sin_auditar(self, monkeypatch, servicio, accion):
        r = _post(monkeypatch, accion, alcance=_alcance(roles=("ceo",), clientes=("cli",), cambia=()))
        assert r.status_code == 404 and r.text == _no_existe(monkeypatch)
        assert servicio.args == [] and servicio.auditados == []

    def test_pais_ajeno_es_404_y_se_audita(self, monkeypatch, servicio, accion):
        r = _post(monkeypatch, accion, cliente="otro-pais")
        assert r.status_code == 404 and r.text == _no_existe(monkeypatch) and servicio.args == []
        assert servicio.auditados == [("ACCESO_FUERA_DE_ALCANCE", {"usuario_id": "u1", "recurso": "cliente=otro-pais"})]

    def test_gid_ajeno_es_404_y_se_audita(self, monkeypatch, servicio, accion):
        servicio.resultado = portal.oportunidades.NO_ENCONTRADO
        r = _post(monkeypatch, accion)
        assert r.status_code == 404 and r.text == _no_existe(monkeypatch)
        ((accion_aud, kw),) = servicio.auditados
        assert accion_aud == "ACCESO_FUERA_DE_ALCANCE" and kw["usuario_id"] == "u1"
        assert kw["recurso"].startswith(f"{accion}:comprasal_sv:96-2026-P00") and "cliente=cli" in kw["recurso"]

    def test_adb_caida_es_503(self, monkeypatch, servicio, accion):
        def boom(*a, **k):
            raise RuntimeError("adb caída")

        monkeypatch.setattr(portal.oportunidades, RUTAS[accion][1], boom, raising=False)
        r = _post(monkeypatch, accion)
        assert r.status_code == 503 and "adb" not in r.text.lower()


def test_quitar_responsable_llega_como_none(monkeypatch, servicio):
    _post(monkeypatch, "responsable", responsable="  ")
    assert servicio.args[0][3] is None


@pytest.mark.parametrize("codigo,texto", [
    ("asignado", "Responsable actualizado."), ("recuperado", "Oportunidad recuperada"),
    ("responsable_invalido", "no puede ser responsable"), ("no_recuperable", "no se puede recuperar"),
])
def test_avisos_nuevos(monkeypatch, codigo, texto):
    r, _ = _get(monkeypatch, f"/portal?aviso={codigo}")
    assert texto in r.text


# ── Lo que no cambia ───────────────────────────────────────────────────────────

def test_caida_de_bd_en_la_vista_es_503(monkeypatch):
    _parchear_kpis(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("adb caída")

    monkeypatch.setattr(portal.tablero, "procesos_del_rango", boom)
    r = _cli(monkeypatch, _alcance(**GERENTE)).get("/portal")
    assert r.status_code == 503 and "adb" not in r.text.lower()


def test_bajo_simulacion_la_plantilla_recibe_solo_vista_y_los_controles_van_deshabilitados(monkeypatch):
    from src.identidad.alcance import Alcance
    ctx = _parchear_kpis(monkeypatch)
    sim = Alcance(usuario_id="u-admin", org_id="org", roles=frozenset({"gerente_pais"}), clientes=frozenset({"cli"}),
                  cambia_estado=frozenset({"cli"}), ve_operativo=False,
                  simulado="gerente_pais")
    r = _cli(monkeypatch, sim).get("/portal")
    assert r.status_code == 200 and ctx["solo_vista"] is True and ctx["puede_cambiar"] is True
    assert "<fieldset disabled>" in r.text


def test_vistas_regional_y_operativa_intactas(monkeypatch):
    _parchear_kpis(monkeypatch)
    regional = _cli(monkeypatch, _alcance(roles=("ceo",), clientes=("sv", "gt"))).get("/portal/regional")
    assert regional.status_code == 200 and "12 oportunidades" in regional.text
    operativo = _cli(monkeypatch).get("/portal/operativo")
    assert operativo.status_code == 200 and "42 procesos" in operativo.text


_PARCIALES = Path(__file__).resolve().parents[1] / "templates" / "portal_pais_tablero.html"


@pytest.mark.skipif(not _PARCIALES.exists(), reason="los parciales de E11 (agente D) aún no están en esta rama")
def test_humo_la_plantilla_real_pinta_el_contexto(monkeypatch):
    """Tras reconciliar: la plantilla REAL renderiza el contexto de `vista_pais` sin errores ni XSS."""
    _parchear_kpis(monkeypatch)
    monkeypatch.setattr(portal, "pagina", __import__("src.web.plantillas", fromlist=["pagina"]).pagina)
    filas = filas_base()
    filas[0]["titulo"] = "<script>alert(1)</script>"
    from test_web_portal import _parchear_e11
    _parchear_e11(monkeypatch, filas=filas)
    for alcance in (_alcance(**GERENTE), _alcance(roles=("ceo",), clientes=("cli",), cambia=())):
        r = _cli(monkeypatch, alcance).get("/portal")
        assert r.status_code == 200 and "96-2026-P0011" in r.text
        assert "<script>alert(1)</script>" not in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]


def test_estaticos_sin_cambio():
    assert TestClient(crear_app(_cfg())).get("/portal/static/charts/tooltip.js").status_code == 200
