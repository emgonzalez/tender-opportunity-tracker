"""Render sin servidor del selector de tema Auto · Claro · Oscuro (E12): atributo, control, CSS y CSP."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.web.plantillas import crear_entorno

_ENTORNO = crear_entorno()
_RAIZ = Path(__file__).resolve().parents[1] / "templates"
_NAV = [{"href": "/portal", "texto": "País", "actual": True}]
_TEMAS = ("auto", "claro", "oscuro")


def _base(**ctx) -> str:
    # `portal_base.html` con la nav incluida (el bloque de contenido queda vacío).
    return _ENTORNO.get_template("portal_base.html").render(nav=_NAV, csrf="tok", **ctx)


def _nav(**ctx) -> str:
    return _ENTORNO.get_template("portal_nav.html").render(nav=_NAV, csrf="tok", **ctx)


def _estilo() -> str:
    return _base().split("<style>")[1].split("</style>")[0]


def _botones(html: str) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in re.finditer(r'<button type="submit" name="tema" value="(\w+)"[^>]*>', html)}


def test_sin_tema_es_auto():
    assert '<html lang="es" data-tema="auto">' in _base()


@pytest.mark.parametrize("tema", _TEMAS)
def test_cada_tema_aparece_en_el_atributo(tema):
    assert f'<html lang="es" data-tema="{tema}">' in _base(tema=tema)


@pytest.mark.parametrize("tema", _TEMAS)
def test_el_boton_actual_lleva_aria_pressed_true_y_los_demas_false(tema):
    botones = _botones(_nav(tema=tema))
    assert set(botones) == set(_TEMAS)
    for valor, etiqueta in botones.items():
        if valor == tema:
            assert 'aria-pressed="true"' in etiqueta and 'class="activo"' in etiqueta
        else:
            assert 'aria-pressed="false"' in etiqueta and "activo" not in etiqueta


def test_sin_tema_el_activo_es_auto():
    assert 'aria-pressed="true"' in _botones(_nav())["auto"]


def test_formulario_apunta_a_portal_tema_con_csrf_y_volver():
    html = _nav(tema="claro", ruta_actual="/portal/regional?days=30")
    assert '<form method="post" action="/portal/theme" class="tema" aria-label="Tema">' in html
    assert '<input type="hidden" name="csrf" value="tok">' in html
    assert '<input type="hidden" name="volver" value="/portal/regional?days=30">' in html
    for texto in (">Auto<", ">Claro<", ">Oscuro<"):
        assert texto in html


def test_volver_por_defecto_es_portal():
    assert 'name="volver" value="/portal"' in _nav()


def test_volver_se_escapa():
    html = _nav(ruta_actual='/portal"><script>alert(1)</script>')
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html and "&#34;" in html


def test_css_define_los_tres_modos():
    css = _estilo()
    assert ':root[data-tema="oscuro"]' in css
    assert ':root[data-tema="claro"]' in css
    assert "prefers-color-scheme: dark" in css
    assert ':root:not([data-tema="claro"])' in css
    for variante in ("light dark", "light", "dark"):
        assert re.search(rf"color-scheme: {variante};", css)


def _bloque(css: str, cabecera: str) -> str:
    inicio = css.index(cabecera) + len(cabecera)
    return css[inicio:css.index("}", inicio)]


def _variables(bloque: str) -> list[str]:
    return re.findall(r"(--[\w-]+):", bloque)


def test_los_tokens_oscuros_estan_en_los_dos_bloques_con_las_mismas_variables():
    css = _estilo()
    en_media = _bloque(css, ':root:not([data-tema="claro"]) {')
    forzado = _bloque(css, ':root[data-tema="oscuro"] {')
    assert _variables(en_media) == _variables(forzado)
    assert len(_variables(forzado)) > 20
    assert re.sub(r"\s+", " ", en_media.replace("color-scheme: dark;", "")).strip() == \
        re.sub(r"\s+", " ", forzado.replace("color-scheme: dark;", "")).strip()


def test_los_tokens_claros_estan_en_root_y_en_claro_con_las_mismas_variables():
    css = _estilo()
    base = _bloque(css, ":root {")
    claro = _bloque(css, ':root[data-tema="claro"] {')
    assert _variables(base) == _variables(claro)
    # Los claros y los oscuros definen el mismo conjunto de variables.
    # (--radio no es de color: solo vive en la paleta clara y el oscuro lo hereda de `:root`.)
    assert [v for v in _variables(base) if v != "--radio"] == _variables(_bloque(css, ':root[data-tema="oscuro"] {'))


def test_ningun_color_fijo_fuera_de_los_tokens():
    fuente = (_RAIZ / "portal_base.html").read_text(encoding="utf-8")
    estilo = fuente.split("<style>")[1].split("</style>")[0]
    estilo = re.sub(r"\{% macro .*?\{% endmacro %\}", "", estilo, flags=re.S)
    estilo = re.sub(r":root[^{]*\{[^}]*\}", "", estilo)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|(?<![-\w])(?:white|black)(?![-\w])|rgba?\(", estilo)


def test_los_botones_del_selector_tienen_objetivo_tactil_y_foco():
    css = _estilo()
    assert re.search(r"max-width: 520px\) \{[^@]*min-height: 44px", css)
    assert "button:focus-visible" in css


def test_sin_javascript_en_la_nav_ni_en_la_base():
    for html in (_nav(tema="oscuro"), _base(tema="oscuro")):
        # La base solo permite el script propio servido desde /portal/static (CSP: script-src 'self').
        sin_propio = re.sub(r'<script src="/portal/static/[^"]+"></script>', "", html)
        assert "<script" not in sin_propio.lower()
        assert not re.search(r"\son\w+\s*=", html, re.I)
        assert "javascript:" not in html.lower()


def test_las_tres_vistas_muestran_el_control():
    from tests.test_plantillas_pais import _contexto

    comunes = {"nav": _NAV, "csrf": "tok", "ver_como": None, "aviso": None, "tema": "oscuro",
               "ruta_actual": "/portal/x?days=30"}
    contextos = {
        "portal_pais.html": {**_contexto(), **comunes},
        "portal_regional.html": {**comunes, "dias": 30, "rangos": [30], "paises": [], "g_paises": ""},
        "portal_operativo.html": {**comunes, "corrida": {"estado": None}, "gasto": {"llamadas": 0}, "serie": [],
                                  "g_medidor": "", "g_latencia": "", "g_errores": ""},
    }
    for vista, ctx in contextos.items():
        html = _ENTORNO.get_template(vista).render(**ctx)
        assert 'data-tema="oscuro"' in html, vista
        assert 'action="/portal/theme"' in html, vista
        assert 'name="volver" value="/portal/x?days=30"' in html, vista
        assert 'aria-pressed="true"' in _botones(html)["oscuro"], vista


def test_e13_css_del_arrastre_y_del_dialogo_usa_tokens_en_los_tres_temas():
    estilo = _estilo()
    for selector in (".tarjeta-op.arrastrando", ".col-tablero.col-valida", ".col-tablero.col-invalida",
                     ".col-tablero.col-sobre", 'dialog.dlg', "dialog.dlg::backdrop", ".dlg-campos", ".dlg-acciones"):
        assert selector in estilo, selector
    assert '.tarjeta-op[draggable="true"] { cursor: grab; }' in estilo
    # el velo del diálogo es un token con valor en claro y en oscuro (los bloques ya se comparan arriba)
    assert estilo.count("--velo:") >= 4
    assert "background: var(--velo)" in estilo
    # el tablero tiene alto fijo y scroll interno (la página no crece con las oportunidades)
    assert re.search(r"\.col-tablero \{[^}]*height: 540px", estilo)
    assert re.search(r"\.col-cuerpo \{[^}]*overflow-y: auto", estilo)
