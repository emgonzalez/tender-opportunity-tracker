"""Render sin servidor de la vista de país (E11): contrato de contexto, formularios, XSS y solo vista."""
from __future__ import annotations

import re

from markupsafe import Markup

from src.web.plantillas import crear_entorno

_ENTORNO = crear_entorno()
_NAV = [{"href": "/portal", "texto": "País", "actual": True}]
_MALO = "<script>alert(1)</script>"


def _tarjeta(**extra) -> dict:
    t = {
        "gid": "g1", "referencia": "1-2026", "titulo": "Servidores", "institucion": "MH", "url": "http://x/1",
        "match": 88, "cierre": {"texto": "cierra en 2 días", "urgencia": "rojo"}, "marcas": ["HPE"],
        "monto_txt": "US$ 10,000", "responsable": "Ana", "responsable_id": "u1", "dias_en_estado": 3,
        "voto": {"n": 1, "motivo": "Fuera de catálogo"}, "siguientes": ["EVALUANDO", "DESCARTADO"],
        "exige_motivo": False, "token": "tok1", "estado": "NUEVO",
    }
    t.update(extra)
    return t


def _contexto(**cambios) -> dict:
    ctx = {
        "nav": _NAV, "csrf": "tok", "ver_como": None, "pais_nombre": "El Salvador", "cliente": "c1",
        "dias": 30, "rangos": [30, 90, 180], "aviso": None, "puede_cambiar": True, "solo_vista": False,
        "cabecera": {"veredicto": "Hay 2 para decidir.", "fecha_larga": "lunes 26 de octubre",
                     "corrida": {"hora": "07:00", "revisados": 120}},
        "kpis": {
            "decidir": {"n": 2, "semana": 1, "primera": "1-2026"},
            "pipeline": {"monto_usd": 50000.0, "activas": 3, "con_monto": 2},
            "relevancia": {"valor": 40, "relevantes": 4, "total": 10, "delta_pts": 2.5, "spark": Markup("<svg></svg>")},
            "estancadas": {"n": 1, "dias": 7},
        },
        "cierres": {"svg": Markup("<svg id='cal'></svg>"), "dias": [
            {"fecha": "2026-10-28", "n": 2, "urgencia": "rojo", "href": "/portal?cliente=c1&dias=30&dia=2026-10-28"}],
            "dia": None},
        "atencion": [{"chip": "Cierra pronto", "clase": "rojo", "titulo": "Servidores", "detalle": "en 2 días", "gid": "g1"}],
        "tablero": {
            "columnas": [
                {"clave": "NUEVO", "titulo": "Nuevo", "n": 1, "tarjetas": [_tarjeta()]},
                {"clave": "EVALUANDO", "titulo": "Evaluando", "n": 0, "tarjetas": []},
                {"clave": "OFERTADO", "titulo": "Ofertado", "n": 0, "tarjetas": []},
                {"clave": "CERRADAS", "titulo": "Cerradas", "n": 1,
                 "tarjetas": [_tarjeta(gid="g2", estado="GANADO", siguientes=[])]},
            ],
            "asignables": [{"usuario_id": "u1", "nombre": "Ana"}, {"usuario_id": "u2", "nombre": "Beto"}],
            "filtros": {"responsable": None, "marca": None,
                        "opciones": {"responsables": [{"valor": "u1", "etiqueta": "Ana"}], "marcas": ["HPE"]}},
            "truncado": False,
        },
        "clasificacion": {
            "total": 10, "grupos": {"decidir": 2, "no_competible": 1, "baja": 1, "descartados": 6},
            "barra": Markup("<div class='barra-apilada'></div>"), "motivos": [("Fuera de catálogo", 4), ("Otro", 2)],
            "descartados": [{"gid": "d1", "referencia": "9-2026", "titulo": "Sillas", "match": 10,
                             "motivo": "No es TI", "token": ""}],
            "hay_mas": True, "total_descartados": 6,
        },
        "votos": {"relevante": 3, "irrelevante": 2, "motivos": [("Fuera de catálogo", 2)],
                  "desacuerdos": [{"gid": "g1", "titulo": "Servidores", "match": 88, "lado": "irrelevante", "detalle": "x"}],
                  "pocos": True},
        "analisis": {
            "g_tendencia": Markup("<svg id='t'></svg>"), "g_marcas": Markup("<svg id='m'></svg>"),
            "g_embudo": Markup("<svg id='e'></svg>"), "g_instituciones": Markup("<svg id='i'></svg>"),
            "g_severidad": Markup("<svg id='s'></svg>"), "g_conversion": Markup("<svg id='c'></svg>"),
            "g_tiempos": Markup("<svg id='ti'></svg>"),
            "conversion": {"etapas": [{"conteo": 3}], "conv_ofertado_pct": 10.0, "conv_ganado_pct": None,
                           "perdido": 1, "descartado": 2},
            "salidas": [{"estado": "NO_RELEVANTE", "conteo": 6}], "hay_severidad": True,
            "tiempos": [{"etapa": "x"}],
            "conflictos": [{"codigo": "1-2026", "estado_manual": "GANADO", "resultado_mercado": "PERDIDO", "fecha": "2026-10-01"}],
        },
    }
    ctx.update(cambios)
    return ctx


def _render(**cambios) -> str:
    return _ENTORNO.get_template("portal_pais.html").render(**_contexto(**cambios))


def _sin_js(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.I)
    assert not re.search(r"\son\w+\s*=", html, re.I)


def _formularios_escritura(html: str) -> list[str]:
    return re.findall(r'<form method="post" action="(/portal/oportunidad/[^"]+)"', html)


def test_contexto_completo_feliz():
    html = _render()
    for texto in ("AMALIA · vista de país", "El Salvador", "Hay 2 para decidir.", "lunes 26 de octubre",
                  "Para decidir", "Pipeline en juego", "US$ 50,000", "Estancadas", "Calendario de cierres",
                  "Requiere atención", "Tablero del pipeline", "Qué hizo AMALIA con los 10 procesos",
                  "Lo que opina el equipo", "Todavía hay pocos votos", "Conflictos con COMPRASAL",
                  "Recuperar", "Fuera de catálogo", "<svg id='cal'>", "<svg id='t'>"):
        assert texto in html, texto
    _sin_js(html)


def test_todo_vacio_no_rompe():
    html = _render(cabecera=None, kpis=None, cierres=None, atencion=None, tablero=None,
                   clasificacion=None, votos=None, analisis=None, rangos=None)
    assert "AMALIA · vista de país" in html
    assert "Nada urgente" in html
    assert "Todavía no hay votos" in html
    assert "AMALIA todavía no clasificó" in html
    assert "monto sin estimar" in html
    _sin_js(html)


def test_contexto_sin_ninguna_clave_opcional():
    ctx = {"nav": _NAV, "csrf": "t", "pais_nombre": "X", "cliente": "c", "dias": 30,
           "puede_cambiar": True, "solo_vista": False}
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "<h1>X</h1>" in html


def test_solo_vista_todos_los_formularios_en_fieldset_disabled():
    html = _render(solo_vista=True)
    assert "vista simulada" in html
    forms = re.findall(r"<form method=\"post\" action=\"/portal/oportunidad/.*?</form>", html, re.S)
    assert len(forms) >= 3  # estado, responsable, recuperar
    for f in forms:
        assert '<fieldset class="solo-vista" disabled>' in f


def test_normal_no_deshabilita():
    html = _render()
    assert "disabled" not in html
    assert "vista simulada" not in html


def test_sin_permiso_no_hay_formularios_de_escritura():
    html = _render(puede_cambiar=False)
    assert _formularios_escritura(html) == []
    assert "solo lectura" in html
    assert "Recuperar" not in html
    assert "Servidores" in html


def test_nombres_de_campo_de_cada_formulario():
    html = _render()
    forms = {}
    for m in re.finditer(r'<form method="post" action="(/portal/oportunidad/g1/(?:estado|responsable)|/portal/oportunidad/d1/recuperar)".*?</form>', html, re.S):
        forms[m.group(1).rsplit("/", 1)[1]] = set(re.findall(r'name="(\w+)"', m.group(0)))
    assert forms["estado"] == {"csrf", "cliente", "dias", "actualizado_en", "estado", "motivo", "monto_ofertado", "competidor"}
    assert forms["responsable"] == {"csrf", "cliente", "dias", "actualizado_en", "responsable_id"}
    assert forms["recuperar"] == {"csrf", "cliente", "dias", "actualizado_en"}
    assert 'value="tok1"' in html


def test_asignar_marca_responsable_actual_y_permite_quitar():
    html = _render()
    assert '<option value="" >' not in html
    assert '<option value="u1" selected>Ana</option>' in html
    assert "Sin responsable" in html


def test_xss_se_escapa():
    t = _tarjeta(titulo=_MALO, institucion=_MALO, responsable=_MALO, voto={"n": 1, "motivo": _MALO}, marcas=[_MALO])
    ctx = _contexto()
    ctx["tablero"]["columnas"][0]["tarjetas"] = [t]
    ctx["tablero"]["asignables"] = [{"usuario_id": "u1", "nombre": _MALO}]
    ctx["clasificacion"]["descartados"] = [{"gid": "d1", "referencia": _MALO, "titulo": _MALO, "match": 1,
                                            "motivo": _MALO, "token": ""}]
    ctx["clasificacion"]["motivos"] = [(_MALO, 1)]
    ctx["votos"]["motivos"] = [(_MALO, 1)]
    ctx["votos"]["desacuerdos"] = [{"gid": "g", "titulo": _MALO, "match": 1, "lado": _MALO, "detalle": _MALO}]
    ctx["atencion"] = [{"chip": _MALO, "clase": "rojo", "titulo": _MALO, "detalle": _MALO, "gid": None}]
    ctx["cabecera"]["veredicto"] = _MALO
    ctx["analisis"]["conflictos"] = [{"codigo": _MALO, "estado_manual": _MALO, "resultado_mercado": _MALO, "fecha": _MALO}]
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert _MALO not in html
    assert "&lt;script&gt;" in html
    _sin_js(html)


def test_monto_vacio_dice_sin_estimar_nunca_cero():
    ctx = _contexto()
    ctx["tablero"]["columnas"][0]["tarjetas"] = [_tarjeta(monto_txt="")]
    ctx["kpis"]["pipeline"] = {"monto_usd": None, "activas": 1, "con_monto": 0}
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert html.count("monto sin estimar") >= 2
    assert "US$ 0" not in html


def test_filtros_del_tablero_son_enlaces_get_codificados():
    ctx = _contexto(cliente="a b&c")
    ctx["cierres"]["dia"] = "2026-10-28"
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "/portal?cliente=a%20b%26c&amp;dias=30&amp;dia=2026-10-28&amp;responsable=u1" in html
    assert "&amp;marca=HPE" in html
