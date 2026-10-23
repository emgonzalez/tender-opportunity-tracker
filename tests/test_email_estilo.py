"""Lenguaje visual del correo (tarjetas, KPIs, chips y botones) sobre la estructura de P5.

La estructura (grupos, orden, textos) la cubre `test_email_digest.py`; aquí se fija lo que el
dueño pidió recuperar de la versión larga: jerarquía visual, color por puntaje y urgencia,
botones sólidos y compatibilidad de correo (tablas, CSS en línea, nada de flex/grid).
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.notifier import email_sender
from src.scraper.models import MarcaRecomendada, ProcesoClasificado, ResultadoAnalisis

REPO = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=-6))
AHORA = datetime(2026, 10, 22, 7, 0, tzinfo=TZ)  # jueves
VOTOS = {"relevante": "https://x.test/f/T/relevante", "irrelevante": "https://x.test/f/T/irrelevante"}


def _p(ref, match, dias, marcas=(), metodo="Subasta Inversa"):
    f = match / 100
    return ProcesoClasificado(
        institucion="Ministerio X (MINX)", descripcion=f"Objeto {ref}", fecha_publicacion=AHORA,
        url_directa=f"https://comprasal.example/{ref}", numero_referencia=ref, forma_contratacion=metodo,
        fecha_limite=AHORA + timedelta(days=dias, hours=5), es_relevante=True,
        categoria_detectada="redes", razon="encaja", match_tecnico=f, match_categoria=f, factibilidad=f,
        marcas_recomendadas=[MarcaRecomendada(marca=m, razon="") for m in marcas],
    )


@pytest.fixture
def html(monkeypatch):
    monkeypatch.setattr(email_sender, "TEMPLATES_DIR", REPO / "templates")
    procesos = [_p("A91", 91, 3, ["Aruba"]), _p("B85", 85, 10, ["HPE", "Citrix"]),
                _p("C82", 82, 30), _p("N1", 95, 5), _p("L1", 60, 4)]
    res = {"N1": ResultadoAnalisis("N1", "ANALIZADO", triaje="GO", competible=False,
                                   motivo_bloqueo="proveedor_unico")}
    cfg = SimpleNamespace(app_name="AMALIA", app_full_name="AMALIA", enable_feedback=False,
                          feedback_base_url="", feedback_hmac_secret="")
    perfil = SimpleNamespace(client_id="c", client_name="Multicómputos El Salvador",
                             catalog=SimpleNamespace(categories=[SimpleNamespace(id="redes", label="Networking")]))
    _, render = email_sender.componer_correo(cfg, perfil, procesos, 72, AHORA, res, AHORA)
    return render(lambda o: VOTOS)[0]


def test_encabezado_y_tres_kpis(html):
    assert "REPORTE DIARIO · COMPRASAL" in html
    assert ">Licitaciones TI<" in html and "para Multicómputos El Salvador" in html
    for etiqueta in ("Procesos revisados", "Oportunidades relevantes", "Cierran esta semana"):
        assert etiqueta in html
    kpis = re.findall(r'font:bold 32px/36px[^>]*>(\d+)</div>', html)
    assert kpis == ["72", "5", "1"]  # revisados, relevantes y «para decidir» que cierran en ≤ 7 días (N1 y L1 no cuentan: es la misma cuenta del asunto)


def test_color_del_match_segun_el_puntaje(html):
    def color(n):
        return re.search(rf'font:bold 34px/36px[^"]*;color:(#[0-9A-F]{{6}})">{n}</div>', html).group(1)
    assert color(91) == "#2E7D4F"   # verde ≥ 90
    assert color(85) == "#8A6D1F"   # ámbar 80–89
    assert color(82) == "#8A6D1F"


def test_tarjeta_con_franja_izquierda_numero_de_orden_y_chips(html):
    assert html.count('class="cd fr-dec"') == 3                     # una por «Para decidir»
    assert html.count("border-left:5px solid #0369A1") >= 3         # franja de la sección…
    assert "border-top:3px solid" not in html                       # …en lugar del borde superior navy
    assert re.findall(r">#(\d)</span>", html) == ["1", "2", "3"]
    assert "Networking" in html and "Subasta Inversa" in html     # categoría y método como chips
    assert re.search(r"background:#E4F1E9;color:#2E7D4F[^>]*>Aruba</span>", html)  # marca como chip
    assert "Portafolio:" in html


def test_urgencia_en_chip_rojo_ambar_y_gris(html):
    assert re.search(r"background:#F6E1DC;color:#A33A2A[^>]*>Cierra [^<]*3 días</span>", html)    # ≤ 7
    assert re.search(r"background:#F4EACB;color:#8A6D1F[^>]*>Cierra [^<]*10 días</span>", html)   # 8–14
    assert re.search(r"background:#ECEAE4;color:#6F6B62[^>]*>Cierra [^<]*30 días</span>", html)   # > 14


def test_botones_solidos_a_prueba_de_outlook(html):
    # Celda con bgcolor + <a> con padding: se ve igual en Outlook de escritorio.
    assert re.search(r'<td class="bt" align="center" bgcolor="#1F3A5F"[^>]*><a href="[^"]+"[^>]*padding:12px', html)
    assert re.search(r'bgcolor="#2E7D4F"[^>]*><a href="https://x.test/f/T/relevante"', html)
    assert re.search(r'bgcolor="#A33A2A"[^>]*><a href="https://x.test/f/T/irrelevante"', html)
    assert html.count("Abrir en COMPRASAL →") >= 3


def _bloque(html, clave):
    """Desde la banda de la sección hasta la siguiente (o el pie)."""
    return html.split(f'class="bd-{clave} px"', 1)[1].split('height="32"', 1)[0]


def test_filas_compactas_son_enlaces_y_texto_no_botones(html):
    for clave in ("nc", "baj"):
        seccion = _bloque(html, clave)
        assert "Abrir en COMPRASAL →" in seccion and "text-decoration:underline" in seccion
        assert "border-radius" not in seccion               # ni pills ni botones
        assert 'bgcolor="#1F3A5F"' not in seccion           # sin el botón sólido de las tarjetas


TOKENS = {  # banda, tinte, franja (claro) y su versión oscura
    "dec": ("#0369A1", "#E6F3FA", "#0369A1", "#0369A1", "#0C2333", "#38BDF8"),
    "nc": ("#A21CAF", "#FAE8FC", "#A21CAF", "#A21CAF", "#2A1430", "#E879F9"),
    "baj": ("#4B5563", "#EFF1F4", "#6B7280", "#374151", "#1C1F25", "#9CA3AF"),
}


@pytest.mark.parametrize("clave,titulo", [("dec", "● Para decidir · 3 · Requieren tu decisión"),
                                          ("nc", "⊘ No competibles · 1 · Solo informativo"),
                                          ("baj", "▽ Baja prioridad · 1 · Match < 80 o NO-GO")])
def test_banda_tinte_y_franja_por_seccion(html, clave, titulo):
    banda, tinte, franja, *_ = TOKENS[clave]
    assert re.search(rf'class="bd-{clave} px" bgcolor="{banda}" style="padding:10px 24px;background:{banda};'
                     rf'font:bold 13px/18px[^"]*text-transform:uppercase;color:#FFFFFF">{re.escape(titulo.replace('<', '&lt;'))}<', html)
    seccion = _bloque(html, clave)
    assert f'class="px tn-{clave}"' in seccion or f"tn-{clave} px" in seccion      # fondo teñido
    assert f"background:{tinte}" in seccion
    assert f"border-left:5px solid {franja}" in seccion                           # franja en cada fila
    assert seccion.count(f"fr-{clave}") >= 1


def test_32px_de_aire_antes_de_cada_banda(html):
    assert html.count('height="32" style="height:32px') == 3


def test_leyenda_con_mini_chips_de_los_colores_de_las_bandas(html):
    assert re.search(r'class="bd-dec" style="[^"]*background:#0369A1[^"]*">3 para decidir</span>', html)
    assert re.search(r'class="bd-nc" style="[^"]*background:#A21CAF[^"]*">1 no competible</span>', html)
    assert re.search(r'class="bd-baj" style="[^"]*background:#4B5563[^"]*">1 baja prioridad</span>', html)


def test_secciones_vacias_no_se_muestran(monkeypatch):
    monkeypatch.setattr(email_sender, "TEMPLATES_DIR", REPO / "templates")
    cfg = SimpleNamespace(app_name="AMALIA", app_full_name="AMALIA", enable_feedback=False,
                          feedback_base_url="", feedback_hmac_secret="")
    perfil = SimpleNamespace(client_id="c", client_name="X",
                             catalog=SimpleNamespace(categories=[SimpleNamespace(id="redes", label="Networking")]))
    _, render = email_sender.componer_correo(cfg, perfil, [_p("A91", 91, 3)], 10, AHORA, {}, AHORA)
    solo = render(lambda o: VOTOS)[0]
    assert 'class="bd-dec px"' in solo
    for clave in ("nc", "baj"):
        assert f'class="bd-{clave}' not in solo         # ni banda ni chip


def test_modo_oscuro_clases_y_outlook(html):
    assert '<meta name="color-scheme" content="light dark">' in html
    assert '<meta name="supported-color-schemes" content="light dark">' in html
    css = html.split("<style", 1)[1].split("</style>", 1)[0]
    for clave, (_, _, _, banda_o, tinte_o, franja_o) in TOKENS.items():
        assert f".tn-{clave}{{background:{tinte_o} !important}}" in css
        assert f".fr-{clave}{{border-left-color:{franja_o} !important}}" in css
        assert f"[data-ogsb] .tn-{clave}{{background:{tinte_o} !important}}" in css
    assert ".bd-baj{background:#374151 !important;color:#E5E7EB !important}" in css
    assert "[data-ogsc] .bd-baj{color:#E5E7EB !important}" in css
    assert css.index("@media (prefers-color-scheme:dark)") < css.index(".fr-dec{border-left-color")


def _lum(h):
    c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in c]
    return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]


def _contraste(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def test_contraste_minimo_4_5_en_ambos_temas(html):
    # Texto de banda sobre banda, y los textos de las filas sobre el tinte de su sección.
    claros = {"dec": ("#0369A1", "#E6F3FA"), "nc": ("#A21CAF", "#FAE8FC"), "baj": ("#4B5563", "#EFF1F4")}
    oscuros = {"dec": ("#0369A1", "#0C2333", "#FFFFFF"), "nc": ("#A21CAF", "#2A1430", "#FFFFFF"),
               "baj": ("#374151", "#1C1F25", "#E5E7EB")}
    for clave, (banda, tinte) in claros.items():
        assert _contraste("#FFFFFF", banda) >= 4.5
        for texto in ("#18181A", "#6F6B62", "#1F3A5F", "#276B45", "#A33A2A"):   # tinta, gris, navy, verde, rojo
            assert _contraste(texto, tinte) >= 4.5, (clave, texto)
    for clave, (banda, tinte, txt) in oscuros.items():
        assert _contraste(txt, banda) >= 4.5
        for texto in ("#EDEDED", "#B8B3A8", "#8AB4F8", "#7FD6A0", "#FF8A80"):   # tx, mu, lk, kv, kr
            assert _contraste(texto, tinte) >= 4.5, (clave, texto)
    assert "#276B45" in html


def test_html_de_correo_sin_flex_grid_ni_variables_css(html):
    for prohibido in ("display:flex", "display:grid", "var(--", "<script"):
        assert prohibido not in html
    assert "<table" in html and 'role="presentation"' in html
    # Los estilos de presentación van en línea: el único <style> trae modo oscuro y móvil.
    assert html.count("<style") == 1
    assert "prefers-color-scheme:dark" in html and "max-width:480px" in html
