"""Textos y moneda por portal en las plantillas (E5): los defaults son los de COMPRASAL, y SICOP
cambia solo las palabras y la moneda; nada más.

Con las variables de COMPRASAL explícitas el HTML tiene que salir igual que sin ellas.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.notifier import email_sender
from src.scraper.models import ResultadoAnalisis
from src.web.plantillas import crear_entorno
from tests import test_email_estilo as te
from tests import test_plantillas_pais as tp

COMPRASAL = {
    "portal_nombre": "COMPRASAL",
    "portal_url": "https://www.comprasal.gob.sv/",
    "etiqueta_documento": "pliego",
    "etiqueta_documento_plural": "pliegos",
    "etiqueta_proceso_plural": "procesos",
    "moneda_monto": "USD",
}
SICOP = {
    "portal_nombre": "SICOP",
    "portal_url": "https://www.sicop.go.cr/",
    "etiqueta_documento": "cartel",
    "etiqueta_documento_plural": "carteles",
    "etiqueta_proceso_plural": "concursos",
    "moneda_monto": "CRC",
}
TZ = timezone(timedelta(hours=-6))
AHORA = datetime(2026, 10, 22, 7, 0, tzinfo=TZ)


def _pagina_pais(extra: dict) -> str:
    ctx = tp._contexto()
    ctx.update(extra)
    return crear_entorno().get_template("portal_pais.html").render(**ctx)


def _pagina_pais_solo_vista(extra: dict) -> str:
    ctx = tp._contexto(puede_cambiar=False, solo_vista=True)
    ctx.update(extra)
    return crear_entorno().get_template("portal_pais.html").render(**ctx)


def _feedback(extra: dict) -> str:
    return crear_entorno().get_template("feedback_confirmar.html").render(**extra)


def _report(monkeypatch, globales: dict) -> str:
    """Correo real (como test_email_estilo) con variables de portal como globals del entorno."""
    monkeypatch.setattr(email_sender, "TEMPLATES_DIR", te.REPO / "templates")

    def entorno():
        env = Environment(
            loader=FileSystemLoader(str(email_sender.TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        env.globals.update(globales)
        return env

    monkeypatch.setattr(email_sender, "_get_jinja_env", entorno)
    procesos = [te._p("A91", 91, 3, ["Aruba"]), te._p("B85", 85, 10, ["HPE", "Citrix"]),
                te._p("C82", 82, 30), te._p("N1", 95, 5), te._p("L1", 60, 4)]
    res = {"N1": ResultadoAnalisis("N1", "ANALIZADO", triaje="GO", competible=False,
                                   motivo_bloqueo="proveedor_unico")}
    cfg = SimpleNamespace(app_name="AMALIA", app_full_name="AMALIA", enable_feedback=False,
                          feedback_base_url="", feedback_hmac_secret="")
    perfil = SimpleNamespace(client_id="c", client_name="Multicómputos El Salvador",
                             catalog=SimpleNamespace(categories=[SimpleNamespace(id="redes", label="Networking")]))
    _, render = email_sender.componer_correo(cfg, perfil, procesos, 72, AHORA, res, AHORA)
    return render(lambda o: {"relevante": "https://x.test/f/T/relevant",
                             "irrelevante": "https://x.test/f/T/irrelevant"})[0]


# --- COMPRASAL: explícito == por defecto -------------------------------------------------

def test_portal_pais_comprasal_explicito_igual_que_por_defecto():
    assert _pagina_pais(COMPRASAL) == _pagina_pais({})


def test_portal_pais_solo_vista_comprasal_explicito_igual_que_por_defecto():
    assert _pagina_pais_solo_vista(COMPRASAL) == _pagina_pais_solo_vista({})


def test_feedback_comprasal_explicito_igual_que_por_defecto():
    assert _feedback({"portal_nombre": "COMPRASAL"}) == _feedback({})


def test_correo_comprasal_explicito_igual_que_por_defecto(monkeypatch):
    por_defecto = _report(monkeypatch, {})
    explicito = _report(monkeypatch, COMPRASAL)
    assert explicito == por_defecto
    assert "REPORTE DIARIO · COMPRASAL" in por_defecto
    assert "Abrir en COMPRASAL →" in por_defecto


# --- SICOP: cambian los textos y la moneda, y nada de COMPRASAL -------------------------

def test_portal_pais_sicop_textos_y_moneda():
    html = _pagina_pais(SICOP)
    for texto in ("Conflictos con SICOP", "SICOP publica", "Monto ofertado (CRC)",
                  "concursos revisados", "cartel o la modalidad", "sin bloqueo en el cartel"):
        assert texto in html, texto
    for ajeno in ("COMPRASAL", "pliego", "Monto ofertado (USD)", "procesos revisados"):
        assert ajeno not in html, ajeno


def test_portal_pais_solo_vista_sicop_no_muestra_moneda_comprasal():
    html = _pagina_pais_solo_vista(SICOP)
    assert "COMPRASAL" not in html
    assert "USD" not in html


def test_feedback_sicop_nombre_del_portal():
    html = _feedback({"portal_nombre": "SICOP"})
    assert "Oportunidades SICOP" in html
    assert "COMPRASAL" not in html


def test_correo_sicop_textos(monkeypatch):
    html = _report(monkeypatch, SICOP)
    assert "REPORTE DIARIO · SICOP" in html
    assert "Abrir en SICOP →" in html
    assert "COMPRASAL" not in html
    assert "cartel" in html
    assert "pliego" not in html
