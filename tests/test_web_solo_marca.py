"""E14 — vistas «solo Oracle» del portal (capa web): no-fuga con datos mixtos.

La capa de datos (`pais`, `tablero`, `regional`, `mercado_repo`) se simula con dobles que **exigen** el keyword
`solo_marcas` (sin valor por defecto): si `portal.py` olvida propagarlo a una lectura, el doble levanta
`TypeError`, la vista responde 503 y el test falla. Con restricción, los dobles filtran como la capa real
(marca `Oracle*` en `marcas`, o «oracle» como palabra completa en el título) y devuelven las cifras globales
como None, igual que `pais.relevancia_estricta` / `tablero.tendencia_semanal`.

Para cada rol Oracle —real y «ver como»— el HTML real (plantillas de verdad, no el stub) de `/portal?client=…`
y `/portal/regional` no contiene ninguna cadena de las filas no-Oracle (título, institución, referencia, marca)
ni las cifras del universo global (revisados, %). `ceo`, con los MISMOS datos, sí las ve (contraste).
"""
from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_web_portal import HOY, _fila

from src.identidad import alcance as alcance_mod
from src.identidad import politica, sesiones
from src.oportunidades import repo as oportunidades_repo
from src.web import csrf as csrf_mod
from src.web import portal, sesion
from src.web.app import crear_app

BASE = "https://dev.amalia.test"
SECRETO = "secreto-de-prueba"
ROLES_ORACLE = sorted(politica.ROLES_SOLO_MARCA)
ACTIVOS = ("sv", "gt")

_OP = {"estado": "status", "responsable": "owner", "recuperar": "recover"}  # acción interna → ruta en inglés


def _cfg():
    return SimpleNamespace(enable_portal=True, portal_cliente_id="sv", enable_feedback=False, daily_budget_usd=5.0,
                           identidad_secret=SECRETO, feedback_base_url=BASE, clients_dir="clients",
                           sesion_ttl_horas=12, sesion_max_dias=7)


# ── datos mixtos ────────────────────────────────────────────────────────────────

def _f(ref, grupo, titulo, institucion, marcas, **kw):
    return _fila(ref, grupo, titulo=titulo, institucion=institucion, marcas=marcas, **kw)


ORACLE = [
    _f("ORA-0001", "decidir", "Licenciamiento de base de datos empresarial", "INSTITUCION ALFA ORA", ["Oracle"],
       fecha_limite=datetime(2026, 10, 30, 21, 0), dias_restantes=3, urgencia="rojo", cierre_texto="Cierra vie 30 oct"),
    # Oracle como alternativa, con co-marca de otro fabricante (la capa de datos la quita de la tarjeta).
    _f("ORA-0002", "decidir", "Plataforma de nube híbrida", "INSTITUCION BETA ORA", ["Dell", "Oracle"],
       estado="EVALUANDO", token="2026-10-01T10:00:00", actualizado_en=datetime(2026, 10, 1, 10, 0),
       responsable_id="u9", responsable_nombre="Rosa"),
    _f("ORA-0003", "baja", "ERP financiero en la nube", "INSTITUCION GAMMA ORA", ["Oracle NetSuite"]),
    # Sin marca, pero «oracle» como palabra completa en el título (respaldo).
    _f("ORA-0004", "no_competible", "Migración de base de datos oracle a la nube", "INSTITUCION DELTA ORA", []),
    _f("ORA-0005", "decidir", "Soporte de Exadata", "INSTITUCION EPSILON ORA", ["Oracle"], estado="GANADO",
       token="2026-10-20T10:00:00", actualizado_en=datetime(2026, 10, 20, 10, 0)),
    _f("ORA-0006", "descartado", "Respaldo de oracle en sitio alterno", "INSTITUCION ZETA ORA", [],
       motivo_descarte="Fuera de rubro"),
]
NO_ORACLE = [
    _f("NOORA-0001", "decidir", "Switches Catalyst sede Quetzal", "ALCALDIA QUETZALTEPEQUE", ["Cisco"],
       fecha_limite=datetime(2026, 10, 29, 21, 0), dias_restantes=2, urgencia="rojo", cierre_texto="Cierra jue 29 oct"),
    _f("NOORA-0002", "decidir", "Firewall perimetral Omega", "HOSPITAL OMEGA NORTE", ["Fortinet"],
       estado="EVALUANDO", token="2026-10-01T10:00:00", actualizado_en=datetime(2026, 10, 1, 10, 0),
       responsable_id="u8", responsable_nombre="Tobías"),
    _f("NOORA-0003", "baja", "Servidores PowerEdge Kappa", "INSTITUTO KAPPA SUR", ["Dell"]),
    # «Oraclexyz» NO es la palabra «oracle»: no entra.
    _f("NOORA-0004", "no_competible", "Plataforma Oraclexyz de inventario Sigma", "MINISTERIO SIGMA ESTE",
       ["Microsoft"]),
    _f("NOORA-0005", "descartado", "Mobiliario de oficina Lambda", "CORTE LAMBDA OESTE", [],
       motivo_descarte="Fuera de rubro"),
    _f("NOORA-0006", "decidir", "Licencias de ofimática Theta", "UNIVERSIDAD THETA CENTRAL", ["Microsoft"],
       estado="GANADO", token="2026-10-20T10:00:00", actualizado_en=datetime(2026, 10, 20, 10, 0)),
]
TODAS = ORACLE + NO_ORACLE

# Cadenas que un rol Oracle NUNCA debe ver: títulos, instituciones, referencias y marcas de los no-Oracle.
PROHIBIDAS = sorted({s for f in NO_ORACLE for s in (f["titulo"], f["institucion"], f["numero_referencia"])}
                    | {"Cisco", "Fortinet", "Dell", "Microsoft", "Tobías"})
# Cifras del universo global (revisados, %) con formato distintivo.
REVISADOS_GLOBAL, CORRIDA_REVISADOS = 8642, 4321
CIFRAS_GLOBALES = ("8642", "8.642", "4321", "4.321", "1,4%", "3,7%", "9.999", "9999", "7531", "7.531")


def sin_tokens(html: str) -> str:
    """El HTML sin tokens hexadecimales (CSRF, nonces): son aleatorios y podrían contener «4321» por azar."""
    return re.sub(r"[0-9a-fA-F]{16,}", "", html)


def _es_oracle(f) -> bool:
    return (any(str(m).lower().startswith("oracle") for m in f.get("marcas") or [])
            or re.search(r"(^|[^0-9A-Za-z_])oracle([^0-9A-Za-z_]|$)", f.get("titulo") or "", re.I) is not None)


def _de(solo_marcas, filas=TODAS):
    if not solo_marcas:
        return [dict(f) for f in filas]
    salida = []
    for f in filas:
        if _es_oracle(f):
            g = dict(f)
            g["marcas"] = [m for m in f["marcas"] if str(m).lower().startswith("oracle")]
            salida.append(g)
    return salida


def simular_datos(monkeypatch, filas=TODAS):
    """Simula toda lectura del portal. Cada doble EXIGE `solo_marcas` (keyword sin defecto) y lo anota."""

    def de(solo_marcas):
        return _de(solo_marcas, filas)

    reg = SimpleNamespace(solo_marcas={}, marcas_cliente=[], escrituras=[])

    def anota(nombre, sm):
        assert isinstance(sm, frozenset), f"{nombre}: solo_marcas debe ser frozenset"
        reg.solo_marcas.setdefault(nombre, set()).add(sm)

    @contextmanager
    def conexion(cfg):
        yield object()

    monkeypatch.setattr(portal.db, "connection", conexion)
    monkeypatch.setattr(portal, "_ahora", lambda zona: HOY.astimezone(zona))
    monkeypatch.setattr(portal.identidad_repo, "usuarios_asignables", lambda conn, org, cl: [])
    monkeypatch.setattr(portal.identidad_repo, "registrar_auditoria", lambda *a, **k: None)

    # tablero
    def procesos_del_rango(conn, cliente, dias, limite=500, *, solo_marcas):
        anota("procesos_del_rango", solo_marcas)
        filas = de(solo_marcas) if cliente == "sv" else []
        return {"filas": filas, "total": len(filas), "truncado": False}

    def clasificar(filas, marcas_cliente, hoy):
        reg.marcas_cliente.append(marcas_cliente)
        return {"filas": filas}

    def votos_por_proceso(conn, cliente, gids, *, solo_marcas):
        anota("votos_por_proceso", solo_marcas)
        visibles = {f["proceso_gid"] for f in de(solo_marcas)}
        return {g: {"relevante": 0, "irrelevante": 1, "motivo": "fuera_de_rubro"} for g in gids if g in visibles}

    def votos_resumen(conn, cliente, dias, *, solo_marcas):
        anota("votos_resumen", solo_marcas)
        return ({"relevante": 2, "irrelevante": 1, "motivos": [("fuera_de_rubro", 1)]} if solo_marcas
                else {"relevante": 7531, "irrelevante": 5, "motivos": [("fuera_de_rubro", 5)]})

    def tendencia_semanal(conn, cliente, semanas=12, *, solo_marcas):
        anota("tendencia_semanal", solo_marcas)
        return [{"semana": datetime(2026, 10, d), "revisados": None if solo_marcas else REVISADOS_GLOBAL,
                 "relevantes": 2 if solo_marcas else 300} for d in (12, 19, 26)]

    def marcas_top(conn, cliente, dias, limite=5, *, solo_marcas):
        anota("marcas_top", solo_marcas)
        return [("Oracle", 3)] if solo_marcas else [("Cisco", 9), ("Fortinet", 4), ("Oracle", 3)]

    def pipeline_en_juego(conn, cliente, *, solo_marcas):
        anota("pipeline_en_juego", solo_marcas)
        return {"monto_usd": 45000.0 if solo_marcas else 987654.0, "activas": 1, "con_monto": 1}

    def estancadas(conn, cliente, dias_sin_mover=7, *, solo_marcas):
        anota("estancadas", solo_marcas)
        return 1 if solo_marcas else 3

    def descartados_del_rango(conn, cliente, dias, limite=10, *, solo_marcas):
        anota("descartados_del_rango", solo_marcas)
        filas = [f for f in de(solo_marcas) if f["grupo"] == "descartado"]
        return {"filas": filas, "total": len(filas) if solo_marcas else 9999, "desde": datetime(2026, 10, 1, 13)}

    monkeypatch.setattr(portal, "tablero", SimpleNamespace(
        procesos_del_rango=procesos_del_rango, clasificar=clasificar, votos_por_proceso=votos_por_proceso,
        votos_resumen=votos_resumen, tendencia_semanal=tendencia_semanal, marcas_top=marcas_top,
        pipeline_en_juego=pipeline_en_juego, estancadas=estancadas, descartados_del_rango=descartados_del_rango))

    # pais
    def relevancia_estricta(conn, cliente, dias=30, *, solo_marcas):
        anota("relevancia_estricta", solo_marcas)
        if solo_marcas:
            return {"revisados": None, "relevantes": 5, "aptos": 2, "pct_aptos": None, "pct_relevantes": None,
                    "filtrado_por_marca": True}
        return {"revisados": REVISADOS_GLOBAL, "relevantes": 321, "aptos": 123, "pct_aptos": 1.4,
                "pct_relevantes": 3.7, "filtrado_por_marca": False}

    def oportunidades_del_mes(conn, cliente, *, solo_marcas):
        anota("oportunidades_del_mes", solo_marcas)
        return {"valor": 3, "delta_pct": None, "serie": [1, 2, 3]}

    def instituciones_top(conn, cliente, dias=90, limite=10, *, solo_marcas):
        anota("instituciones_top", solo_marcas)
        conteo = Counter(f["institucion"] for f in de(solo_marcas))
        return [{"institucion": i, "conteo": n} for i, n in conteo.most_common(limite)]

    def embudo_pipeline(conn, cliente, dias=90, *, solo_marcas):
        anota("embudo_pipeline", solo_marcas)
        return {"camino": [{"estado": "DESCUBIERTO", "conteo": 6}], "salidas": []}

    def severidad_riesgos(conn, cliente, dias=90, *, solo_marcas):
        anota("severidad_riesgos", solo_marcas)
        return [{"severidad": "alta", "conteo": 1}]

    def conversion_comercial(conn, cliente, dias=90, *, solo_marcas):
        anota("conversion_comercial", solo_marcas)
        return {"detectadas": 6, "etapas": [{"estado": "EVALUANDO", "conteo": 1}], "perdido": 0, "descartado": 0,
                "conv_ofertado_pct": None, "conv_ganado_pct": None}

    def tiempo_por_etapa(conn, cliente, dias=90, *, solo_marcas):
        anota("tiempo_por_etapa", solo_marcas)
        return [{"etapa": "EVALUANDO", "dias_promedio": 2.0, "muestra": 1}]

    for nombre, f in (("relevancia_estricta", relevancia_estricta), ("oportunidades_del_mes", oportunidades_del_mes),
                      ("instituciones_top", instituciones_top), ("embudo_pipeline", embudo_pipeline),
                      ("severidad_riesgos", severidad_riesgos), ("conversion_comercial", conversion_comercial),
                      ("tiempo_por_etapa", tiempo_por_etapa)):
        monkeypatch.setattr(portal.pais, nombre, f)

    def conflictos(conn, cliente, *, solo_marcas):
        anota("listar_conflictos_oportunidad_v2", solo_marcas)
        return [{"proceso_gid": f["proceso_gid"], "estado_manual": "PERDIDO", "resultado_mercado": "GANADO",
                 "responsable_usuario_id": None, "actualizado_en": datetime(2026, 10, 20)}
                for f in de(solo_marcas) if f.get("estado") == "GANADO"]

    monkeypatch.setattr(portal.mercado_repo, "listar_conflictos_oportunidad_v2", conflictos)

    # regional
    def oportunidades_por_pais(conn, clientes, dias=90, *, solo_marcas):
        anota("oportunidades_por_pais", solo_marcas)
        n = len([f for f in de(solo_marcas) if f["grupo"] != "descartado"])
        return [{"cliente_id": c, "conteo": n if c == "sv" else 0} for c in clientes]

    monkeypatch.setattr(portal.regional, "oportunidades_por_pais", oportunidades_por_pais)
    monkeypatch.setattr(portal.regional, "nombres_clientes",
                        lambda conn, cls: {c: {"sv": "El Salvador", "gt": "Guatemala"}.get(c, c) for c in cls})
    monkeypatch.setattr(portal.operativo, "estado_ultima_corrida", lambda conn: {
        "estado": "OK", "hace_minutos": 125, "procesos_revisados": CORRIDA_REVISADOS, "version": "x", "error_msg": None})
    monkeypatch.setattr(portal.ver_como, "opciones", lambda conn, real: [])

    # escrituras: el repo de oportunidades (lo que el servicio REAL usa) honra la marca y anota toda escritura.
    visibles = {f["proceso_gid"] for f in filas}

    def proceso_visible(conn, cliente, gid, *, solo_marcas=frozenset()):
        return gid in {f["proceso_gid"] for f in de(solo_marcas)} and gid in visibles

    def escribe(nombre):
        def f(*a, **k):
            reg.escrituras.append(nombre)
            return True
        return f

    monkeypatch.setattr(oportunidades_repo, "proceso_visible", proceso_visible)
    monkeypatch.setattr(oportunidades_repo, "obtener", lambda conn, cl, gid, *, solo_marcas=frozenset(): None)
    monkeypatch.setattr(oportunidades_repo, "es_descartado_por_amalia",
                        lambda conn, cl, gid, *, solo_marcas=frozenset(): None)
    for nombre in ("crear_estado", "actualizar_estado", "insertar_evento", "fijar_responsable"):
        monkeypatch.setattr(oportunidades_repo, nombre, escribe(nombre))
    monkeypatch.setattr("src.identidad.repo.registrar_auditoria", lambda *a, **k: None)
    return reg


@pytest.fixture
def datos(monkeypatch):
    return simular_datos(monkeypatch)


# ── alcances ────────────────────────────────────────────────────────────────────

def _real(rol):
    """El alcance que resolvería una persona con `rol` (constructor real: la bandera sale del rol)."""
    return alcance_mod._alcance_desde("u1", "org", {rol}, (), ACTIVOS)


ADMIN = alcance_mod.Alcance(usuario_id="u-admin", org_id="org", roles=frozenset({"admin_plataforma"}),
                            clientes=frozenset(), cambia_estado=frozenset(), ve_operativo=True)


def _simulado(rol, monkeypatch):
    """El alcance que `alcance_simulado` le da al admin en «ver como» `rol`."""
    monkeypatch.setattr(alcance_mod.repo, "clientes_activos", lambda conn, org: list(ACTIVOS))
    sim = alcance_mod.alcance_simulado(object(), ADMIN, rol)
    assert sim is not None and sim.simulado == rol
    return sim


def _cli(monkeypatch, efectivo, real=None):
    monkeypatch.setattr(sesion, "_resolver", lambda c, token: (
        sesion.Resolucion(efectivo, real or efectivo, None, datetime(2026, 10, 27, 15, 0) if real else None)
        if token == "t" else None))
    monkeypatch.setattr(sesion, "_pendiente", lambda c, token: False)
    c = TestClient(crear_app(_cfg()), base_url=BASE, follow_redirects=False)
    c.cookies.set(sesion.COOKIE, "t")
    return c


def _vistas(monkeypatch, rol, modo):
    if modo == "real":
        return _cli(monkeypatch, _real(rol))
    return _cli(monkeypatch, _simulado(rol, monkeypatch), ADMIN)


def _sin_fugas(html: str):
    html = sin_tokens(html)
    fugas = [s for s in PROHIBIDAS if s in html]
    assert not fugas, f"cadenas no-Oracle en el HTML: {fugas}"
    cifras = [s for s in CIFRAS_GLOBALES if s in html]
    assert not cifras, f"cifras globales en el HTML: {cifras}"
    assert "revisados" not in html.lower()
    assert "no relevantes" not in html.lower()
    assert "?brand=" not in html and "&amp;brand=" not in html and "Filtro por marca" not in html


# ── tests ───────────────────────────────────────────────────────────────────────

def test_los_tres_roles_oracle_restringen_a_oracle():
    assert len(ROLES_ORACLE) == 3
    for rol in ROLES_ORACLE:
        assert _real(rol).solo_marcas == frozenset({"oracle"})
    assert _real("ceo").solo_marcas == frozenset()


@pytest.mark.parametrize("modo", ["real", "ver_como"])
@pytest.mark.parametrize("rol", ROLES_ORACLE)
class TestRolOracle:
    def test_pais_sin_fugas(self, datos, monkeypatch, rol, modo):
        c = _vistas(monkeypatch, rol, modo)
        for url in ("/portal?client=sv", "/portal?client=sv&days=30", "/portal?client=sv&brand=Cisco",
                    "/portal?client=sv&brand=Oracle&owner=none"):
            r = c.get(url)
            assert r.status_code == 200, (url, r.status_code)
            _sin_fugas(r.text)
            assert "Vista filtrada: solo oportunidades Oracle" in r.text
            # Sí ve lo Oracle (también la alternativa, NetSuite y el respaldo por título).
            for f in ORACLE:
                if f["grupo"] != "descartado":
                    assert f["numero_referencia"] in r.text, f["numero_referencia"]
        # Toda lectura recibió la restricción (y ninguna sin ella).
        assert all(v == {frozenset({"oracle"})} for v in datos.solo_marcas.values()), datos.solo_marcas
        assert "marcas_top" not in datos.solo_marcas  # el gráfico de marcas ni se pide

    def test_regional_sin_fugas_y_paises_en_cero(self, datos, monkeypatch, rol, modo):
        r = _vistas(monkeypatch, rol, modo).get("/portal/regional")
        assert r.status_code == 200
        _sin_fugas(r.text)
        assert "Vista filtrada: solo oportunidades Oracle" in r.text
        assert "Guatemala</a>:" in r.text and "sin oportunidades en el rango" in r.text  # país en 0, no se oculta
        assert datos.solo_marcas["oportunidades_por_pais"] == {frozenset({"oracle"})}

    def test_operativo_es_404(self, datos, monkeypatch, rol, modo):
        assert _vistas(monkeypatch, rol, modo).get("/portal/operations").status_code == 404

    def test_competibilidad_solo_con_marcas_oracle(self, datos, monkeypatch, rol, modo):
        perfil = SimpleNamespace(catalog=SimpleNamespace(categories=[
            SimpleNamespace(brands=[SimpleNamespace(name="Oracle"), SimpleNamespace(name="Cisco")]),
            SimpleNamespace(brands=[SimpleNamespace(name="oracle NetSuite"), SimpleNamespace(name="Dell")])]))
        monkeypatch.setattr(portal, "_perfil", lambda cfg, cliente: perfil)
        monkeypatch.setattr(portal.feedback_catalogo, "motivos_de", lambda p: [])
        assert _vistas(monkeypatch, rol, modo).get("/portal?client=sv").status_code == 200
        assert datos.marcas_cliente == [("Oracle", "oracle NetSuite")]


def test_ceo_ve_todo_con_los_mismos_datos(datos, monkeypatch):
    """Contraste: sin restricción, las cadenas y cifras que los roles Oracle no ven SÍ están."""
    c = _cli(monkeypatch, _real("ceo"))
    html = c.get("/portal?client=sv").text
    for s in ("Switches Catalyst sede Quetzal", "ALCALDIA QUETZALTEPEQUE", "NOORA-0001", "Cisco", "Fortinet",
              "8642", "4321 procesos revisados", "No relevantes", "Filtro por marca"):
        assert s in html, s
    assert "Vista filtrada" not in html
    assert all(v == {frozenset()} for v in datos.solo_marcas.values())
    regional = c.get("/portal/regional").text
    assert "Vista filtrada" not in regional


def test_ver_como_ve_lo_mismo_que_el_rol_real(datos, monkeypatch):
    """La bandera sale del `Alcance` (no del rol): «ver como» y la persona real reciben el mismo contexto."""
    capturas = []
    real_pagina = portal.pagina

    def pagina(request, nombre, status=200, **ctx):
        capturas.append(ctx)
        return real_pagina(request, nombre, status=status, **ctx)

    monkeypatch.setattr(portal, "pagina", pagina)
    rol = ROLES_ORACLE[0]
    _vistas(monkeypatch, rol, "real").get("/portal?client=sv")
    _vistas(monkeypatch, rol, "ver_como").get("/portal?client=sv")
    a, b = capturas
    for clave in ("cabecera", "kpis", "atencion", "atencion_mas", "clasificacion", "votos", "resumenes",
                  "solo_marca"):
        assert a[clave] == b[clave], clave
    assert [c["n"] for c in a["tablero"]["columnas"]] == [c["n"] for c in b["tablero"]["columnas"]]


def _post(c, ruta, **datos):
    base = dict(cliente="sv", estado="EVALUANDO", actualizado_en="", dias="30", responsable="",
                csrf=csrf_mod.emitir(SECRETO, sesiones.hash_token("t")))
    base.update(datos)
    return c.post(ruta, data=base, headers={"origin": BASE})


_RUTAS_POST = ("estado", "responsable", "recuperar")
_GID_AJENO = "comprasal_sv:NOORA-0001"
_GID_ORACLE = "comprasal_sv:ORA-0001"


@pytest.mark.parametrize("rol", ROLES_ORACLE)
@pytest.mark.parametrize("accion", _RUTAS_POST)
def test_post_por_gid_no_oracle_es_404_sin_escribir(datos, monkeypatch, rol, accion):
    # El rol real es de solo lectura: 404 antes de llegar al servicio.
    r = _post(_cli(monkeypatch, _real(rol)), f"/portal/opportunity/{_GID_AJENO}/{_OP[accion]}")
    assert r.status_code == 404 and datos.escrituras == []
    # «Ver como»: solo lectura, ni consulta la base.
    r = _post(_cli(monkeypatch, _simulado(rol, monkeypatch), ADMIN), f"/portal/opportunity/{_GID_AJENO}/{_OP[accion]}")
    assert r.status_code == 303 and "notice=simulacion" in r.headers["location"] and datos.escrituras == []


@pytest.mark.parametrize("accion", _RUTAS_POST)
def test_restriccion_con_permiso_de_escritura_el_gid_ajeno_es_no_encontrado(datos, monkeypatch, accion):
    """Defensa en profundidad: aunque un alcance restringido pudiera escribir, un gid no-Oracle del mismo cliente
    es NO_ENCONTRADO (404 idéntico al inexistente) en el servicio real, sin ninguna escritura."""
    from dataclasses import replace
    escritor = replace(_real(ROLES_ORACLE[0]), cambia_estado=frozenset({"sv"}))
    c = _cli(monkeypatch, escritor)
    r = _post(c, f"/portal/opportunity/{_GID_AJENO}/{_OP[accion]}")
    assert r.status_code == 404 and datos.escrituras == []
    assert r.text == c.get("/no-hay-ruta").text
    # El mismo alcance sobre un gid Oracle sí llega a escribir (el filtro no es un «todo 404»).
    # (Solo «estado»: asignar «nadie» donde no hay nadie no escribe, y recuperar exige un descartado.)
    if accion == "estado":
        r = _post(c, f"/portal/opportunity/{_GID_ORACLE}/{_OP[accion]}")
        assert r.status_code == 303 and "notice=ok" in r.headers["location"] and datos.escrituras


def test_guardian_las_rutas_post_no_cambian():
    def aplanar(rutas):
        for r in rutas:
            sub = getattr(r, "original_router", None)
            yield from aplanar(sub.routes) if sub is not None else (r,)

    rutas = [r for r in aplanar(crear_app(_cfg()).routes) if (getattr(r, "path", "") or "").startswith("/portal")]
    posts = {r.path for r in rutas if "POST" in (getattr(r, "methods", None) or set())}
    assert posts == {"/portal/opportunity/{proceso_gid}/status", "/portal/view-as",
                     "/portal/opportunity/{proceso_gid}/owner",
                     "/portal/opportunity/{proceso_gid}/recover", "/portal/theme"}


def test_marcas_portafolio_sin_restriccion_devuelve_todas():
    perfil = SimpleNamespace(catalog=SimpleNamespace(categories=[
        SimpleNamespace(brands=[SimpleNamespace(name="ORACLE"), SimpleNamespace(name="Cisco"),
                                SimpleNamespace(name="Oraclexyz")])]))
    assert portal._marcas_portafolio(perfil) == ("ORACLE", "Cisco", "Oraclexyz")
    # Nombre de marca que empieza por «oracle» como palabra completa (igual que en la capa de datos).
    assert portal._marcas_portafolio(perfil, frozenset({"oracle"})) == ("ORACLE",)
    assert portal._marcas_portafolio(None, frozenset({"oracle"})) is None


def test_tendencia_con_none_no_rompe_y_resumen_sin_revisados(datos, monkeypatch):
    r = _cli(monkeypatch, _real(ROLES_ORACLE[0])).get("/portal?client=sv")
    assert r.status_code == 200 and "Relevantes por semana" in r.text
    assert portal._resumen_clasificacion(0, 5, 2, filtrado=True) == "5 relevantes · 2 aptos para decidir"
    assert portal._resumen_clasificacion(0, 0, 0, filtrado=True) == "Sin oportunidades en el rango"
