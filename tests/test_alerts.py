"""Tests del canal de alertas multi-canal (src/notifier/alerts.py).

El agujero que cierra este módulo: antes TODO salía por SMTP, incluido el aviso
de que SMTP había fallado. Los dos primeros tests son exactamente los criterios
de aceptación de la Fase 6 — el resto cubre las formas de fallar en silencio que
hacen inútil a una alerta (secretos en el log, un canal tumbando a los demás,
Telegram rechazando el mensaje por formato).

Toda la red está mockeada: un test que llame de verdad a api.telegram.org es un
test que falla en CI por razones ajenas al código.
"""
import json
import logging
import types
import urllib.error
from unittest.mock import MagicMock, patch

from src.notifier import alerts
from src.notifier.email_sender import send_circuit_breaker_alert, send_session_expired_alert

TOKEN = "123456:AAH-super-secreto-que-no-debe-aparecer"
CHAT = "-1001234567890"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxxxSECRETOxxxx"
ADMINS = ["ops@multicomputos.com"]


def _cfg(*, token="", chat="", webhook="", override=""):
    return types.SimpleNamespace(
        app_name="AMALIA", app_full_name="Tender Opportunity Tracker",
        notification_from="AMALIA <bot@gmail.com>",
        override_recipients=[a.strip() for a in override.split(",") if a.strip()],
        smtp_host="smtp.test", smtp_port=587,
        smtp_user="bot@gmail.com", smtp_password="x",
        alert_telegram_bot_token=token, alert_telegram_chat_id=chat,
        alert_webhook_url=webhook,
    )


def _perfil(admins=ADMINS):
    return types.SimpleNamespace(
        client_id="multicomputos", client_name="MULTICOMPUTOS",
        notification=types.SimpleNamespace(system_admins_email=admins, email_to=["cliente@x.com"]),
    )


def _respuesta(status=200):
    """Doble de la respuesta de urlopen, que se usa como context manager."""
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _urlopen(**kw):
    return patch("src.notifier.alerts.urllib.request.urlopen", **kw)


def _smtp_roto():
    """SMTP que revienta al conectar: credenciales inválidas, red caída, da igual."""
    return patch("src.notifier.email_sender.smtplib.SMTP",
                 side_effect=OSError("[Errno 61] Connection refused"))


def _smtp_ok():
    server = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=server)
    ctx.__exit__ = MagicMock(return_value=False)
    return patch("src.notifier.email_sender.smtplib.SMTP", return_value=ctx), server


def _cuerpo_enviado(urlopen_mock):
    """Payload JSON del POST que se hizo."""
    peticion = urlopen_mock.call_args[0][0]
    return json.loads(peticion.data.decode("utf-8"))


class TestCriteriosDeAceptacion:
    """Los dos criterios textuales de la Fase 6."""

    def test_con_smtp_roto_la_alerta_llega_igual_por_telegram(self):
        """El criterio central: el correo caído ya no silencia el incidente.

        Se ejercita la ruta real de producción (circuit breaker), no `notify`
        directamente: lo que importa es que la alerta que main.py dispara sea la
        que sobrevive al SMTP caído.
        """
        with _urlopen(return_value=_respuesta()) as urlopen, _smtp_roto():
            entregados = send_circuit_breaker_alert(
                _cfg(token=TOKEN, chat=CHAT), _perfil(), 3, "SMTPAuthenticationError",
            )

        assert entregados == ["telegram"], "el email debe fallar y telegram entregar"
        assert urlopen.call_count == 1
        cuerpo = _cuerpo_enviado(urlopen)
        assert cuerpo["chat_id"] == CHAT
        assert "3 veces consecutivas" in cuerpo["text"]

    def test_sin_ningun_canal_el_log_lo_dice_explicitamente(self, caplog):
        """Sin canales, la única defensa es que el log lo grite. Y que sea greppable."""
        with caplog.at_level(logging.ERROR, logger="src.notifier.alerts"):
            entregados = alerts.notify(_cfg(), "Respaldo FALLÓ", "detalle del fallo")

        assert entregados == []
        assert "ALERTA NO ENTREGADA" in caplog.text
        # El contenido de la alerta también, o el log no sirve para reconstruirla.
        assert "Respaldo FALLÓ" in caplog.text


class TestBestEffort:
    def test_un_canal_caido_no_impide_los_demas(self):
        """Telegram rechaza, pero email y webhook igual se intentan y entregan."""
        smtp, _server = _smtp_ok()
        respuestas = [urllib.error.URLError("telegram inalcanzable"), _respuesta()]
        with _urlopen(side_effect=respuestas), smtp:
            entregados = alerts.notify(
                _cfg(token=TOKEN, chat=CHAT, webhook=WEBHOOK),
                "Asunto", "mensaje", email_recipients=ADMINS,
            )

        assert entregados == ["email", "webhook"]

    def test_un_canal_que_revienta_no_tumba_la_corrida(self):
        """Una excepción inesperada (no de red) tampoco puede propagarse."""
        with patch.object(alerts, "_enviar_telegram", side_effect=RuntimeError("boom")), \
             _urlopen(return_value=_respuesta()):
            entregados = alerts.notify(_cfg(webhook=WEBHOOK), "Asunto", "mensaje")

        assert entregados == ["webhook"]

    def test_http_de_error_cuenta_como_no_entregado(self):
        """Un 500 del webhook no es una entrega, aunque no lance excepción."""
        with _urlopen(return_value=_respuesta(500)):
            assert alerts.notify(_cfg(webhook=WEBHOOK), "Asunto", "mensaje") == []

    def test_los_canales_sin_configurar_simplemente_no_se_intentan(self):
        with _urlopen() as urlopen:
            alerts.notify(_cfg(), "Asunto", "mensaje")
        urlopen.assert_not_called()


class TestNoFiltraSecretos:
    def test_el_token_de_telegram_no_aparece_en_el_log(self, caplog):
        """El token viaja en la URL; `journalctl` no tiene ninguna protección."""
        with caplog.at_level(logging.DEBUG), \
             _urlopen(side_effect=urllib.error.URLError(f"fallo con {TOKEN}")):
            alerts.notify(_cfg(token=TOKEN, chat=CHAT), "Asunto", "mensaje")

        assert TOKEN not in caplog.text
        assert "***" in caplog.text

    def test_la_url_del_webhook_no_aparece_en_el_log(self, caplog):
        """La URL de un webhook de Slack ES la credencial."""
        with caplog.at_level(logging.DEBUG), \
             _urlopen(side_effect=urllib.error.URLError(f"no resuelve {WEBHOOK}")):
            alerts.notify(_cfg(webhook=WEBHOOK), "Asunto", "mensaje")

        assert WEBHOOK not in caplog.text


class TestTelegram:
    def test_va_sin_parse_mode(self):
        """Con Markdown, un guión bajo sin cerrar hace fallar la alerta.

        Y los mensajes de error están llenos: nombres de variables, de host, de
        unidades de systemd. Fallaría justo cuando hay algo que reportar.
        """
        with _urlopen(return_value=_respuesta()) as urlopen:
            alerts.notify(_cfg(token=TOKEN, chat=CHAT), "Fallo en amalia_backup",
                          "unit amalia-backup.service: exit_code=1 *rota*")

        cuerpo = _cuerpo_enviado(urlopen)
        assert "parse_mode" not in cuerpo
        # El texto viaja crudo, sin escapar ni recortar caracteres de formato.
        assert "exit_code=1 *rota*" in cuerpo["text"]

    def test_manda_timeout_para_no_colgar_la_corrida(self):
        """Sin timeout, un canal colgado se come el TimeoutStartSec=30min."""
        with _urlopen(return_value=_respuesta()) as urlopen:
            alerts.notify(_cfg(token=TOKEN, chat=CHAT), "Asunto", "mensaje")

        assert urlopen.call_args.kwargs["timeout"] == alerts.HTTP_TIMEOUT_SECONDS

    def test_trunca_los_mensajes_largos(self):
        """Telegram devuelve 400 pasados los 4096 caracteres: mejor truncada que ninguna."""
        with _urlopen(return_value=_respuesta()) as urlopen:
            alerts.notify(_cfg(token=TOKEN, chat=CHAT), "Asunto", "x" * 9000)

        texto = _cuerpo_enviado(urlopen)["text"]
        assert len(texto) <= alerts.TELEGRAM_MAX_CHARS + 20
        assert texto.endswith("(truncado)")


class TestAlertasDeEmailSender:
    def test_sesion_expirada_sale_por_telegram_sin_admins_configurados(self):
        """Antes abortaba sin `system_admins_email`. Ahora el correo es un canal más."""
        with _urlopen(return_value=_respuesta()) as urlopen:
            entregados = send_session_expired_alert(_cfg(token=TOKEN, chat=CHAT), _perfil([]))

        assert entregados == ["telegram"]
        assert "login --manual" in _cuerpo_enviado(urlopen)["text"]

    def test_el_correo_se_conserva_como_canal(self):
        """No se reemplazó SMTP: se le sumó otro camino."""
        smtp, server = _smtp_ok()
        with smtp, _urlopen(return_value=_respuesta()):
            entregados = send_session_expired_alert(_cfg(token=TOKEN, chat=CHAT), _perfil())

        assert entregados == ["telegram", "email"]
        assert server.sendmail.call_args[0][1] == ADMINS

    def test_el_override_de_validacion_sigue_gobernando_el_correo(self):
        """La ruta nueva no puede ser un atajo para escribirle a un cliente real."""
        smtp, server = _smtp_ok()
        with smtp:
            send_circuit_breaker_alert(_cfg(override="prueba@gmail.com"), _perfil(), 3, "boom")

        assert server.sendmail.call_args[0][1] == ["prueba@gmail.com"]
