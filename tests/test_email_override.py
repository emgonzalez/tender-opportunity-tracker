"""Tests del override de destinatarios (modo validación).

Garantía que se prueba: con `NOTIFICATION_OVERRIDE_TO` configurado, NINGÚN
correo puede llegar a un destinatario real de `clients/*.yaml`.
"""
import types
from unittest.mock import MagicMock, patch

from src.notifier.email_sender import _build_message, _send, resolve_recipients, send_access_code

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


class TestCodigoDeAcceso:
    """P7/T9: el código de acceso es la única excepción al modo validación."""

    def _enviar(self, override, caplog=None):
        ctx = MagicMock()
        server = MagicMock()
        ctx.__enter__ = MagicMock(return_value=server)
        ctx.__exit__ = MagicMock(return_value=False)
        cfg = _cfg(override)
        cfg.app_name = "AMALIA"
        with patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            send_access_code(cfg, "gerente@multicomputos.com", "K7M2QX9P", 10)
        return server

    def test_con_override_activo_va_al_destinatario_real(self):
        server = self._enviar(MIO)
        assert server.sendmail.call_args[0][1] == ["gerente@multicomputos.com"]
        msg = server.sendmail.call_args[0][2]
        assert "VALIDACIÓN" not in msg and "X-Original-To" not in msg

    def test_trae_el_codigo_y_la_vigencia(self):
        msg = self._enviar("").sendmail.call_args[0][2]
        assert "K7M2QX9P" in msg or "K7M2QX9P" in _decodificado(msg)
        assert "Tu código de acceso" in _decodificado(msg) or "c=C3=B3digo" in msg

    def test_no_escribe_el_codigo_en_el_log(self, caplog):
        with caplog.at_level("DEBUG"):
            self._enviar(MIO)
        assert "K7M2QX9P" not in caplog.text

    def test_el_reporte_diario_sigue_redirigido(self):
        efectivos, _ = resolve_recipients(_cfg(MIO), ["gerente@multicomputos.com"])
        assert efectivos == [MIO]


def _decodificado(raw: str) -> str:
    import email
    m = email.message_from_string(raw)
    from email.header import decode_header, make_header
    partes = [str(make_header(decode_header(m["Subject"])))]
    for p in m.walk():
        if p.get_content_type() in ("text/plain", "text/html"):
            partes.append(p.get_payload(decode=True).decode("utf-8"))
    return "\n".join(partes)
