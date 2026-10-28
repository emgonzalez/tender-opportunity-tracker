"""E13 — `static/portal/tablero.js`: arrastrar tarjetas del tablero y abrir acordeones por ancla.

Dos niveles:
  (a) estáticos (corren en el CI): lo que el archivo NO puede hacer (eval, innerHTML, fetch…),
      qué `data-*` lee, que se sirve bajo `/portal/static/portal/` y que el diálogo no trae
      scripts ni estilos inline;
  (b) de navegador: una página mínima según el contrato con las plantillas, servida con la CSP
      real del portal, donde se simula el arrastre con eventos DnD y se capturan los envíos.
      Se omiten si no hay Playwright o Chromium (`$CHROMIUM_EXE` o el de `ms-playwright`).
"""
from __future__ import annotations

import glob
import json
import os
import re
from html import escape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web_portal import _cfg

from src.web.app import crear_app
from src.web.seguridad import CSP_PORTAL

RAIZ = Path(__file__).resolve().parents[1]
JS = RAIZ / "static" / "portal" / "tablero.js"
DIALOGO = RAIZ / "templates" / "portal_pais_dialogo.html"

DATA_DEL_CONTRATO = {"data-editable", "data-columna", "data-estados", "data-gid", "data-estado",
                     "data-siguientes", "data-exige-motivo"}


def _codigo_sin_comentarios() -> str:
    txt = JS.read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}),])\s*//.*$", "", txt)


# ── (a) Estáticos ─────────────────────────────────────────────────────────────

def test_el_archivo_existe_en_static_portal():
    assert JS.is_file()


@pytest.mark.parametrize("prohibido", [
    "eval", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
    "fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "sessionStorage", "setTimeout(\"",
])
def test_no_usa_apis_peligrosas(prohibido):
    codigo = JS.read_text(encoding="utf-8")
    if prohibido == "eval":
        assert not re.search(r"\beval\b", codigo)
    else:
        assert prohibido not in codigo


def test_solo_lee_los_data_del_contrato():
    usados = set(re.findall(r"data-[a-z][a-z-]*", _codigo_sin_comentarios()))
    assert usados <= DATA_DEL_CONTRATO, usados - DATA_DEL_CONTRATO


def test_es_un_iife_estricto():
    codigo = _codigo_sin_comentarios().strip()
    assert codigo.startswith("(function () {") and codigo.endswith("})();")
    assert '"use strict"' in codigo


def test_se_sirve_bajo_portal_static_como_js():
    r = TestClient(crear_app(_cfg())).get("/portal/static/portal/tablero.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.text == JS.read_text(encoding="utf-8")


def test_dialogo_sin_script_ni_estilos_ni_manejadores_inline():
    html = DIALOGO.read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "style=" not in html.lower()
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I)


@pytest.mark.parametrize("fragmento", [
    '<dialog id="dlg-mover" class="dlg" aria-labelledby="dlg-titulo"',
    '<h3 id="dlg-titulo">', '<p id="dlg-resumen">', '<fieldset id="dlg-estados">',
    'id="dlg-motivo" maxlength="500"', 'id="dlg-monto" inputmode="decimal" maxlength="16"',
    'id="dlg-competidor" maxlength="300"', '<p id="dlg-error" role="alert">',
    '<button type="button" id="dlg-cancelar"', '<button type="button" id="dlg-guardar"',
    'class="dlg-campos"', 'class="dlg-acciones"',
])
def test_dialogo_trae_los_ids_del_contrato(fragmento):
    assert fragmento in DIALOGO.read_text(encoding="utf-8")


def test_dialogo_renderiza_con_jinja():
    from src.web.plantillas import crear_entorno
    html = crear_entorno().get_template("portal_pais_dialogo.html").render()
    assert html.count('id="dlg-mover"') == 1


# ── (b) Navegador ─────────────────────────────────────────────────────────────

def _chromium() -> str | None:
    explicito = os.environ.get("CHROMIUM_EXE")
    if explicito and Path(explicito).is_file():
        return explicito
    hallados = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/**/chrome-headless-shell"), recursive=True))
    return hallados[-1] if hallados else None


try:  # pragma: no cover - depende del entorno
    import playwright.sync_api  # noqa: F401
    _HAY_PLAYWRIGHT = True
except Exception:  # pragma: no cover
    _HAY_PLAYWRIGHT = False

navegador = pytest.mark.skipif(not (_HAY_PLAYWRIGHT and _chromium()),
                               reason="sin Playwright o sin Chromium (define CHROMIUM_EXE)")

ORIGEN = "http://amalia.test"

COLUMNAS = [
    ("nuevo", "Nuevo", "NUEVO"),
    ("evaluando", "Evaluando", "EVALUANDO"),
    ("ofertado", "Ofertado", "OFERTADO"),
    ("cerradas", "Cerradas", "GANADO,PERDIDO,DESCARTADO"),
]
SIGUIENTES = {
    "NUEVO": ["EVALUANDO", "DESCARTADO"], "EVALUANDO": ["OFERTADO", "DESCARTADO"],
    "OFERTADO": ["GANADO", "PERDIDO", "DESCARTADO"], "GANADO": ["PERDIDO", "DESCARTADO"],
    "PERDIDO": ["GANADO", "DESCARTADO"], "DESCARTADO": ["EVALUANDO"],
}
FINALES = {"GANADO", "PERDIDO", "DESCARTADO"}


def _tarjeta(gid, estado, titulo="Servidores", exige=None, editable=True):
    sig = SIGUIENTES[estado]
    if exige is None:
        exige = json.dumps({e: e == "DESCARTADO" or estado in FINALES for e in sig})
    opciones = "".join(f'<option value="{e}">{e.capitalize()}</option>' for e in sig)
    drag = ' draggable="true"' if editable else ""
    return f"""
<article class="tarjeta-op" id="op-{gid}" data-gid="{gid}" data-estado="{estado}"
  data-siguientes="{",".join(sig)}" data-exige-motivo="{escape(exige, quote=True)}"{drag} tabindex="0">
  <span class="tk-ref mono">REF-{gid}</span>
  <div class="tk-t">{escape(titulo)}</div>
  <div class="acciones"><details><summary>Mover a…</summary>
    <form method="post" action="/portal/oportunidad/{gid}/estado" class="cambio">
      <fieldset class="solo-vista">
        <input type="hidden" name="csrf" value="tok-csrf">
        <input type="hidden" name="cliente" value="cli">
        <input type="hidden" name="dias" value="30">
        <input type="hidden" name="actualizado_en" value="2026-10-01T00:00:00">
        <select name="estado" required>{opciones}</select>
        <input type="text" name="motivo" maxlength="500">
        <input type="text" name="monto_ofertado" inputmode="decimal" maxlength="16">
        <input type="text" name="competidor" maxlength="300">
        <button type="submit">Guardar</button>
      </fieldset>
    </form></details></div>
</article>"""


def _pagina(tarjetas: dict[str, str], editable=True, con_dialogo=True, extra=""):
    """`tarjetas`: clave de columna → HTML de sus tarjetas."""
    cols = "".join(
        f'<section class="col-tablero" data-columna="{c}" data-estados="{e}" aria-labelledby="col-{c}">'
        f'<h3 id="col-{c}"><span>{t}</span> <span class="chip n">1</span></h3>'
        f'<div class="col-cuerpo">{tarjetas.get(c, "")}</div></section>'
        for c, t, e in COLUMNAS)
    dialogo = DIALOGO.read_text(encoding="utf-8") if con_dialogo else ""
    dialogo = re.sub(r"\{#.*?#\}", "", dialogo, flags=re.S)
    attr = ' data-editable="1"' if editable else ""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>t</title>
<style>.tablero {{ display: flex; gap: 8px; }} .col-tablero {{ flex: 1; min-height: 120px; }}
.col-cuerpo {{ min-height: 100px; }} .tarjeta-op {{ min-height: 40px; }}</style></head>
<body>{extra}<div id="tablero"{attr}><div class="tablero">{cols}</div>
<div id="aviso-dnd" class="solo-lectores" role="status" aria-live="polite"></div>{dialogo}</div>
<script src="/portal/static/portal/tablero.js"></script></body></html>"""


CAPTURA = """
window.__envios = [];
window.__errores = [];
window.addEventListener('error', function (e) { window.__errores.push(String(e.message)); });
document.addEventListener('submit', function (e) {
  e.preventDefault();
  var d = {}; new FormData(e.target).forEach(function (v, k) { d[k] = v; });
  d.__action = e.target.getAttribute('action');
  window.__envios.push(d);
}, true);
"""


@pytest.fixture(scope="module")
def _browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        yield b
        b.close()


@pytest.fixture
def abrir(_browser):
    """Abre la página servida con la CSP del portal; devuelve la `page`."""
    paginas = []

    def _abrir(html, ruta="/portal"):
        page = _browser.new_page()
        violaciones = []
        page.on("console", lambda m: violaciones.append(m.text) if "Content Security Policy" in m.text else None)
        page._violaciones_csp = violaciones

        def servir(route):
            url = route.request.url
            if url.endswith("/portal/static/portal/tablero.js"):
                route.fulfill(body=JS.read_text(encoding="utf-8"), content_type="text/javascript")
            else:
                route.fulfill(body=html, content_type="text/html; charset=utf-8",
                              headers={"Content-Security-Policy": CSP_PORTAL})
        page.route(ORIGEN + "/**", servir)
        page.add_init_script(CAPTURA)
        page.goto(ORIGEN + ruta)
        paginas.append(page)
        return page

    yield _abrir
    for p in paginas:
        p.close()


def _arrastrar(page, tarjeta_sel, columna, soltar=True):
    dt = page.evaluate_handle("new DataTransfer()")
    destino = f'[data-columna="{columna}"] .col-cuerpo'
    page.dispatch_event(tarjeta_sel, "dragstart", {"dataTransfer": dt})
    page.dispatch_event(destino, "dragenter", {"dataTransfer": dt})
    page.dispatch_event(destino, "dragover", {"dataTransfer": dt})
    if soltar:
        page.dispatch_event(destino, "drop", {"dataTransfer": dt})
    page.dispatch_event(tarjeta_sel, "dragend", {"dataTransfer": dt})


def _envios(page):
    return page.evaluate("window.__envios")


def _sin_errores(page):
    assert page.evaluate("window.__errores") == []
    assert page._violaciones_csp == []


@navegador
def test_destino_unico_envia_sin_dialogo(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO")}))
    _arrastrar(page, "#op-g1", "evaluando")
    env = _envios(page)
    assert len(env) == 1
    assert env[0]["estado"] == "EVALUANDO" and env[0]["motivo"] == ""
    assert env[0]["csrf"] == "tok-csrf" and env[0]["actualizado_en"] == "2026-10-01T00:00:00"
    assert env[0]["__action"] == "/portal/oportunidad/g1/estado"
    assert not page.evaluate("document.getElementById('dlg-mover').open")
    _sin_errores(page)


@navegador
def test_resalta_columnas_validas_durante_el_arrastre(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO")}))
    dt = page.evaluate_handle("new DataTransfer()")
    page.dispatch_event("#op-g1", "dragstart", {"dataTransfer": dt})
    clases = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.col-tablero')]
        .map(c => [c.dataset.columna, c.className]))""")
    assert "col-valida" in clases["evaluando"] and "col-valida" in clases["cerradas"]
    assert "col-invalida" in clases["ofertado"] and "col-invalida" in clases["nuevo"]
    assert "arrastrando" in page.get_attribute("#op-g1", "class")
    page.dispatch_event("#op-g1", "dragend", {"dataTransfer": dt})
    assert page.evaluate("document.querySelectorAll('.col-valida,.col-invalida,.arrastrando').length") == 0


@navegador
def test_nuevo_a_cerradas_exige_motivo_y_envia_descartado(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO")}))
    _arrastrar(page, "#op-g1", "cerradas")
    assert page.evaluate("document.getElementById('dlg-mover').open")
    radios = page.evaluate("[...document.querySelectorAll('input[name=dlg_estado]')].map(r => [r.value, r.checked])")
    assert radios == [["DESCARTADO", True]]
    assert page.is_visible("#dlg-motivo-nota")
    page.click("#dlg-guardar")
    assert "obligatorio" in page.text_content("#dlg-error")
    assert _envios(page) == []
    page.fill("#dlg-motivo", "Fuera de catálogo")
    page.click("#dlg-guardar")
    env = _envios(page)
    assert len(env) == 1 and env[0]["estado"] == "DESCARTADO" and env[0]["motivo"] == "Fuera de catálogo"
    assert not page.evaluate("document.getElementById('dlg-mover').open")
    _sin_errores(page)


@navegador
def test_ofertado_a_cerradas_elige_ganado_con_monto_y_competidor(abrir):
    page = abrir(_pagina({"ofertado": _tarjeta("g2", "OFERTADO")}))
    _arrastrar(page, "#op-g2", "cerradas")
    valores = page.evaluate("[...document.querySelectorAll('input[name=dlg_estado]')].map(r => r.value)")
    assert valores == ["GANADO", "PERDIDO", "DESCARTADO"]
    assert page.is_visible("#dlg-monto") and page.is_visible("#dlg-competidor")
    assert not page.is_visible("#dlg-motivo-nota")
    page.check('input[name=dlg_estado][value="PERDIDO"]')
    assert not page.is_visible("#dlg-monto") and page.is_visible("#dlg-competidor")
    page.check('input[name=dlg_estado][value="GANADO"]')
    page.fill("#dlg-monto", "12500.50")
    page.fill("#dlg-competidor", "Otro S.A.")
    page.click("#dlg-guardar")
    env = _envios(page)
    assert len(env) == 1
    assert (env[0]["estado"], env[0]["monto_ofertado"], env[0]["competidor"], env[0]["motivo"]) == \
        ("GANADO", "12500.50", "Otro S.A.", "")
    _sin_errores(page)


@navegador
def test_cancelar_no_envia(abrir):
    page = abrir(_pagina({"ofertado": _tarjeta("g2", "OFERTADO")}))
    _arrastrar(page, "#op-g2", "cerradas")
    page.click("#dlg-cancelar")
    assert not page.evaluate("document.getElementById('dlg-mover').open")
    _arrastrar(page, "#op-g2", "cerradas")
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('dlg-mover').open")
    assert _envios(page) == []


@navegador
def test_correccion_de_estado_final_exige_motivo_aun_con_destino_unico(abrir):
    page = abrir(_pagina({"cerradas": _tarjeta("g3", "DESCARTADO")}))
    _arrastrar(page, "#op-g3", "evaluando")
    assert page.evaluate("document.getElementById('dlg-mover').open")
    page.click("#dlg-guardar")
    assert _envios(page) == []
    page.fill("#dlg-motivo", "Se reabrió")
    page.click("#dlg-guardar")
    assert _envios(page)[0]["estado"] == "EVALUANDO"


@navegador
def test_transicion_invalida_no_envia_y_avisa(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO")}))
    _arrastrar(page, "#op-g1", "ofertado")
    assert _envios(page) == []
    assert page.text_content("#aviso-dnd") == "No se puede pasar de Nuevo a Ofertado."
    _arrastrar(page, "#op-g1", "nuevo")
    assert _envios(page) == []
    assert "ya está en Nuevo" in page.text_content("#aviso-dnd")
    _sin_errores(page)


@navegador
def test_sin_data_editable_no_hace_nada(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO")}, editable=False))
    _arrastrar(page, "#op-g1", "evaluando")
    assert _envios(page) == []
    assert page.text_content("#aviso-dnd") == ""
    assert "arrastrando" not in (page.get_attribute("#op-g1", "class") or "")


@navegador
def test_tarjeta_sin_draggable_no_se_arrastra(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO", editable=False)}))
    _arrastrar(page, "#op-g1", "evaluando")
    assert _envios(page) == []


@navegador
def test_json_de_exige_motivo_corrupto_no_rompe(abrir):
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO", exige="{no es json") + _tarjeta("g4", "NUEVO")}))
    _arrastrar(page, "#op-g1", "evaluando")
    assert _envios(page) == []
    assert page.evaluate("document.querySelectorAll('.col-valida,.arrastrando').length") == 0
    _arrastrar(page, "#op-g4", "evaluando")   # la otra tarjeta sigue funcionando
    assert [e["estado"] for e in _envios(page)] == ["EVALUANDO"]
    _sin_errores(page)


@navegador
def test_textos_con_html_no_se_interpretan(abrir):
    malo = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
    page = abrir(_pagina({"ofertado": _tarjeta("g2", "OFERTADO", titulo=malo)}))
    _arrastrar(page, "#op-g2", "cerradas")
    assert malo in page.text_content("#dlg-resumen")
    assert page.evaluate("document.querySelectorAll('#dlg-mover img, #dlg-mover script').length") == 0
    assert page.evaluate("window.__xss") is None


@navegador
def test_ancla_al_cargar_abre_el_details(abrir):
    extra = ('<nav><a id="ir" href="#equipo">Equipo</a></nav>'
             '<details id="clasificacion"><summary>Clasificación</summary><p>x</p></details>'
             '<details><summary>Equipo</summary><p id="equipo">y</p></details>')
    page = abrir(_pagina({}, extra=extra), ruta="/portal#clasificacion")
    assert page.evaluate("document.getElementById('clasificacion').open")
    assert not page.evaluate("document.getElementById('equipo').parentElement.open")
    page.click("#ir")
    assert page.evaluate("document.getElementById('equipo').parentElement.open")
    page.evaluate("document.getElementById('clasificacion').open = false; location.hash = '#nada'")
    page.evaluate("location.hash = '#clasificacion'")
    page.wait_for_function("document.getElementById('clasificacion').open")
    _sin_errores(page)


@navegador
def test_ancla_inexistente_no_lanza(abrir):
    page = abrir(_pagina({}), ruta="/portal#no-existe%ZZ")
    _sin_errores(page)


@navegador
def test_arrastre_real_con_raton(abrir):
    """Arrastre nativo de Chromium (ratón), no eventos sintéticos: valida dragover/preventDefault de verdad."""
    page = abrir(_pagina({"nuevo": _tarjeta("g1", "NUEVO"), "ofertado": _tarjeta("g2", "OFERTADO")}))
    page.drag_and_drop("#op-g1 .tk-t", '[data-columna="evaluando"] .col-cuerpo')
    assert [e["estado"] for e in _envios(page)] == ["EVALUANDO"]
    page.drag_and_drop("#op-g1 .tk-t", '[data-columna="ofertado"] .col-cuerpo')
    assert len(_envios(page)) == 1
    page.drag_and_drop("#op-g2 .tk-t", '[data-columna="cerradas"] .col-cuerpo')
    assert page.evaluate("document.getElementById('dlg-mover').open")
    _sin_errores(page)
