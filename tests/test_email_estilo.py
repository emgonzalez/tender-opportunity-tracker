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


def test_tarjeta_con_borde_navy_numero_de_orden_y_chips(html):
    assert html.count("border-top:3px solid #1F3A5F") == 3          # una por «Para decidir»
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


def test_filas_compactas_usan_pills_y_no_botones_grandes(html):
    for titulo in ("No competibles", "Baja prioridad"):
        seccion = html.split(f">{titulo} ·", 1)[1].split("</table>", 1)[0]
        assert "border-radius:10px" in seccion          # mini-pills
        assert 'bgcolor="#1F3A5F"' not in seccion       # sin el botón sólido de las tarjetas


def test_html_de_correo_sin_flex_grid_ni_variables_css(html):
    for prohibido in ("display:flex", "display:grid", "var(--", "<script"):
        assert prohibido not in html
    assert "<table" in html and 'role="presentation"' in html
    # Los estilos de presentación van en línea: el único <style> trae modo oscuro y móvil.
    assert html.count("<style") == 1
    assert "prefers-color-scheme:dark" in html and "max-width:480px" in html
