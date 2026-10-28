"""E11 — vista de país «qué hago hoy»: contexto del contrato, filtros, y las dos escrituras nuevas.

Lo que lee la base (`analytics.tablero`, `usuarios_asignables`) y los servicios de escritura se simulan;
`portal_pais.html` se sustituye por el render mínimo del contrato (`STUB_PAIS`). La prueba de humo del
final pinta la plantilla REAL cuando ya existen los parciales de E11.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
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


def _dias_del_calendario(monkeypatch, url="/portal", **parche):
    """Los días que la vista le pasa a `graficos.barras_dias` (el contexto ya no los lleva) y el contexto."""
    capturado = []
    real = portal.graficos.barras_dias

    def espia(dias, *a, **k):
        capturado.append(dias)
        return real(dias, *a, **k)

    ctx = _parchear_kpis(monkeypatch)
    if parche:
        from test_web_portal import _parchear_e11
        _parchear_e11(monkeypatch, **parche)
    monkeypatch.setattr(portal.graficos, "barras_dias", espia)
    _cli(monkeypatch, _alcance(**GERENTE)).get(url)
    return capturado[-1], ctx


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
        assert set(ctx["kpis"]["relevancia"]) == {"pct_aptos", "aptos", "revisados", "relevantes", "pct_relevantes",
                                                  "delta_pts", "spark"}
        assert set(ctx["kpis"]["estancadas"]) == {"n", "dias"}
        # E11.1: el calendario ya no lleva la lista de días (solo el SVG con sus enlaces `?dia=`).
        assert set(ctx["cierres"]) == {"svg", "dia"} and isinstance(ctx["cierres"]["svg"], Markup)
        assert all(set(a) == {"chip", "clase", "titulo", "detalle", "gid"} for a in ctx["atencion"])
        assert all(a["clase"] in ("rojo", "ambar", "crema") for a in ctx["atencion"])
        assert set(ctx["tablero"]) == {"columnas", "vencidas", "asignables", "filtros", "truncado"}
        assert set(ctx["tablero"]["vencidas"]) == {"n"} and isinstance(ctx["tablero"]["vencidas"]["n"], int)
        assert set(ctx["tablero"]["filtros"]) == {"responsable", "marca", "opciones"}
        assert set(ctx["tablero"]["filtros"]["opciones"]) == {"responsables", "marcas"}
        assert all(set(o) == {"valor", "etiqueta"} for o in ctx["tablero"]["filtros"]["opciones"]["responsables"])
        assert all(isinstance(m, str) for m in ctx["tablero"]["filtros"]["opciones"]["marcas"])
        for c in ctx["tablero"]["columnas"]:
            assert set(c) == {"clave", "titulo", "n", "mas", "tarjetas"}
            assert c["mas"] == c["n"] - len(c["tarjetas"]) and len(c["tarjetas"]) <= 8
            for t in c["tarjetas"]:
                assert set(t) >= {"gid", "referencia", "titulo", "institucion", "url", "match", "cierre", "marcas",
                                  "monto_txt", "responsable", "responsable_id", "dias_en_estado", "voto",
                                  "siguientes", "exige_motivo", "token", "estado"}
        cl = ctx["clasificacion"]
        assert set(cl) == {"revisados", "relevantes", "no_relevantes", "grupos", "barra", "motivos", "descartados",
                           "total_descartados", "hay_mas", "desde", "truncado"}
        assert set(cl["grupos"]) == {"decidir", "no_competible", "baja"}
        assert all(set(d) == {"gid", "referencia", "titulo", "match", "motivo", "token"} for d in cl["descartados"])
        assert all(isinstance(m, tuple) and len(m) == 2 for m in cl["motivos"])
        p = ctx["kpis"]["decidir"]["primera"]
        assert p is None or set(p) == {"gid", "titulo", "institucion", "texto", "dias"}
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
        dias, ctx = _dias_del_calendario(monkeypatch)
        assert len(dias) == 14 and dias[0]["fecha"] == date(2026, 10, 27) and dias[-1]["fecha"] == date(2026, 11, 9)
        por_fecha = {d["fecha"]: d for d in dias}
        # Solo lo «para decidir»: P0011 (vie 30). El no competible de hoy y el de nov 20 no cuentan.
        assert por_fecha[date(2026, 10, 30)]["n"] == 1 and sum(d["n"] for d in dias) == 1
        assert por_fecha[date(2026, 10, 30)]["urgencia"] == "rojo"
        assert por_fecha[date(2026, 11, 9)]["urgencia"] == "ambar"
        assert por_fecha[date(2026, 10, 30)]["href"] == "/portal?cliente=cli&dias=90&dia=2026-10-30"
        assert ctx["cierres"]["dia"] is None and "dia=2026-10-30" in ctx["cierres"]["svg"]

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
        # Mismo stub para 90 y 180 días → sin ventana previa: sin delta.
        rel = k["relevancia"]
        assert (rel["pct_aptos"], rel["aptos"], rel["revisados"], rel["relevantes"], rel["pct_relevantes"]) == (
            8.0, 8, 100, 16, 16.0)
        assert rel["delta_pts"] is None

    def test_delta_de_relevancia_contra_el_rango_previo_de_igual_longitud(self, monkeypatch):
        datos = {30: {"revisados": 100, "relevantes": 16, "aptos": 8, "pct_aptos": 8.0, "pct_relevantes": 16.0},
                 60: {"revisados": 300, "relevantes": 40, "aptos": 18, "pct_aptos": 6.0, "pct_relevantes": 13.3}}
        _, ctx = _get(monkeypatch, "/portal?dias=30", relevancia=datos)
        assert ctx["kpis"]["relevancia"]["delta_pts"] == 3.0  # 8 % ahora vs 10/200 = 5 % antes
        assert ctx["kpis"]["relevancia"]["pct_aptos"] == 8.0

    @pytest.mark.parametrize("datos", [
        {"revisados": 0, "relevantes": 0, "aptos": 0, "pct_aptos": None, "pct_relevantes": None},
        {},
    ])
    def test_relevancia_sin_revisados_no_divide_por_cero(self, monkeypatch, datos):
        r, ctx = _get(monkeypatch, relevancia=datos)
        assert r.status_code == 200
        rel = ctx["kpis"]["relevancia"]
        assert rel["pct_aptos"] is None and rel["pct_relevantes"] is None and rel["delta_pts"] is None
        assert rel["revisados"] == rel["aptos"] == rel["relevantes"] == 0
        assert ctx["clasificacion"]["no_relevantes"] == 0

    def test_la_relevancia_vieja_ya_no_se_usa(self, monkeypatch):
        _parchear_kpis(monkeypatch)

        def vieja(*a, **k):
            raise AssertionError("pct_relevancia_clasificador no debe usarse en la vista de país")

        monkeypatch.setattr(portal.pais, "pct_relevancia_clasificador", vieja)
        assert _cli(monkeypatch, _alcance(**GERENTE)).get("/portal").status_code == 200

    def test_pipeline_sin_montos_es_none_y_nunca_cero(self, monkeypatch):
        for pipeline in ({"monto_usd": 0, "activas": 5, "con_monto": 0}, {"monto_usd": None, "activas": 5}):
            _, ctx = _get(monkeypatch, pipeline=pipeline)
            assert ctx["kpis"]["pipeline"] == {"monto_usd": None, "activas": 5, "con_monto": 0}

    def test_primera_y_atencion_con_titulo_largo_recortado(self, monkeypatch):
        filas = filas_base()
        filas[0]["titulo"] = "Adquisición de " + "servidores " * 20
        _, ctx = _get(monkeypatch, filas=filas)
        primera = ctx["kpis"]["decidir"]["primera"]
        assert len(primera["titulo"]) <= 80 and primera["titulo"].endswith("…")
        assert primera["texto"] == "Cierra vie 30 oct · 3 días" and primera["dias"] == 3
        assert primera["institucion"] == "Min Salud" and primera["gid"] == "comprasal_sv:96-2026-P0011"
        assert ctx["atencion"][0]["titulo"].endswith("…")
        assert all(len(a["titulo"]) <= 90 for a in ctx["atencion"])

    def test_recorta(self):
        assert portal._recorta("corto", 80) == "corto" and portal._recorta(None, 80) == ""
        assert portal._recorta("x" * 100, 80) == "x" * 79 + "…"

    def test_clasificacion(self, monkeypatch):
        from test_web_portal import _parchear_e11
        ctx = _parchear_kpis(monkeypatch)
        reg = _parchear_e11(monkeypatch)
        assert _cli(monkeypatch, _alcance(**GERENTE)).get("/portal").status_code == 200
        c = ctx["clasificacion"]
        # revisados/relevantes de `relevancia_estricta`; los grupos del rango (decidir sin lo ya ganado).
        assert (c["revisados"], c["relevantes"], c["no_relevantes"]) == (100, 16, 84)
        assert c["grupos"] == {"decidir": 2, "no_competible": 1, "baja": 1}
        assert c["motivos"] == [("Fuera de rubro", 1)]
        assert c["descartados"] == [{"gid": "comprasal_sv:96-2026-P0014", "referencia": "96-2026-P0014",
                                     "titulo": "Mobiliario", "match": 70, "motivo": "Fuera de rubro", "token": ""}]
        assert c["hay_mas"] is False and c["total_descartados"] == 1 and isinstance(c["barra"], Markup)
        assert c["desde"] == datetime(2026, 10, 27, 13, 0) and c["truncado"] is False
        assert ("descartados_del_rango", ("cli", 90), {"limite": 10}) in reg.llamadas
        assert ("relevancia_estricta", ("cli", 90), {}) in reg.llamadas
        assert ("relevancia_estricta", ("cli", 180), {}) in reg.llamadas

    def test_barra_de_clasificacion_con_sus_segmentos(self, monkeypatch):
        capturado = []
        _parchear_kpis(monkeypatch)
        monkeypatch.setattr(portal.graficos, "barra_apilada", lambda seg: capturado.append(seg) or Markup("<b/>"))
        _cli(monkeypatch, _alcance(**GERENTE)).get("/portal")
        assert capturado == [[("Para decidir", 2, "critico"), ("No competibles", 1, "muted"),
                              ("Baja prioridad", 1, "aviso"), ("No relevantes", 84, "borde")]]

    def test_sin_descartados_guardados_desde_es_none(self, monkeypatch):
        _, ctx = _get(monkeypatch, descartados={"filas": [], "total": 0, "desde": None})
        c = ctx["clasificacion"]
        assert c["descartados"] == [] and c["total_descartados"] == 0 and c["hay_mas"] is False
        assert c["desde"] is None

    def test_descartados_hay_mas_segun_el_total(self, monkeypatch):
        guardados = {"filas": [{"proceso_gid": f"comprasal_sv:D{i:02d}", "numero_referencia": f"D{i:02d}",
                                "titulo": "<b>x</b>", "institucion": "MH", "match": 90 - i,
                                "motivo_descarte": "fuera del portafolio", "token": f"t{i}"} for i in range(10)],
                     "total": 37, "desde": datetime(2026, 10, 20)}
        _, ctx = _get(monkeypatch, descartados=guardados)
        c = ctx["clasificacion"]
        assert len(c["descartados"]) == 10 and c["total_descartados"] == 37 and c["hay_mas"] is True
        assert c["descartados"][0] == {"gid": "comprasal_sv:D00", "referencia": "D00", "titulo": "<b>x</b>",
                                       "match": 90, "motivo": "fuera del portafolio", "token": "t0"}

    def test_un_descartado_recuperado_esta_en_nuevo_y_no_en_motivos(self, monkeypatch):
        filas = [_fila("R1", "descartado", estado="NUEVO", token="2026-10-26T10:00:00.000000",
                       actualizado_en=datetime(2026, 10, 26, 10))]
        _, ctx = _get(monkeypatch, filas=filas, votos={"comprasal_sv:R1": {"relevante": 2, "irrelevante": 0}})
        assert [t["referencia"] for t in ctx["tablero"]["columnas"][0]["tarjetas"]] == ["R1"]
        assert ctx["clasificacion"]["motivos"] == []
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
        assert ctx["clasificacion"]["grupos"] == {"decidir": 0, "no_competible": 0, "baja": 0}
        assert ctx["tablero"]["vencidas"] == {"n": 0}
        assert ctx["votos"] == {"relevante": 0, "irrelevante": 0, "motivos": [], "desacuerdos": [], "pocos": True}
        assert all(c["mas"] == 0 for c in ctx["tablero"]["columnas"])
        assert ctx["tablero"]["filtros"]["opciones"] == {
            "responsables": [], "marcas": []}

    def test_truncado(self, monkeypatch):
        _, ctx = _get(monkeypatch, truncado=True)
        assert ctx["tablero"]["truncado"] is True

    def test_zona_invalida_cae_a_el_salvador(self, monkeypatch):
        monkeypatch.setattr(portal.portales, "portal_de_cliente", lambda d, c: "no-existe")
        assert portal._zona(_cfg(), "cli").key == "America/El_Salvador"


# ── E11.1: vencidas, tope por columna, marcas y volumen real ───────────────────

def _decidir(ref, dias, **kw):
    """Una fila «para decidir» que cierra en `dias` (negativo = vencida; None = sin fecha)."""
    texto = None if dias is None else f"Cierra en {dias} días"
    return _fila(ref, "decidir", dias_restantes=dias, cierre_texto=texto,
                 fecha_limite=None if dias is None else datetime(2026, 10, 27, 20, 0) + timedelta(days=dias), **kw)


class TestVencidasYTope:
    def test_vencidas_fuera_de_para_decidir_y_del_tablero(self, monkeypatch):
        filas = [_decidir("OK-1", 2), _decidir("V-1", -1), _decidir("V-2", -30),
                 _fila("V-3", "baja", dias_restantes=-3, cierre_texto="Cerró"),
                 _decidir("EV-1", -5, estado="EVALUANDO", token="2026-10-26T10:00:00.000000",
                          actualizado_en=datetime(2026, 10, 26, 10), responsable_id="u2")]
        dias, ctx = _dias_del_calendario(monkeypatch, filas=filas)
        assert ctx["tablero"]["vencidas"] == {"n": 3}
        col = {c["clave"]: [t["referencia"] for t in c["tarjetas"]] for c in ctx["tablero"]["columnas"]}
        assert col["nuevo"] == ["OK-1"] and col["evaluando"] == ["EV-1"]
        assert ctx["kpis"]["decidir"]["n"] == 1 and ctx["kpis"]["decidir"]["primera"]["gid"] == "comprasal_sv:OK-1"
        assert ctx["clasificacion"]["grupos"]["decidir"] == 1
        assert [a["gid"] for a in ctx["atencion"] if a["chip"].startswith("Cierra")] == ["comprasal_sv:OK-1"]
        assert ctx["cabecera"]["veredicto"] == "Hoy hay 1 oportunidad que cierra esta semana y 1 sin responsable."
        assert sum(d["n"] for d in dias) == 1

    def test_solo_vencidas_no_hay_nada_para_decidir(self, monkeypatch):
        _, ctx = _get(monkeypatch, filas=[_decidir(f"V-{i}", -i - 1) for i in range(4)])
        assert ctx["kpis"]["decidir"] == {"n": 0, "semana": 0, "primera": None}
        assert ctx["cabecera"]["veredicto"] == "Nada urgente hoy." and ctx["atencion"] == []
        assert ctx["tablero"]["vencidas"] == {"n": 4} and all(c["n"] == 0 for c in ctx["tablero"]["columnas"])

    def test_tope_de_ocho_por_columna_con_mas_y_orden_por_urgencia(self, monkeypatch):
        filas = [_decidir(f"N-{i:02d}", d) for i, d in enumerate([9, None, 3, 12, 0, None, 5, 1, 20, 2, 7, 4])]
        _, ctx = _get(monkeypatch, filas=filas)
        nuevo = ctx["tablero"]["columnas"][0]
        assert nuevo["n"] == 12 and nuevo["mas"] == 4 and len(nuevo["tarjetas"]) == 8
        assert [t["referencia"] for t in nuevo["tarjetas"]] == [
            "N-04", "N-07", "N-09", "N-02", "N-11", "N-06", "N-10", "N-00"]  # 0,1,2,3,4,5,7,9 días
        assert all(c["mas"] == 0 for c in ctx["tablero"]["columnas"][1:])

    def test_sin_fecha_al_final(self, monkeypatch):
        _, ctx = _get(monkeypatch, filas=[_decidir("SF", None), _decidir("C", 30)])
        assert [t["referencia"] for t in ctx["tablero"]["columnas"][0]["tarjetas"]] == ["C", "SF"]

    def test_n_cuenta_lo_filtrado_y_mas_lo_que_no_cabe(self, monkeypatch):
        filas = [_decidir(f"D-{i:02d}", 3, marcas=["Dell"]) for i in range(10)]
        filas += [_decidir(f"H-{i:02d}", 3, marcas=["HPE"]) for i in range(3)]
        _, ctx = _get(monkeypatch, "/portal?marca=HPE", filas=filas)
        nuevo = ctx["tablero"]["columnas"][0]
        assert (nuevo["n"], nuevo["mas"], len(nuevo["tarjetas"])) == (3, 0, 3)
        _, ctx = _get(monkeypatch, "/portal?marca=Dell", filas=filas)
        nuevo = ctx["tablero"]["columnas"][0]
        assert (nuevo["n"], nuevo["mas"], len(nuevo["tarjetas"])) == (10, 2, 8)
        # Los KPIs no se filtran.
        assert ctx["kpis"]["decidir"]["n"] == 13


class TestMarcas:
    def _filas(self):
        """36 marcas: M00 en 36 tarjetas, M01 en 35, …, M35 en 1."""
        filas = []
        for i in range(36):
            filas.append(_decidir(f"P-{i:02d}", 3, marcas=[f"M{j:02d}" for j in range(36 - i)]))
        return filas

    def test_ocho_mas_frecuentes(self, monkeypatch):
        _, ctx = _get(monkeypatch, filas=self._filas())
        assert ctx["tablero"]["filtros"]["opciones"]["marcas"] == [f"M{j:02d}" for j in range(8)]

    def test_la_elegida_fuera_del_top_se_valida_contra_todas_y_se_agrega(self, monkeypatch):
        _, ctx = _get(monkeypatch, "/portal?marca=M30", filas=self._filas())
        f = ctx["tablero"]["filtros"]
        assert f["marca"] == "M30" and f["opciones"]["marcas"] == [f"M{j:02d}" for j in range(8)] + ["M30"]
        assert ctx["tablero"]["columnas"][0]["n"] == 6  # P-00..P-05 llevan M30

    def test_marca_que_no_esta_en_el_tablero_se_ignora(self, monkeypatch):
        _, ctx = _get(monkeypatch, "/portal?marca=M99", filas=self._filas())
        assert ctx["tablero"]["filtros"]["marca"] is None and len(ctx["tablero"]["filtros"]["opciones"]["marcas"]) == 8

    def test_marcas_de_vencidas_o_descartadas_no_entran(self, monkeypatch):
        filas = [_decidir("A", 3, marcas=["Dell"]), _decidir("V", -2, marcas=["Cisco"]),
                 _fila("X", "descartado", marcas=["Lenovo"])]
        _, ctx = _get(monkeypatch, "/portal?marca=Cisco", filas=filas)
        assert ctx["tablero"]["filtros"]["opciones"]["marcas"] == ["Dell"]
        assert ctx["tablero"]["filtros"]["marca"] is None


def test_contexto_con_trescientas_filas(monkeypatch):
    """El volumen de DEV (≈ 260-300 procesos): nada crece sin tope."""
    filas = []
    for i in range(300):
        d = (i % 40) - 10  # 10 de cada 40 vencidas
        grupo = ("decidir", "decidir", "no_competible", "baja", "descartado", "pendiente")[i % 6]
        filas.append(_fila(f"P-{i:03d}", grupo, dias_restantes=d, cierre_texto=f"Cierra en {d} días",
                           fecha_limite=datetime(2026, 10, 27, 20, 0) + timedelta(days=d),
                           marcas=[f"Marca{i % 36}"], titulo="Título muy largo " * 10))
    r, ctx = _get(monkeypatch, filas=filas, truncado=True)
    assert r.status_code == 200
    nuevo = ctx["tablero"]["columnas"][0]
    en_tablero = [f for f in filas if f["grupo"] in ("decidir", "no_competible", "baja")]
    vencidas = sum(1 for f in en_tablero if f["dias_restantes"] < 0)
    assert ctx["tablero"]["vencidas"]["n"] == vencidas
    assert nuevo["n"] == len(en_tablero) - vencidas and len(nuevo["tarjetas"]) == 8
    assert nuevo["mas"] == nuevo["n"] - 8
    assert all(t["cierre"]["texto"].startswith("Cierra en 0") for t in nuevo["tarjetas"][:2])
    assert len(ctx["tablero"]["filtros"]["opciones"]["marcas"]) <= 8
    assert len(ctx["atencion"]) <= 8 and all(len(a["titulo"]) <= 90 for a in ctx["atencion"])
    decidir = [f for f in filas if f["grupo"] == "decidir" and f["dias_restantes"] >= 0]
    assert ctx["kpis"]["decidir"]["n"] == len(decidir)
    assert len(ctx["kpis"]["decidir"]["primera"]["titulo"]) <= 80
    assert ctx["tablero"]["truncado"] is True and ctx["clasificacion"]["truncado"] is True
    assert ctx["clasificacion"]["motivos"] == [("Sin motivo registrado", 50)]


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


def _parchear_e11_real(monkeypatch, filas, tablero_real):
    """Como `_parchear_e11`, pero `clasificar` es la real: lo demás que toca la base sigue simulado."""
    from test_web_portal import _parchear_e11
    reg = _parchear_e11(monkeypatch, filas=filas)
    monkeypatch.setattr(portal.tablero, "clasificar", tablero_real.clasificar, raising=False)
    return reg


# ── Contrato real tablero → portal (sin stub de `clasificar`) ─────────────────

class TestContratoConClasificarReal:
    """La vista lee `estado`, `fecha_limite` y `actualizado_en`; `procesos_del_rango` entrega
    `op_estado`, `fecha_limite_utc` y `op_actualizado_en`. Con `clasificar` stubeada esa costura no se ve."""

    def _fila_cruda(self, ref, **kw):
        base = {
            "proceso_gid": f"comprasal_sv:{ref}", "numero_referencia": ref, "titulo": f"Proceso {ref}",
            "institucion": "MH", "url": "https://www.comprasal.gob.sv/x", "modalidad_raw": "Licitación",
            "tz_portal": "America/El_Salvador", "fecha_limite_utc": None, "monto_usd": None, "monto_estimado": None,
            "match_tecnico": 0.9, "match_categoria": 0.9, "factibilidad": 0.9, "triaje": "GO",
            "estado_cp": "NOTIFICADO", "es_relevante": True, "categoria_detectada": "", "marcas": ["Dell"],
            "razon": "", "matriz_riesgos": None, "severidad_max": None, "resumen": None, "op_estado": None,
            "responsable_id": None, "responsable_nombre": None, "op_actualizado_en": None, "token": "",
        }
        return {**base, **kw}

    def _contexto(self, monkeypatch, filas):
        from src.analytics import tablero as tablero_real
        _, ctx = _get(monkeypatch)
        _parchear_e11_real(monkeypatch, filas, tablero_real)
        r = _cli(monkeypatch, _alcance(**GERENTE)).get("/portal")
        assert r.status_code == 200
        return ctx

    def test_ofertado_va_a_su_columna_y_el_cierre_entra_al_calendario(self, monkeypatch):
        filas = [
            self._fila_cruda("A-1", op_estado="OFERTADO", fecha_limite_utc=datetime(2026, 10, 30, 18, 0),
                             op_actualizado_en=datetime(2026, 10, 20, 12, 0), token="t1", monto_usd=1.0,
                             monto_estimado=9000.0),
            self._fila_cruda("A-2", fecha_limite_utc=datetime(2026, 10, 29, 18, 0)),
            self._fila_cruda("A-3", op_estado="GANADO", op_actualizado_en=datetime(2026, 10, 1, 12, 0), token="t3"),
        ]
        ctx = self._contexto(monkeypatch, filas)
        columnas = {c["clave"]: {t["referencia"] for t in c["tarjetas"]} for c in ctx["tablero"]["columnas"]}
        assert any("A-1" in refs for clave, refs in columnas.items() if "OFERT" in clave.upper())
        assert any("A-3" in refs for clave, refs in columnas.items() if "CERR" in clave.upper())
        assert any("A-2" in refs for clave, refs in columnas.items() if "NUEV" in clave.upper())
        assert ctx["kpis"]["decidir"]["n"] == 1                       # solo A-2: lo ofertado o ganado no se decide
        assert "dia=2026-10-29" in ctx["cierres"]["svg"]              # A-2 en el calendario...
        assert "dia=2026-10-30" not in ctx["cierres"]["svg"]          # ...A-1 ya se ofertó: no es una decisión

    def test_lo_que_sigue_en_curso_no_es_descartado_ni_esta_en_el_tablero(self, monkeypatch):
        filas = [self._fila_cruda("B-1", estado_cp="DESCUBIERTO", es_relevante=None, triaje=None),
                 self._fila_cruda("B-2", estado_cp="DESCARTADO_REGEX", es_relevante=False, triaje=None)]
        ctx = self._contexto(monkeypatch, filas)
        assert ctx["clasificacion"]["motivos"] == [("filtro de texto", 1)]
        assert all(c["n"] == 0 for c in ctx["tablero"]["columnas"]) and ctx["tablero"]["vencidas"] == {"n": 0}

    def test_filas_crudas_vencidas_tope_y_kpis(self, monkeypatch):
        """Filas como las de `procesos_del_rango` (cierres en UTC, `op_estado`…): vencidas fuera, tope de 8."""
        filas = [self._fila_cruda(f"N-{i:02d}", fecha_limite_utc=datetime(2026, 10, 28 + i % 3, 18, 0))
                 for i in range(12)]
        filas += [self._fila_cruda(f"V-{i}", fecha_limite_utc=datetime(2026, 10, 20, 18, 0)) for i in range(5)]
        filas += [self._fila_cruda("S-1"),  # sin fecha: al final
                  self._fila_cruda("E-1", op_estado="EVALUANDO", fecha_limite_utc=datetime(2026, 10, 20, 18, 0),
                                   op_actualizado_en=datetime(2026, 10, 25, 12, 0), token="te")]
        ctx = self._contexto(monkeypatch, filas)
        col = {c["clave"]: c for c in ctx["tablero"]["columnas"]}
        nuevo = col["nuevo"]
        assert nuevo["n"] == 13 and len(nuevo["tarjetas"]) == 8 and nuevo["mas"] == 5
        refs = [t["referencia"] for t in nuevo["tarjetas"]]
        assert not any(r.startswith("V-") for r in refs) and "S-1" not in refs
        assert [t["cierre"]["texto"][:13] for t in nuevo["tarjetas"]][:4] == ["Cierra mié 28"] * 4
        assert ctx["tablero"]["vencidas"] == {"n": 5}
        # E-1 está en evaluación aunque venció: sigue en su columna, pero no es «para decidir».
        assert [t["referencia"] for t in col["evaluando"]["tarjetas"]] == ["E-1"]
        assert ctx["kpis"]["decidir"]["n"] == 13 and ctx["kpis"]["decidir"]["semana"] == 12
        assert ctx["kpis"]["decidir"]["primera"]["dias"] == 1
        assert ctx["cabecera"]["veredicto"] == ("Hoy hay 12 oportunidades que cierran esta semana "
                                                "y 13 sin responsable.")
        assert ctx["tablero"]["filtros"]["opciones"]["marcas"] == ["Dell"]
