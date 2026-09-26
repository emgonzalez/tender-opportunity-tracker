"""Tests de `send_report` con y sin feedback (botones Relevante / Irrelevante).

Hasta acá NADA renderizaba `templates/report.html`: estos tests lo hacen de verdad
(parcheando `TEMPLATES_DIR`) y leen los mensajes que salen por el SMTP simulado.

Garantías que se prueban:
  - Feedback apagado = comportamiento anterior (un mensaje, todos en `To`, sin botones).
  - Feedback encendido = un mensaje por destinatario efectivo, con enlaces propios.
  - MODO VALIDACIÓN sigue sin escribirle a ningún destinatario real.
"""
import email
import email.header
import re
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.feedback import tokens
from src.notifier import email_sender
from src.notifier.email_sender import send_report
from src.scraper.models import ProcesoClasificado

REPO_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
SECRETO = "secreto-de-prueba"
BASE_URL = "https://feedback.example.com"

ANA = "ana@cliente.com"
LUIS = "luis@cliente.com"
REALES = [ANA, LUIS]
MIO = "yo@validacion.com"


def _cfg(override: str = "", feedback: bool = False):
    return SimpleNamespace(
        notification_override_to=override,
        override_recipients=[a.strip() for a in override.split(",") if a.strip()],
        notification_from="AMALIA <bot@gmail.com>",
        smtp_host="smtp.test", smtp_port=587, smtp_user="bot@gmail.com", smtp_password="x",
        app_name="AMALIA", app_full_name="AMALIA Test",
        enable_feedback=feedback, feedback_base_url=BASE_URL, feedback_hmac_secret=SECRETO,
    )


def _perfil(email_to=None):
    return SimpleNamespace(
        client_id="multicomputos", client_name="MULTICOMPUTOS", confidence_threshold=0.5,
        notification=SimpleNamespace(
            email_to=list(REALES if email_to is None else email_to),
            subject_prefix="[TOT]", greeting="Hola", report_title="Reporte",
            intro_message="", system_admins_email=[],
        ),
    )


def _proceso(ref: str, match: float = 0.8) -> ProcesoClasificado:
    return ProcesoClasificado(
        institucion="Ministerio X", descripcion=f"Servidores para {ref}",
        fecha_publicacion=datetime.now(timezone.utc),
        url_directa=f"https://comprasal.example/{ref}", numero_referencia=ref,
        forma_contratacion="Licitación", fecha_limite=datetime.now(timezone.utc) + timedelta(days=5),
        es_relevante=True, categoria_detectada="infraestructura", razon="encaja",
        match_tecnico=match, match_categoria=match, factibilidad=match,
    )


class _Smtp:
    """Sustituye a `smtplib.SMTP`: registra conexiones y mensajes, y puede fallar a pedido."""

    def __init__(self, falla_para: str = ""):
        self.conexiones = 0
        self.enviados: list[tuple[list[str], str]] = []  # (destinatarios del sobre, mensaje)
        self.eventos: list[str] = []
        self.falla_para = falla_para

    def __call__(self, *_a, **_k):
        self.conexiones += 1
        server = MagicMock()
        server.__enter__.return_value = server
        server.__exit__.return_value = False

        def sendmail(_desde, destinatarios, cuerpo):
            self.eventos.append("sendmail")
            if self.falla_para and self.falla_para in destinatarios:
                raise smtplib.SMTPRecipientsRefused({self.falla_para: (550, b"no")})
            self.enviados.append((list(destinatarios), cuerpo))

        server.sendmail.side_effect = sendmail
        return server

    def mensajes(self):
        return [email.message_from_string(c) for _, c in self.enviados]

    @staticmethod
    def html(msg) -> str:
        return msg.get_payload(0).get_payload(decode=True).decode("utf-8")


@pytest.fixture(autouse=True)
def plantillas_reales():
    with patch.object(email_sender, "TEMPLATES_DIR", REPO_TEMPLATES):
        yield


@pytest.fixture(autouse=True)
def sin_adb():
    """Ningún test toca la ADB: el registro de reportadas se prueba con su propio mock."""
    with patch("src.notifier.email_sender.feedback.registrar_reportadas"):
        yield


def _asunto(msg) -> str:
    return str(email.header.make_header(email.header.decode_header(msg["Subject"])))


@pytest.fixture
def smtp():
    fake = _Smtp()
    with patch("src.notifier.email_sender.smtplib.SMTP", fake):
        yield fake


def _enviar(cfg, perfil=None, procesos=None):
    send_report(
        cfg, perfil or _perfil(), procesos if procesos is not None else [_proceso("P-1"), _proceso("P-2", 0.6)],
        total_revisados=10, run_timestamp=datetime.now(timezone.utc),
    )


def _enlaces(html: str) -> list[str]:
    return re.findall(r'href="([^"]*/f/[^"]+)"', html)


def _token_de(url: str) -> str:
    return url.split("/f/")[1].split("/")[0]


class TestFeedbackApagado:
    """Red de seguridad de regresión: con el flag apagado nada cambia."""

    def test_un_mensaje_con_todos_en_to(self, smtp):
        _enviar(_cfg())
        assert smtp.conexiones == 1
        assert len(smtp.enviados) == 1
        sobre, _ = smtp.enviados[0]
        assert sobre == REALES
        assert smtp.mensajes()[0]["To"] == ", ".join(REALES)

    def test_sin_botones_ni_enlaces(self, smtp):
        _enviar(_cfg())
        html = smtp.html(smtp.mensajes()[0])
        assert "Irrelevante" not in html
        assert "/f/" not in html
        assert "Abrir en COMPRASAL" in html  # la plantilla sí se renderizó

    def test_no_toca_la_adb(self, smtp):
        _enviar(_cfg())
        email_sender.feedback.registrar_reportadas.assert_not_called()

    @pytest.mark.parametrize("falta", ["enable_feedback", "feedback_base_url", "feedback_hmac_secret"])
    def test_flag_encendido_pero_sin_url_o_secreto_tambien_es_apagado(self, smtp, falta):
        cfg = _cfg(feedback=True)
        setattr(cfg, falta, False if falta == "enable_feedback" else "")
        _enviar(cfg)
        assert len(smtp.enviados) == 1
        assert "Irrelevante" not in smtp.html(smtp.mensajes()[0])


class TestFeedbackEncendido:
    def test_un_mensaje_por_destinatario_con_un_solo_to(self, smtp):
        _enviar(_cfg(feedback=True))
        assert smtp.conexiones == 1  # una sola conexión para todo el bucle
        assert [s for s, _ in smtp.enviados] == [[ANA], [LUIS]]
        assert [m["To"] for m in smtp.mensajes()] == [ANA, LUIS]

    def test_asunto_y_from_iguales_que_sin_feedback(self, smtp):
        _enviar(_cfg())
        base = smtp.mensajes()[0]
        smtp.enviados.clear()
        _enviar(_cfg(feedback=True))
        for msg in smtp.mensajes():
            assert _asunto(msg) == _asunto(base)
            assert msg["From"] == base["From"]
            assert msg["X-Original-To"] is None

    def test_cada_oportunidad_lleva_sus_dos_botones(self, smtp):
        _enviar(_cfg(feedback=True))
        for msg in smtp.mensajes():
            html = smtp.html(msg)
            assert html.count(">Relevante</a>") == 2
            assert html.count(">Irrelevante</a>") == 2
            assert "¿Te sirve esta oportunidad?" in html
            assert "#0F7A4A" in html and "#A1281F" in html

    def test_enlaces_propios_y_verificables_por_destinatario(self, smtp):
        _enviar(_cfg(feedback=True))
        html_ana, html_luis = (smtp.html(m) for m in smtp.mensajes())
        enlaces_ana, enlaces_luis = _enlaces(html_ana), _enlaces(html_luis)
        assert len(enlaces_ana) == len(enlaces_luis) == 4  # 2 oportunidades x 2 votos
        assert set(enlaces_ana).isdisjoint(enlaces_luis)

        for destino, enlaces in ((ANA, enlaces_ana), (LUIS, enlaces_luis)):
            procesos_vistos = set()
            for url in enlaces:
                payload = tokens.verificar(SECRETO, _token_de(url))
                assert payload is not None
                assert payload.destinatario_id == tokens.destinatario_id(destino)
                assert payload.cliente_id == "multicomputos"
                procesos_vistos.add(payload.proceso_id)
                assert url.rsplit("/", 1)[1] in tokens.VOTOS
            assert procesos_vistos == {"P-1", "P-2"}

    def test_el_boton_apunta_al_voto_que_dice(self, smtp):
        _enviar(_cfg(feedback=True), procesos=[_proceso("P-1")])
        html = smtp.html(smtp.mensajes()[0])
        assert re.search(r'href="[^"]*/f/[^"/]+/relevante"[^>]*>Relevante</a>', html)
        assert re.search(r'href="[^"]*/f/[^"/]+/irrelevante"[^>]*>Irrelevante</a>', html)

    def test_registra_las_reportadas_una_vez_y_antes_del_primer_envio(self, smtp):
        cfg = _cfg(feedback=True)
        procesos = [_proceso("P-1"), _proceso("P-2", 0.6)]

        def registrar(*_a, **_k):
            smtp.eventos.append("registrar")

        with patch("src.notifier.email_sender.feedback.registrar_reportadas",
                   side_effect=registrar) as reg:
            _enviar(cfg, procesos=procesos)

        reg.assert_called_once()
        args = reg.call_args[0]
        assert args[0] is cfg and args[1] == "multicomputos"
        assert {p.numero_referencia for p in args[2]} == {"P-1", "P-2"}
        assert smtp.eventos == ["registrar", "sendmail", "sendmail"]

    def test_sin_oportunidades_no_hay_botones_pero_el_reporte_sale(self, smtp):
        _enviar(_cfg(feedback=True), procesos=[])
        assert len(smtp.enviados) == 2
        assert "Irrelevante" not in smtp.html(smtp.mensajes()[0])

    def test_oportunidad_sin_numero_referencia_no_lleva_botones_ni_rompe(self, smtp):
        _enviar(_cfg(feedback=True), procesos=[_proceso(""), _proceso("P-2", 0.6)])
        html = smtp.html(smtp.mensajes()[0])
        assert html.count(">Irrelevante</a>") == 1
        assert {tokens.verificar(SECRETO, _token_de(u)).proceso_id for u in _enlaces(html)} == {"P-2"}

    def test_sin_destinatarios_no_envia_ni_registra(self, smtp):
        _enviar(_cfg(feedback=True), perfil=_perfil(email_to=[]))
        assert smtp.conexiones == 0
        email_sender.feedback.registrar_reportadas.assert_not_called()


class TestModoValidacionConFeedback:
    def test_nadie_real_recibe_nada_y_x_original_to_lista_los_originales(self, smtp):
        _enviar(_cfg(override=MIO, feedback=True))
        assert [s for s, _ in smtp.enviados] == [[MIO]]
        msg = smtp.mensajes()[0]
        assert msg["To"] == MIO
        assert msg["X-Original-To"] == ", ".join(REALES)
        assert _asunto(msg).startswith("[VALIDACIÓN] ")
        # Ni en el sobre ni en las cabeceras de entrega hay un destinatario real.
        for sobre, _ in smtp.enviados:
            assert not set(sobre) & set(REALES)
        assert not any(real in (msg["To"] or "") + (msg["Cc"] or "") for real in REALES)

    def test_el_token_identifica_a_la_direccion_efectiva(self, smtp):
        _enviar(_cfg(override=MIO, feedback=True))
        for url in _enlaces(smtp.html(smtp.mensajes()[0])):
            payload = tokens.verificar(SECRETO, _token_de(url))
            assert payload.destinatario_id == tokens.destinatario_id(MIO)
            assert payload.destinatario_id not in {tokens.destinatario_id(r) for r in REALES}

    def test_varias_direcciones_de_override_un_correo_cada_una(self, smtp):
        otro = "otro@validacion.com"
        _enviar(_cfg(override=f"{MIO}, {otro}", feedback=True))
        assert [s for s, _ in smtp.enviados] == [[MIO], [otro]]
        assert all(m["X-Original-To"] == ", ".join(REALES) for m in smtp.mensajes())


class TestFalloDeEnvio:
    def test_si_falla_uno_los_demas_igual_se_envian_y_el_error_se_propaga(self):
        fake = _Smtp(falla_para=ANA)
        with patch("src.notifier.email_sender.smtplib.SMTP", fake):
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                _enviar(_cfg(feedback=True))
        assert [s for s, _ in fake.enviados] == [[LUIS]]

    def test_si_fallan_todos_se_propaga_el_primero(self):
        fake = _Smtp(falla_para=ANA)
        fake.enviados = []
        with patch("src.notifier.email_sender.smtplib.SMTP", fake):
            with pytest.raises(smtplib.SMTPException):
                _enviar(_cfg(feedback=True), perfil=_perfil(email_to=[ANA]))
        assert fake.enviados == []
