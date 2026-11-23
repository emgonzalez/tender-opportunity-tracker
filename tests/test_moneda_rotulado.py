"""A5: moneda original con equivalente aproximado, textos por portal y PX en «No competibles».

COMPRASAL queda idéntico al texto de siempre; SICOP rotula CRC con «≈ USD» solo si hay tasa.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.notifier import email_sender
from src.notifier.agrupamiento import GRUPO_NO_COMPETIBLE, plural_documento
from src.scraper.models import MOTIVO_MODALIDAD_NO_COMPETIBLE, MOTIVOS_BLOQUEO, ProcesoClasificado
from src.web import portal as web_portal

TZ = timezone(timedelta(hours=-6))
AHORA = datetime(2026, 10, 22, 7, 0, tzinfo=TZ)


def _p(ref="X-1", **kw):
    base = dict(
        institucion="Ministerio X", descripcion=f"Objeto {ref}", fecha_publicacion=AHORA,
        url_directa="https://x.example/", numero_referencia=ref, forma_contratacion="Subasta",
        fecha_limite=AHORA + timedelta(days=9), es_relevante=True, categoria_detectada="servicios_ti",
        match_tecnico=0.9, match_categoria=0.9, factibilidad=0.9,
    )
    base.update(kw)
    return ProcesoClasificado(**base)


def _perfil():
    return SimpleNamespace(
        client_id="c", client_name="Cliente",
        catalog=SimpleNamespace(categories=[SimpleNamespace(id="servicios_ti", label="TI", brands=[])]),
        notification=SimpleNamespace(),
    )


REPO = Path(__file__).resolve().parent.parent


def _correo(monkeypatch, portal_id, procesos, revisados=10):
    monkeypatch.setattr(email_sender, "TEMPLATES_DIR", REPO / "templates")
    monkeypatch.setattr(email_sender, "portal_de_cliente", lambda *_a, **_k: portal_id)
    cfg = SimpleNamespace(app_name="AMALIA", app_full_name="AMALIA", enable_feedback=False,
                          feedback_base_url="", feedback_hmac_secret="", clients_dir="")
    asunto, render = email_sender.componer_correo(cfg, _perfil(), procesos, revisados, AHORA, {}, AHORA)
    html, texto = render(lambda o: None)
    return asunto, html, texto


def test_comprasal_identico(monkeypatch):
    _, html, texto = _correo(monkeypatch, "comprasal_sv", [_p(monto=45000.0)])
    assert "USD 45,000" in html and "(≈" not in html
    assert "COMPRASAL: https://x.example/" in texto and "Búscalo" not in texto
    assert "revisa el pliego en COMPRASAL antes de cotizar" in texto


def test_sicop_moneda_original_con_equivalente_solo_con_tasa(monkeypatch):
    sin = _p("S-1", portal_id="sicop_cr", monto=12_500_000.0, moneda="CRC")
    con = _p("S-2", portal_id="sicop_cr", monto=12_500_000.0, moneda="CRC", monto_usd=24000.0)
    _, html, texto = _correo(monkeypatch, "sicop_cr", [sin, con])
    assert "CRC 12,500,000 (≈ USD 24,000)" in html
    assert html.count("CRC 12,500,000") == 2 and html.count("(≈ USD") == 1   # sin tasa: solo el original
    assert "USD 12,500,000" not in html


def test_sicop_textos_e_instruccion_de_busqueda(monkeypatch):
    p = _p("2026LD-000123-0001", portal_id="sicop_cr")
    asunto, html, texto = _correo(monkeypatch, "sicop_cr", [p])
    assert "SICOP" in texto and "COMPRASAL" not in texto
    assert "Búscalo por número de procedimiento en SICOP: 2026LD-000123-0001" in texto
    assert "revisa el cartel en SICOP antes de cotizar" in texto


def test_sin_oportunidades_usa_las_etiquetas_del_portal(monkeypatch):
    _, html, texto = _correo(monkeypatch, "sicop_cr", [], revisados=5)
    assert "5 concursos revisados en SICOP" in texto or "5 concursos revisados en SICOP" in html


def test_px_va_a_no_competibles_con_su_motivo(monkeypatch):
    px = _p("PX-1", portal_id="sicop_cr", modalidad_codigo="PX")
    grupos = email_sender.agrupar([px], {}, AHORA, _perfil())
    assert [o["code"] for o in grupos[GRUPO_NO_COMPETIBLE]] == ["PX-1"]
    assert grupos[GRUPO_NO_COMPETIBLE][0]["motivo"] == MOTIVOS_BLOQUEO[MOTIVO_MODALIDAD_NO_COMPETIBLE]
    assert not grupos["decidir"]


def test_contexto_de_e5(monkeypatch):
    monkeypatch.setattr(email_sender, "portal_de_cliente", lambda *_a, **_k: "sicop_cr")
    capturado = {}
    real = email_sender._get_jinja_env

    class _T:
        def __init__(self, t):
            self.t = t

        def render(self, **c):
            capturado.update(c)
            return self.t.render(**c)

    class _E:
        def __init__(self, e):
            self.e = e

        def get_template(self, n):
            return _T(self.e.get_template(n))

    monkeypatch.setattr(email_sender, "_get_jinja_env", lambda: _E(real()))
    _correo(monkeypatch, "sicop_cr", [_p(portal_id="sicop_cr")])
    assert capturado["portal_nombre"] == "SICOP" and capturado["moneda_monto"] == "CRC"
    assert capturado["etiqueta_documento"] == "cartel" and capturado["etiqueta_documento_plural"] == "carteles"
    assert capturado["etiqueta_proceso_plural"] == "concursos" and capturado["moneda_reporte"] == "USD"
    assert capturado["comprasal_url"] == capturado["portal_url"]


def test_plural_documento():
    assert plural_documento("pliego") == "pliegos" and plural_documento("cartel") == "carteles"
    assert plural_documento("pliego de condiciones") == "pliegos de condiciones"


def test_portal_web_monto_txt():
    assert web_portal._monto_txt(45000) == "USD 45,000".replace(",", ".")
    assert web_portal._monto_txt(12_500_000, "CRC", 24000) == "CRC 12.500.000 (≈ USD 24.000)"
    assert web_portal._monto_txt(12_500_000, "CRC") == "CRC 12.500.000"
    assert web_portal._monto_txt(0, "CRC") == "" and "sin confirmar" in web_portal._monto_txt(5, "")
