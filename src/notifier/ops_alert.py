"""
Alerta de operación: correo al equipo que mantiene la VM, no a los clientes.

Existe por el respaldo (Fase 5). Un respaldo que falla en silencio es
indistinguible de uno que no existe, y el mecanismo de respaldo es shell puro
—corre fuera del contenedor, sin acceso a SMTP—. Este módulo es el puente: la
unit `amalia-backup-failure.service` lo invoca dentro del contenedor de la app.

Se apoya en `email_sender` en vez de reimplementar SMTP porque la configuración
de correo (credenciales, `From`, y sobre todo el override de validación) tiene un
único hogar. En particular, pasar por `_build_message`/`_send` garantiza que una
alerta de operación tampoco pueda escribirle a un cliente real cuando
`NOTIFICATION_OVERRIDE_TO` está activo.

Uso (desde la VM):
    docker compose -f <compose> run --rm --no-deps amalia \\
        python -m src.notifier.ops_alert --subject "..." --body "..."
"""
import argparse
import html
import logging
import sys

from src.classifier.client_profile import load_all_active_profiles
from src.config import Config
from src.notifier.email_sender import _build_message, _send

logger = logging.getLogger(__name__)


def resolve_ops_recipients(config: Config) -> list[str]:
    """Administradores de sistema de todos los perfiles activos, sin repetir.

    Los admins se declaran por cliente (`notification.system_admins_email`) pero
    una alerta de infraestructura no le pertenece a ningún cliente: le
    corresponde a todo el que opere esta instalación. De ahí la unión.
    """
    vistos: list[str] = []
    for profile in load_all_active_profiles(config.clients_dir):
        for correo in profile.notification.system_admins_email:
            if correo and correo not in vistos:
                vistos.append(correo)
    return vistos


def send_ops_alert(config: Config, subject: str, body: str) -> bool:
    """Envía la alerta. Devuelve False si no había a quién avisarle."""
    destinatarios = resolve_ops_recipients(config)
    # `config.override_recipients` basta para tener destino: en modo validación
    # el override manda y no queremos perder alertas justo mientras se valida.
    if not destinatarios and not config.override_recipients:
        logger.error(
            "Sin system_admins_email en ningún cliente activo y sin override: "
            "la alerta '%s' no se puede enviar.", subject,
        )
        return False

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
    msg = _build_message(config, destinatarios, f"[{app}] {subject}", cuerpo_html)
    _send(config, "ops", destinatarios, msg)
    return True


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
