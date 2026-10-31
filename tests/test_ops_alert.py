"""Tests de la alerta de operación (src/notifier/ops_alert.py).

Es el único canal por el que un fallo del respaldo llega a un humano: el
mecanismo de respaldo es shell puro y corre fuera del contenedor. Si esto se
rompe, el respaldo puede estar caído sin que nadie se entere — que es el modo de
falla que toda la Fase 5 existe para evitar.
"""
import email
import types
from unittest.mock import MagicMock, patch

from src.notifier.ops_alert import main, resolve_ops_recipients, send_ops_alert

ADMINS_A = ["ops@multicomputos.com", "emilio@multicomputos.com"]
ADMINS_B = ["ops@multicomputos.com", "otro@cliente.com"]


def _perfil(client_id, admins, email_to):
    return types.SimpleNamespace(
        client_id=client_id,
        notification=types.SimpleNamespace(
            system_admins_email=admins, email_to=email_to,
        ),
    )


def _cfg(override: str = ""):
    return types.SimpleNamespace(
        clients_dir="/app/clients",
        app_name="AMALIA", app_full_name="Tender Opportunity Tracker",
        notification_from="AMALIA <bot@gmail.com>",
        override_recipients=[a.strip() for a in override.split(",") if a.strip()],
        smtp_host="smtp.test", smtp_port=587,
        smtp_user="bot@gmail.com", smtp_password="x",
        # Canales fuera de banda apagados: estos tests miran el camino del correo.
        alert_telegram_bot_token="", alert_telegram_chat_id="", alert_webhook_url="",
    )


def _perfiles(*perfiles):
    """Los admins ya no salen de los perfiles sino de los usuarios `admin_plataforma` de la ADB: se simula
    esa lectura con los admins que declaran estos perfiles de prueba (sin repetidos ni vacíos)."""
    admins = list(dict.fromkeys(a for p in perfiles for a in p.notification.system_admins_email if a))
    return patch("src.notifier.alerts.admins_plataforma", return_value=admins)


def _cuerpo_html(raw: str) -> str:
    """El cuerpo viaja en base64 dentro del MIME; los tests miran el HTML real."""
    parte = email.message_from_string(raw).get_payload(0)
    return parte.get_payload(decode=True).decode("utf-8")


def _smtp():
    server = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=server)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, server


class TestResolveOpsRecipients:
    def test_une_los_admins_de_todos_los_clientes_sin_repetir(self):
        """Una alerta de infraestructura no le pertenece a ningún cliente."""
        with _perfiles(_perfil("a", ADMINS_A, ["cli-a@x.com"]),
                       _perfil("b", ADMINS_B, ["cli-b@x.com"])):
            assert resolve_ops_recipients(_cfg()) == [
                "ops@multicomputos.com", "emilio@multicomputos.com", "otro@cliente.com",
            ]

    def test_ignora_entradas_vacias(self):
        with _perfiles(_perfil("a", ["", "ops@x.com"], [])):
            assert resolve_ops_recipients(_cfg()) == ["ops@x.com"]


class TestSendOpsAlert:
    def test_manda_a_los_admins_y_no_a_los_clientes(self):
        ctx, server = _smtp()
        with _perfiles(_perfil("a", ADMINS_A, ["cliente-real@multicomputos.com"])), \
             patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            assert send_ops_alert(_cfg(), "Respaldo FALLÓ", "detalle") is True

        _remitente, destinatarios, raw = server.sendmail.call_args[0]
        assert destinatarios == ADMINS_A
        cuerpo = _cuerpo_html(raw)
        assert "cliente-real@multicomputos.com" not in cuerpo
        assert "Respaldo FALL" in cuerpo

    def test_el_override_de_validacion_tambien_gobierna_las_alertas(self):
        """No hay ruta alterna hacia sendmail: pasa por el mismo resolutor."""
        ctx, server = _smtp()
        with _perfiles(_perfil("a", ADMINS_A, [])), \
             patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            send_ops_alert(_cfg("prueba@gmail.com"), "Respaldo FALLÓ", "detalle")

        _remitente, destinatarios, _cuerpo = server.sendmail.call_args[0]
        assert destinatarios == ["prueba@gmail.com"]

    def test_sin_admins_ni_override_avisa_que_no_pudo(self):
        """Devolver False, no fingir éxito: quien llama es systemd y mira el exit."""
        with _perfiles(_perfil("a", [], ["cliente@x.com"])):
            assert send_ops_alert(_cfg(), "Respaldo FALLÓ", "d") is False

    def test_el_cuerpo_se_escapa_como_html(self):
        """El texto viene de un script de shell; no puede inyectar marcado."""
        ctx, server = _smtp()
        with _perfiles(_perfil("a", ADMINS_A, [])), \
             patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            send_ops_alert(_cfg(), "Fallo", "<script>alert(1)</script>")
        cuerpo = _cuerpo_html(server.sendmail.call_args[0][2])
        assert "<script>" not in cuerpo
        assert "&lt;script&gt;" in cuerpo


class TestMain:
    def test_una_falla_de_smtp_es_exit_code_y_no_traceback(self):
        with _perfiles(_perfil("a", ADMINS_A, [])), \
             patch("src.notifier.ops_alert.Config", return_value=_cfg()), \
             patch("src.notifier.email_sender.smtplib.SMTP", side_effect=OSError("sin red")):
            assert main(["--subject", "x", "--body", "y"]) == 1

    def test_exito_devuelve_cero(self):
        ctx, _server = _smtp()
        with _perfiles(_perfil("a", ADMINS_A, [])), \
             patch("src.notifier.ops_alert.Config", return_value=_cfg()), \
             patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx):
            assert main(["--subject", "x", "--body", "y"]) == 0
