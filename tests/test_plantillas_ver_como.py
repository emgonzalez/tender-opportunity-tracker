"""Render de las plantillas de «ver como» (E10): selector, banner y vista de solo lectura."""
from __future__ import annotations

import re
from types import SimpleNamespace as NS

from src.web.plantillas import crear_entorno

_ENTORNO = crear_entorno()
_NAV = [{"href": "/portal", "texto": "País", "actual": True}]
_OPCIONES = [
    NS(valor="org:gerente", etiqueta="Gerente org", grupo="Roles de organización"),
    NS(valor="pais:SV:gerente", etiqueta="Gerente pais · El Salvador", grupo="Roles de país"),
]


def _nav(ver_como) -> str:
    return _ENTORNO.get_template("portal_nav.html").render(nav=_NAV, csrf="tok", ver_como=ver_como)


def _vc(puede=True, simulando=None, restante_min=None, opciones=_OPCIONES, valor=None) -> dict:
    return {"puede": puede, "opciones": opciones, "simulando": simulando, "valor": valor, "restante_min": restante_min}


def _sin_js(html: str) -> None:
    assert "<script" not in html.lower()
    assert not re.search(r"\son\w+\s*=", html, re.I)


def test_no_admin_no_ve_el_selector():
    html = _nav(_vc(puede=False))
    assert "/portal/ver-como" not in html
    _sin_js(html)


def test_sin_contexto_ver_como_no_rompe():
    assert "/portal/ver-como" not in _nav(None)


def test_admin_sin_simulacion_mi_vista_seleccionada_y_sin_banner():
    html = _nav(_vc())
    assert 'action="/portal/ver-como"' in html
    assert '<option value="" selected>Mi vista</option>' in html
    assert "Roles de organización" in html and "Roles de país" in html
    assert "banner-ver-como" not in html
    assert 'name="csrf" value="tok"' in html
    _sin_js(html)


def test_simulando_muestra_banner_y_opcion_correcta():
    html = _nav(_vc(simulando="Gerente pais · El Salvador", restante_min=42, valor="pais:SV:gerente"))
    assert 'role="status"' in html
    assert "Viendo como" in html and "solo lectura" in html and "expira en 42 min" in html
    assert "Volver a mi vista" in html
    assert '<option value="pais:SV:gerente" selected>' in html
    assert '<option value="" selected>' not in html
    _sin_js(html)


def test_etiquetas_se_escapan():
    malo = "<script>alert(1)</script>"
    ops = [NS(valor='x"><b>', etiqueta=malo, grupo="G<i>")]
    html = _nav(_vc(simulando=malo, restante_min=1, opciones=ops))
    assert malo not in html
    assert "&lt;script&gt;" in html
    _sin_js(html)


class _Obj(dict):
    """Fila que se lee como atributo, igual que la plantilla la usa."""

    __getattr__ = dict.__getitem__


def _pais(solo_vista: bool) -> str:
    tablero = {
        "columnas": [{"clave": "NUEVO", "titulo": "Nuevo", "n": 1, "tarjetas": [{
            "gid": "p1", "referencia": "1-2026", "titulo": "Servidores", "institucion": "MH", "url": "",
            "match": 80, "marcas": [], "monto_txt": "", "siguientes": ["EVALUANDO"], "token": "t",
            "estado": "NUEVO", "exige_motivo": False}]}],
        "asignables": [], "filtros": {"opciones": {}}, "truncado": False,
    }
    return _ENTORNO.get_template("portal_pais.html").render(
        nav=_NAV, csrf="tok", ver_como=_vc(simulando="X" if solo_vista else None, restante_min=5),
        pais_nombre="El Salvador", rangos=[7, 30], dias=30, cliente="c1", aviso=None,
        tablero=tablero, puede_cambiar=True, solo_vista=solo_vista,
    )


def test_pais_solo_vista_deshabilita_controles():
    html = _pais(True)
    assert '<fieldset class="solo-vista" disabled>' in html
    assert "vista simulada" in html
    assert not re.search(r"\son\w+\s*=", html, re.I)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.I)


def test_pais_normal_no_deshabilita():
    html = _pais(False)
    assert "disabled" not in html
    assert "vista simulada" not in html
