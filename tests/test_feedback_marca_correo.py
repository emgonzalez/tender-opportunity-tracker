"""Confirmaciones de marca del correo: render real sin SMTP ni ADB."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.notifier import email_sender
from src.scraper.models import MarcaRecomendada, ProcesoClasificado


@pytest.fixture(autouse=True)
def plantillas(monkeypatch):
    monkeypatch.setattr(email_sender, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "templates")


def _render(*, activo=True, secreto="prueba", restringido=False, marca="Oracle"):
    hoy = datetime(2026, 11, 2, tzinfo=timezone.utc)
    cfg = SimpleNamespace(
        marca_veredicto="activo", app_name="AMALIA", app_full_name="AMALIA",
        enable_feedback=activo, feedback_base_url="https://feedback.example.com",
        feedback_hmac_secret=secreto,
    )
    perfil = SimpleNamespace(client_id="cliente_prueba", client_name="Cliente de prueba", catalog=None)
    proceso = ProcesoClasificado(
        institucion="Institución de prueba", descripcion="Licencias de software", numero_referencia="P-1",
        fecha_publicacion=hoy, fecha_limite=hoy + timedelta(days=3),
        url_directa="https://compras.example.com/P-1", es_relevante=True,
        match_tecnico=.85, match_categoria=.85, factibilidad=.85,
        marcas_recomendadas=[MarcaRecomendada(marca=marca, veredicto="por_confirmar")],
    )
    _, render = email_sender.componer_correo(
        cfg, perfil, [proceso], 1, hoy, {}, hoy,
        solo_marcas=frozenset({"oracle"}) if restringido else frozenset(),
    )
    return render


@pytest.mark.parametrize("restringido", [False, True])
def test_pendiente_lleva_confirmaciones_en_html_y_texto(restringido):
    llamadas = []

    def enlaces(o, marca):
        llamadas.append((o["code"], marca))
        return {"si": "https://feedback.example.com/f/firmado/brand-yes",
                "no": "https://feedback.example.com/f/firmado/brand-no"}

    html, texto = _render(restringido=restringido)(lambda o: None, enlaces)
    assert llamadas == [("P-1", "Oracle")]
    for frase in ("Sí, es de Oracle", "No es de Oracle"):
        assert frase in html and frase in texto
    for final in ("brand-yes", "brand-no"):
        assert f"https://feedback.example.com/f/firmado/{final}" in html
        assert f"https://feedback.example.com/f/firmado/{final}" in texto


@pytest.mark.parametrize("activo,secreto", [(False, "prueba"), (True, "")])
def test_sin_flag_o_secreto_html_texto_identicos(activo, secreto):
    render = _render(activo=activo, secreto=secreto)

    def no_firmar(*args):
        pytest.fail("no debe generar tokens")

    assert render(lambda o: None, no_firmar) == render(lambda o: None)


def test_jinja_escapa_nombre_de_marca_y_enlaces():
    html, _ = _render(marca='<script>alert("marca")</script>')(
        lambda o: None, lambda o, m: {"si": 'https://feedback.example.com/"<script>', "no": "https://feedback.example.com/no"},
    )
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert '&#34;' in html
