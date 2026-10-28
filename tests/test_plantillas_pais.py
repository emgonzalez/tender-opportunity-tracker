"""Render sin servidor de la vista de país (E11.1): contrato de contexto, formularios, XSS y solo vista."""
from __future__ import annotations

import re
from pathlib import Path

from markupsafe import Markup

from src.web.plantillas import crear_entorno

_ENTORNO = crear_entorno()
_NAV = [{"href": "/portal", "texto": "País", "actual": True}, {"href": "/portal/regional", "texto": "Regional", "actual": False}]
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
            "decidir": {"n": 2, "semana": 1, "primera": {"gid": "g1", "titulo": "Servidores", "institucion": "MH",
                                                         "texto": "Cierra mar 3 nov", "dias": 7}},
            "pipeline": {"monto_usd": 50000.0, "activas": 3, "con_monto": 2},
            "relevancia": {"pct_aptos": 7.5, "aptos": 9, "revisados": 120, "relevantes": 19, "pct_relevantes": 15.8,
                           "delta_pts": 2.5, "spark": Markup("<svg id='spark'></svg>")},
            "estancadas": {"n": 1, "dias": 7},
        },
        "cierres": {"svg": Markup("<div id='cal'></div>"), "dia": None},
        "atencion": [{"chip": "Cierra pronto", "clase": "rojo", "titulo": "Servidores", "detalle": "en 2 días", "gid": "g1"}],
        "tablero": {
            "columnas": [
                {"clave": "NUEVO", "titulo": "Nuevo", "n": 20, "mas": 12, "tarjetas": [_tarjeta()]},
                {"clave": "EVALUANDO", "titulo": "Evaluando", "n": 0, "mas": 0, "tarjetas": []},
                {"clave": "OFERTADO", "titulo": "Ofertado", "n": 0, "mas": 0, "tarjetas": []},
                {"clave": "CERRADAS", "titulo": "Cerradas", "n": 1, "mas": 0,
                 "tarjetas": [_tarjeta(gid="g2", estado="GANADO", siguientes=[])]},
            ],
            "vencidas": {"n": 90},
            "asignables": [{"usuario_id": "u1", "nombre": "Ana"}, {"usuario_id": "u2", "nombre": "Beto"}],
            "filtros": {"responsable": None, "marca": None,
                        "opciones": {"responsables": [{"valor": "u1", "etiqueta": "Ana"}], "marcas": ["HPE"]}},
            "truncado": False,
        },
        "clasificacion": {
            "revisados": 120, "relevantes": 19, "no_relevantes": 101,
            "grupos": {"decidir": 9, "no_competible": 2, "baja": 8},
            "barra": Markup("<div class='barra-apilada'></div>"), "motivos": [("Fuera de catálogo", 4), ("Otro", 2)],
            "descartados": [{"gid": "d1", "referencia": "9-2026", "titulo": "Sillas", "match": 10,
                             "motivo": "No es TI", "token": ""}],
            "total_descartados": 101, "hay_mas": True, "desde": "27 oct 2026", "truncado": False,
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
    assert "javascript:" not in html.lower()


def _formularios_escritura(html: str) -> list[str]:
    return re.findall(r'<form method="post" action="(/portal/oportunidad/[^"]+)"', html)


def test_contexto_completo_feliz():
    html = _render()
    for texto in ("AMALIA · PORTAL", "El Salvador", "Hay 2 para decidir.", "Lunes 26 de octubre",
                  "última corrida 07:00", "120 procesos revisados",
                  "Para decidir", "Pipeline en juego", "US$ 50.000", "Estancadas", "Calendario de cierres",
                  "Aptos para decidir", "7,5%", "15,8% marcados relevantes (19 de 120)",
                  "Requiere atención", "Tablero del pipeline", "Qué hizo AMALIA con los 120 procesos",
                  "No relevantes", "Lo que opina el equipo", "Todavía hay pocos votos",
                  "Conflictos con COMPRASAL", "Recuperar", "Fuera de catálogo", "<div id='cal'>", "<svg id='t'>",
                  "<svg id='spark'>", "90 vencidas", "Se guardan desde el 27 oct 2026"):
        assert texto in html, texto
    _sin_js(html)


def test_mas_de_cada_columna_es_visible():
    html = _render()
    assert "+ 12 más" in html
    assert html.count("más</p>") == 1  # solo la columna con `mas` > 0


def test_el_calendario_no_trae_la_lista_de_dias():
    ctx = _contexto()
    ctx["cierres"]["dias"] = [{"fecha": "2026-10-28", "n": 2, "urgencia": "rojo", "href": "/portal?dia=1"}]
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "Días con cierres" not in html
    assert "2026-10-28" not in html
    assert 'class="salidas" aria-label="Días con cierres"' not in html


def test_todo_vacio_no_rompe():
    html = _render(cabecera=None, kpis=None, cierres=None, atencion=None, tablero=None,
                   clasificacion=None, votos=None, analisis=None, rangos=None)
    assert "AMALIA · PORTAL" in html
    assert "Nada urgente" in html
    assert "Todavía no hay votos" in html
    assert "AMALIA todavía no clasificó" in html
    assert "No hay procesos con cierre próximo" in html
    assert "Sin corridas en el rango" in html
    assert "monto estimado en 0 de 0" in html
    _sin_js(html)


def test_sin_revisados_el_kpi_de_relevancia_es_un_guion():
    ctx = _contexto()
    ctx["kpis"]["relevancia"] = {"pct_aptos": None, "aptos": 0, "revisados": 0, "relevantes": 0,
                                 "pct_relevantes": None, "delta_pts": None, "spark": ""}
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "Sin corridas en el rango" in html
    assert "marcados relevantes" not in html


def test_sin_descartados_guardados_dice_desde_cuando():
    ctx = _contexto()
    ctx["clasificacion"].update({"descartados": [], "total_descartados": 0, "hay_mas": False, "desde": None})
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "Aún no se guardan los descartados: aparecerán desde la próxima corrida" in html
    assert "Recuperar" not in html


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
    assert forms["responsable"] == {"csrf", "cliente", "dias", "actualizado_en", "responsable"}
    assert forms["recuperar"] == {"csrf", "cliente", "dias", "actualizado_en"}
    assert 'value="tok1"' in html


def test_asignar_marca_responsable_actual_y_permite_quitar():
    html = _render()
    assert '<option value="u1" selected>Ana</option>' in html
    assert "Sin responsable" in html


def test_xss_se_escapa():
    t = _tarjeta(titulo=_MALO, institucion=_MALO, responsable=_MALO, voto={"n": 1, "motivo": _MALO}, marcas=[_MALO],
                 url="javascript:alert(1)", referencia=_MALO)
    ctx = _contexto()
    ctx["tablero"]["columnas"][0]["tarjetas"] = [t]
    ctx["tablero"]["asignables"] = [{"usuario_id": "u1", "nombre": _MALO}]
    ctx["clasificacion"]["descartados"] = [{"gid": "d1", "referencia": _MALO, "titulo": _MALO, "match": 1,
                                            "motivo": _MALO, "token": ""}]
    ctx["clasificacion"]["motivos"] = [(_MALO, 1)]
    ctx["clasificacion"]["desde"] = _MALO
    ctx["votos"]["motivos"] = [(_MALO, 1)]
    ctx["votos"]["desacuerdos"] = [{"gid": "g", "titulo": _MALO, "match": 1, "lado": _MALO, "detalle": _MALO}]
    ctx["atencion"] = [{"chip": _MALO, "clase": "rojo", "titulo": _MALO, "detalle": _MALO, "gid": None}]
    ctx["cabecera"]["veredicto"] = _MALO
    ctx["kpis"]["decidir"]["primera"] = {"titulo": _MALO, "institucion": _MALO, "texto": _MALO}
    ctx["analisis"]["conflictos"] = [{"codigo": _MALO, "estado_manual": _MALO, "resultado_mercado": _MALO, "fecha": _MALO}]
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert _MALO not in html
    assert "&lt;script&gt;" in html
    _sin_js(html)  # también: una URL `javascript:` del dato no llega a ser enlace


def test_enlace_externo_http_se_conserva_con_rel():
    html = _render()
    assert 'href="http://x/1" rel="noopener noreferrer" target="_blank"' in html


def test_monto_vacio_dice_sin_estimar_nunca_cero():
    ctx = _contexto()
    ctx["tablero"]["columnas"][0]["tarjetas"] = [_tarjeta(monto_txt="")]
    ctx["kpis"]["pipeline"] = {"monto_usd": None, "activas": 1, "con_monto": 0}
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "monto sin estimar" in html  # en la tarjeta
    assert "monto estimado en 0 de 1" in html  # el KPI: guion y leyenda, no una frase gigante
    assert not re.search(r'class="kv">monto sin estimar', html)
    assert "US$ 0" not in html


def test_monto_grande_se_abrevia_en_millones():
    ctx = _contexto()
    ctx["kpis"]["pipeline"] = {"monto_usd": 1240000.0, "activas": 12, "con_monto": 9}
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "US$ 1,24 M" in html


def test_filtros_del_tablero_son_enlaces_get_codificados():
    ctx = _contexto(cliente="a b&c")
    ctx["cierres"]["dia"] = "2026-10-28"
    html = _ENTORNO.get_template("portal_pais.html").render(**ctx)
    assert "/portal?cliente=a%20b%26c&amp;dias=30&amp;dia=2026-10-28&amp;responsable=u1" in html
    assert "&amp;marca=HPE" in html


def test_rango_activo_lleva_aria_current():
    html = _render()
    assert 'aria-current="true">30 días' in html


def test_los_estilos_no_usan_colores_fijos_fuera_de_los_tokens():
    css = (Path(__file__).resolve().parents[1] / "templates" / "portal_base.html").read_text(encoding="utf-8")
    estilo = css.split("<style>")[1].split("</style>")[0]
    # Los tokens viven en los dos bloques `:root`; el resto del CSS solo puede usar `var(--…)`.
    # (E12: las paletas viven en macros Jinja que se emiten dentro de bloques `:root…`.)
    sin_tokens = re.sub(r"\{% macro .*?\{% endmacro %\}", "", estilo, flags=re.S)
    sin_tokens = re.sub(r":root[^{]*\{[^}]*\}", "", sin_tokens)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|(?<![-\w])(?:white|black)(?![-\w])|rgba?\(", sin_tokens)
    assert "color-scheme: light dark" in estilo
    assert "prefers-color-scheme: dark" in estilo
