"""E13 — revisión adversarial: pruebas que fijan los hallazgos de la revisión.

Las marcadas `xfail(strict=True, reason="HALLAZGO …")` demuestran un defecto real: hoy fallan. Quien lo arregle
debe quitar la marca (strict hace que el arreglo rompa la suite hasta que se quite). Las demás son guardas de lo
que la revisión confirmó que funciona y no debe romperse.

La prueba de punta a punta (`test_matriz_de_arrastre_de_punta_a_punta`) hace lo que nadie había hecho: toma el
HTML REAL de `/portal` (TestClient, plantillas reales), lo carga en Chromium con la CSP real, arrastra la tarjeta
de cada estado a cada columna y reenvía el POST que produce el navegador al MISMO TestClient (CSRF, Origin, la
ruta, `oportunidades.service` y `politica` reales; solo el repo SQL está simulado).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote

import pytest
from test_tablero_js import CAPTURA, JS, _chromium, navegador
from test_web_portal import BASE, HOY, _alcance, _cli, _fila, _parchear_e11, _parchear_kpis

from src.oportunidades import repo as op_repo
from src.oportunidades import service as op_service
from src.web import portal
from src.web.plantillas import volver_seguro
from src.web.seguridad import CSP_PORTAL

ESTADOS = {"NUEVO": None, "EVALUANDO": "EVALUANDO", "OFERTADO": "OFERTADO", "GANADO": "GANADO",
           "PERDIDO": "PERDIDO", "DESCARTADO": "DESCARTADO"}
COLUMNAS = ("nuevo", "evaluando", "ofertado", "cerradas")


def _filas_por_estado():
    out = []
    for i, (nombre, estado) in enumerate(ESTADOS.items()):
        out.append(_fila(f"X-{nombre}", "decidir", titulo=f'Título "{nombre}" <b>&amp;</b>', estado=estado,
                         token=f"2026-10-2{i}T10:00:00.000000" if estado else "",
                         actualizado_en=datetime(2026, 10, 20 + i, 10) if estado else None,
                         fecha_limite=HOY + timedelta(days=20), dias_restantes=20, urgencia="verde",
                         cierre_texto="Cierra en 20 días"))
    return out


@pytest.fixture
def servidor(monkeypatch):
    """`ns.html(filas)` → HTML real de /portal; `ns.cliente` el TestClient; `ns.escritos` lo que llegó al repo."""
    ns = SimpleNamespace(escritos=[], cliente=None)
    escritos = ns.escritos
    estado = {"filas": {}}
    real = portal.pagina
    _parchear_kpis(monkeypatch)

    def html_de(filas, cambia=("cli",)):
        estado["filas"] = {f["proceso_gid"]: f for f in filas}
        _parchear_e11(monkeypatch, filas=filas, asignables=[{"usuario_id": "u2", "nombre": "Ana"}])
        monkeypatch.setattr(portal, "pagina", real)
        cli = _cli(monkeypatch, _alcance(roles=("gerente_pais",), clientes=("cli",), cambia=cambia))
        ns.cliente = cli
        r = cli.get("/portal?dias=30")
        assert r.status_code == 200
        return r.text

    monkeypatch.setattr(op_repo, "proceso_visible", lambda c, cl, g: g in estado["filas"])
    monkeypatch.setattr(op_repo, "obtener", lambda c, cl, g: (
        {"estado": estado["filas"][g]["estado"], "token": estado["filas"][g]["token"]}
        if estado["filas"].get(g, {}).get("estado") else None))
    monkeypatch.setattr(op_repo, "actualizar_estado", lambda *a: escritos.append((a[2], a[4])) or True)
    monkeypatch.setattr(op_repo, "crear_estado", lambda *a: escritos.append((a[2], a[3])) or True)
    monkeypatch.setattr(op_repo, "insertar_evento", lambda *a: None)
    monkeypatch.setattr(op_service.identidad, "registrar_auditoria", lambda *a, **k: None)
    ns.html = html_de
    return ns


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


# ── (a) el servidor revalida lo que el JS no puede garantizar ─────────────────────────────────────

@pytest.mark.parametrize("gid, estado, token, motivo, aviso", [
    ("X-NUEVO", "GANADO", "", "", "invalido"),                       # salto de etapa
    ("X-NUEVO", "DESCARTADO", "", "", "invalido"),                   # sin motivo
    ("X-NUEVO", "BOGUS", "", "", "invalido"),                        # estado inventado
    ("X-EVALUANDO", "OFERTADO", "viejo", "", "conflicto"),           # token viejo
    ("X-GANADO", "PERDIDO", "2026-10-23T10:00:00.000000", "", "invalido"),  # corregir un final sin motivo
])
def test_post_manipulado_lo_rechaza_el_servidor(servidor, gid, estado, token, motivo, aviso):
    html = servidor.html(_filas_por_estado())
    r = servidor.cliente.post(f"/portal/oportunidad/comprasal_sv:{gid}/estado", headers={"Origin": BASE}, data={
        "csrf": _csrf(html), "cliente": "cli", "dias": "30", "estado": estado, "actualizado_en": token,
        "motivo": motivo})
    assert r.status_code == 303
    assert f"aviso={aviso}" in r.headers["location"]
    assert r.headers["location"].endswith(f"#op-comprasal_sv%3A{gid}")
    assert servidor.escritos == []


def test_gid_con_crlf_no_inyecta_cabeceras_en_el_ancla(servidor):
    html = servidor.html(_filas_por_estado())
    r = servidor.cliente.post("/portal/oportunidad/a%0d%0aSet-Cookie:%20x=1/estado", headers={"Origin": BASE},
                              data={"csrf": _csrf(html), "cliente": "cli", "dias": "30", "estado": "BOGUS"})
    assert r.status_code == 303
    assert "\r" not in r.headers["location"] and "\n" not in r.headers["location"]
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}


# ── (b) selector de tema: `volver` ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor", [
    "/portal@evil", "/portal%2f%2fevil", "/portal/\\evil", "/portal?x=\r\nSet-Cookie: a=1", "/portal?x=%0d%0a",
    "/PORTAL", "/portal∕∕evil", "/portal／／evil.com", "/portal?x=//evil", "/portal#//evil",
    "/portal/%5c%5cevil", "/portal/\t/evil", "//evil.com", "https://evil.com/portal", "/portalx",
])
def test_volver_nunca_sale_del_origen(valor):
    assert volver_seguro(valor) == "/portal"


@pytest.mark.parametrize("valor", ["/portal/../acceso", "/portal/%2e%2e/salir", "/portal/.%2e/acceso",
                                   "/portal/%2E%2E/f/tok/relevante"])
def test_volver_rechaza_segmentos_punto(valor):
    assert volver_seguro(valor) == "/portal"


# ── (c) negocio: lo urgente tiene que poder accionarse ──────────────────────────────────────────

def test_lo_urgente_de_atencion_siempre_esta_en_el_tablero(servidor):
    filas = [_fila(f"B-{i:03d}", "baja", match=50, fecha_limite=HOY, dias_restantes=0, urgencia="rojo",
                   cierre_texto="Cierra hoy") for i in range(30)]
    filas.append(_fila("URGENTE", "decidir", match=95, fecha_limite=HOY + timedelta(days=3), dias_restantes=3,
                       urgencia="rojo", cierre_texto="Cierra en 3 días"))
    html = servidor.html(filas)
    atencion = html.split('id="h-atencion"', 1)[1].split("</article>", 1)[0]
    assert "#op-comprasal_sv%3AURGENTE" in atencion            # está en «Requiere atención»…
    assert 'id="op-comprasal_sv%3AURGENTE"' in html             # …y su tarjeta existe para poder moverla


def test_anclas_de_atencion_y_aria_labelledby_resuelven_con_pocos_datos(servidor):
    html = servidor.html(_filas_por_estado())
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    assert [x for x in re.findall(r'href="#([^"]+)"', html) if x not in ids] == []
    assert [x for x in re.findall(r'aria-labelledby="([^"]+)"', html) if x not in ids] == []


def test_tarjeta_sin_destino_fuera_de_su_columna_no_es_arrastrable(servidor):
    html = servidor.html(_filas_por_estado())
    for nombre in ("GANADO", "PERDIDO"):
        t = re.search(rf'<article [^>]*id="op-comprasal_sv%3AX-{nombre}"[^>]*>', html).group(0)
        assert "draggable" not in t, nombre


# ── (e) robustez del JS ──────────────────────────────────────────────────────────────────────────

def _abrir(browser, html, extra_js=0):
    page = browser.new_page()
    errores = []
    page.on("pageerror", lambda e: errores.append(str(e)))
    page._errores = errores
    for i in range(extra_js):
        html = html.replace("</body>", f'<script src="/portal/static/portal/tablero.js?v={i}" defer></script></body>')

    def servir(route, _req=None):
        url = route.request.url.split("?")[0]
        if url.endswith("/portal/static/portal/tablero.js"):
            route.fulfill(body=JS.read_text(encoding="utf-8"), content_type="text/javascript")
        elif "/portal/static/" in url:
            route.fulfill(body="", content_type="text/javascript")
        elif route.request.method == "POST":
            datos = {k: v[0] for k, v in parse_qs(route.request.post_data or "", keep_blank_values=True).items()}
            r = servidor_actual.cliente.post(url[len(BASE):], data=datos, headers={"Origin": BASE})
            page._posts.append((url[len(BASE):], datos, r.status_code, r.headers.get("location")))
            route.fulfill(body="<p>ok</p>", content_type="text/html")
        else:
            route.fulfill(body=html, content_type="text/html; charset=utf-8",
                          headers={"Content-Security-Policy": CSP_PORTAL})
    page._posts = []
    page.route(BASE + "/**", servir)
    page.goto(BASE + "/portal?dias=30")
    return page


servidor_actual = None


def _arrastrar(page, sel, col):
    dt = page.evaluate_handle("new DataTransfer()")
    dest = f'[data-columna="{col}"] .col-cuerpo'
    for ev, s in (("dragstart", sel), ("dragenter", dest), ("dragover", dest), ("drop", dest)):
        page.dispatch_event(s, ev, {"dataTransfer": dt})
    try:
        page.dispatch_event(sel, "dragend", {"dataTransfer": dt}, timeout=500)
    except Exception:  # noqa: BLE001 — la página ya navegó tras el envío
        pass


@pytest.fixture(scope="module")
def navegador_chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chromium())
        yield b
        b.close()


# Matriz esperada: (estado de la tarjeta, columna destino) → estado enviado; None = no envía.
# «D» = abre el diálogo: todo soltar válido se confirma (el cambio no se puede deshacer arrastrando).
ESPERADO = {
    ("NUEVO", "evaluando"): "D:EVALUANDO", ("NUEVO", "cerradas"): "D:DESCARTADO",
    ("EVALUANDO", "ofertado"): "D:OFERTADO", ("EVALUANDO", "cerradas"): "D:DESCARTADO",
    ("OFERTADO", "cerradas"): "D:GANADO",
    ("DESCARTADO", "evaluando"): "D:EVALUANDO",
}


@navegador
@pytest.mark.parametrize("estado", list(ESTADOS))
@pytest.mark.parametrize("columna", COLUMNAS)
def test_matriz_de_arrastre_de_punta_a_punta(servidor, navegador_chromium, estado, columna):
    global servidor_actual
    html = servidor.html(_filas_por_estado())
    servidor_actual = servidor
    page = _abrir(navegador_chromium, html)
    sel = f'[id="op-comprasal_sv%3AX-{estado}"]'
    if estado in ("GANADO", "PERDIDO"):
        # c2: todos sus destinos están en su propia columna: no se arrastran (se corrigen con «Mover a…»)
        assert page.evaluate("(s) => !document.querySelector(s).hasAttribute('draggable')", sel)
        page.close()
        return
    _arrastrar(page, sel, columna)
    page.wait_for_timeout(150)
    esperado = ESPERADO.get((estado, columna))
    if esperado and esperado.startswith("D:"):
        assert page.evaluate("document.getElementById('dlg-mover').open")
        assert page._posts == []                           # nada se envía sin confirmar
        if page.locator("input[name=dlg_estado]").count():  # varios destinos: ninguno preseleccionado
            assert page.locator("input[name=dlg_estado]:checked").count() == 0
            page.check(f'input[name=dlg_estado][value="{esperado[2:]}"]')
        motivo_req = page.evaluate("document.getElementById('dlg-motivo').required")
        if motivo_req:
            page.click("#dlg-guardar")
            page.wait_for_timeout(100)
            assert page._posts == []                       # sin motivo no se envía
            page.fill("#dlg-motivo", "prueba e2e")
        page.click("#dlg-guardar")
        page.wait_for_timeout(300)
        esperado = esperado[2:]
    else:
        page.wait_for_timeout(300)
    if esperado is None:
        assert page._posts == []
        assert page.text_content("#aviso-dnd")              # y lo dice en la región aria-live
    else:
        assert len(page._posts) == 1
        ruta, datos, status, location = page._posts[0]
        assert ruta == f"/portal/oportunidad/comprasal_sv%3AX-{estado}/estado" or unquote(ruta).endswith(
            f"X-{estado}/estado")
        assert datos["estado"] == esperado and datos["csrf"] and "actualizado_en" in datos
        assert status == 303 and "aviso=ok" in location and location.endswith(f"#op-comprasal_sv%3AX-{estado}")
        assert servidor.escritos == [(f"comprasal_sv:X-{estado}", esperado)]
    assert page._errores == []
    page.close()


@navegador
def test_doble_soltar_antes_del_prg_envia_una_sola_vez(navegador_chromium):
    from test_tablero_js import _pagina, _tarjeta
    page = navegador_chromium.new_page()
    page.route("http://amalia.test/**", lambda route, _r=None: route.fulfill(
        body=JS.read_text(encoding="utf-8") if route.request.url.endswith(".js") else
        _pagina({"nuevo": _tarjeta("g1", "NUEVO")}),
        content_type="text/javascript" if route.request.url.endswith(".js") else "text/html",
        headers={"Content-Security-Policy": CSP_PORTAL}))
    page.add_init_script(CAPTURA)
    page.goto("http://amalia.test/portal")
    _arrastrar(page, "#op-g1", "evaluando")
    page.click("#dlg-guardar")
    _arrastrar(page, "#op-g1", "evaluando")
    page.evaluate("document.getElementById('dlg-guardar').click()")
    assert len(page.evaluate("window.__envios")) == 1
    page.close()


@navegador
def test_script_cargado_dos_veces_no_duplica_envios(navegador_chromium):
    from test_tablero_js import _pagina, _tarjeta
    html = _pagina({"nuevo": _tarjeta("g1", "NUEVO")}).replace(
        "</body>", '<script src="/portal/static/portal/tablero.js?v=2"></script></body>')
    page = navegador_chromium.new_page()
    page.route("http://amalia.test/**", lambda route, _r=None: route.fulfill(
        body=JS.read_text(encoding="utf-8") if ".js" in route.request.url else html,
        content_type="text/javascript" if ".js" in route.request.url else "text/html",
        headers={"Content-Security-Policy": CSP_PORTAL}))
    page.add_init_script(CAPTURA)
    page.goto("http://amalia.test/portal")
    _arrastrar(page, "#op-g1", "evaluando")
    page.click("#dlg-guardar")
    assert len(page.evaluate("window.__envios")) == 1
    page.close()
