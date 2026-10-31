"""
Alerta de operación: aviso al equipo que mantiene la VM, no a los clientes.

Existe por el respaldo (Fase 5). Un respaldo que falla en silencio es
indistinguible de uno que no existe, y el mecanismo de respaldo es shell puro
—corre fuera del contenedor, sin acceso a SMTP—. Este módulo es el puente: la
unit `amalia-backup-failure.service` lo invoca dentro del contenedor de la app.

Desde la Fase 6 delega el envío en `notifier.alerts`, que es multi-canal. El
motivo es el mismo que lo hizo nacer llevado un paso más allá: si la única vía
fuera el correo, un SMTP caído volvería a dejar el fallo del respaldo en
silencio. `alerts` sigue mandando el correo por `email_sender`, así que la
configuración de SMTP —y sobre todo el override de validación— conserva su
único hogar: una alerta de operación tampoco puede escribirle a un cliente real
cuando `NOTIFICATION_OVERRIDE_TO` está activo.

Uso (desde la VM):
    docker compose -f <compose> run --rm --no-deps amalia \\
        python -m src.notifier.ops_alert --subject "..." --body "..."
"""
import argparse
import html
import logging
import sys

from src.config import Config
from src.notifier import alerts

logger = logging.getLogger(__name__)


def resolve_ops_recipients(config: Config) -> list[str]:
    """Administradores de sistema: los usuarios con rol `admin_plataforma` de la ADB.

    Una alerta de infraestructura no le pertenece a ningún cliente: le corresponde a
    quien opera esta instalación, de ahí que se lean de todas las organizaciones. Con
    la ADB caída la lista queda vacía y la alerta sigue por Telegram y webhook.
    """
    return alerts.admins_plataforma(config)


def send_ops_alert(config: Config, subject: str, body: str) -> bool:
    """Envía la alerta por todos los canales configurados.

    Devuelve False solo si no llegó a NINGUNO. Ya no exige que haya
    `system_admins_email`: desde la Fase 6 el correo es un canal más, y un
    Telegram configurado alcanza para que la alerta llegue aunque no haya
    ninguna dirección declarada —o aunque el SMTP esté caído, que para un aviso
    de respaldo fallido es un escenario perfectamente plausible—.
    """
    destinatarios = resolve_ops_recipients(config)
    app = config.app_name
    cuerpo_html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">🛑 {html.escape(app)}: alerta de operación</h2>
    <p>{html.escape(subject)}</p>
    <pre style="background: #f8f8f8; padding: 10px; border-left: 4px solid #e74c3c; font-size: 12px; white-space: pre-wrap;">
{html.escape(body)}
    </pre>
    <p style="color: #7f8c8d; font-size: 12px;">
      Generado automáticamente por {html.escape(app)} ({html.escape(config.app_full_name)}).
      Esta alerta es de infraestructura: no la origina ningún cliente.
    </p>
    </body></html>
    """
    entregados = alerts.notify(
        config,
        f"[{app}] {subject}",
        body or subject,
        email_recipients=destinatarios,
        email_html=cuerpo_html,
        client_id="ops",
    )
    return bool(entregados)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Envía una alerta de operación por correo.")
    parser.add_argument("--subject", required=True, help="Asunto (una línea).")
    parser.add_argument("--body", default="", help="Cuerpo en texto plano.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Cualquier excepción tiene que traducirse a un exit code, no a un traceback:
    # quien invoca es una unit de systemd que solo mira el estado de salida.
    try:
        return 0 if send_ops_alert(Config(), args.subject, args.body) else 1
    except Exception as exc:  # noqa: BLE001 — el llamador es systemd, no un humano
        logger.error("No se pudo enviar la alerta de operación: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
