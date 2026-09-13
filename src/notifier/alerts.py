"""Alertas operativas multi-canal, best-effort.

Existe por un punto ciego concreto: hasta ahora **todo salía por SMTP, incluido
el aviso de que SMTP había fallado**. Si el correo se caía —credenciales
revocadas, Gmail bloqueando la app, la VM sin salida al 587— el sistema detectaba
el problema y no se enteraba nadie. Detectar sin poder notificar no sirve de nada.

Principio de diseño: `notify` intenta **todos** los canales configurados, devuelve
a cuáles llegó, y si no llegó a ninguno **deja constancia explícita y greppable**
en el log en vez de fallar en silencio. Nunca lanza excepción: una alerta que
revienta la corrida es peor que no tener alerta, porque convierte un incidente
reportable en una caída.

Canales, todos opcionales y activados por variables de entorno:
- Telegram (`ALERT_TELEGRAM_BOT_TOKEN` + `ALERT_TELEGRAM_CHAT_ID`)
- Email    (destinatarios de `clients/*.yaml` + SMTP ya configurado)
- Webhook  (`ALERT_WEBHOOK_URL`) — formato `{"text": …}` de Slack/Discord

Se usa `urllib` de la stdlib y no `requests` —que sí está en las dependencias—
a propósito: este es el camino que tiene que funcionar cuando lo demás ya falló,
así que conviene que no dependa de nada instalable. También pesa menos en una VM
de 1 GB.
"""
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Timeout OBLIGATORIO en toda llamada de red. La corrida diaria vive dentro de un
# `TimeoutStartSec=30min` de systemd: un canal colgado se comería el presupuesto
# de la corrida entera para, irónicamente, no entregar la alerta.
HTTP_TIMEOUT_SECONDS = 10

# Telegram rechaza (400) los mensajes de más de 4096 caracteres. Un traceback
# largo cabe de sobra en ese límite, así que se recorta antes de enviar: más vale
# una alerta truncada que ninguna.
TELEGRAM_MAX_CHARS = 3900


def _sin_secretos(texto: str, *secretos: str) -> str:
    """Enmascara credenciales antes de que toquen el log.

    El token de Telegram viaja EN LA URL (`/bot<token>/sendMessage`) y la URL de
    un webhook de Slack es ella misma la credencial. Cualquiera de las dos puede
    colarse en el mensaje de una excepción (`urllib` a veces incluye la URL), y
    los logs de la VM se leen por `journalctl` sin ninguna protección especial.
    """
    for secreto in secretos:
        if secreto:
            texto = texto.replace(secreto, "***")
    return texto


def _post_json(url: str, payload: dict) -> int:
    """POST de JSON con timeout. Devuelve el código HTTP."""
    peticion = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=HTTP_TIMEOUT_SECONDS) as respuesta:
        return respuesta.status


def _enviar_telegram(config, subject: str, message: str) -> bool:
    """Telegram Bot API. No necesita webhook público: es una llamada saliente.

    Nota: el `{"text": …}` genérico del webhook NO le sirve a Telegram, que exige
    `chat_id`. Por eso este canal no puede reutilizar el otro.
    """
    token = config.alert_telegram_bot_token
    chat_id = config.alert_telegram_chat_id
    if not (token and chat_id):
        return False

    texto = f"🚨 {subject}\n\n{message}"
    if len(texto) > TELEGRAM_MAX_CHARS:
        texto = texto[:TELEGRAM_MAX_CHARS] + "\n… (truncado)"

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        estado = _post_json(url, {
            "chat_id": chat_id,
            # TEXTO PLANO a propósito: sin `parse_mode`. Con Markdown, Telegram
            # responde 400 si el cuerpo trae caracteres de formato sin cerrar, y
            # los mensajes de error los traen todo el tiempo: guiones bajos en
            # nombres de host y de variables, asteriscos, backticks. La alerta
            # fallaría exactamente cuando hay un incidente real que reportar.
            "text": texto,
            "disable_web_page_preview": True,
        })
        if 200 <= estado < 300:
            return True
        logger.error("Telegram rechazó la alerta: HTTP %s", estado)
        return False
    except urllib.error.HTTPError as exc:
        # El cuerpo de Telegram explica la causa (chat_id inválido, bot bloqueado…)
        detalle = ""
        try:
            detalle = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 — el detalle es opcional, el aviso no
            pass
        logger.error(
            "Telegram rechazó la alerta: HTTP %s %s",
            exc.code, _sin_secretos(detalle, token),
        )
        return False
    except Exception as exc:  # noqa: BLE001 — red, DNS, TLS: todo es "no llegó"
        logger.error("fallo enviando alerta por Telegram: %s", _sin_secretos(str(exc), token))
        return False


def _enviar_email(config, subject: str, html_body: str, recipients: list[str], client_id: str) -> bool:
    """Correo por el mismo camino que todo lo demás.

    Import diferido a propósito: `email_sender` importa este módulo para sus
    alertas, así que hacerlo arriba sería un ciclo. Además mantiene el pago del
    import de Jinja2/smtplib para cuando el canal está realmente configurado.

    Se apoya en `_build_message`/`_send` en vez de reimplementar SMTP para que el
    override de validación (`NOTIFICATION_OVERRIDE_TO`) siga teniendo un único
    hogar: no puede existir una alerta que le escriba a un cliente real.
    """
    from src.notifier.email_sender import _build_message, _send, resolve_recipients

    efectivos, _originales = resolve_recipients(config, recipients)
    if not efectivos:
        return False
    try:
        msg = _build_message(config, recipients, subject, html_body)
        _send(config, client_id, recipients, msg)
        return True
    except Exception as exc:  # noqa: BLE001 — SMTP caído es justo el caso a cubrir
        logger.error("fallo enviando alerta por email: %s", exc)
        return False


def _enviar_webhook(config, subject: str, message: str) -> bool:
    """Webhook genérico estilo Slack/Discord (`{"text": …}`)."""
    url = config.alert_webhook_url
    if not url:
        return False
    try:
        estado = _post_json(url, {"text": f"{subject}\n{message}"})
        if 200 <= estado < 300:
            return True
        logger.error("el webhook rechazó la alerta: HTTP %s", estado)
        return False
    except Exception as exc:  # noqa: BLE001 — ningún fallo de red tumba la corrida
        # La URL del webhook ES la credencial: nunca al log.
        logger.error("fallo enviando alerta por webhook: %s", _sin_secretos(str(exc), url))
        return False


def _html_por_defecto(config, subject: str, message: str) -> str:
    """Cuerpo mínimo para cuando quien llama solo tiene texto plano."""
    import html as _html

    app = _html.escape(config.app_name)
    return (
        '<html><body style="font-family: Arial, sans-serif; max-width: 600px;'
        ' margin: 0 auto; padding: 20px;">'
        f'<h2 style="color: #e74c3c;">🚨 {app}</h2>'
        f"<p>{_html.escape(subject)}</p>"
        '<pre style="background: #f8f8f8; padding: 10px; border-left: 4px solid #e74c3c;'
        ' font-size: 12px; white-space: pre-wrap;">'
        f"{_html.escape(message)}</pre>"
        "</body></html>"
    )


def notify(
    config,
    subject: str,
    message: str,
    *,
    email_recipients: list[str] | None = None,
    email_html: str | None = None,
    client_id: str = "ops",
) -> list[str]:
    """Envía la alerta por todos los canales configurados.

    `message` es TEXTO PLANO: es lo que ven Telegram y el webhook. `email_html`
    es opcional y solo lo usa el correo, que sigue siendo HTML por continuidad
    con los avisos que ya recibían los administradores.

    Devuelve la lista de canales que la aceptaron. Que un canal falle no impide
    que se intenten los demás, y ninguno puede propagar una excepción hacia
    arriba: el llamador está reportando un incidente, no puede caerse haciéndolo.
    """
    destinatarios = list(email_recipients or [])
    cuerpo_html = email_html or _html_por_defecto(config, subject, message)

    canales = (
        ("telegram", lambda: _enviar_telegram(config, subject, message)),
        ("email", lambda: _enviar_email(config, subject, cuerpo_html, destinatarios, client_id)),
        ("webhook", lambda: _enviar_webhook(config, subject, message)),
    )

    entregados: list[str] = []
    for nombre, envio in canales:
        try:
            if envio():
                entregados.append(nombre)
        except Exception as exc:  # noqa: BLE001 — ningún canal puede tumbar la corrida
            logger.error("canal de alerta '%s' falló inesperadamente: %s", nombre, exc)

    if entregados:
        logger.info("alerta entregada por: %s", ", ".join(entregados))
    else:
        # Que quede greppable en `journalctl`: significa que el sistema detectó un
        # problema y NADIE se va a enterar por sí solo.
        logger.error(
            "ALERTA NO ENTREGADA — ningún canal configurado o todos fallaron: %s | %s",
            subject, message,
        )
    return entregados
