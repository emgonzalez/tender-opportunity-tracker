"""Tests del override de destinatarios (modo validación).

Garantía que se prueba: con `NOTIFICATION_OVERRIDE_TO` configurado, NINGÚN
correo puede llegar a un destinatario real de `clients/*.yaml`.
"""
import types
from unittest.mock import MagicMock, patch

import pytest

from src.notifier.email_sender import resolve_recipients, _build_message, _send

REALES = ["michael.albert@multicomputos.com", "astrid.cruz@multicomputos.com"]
MIO = "emiliogonzalez777@gmail.com"


def _cfg(override: str = ""):
    return types.SimpleNamespace(
        notification_override_to=override,
        override_recipients=[a.strip() for a in override.split(",") if a.strip()],
        notification_from="AMALIA <bot@gmail.com>",
        smtp_host="smtp.test", smtp_port=587,
        smtp_user="bot@gmail.com", smtp_password="x",
    )


class TestResolveRecipients:
    def test_sin_override_no_cambia_nada(self):
        efectivos, originales = resolve_recipients(_cfg(), REALES)
        assert efectivos == REALES and originales == REALES

    def test_con_override_redirige(self):
        efectivos, originales = resolve_recipients(_cfg(MIO), REALES)
        assert efectivos == [MIO]
        assert originales == REALES

    def test_override_acepta_varias_direcciones(self):
        efectivos, _ = resolve_recipients(_cfg(f"{MIO}, otro@x.com"), REALES)
        assert efectivos == [MIO, "otro@x.com"]

    def test_override_en_blanco_se_ignora(self):
        efectivos, _ = resolve_recipients(_cfg("   "), REALES)
        assert efectivos == REALES


class TestBuildMessage:
    def test_sin_override_asunto_intacto(self):
        msg = _build_message(_cfg(), REALES, "Reporte diario", "<p>hola</p>")
        assert msg["Subject"] == "Reporte diario"
        assert msg["To"] == ", ".join(REALES)
        assert msg["X-Original-To"] is None

    def test_con_override_marca_asunto_y_conserva_trazabilidad(self):
        msg = _build_message(_cfg(MIO), REALES, "Reporte diario", "<p>hola</p>")
        assert msg["Subject"] == "[VALIDACIÓN] Reporte diario"
        assert msg["To"] == MIO
        assert msg["X-Original-To"] == ", ".join(REALES)
        # ningún destinatario real en la cabecera To
        assert not any(r in msg["To"] for r in REALES)


class TestSend:
    def _smtp_mock(self):
        server = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=server)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx, server

    def test_sendmail_recibe_solo_el_override(self):
        ctx, server = self._smtp_mock()
        with patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            msg = _build_message(_cfg(MIO), REALES, "Asunto", "<p>x</p>")
            _send(_cfg(MIO), "multicomputos", REALES, msg)
        destinatarios = server.sendmail.call_args[0][1]
        assert destinatarios == [MIO]
        for real in REALES:
            assert real not in destinatarios

    def test_sin_override_sendmail_usa_los_reales(self):
        ctx, server = self._smtp_mock()
        with patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            msg = _build_message(_cfg(), REALES, "Asunto", "<p>x</p>")
            _send(_cfg(), "multicomputos", REALES, msg)
        assert server.sendmail.call_args[0][1] == REALES

    def test_sin_destinatarios_no_envia(self):
        ctx, server = self._smtp_mock()
        with patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            msg = _build_message(_cfg(), [], "Asunto", "<p>x</p>")
            _send(_cfg(), "cli", [], msg)
        server.sendmail.assert_not_called()
