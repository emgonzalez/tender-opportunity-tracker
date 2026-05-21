"""
Envío de reportes por email usando SMTP (Gmail con App Password).

Cada email usa la configuración de notificación del ClientProfile:
  - Destinatario (notification.email_to)
  - Título del reporte (notification.report_title)
  - Saludo y mensaje introductorio personalizados

Las credenciales SMTP (usuario, contraseña, host) permanecen en .env
ya que son compartidas por todos los clientes.
"""
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import Config
from src.classifier.client_profile import ClientProfile
from src.scraper.models import ProcesoClasificado

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path("/app/templates")


def _get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _build_message(
    config: Config,
    client_profile: ClientProfile,
    subject: str,
    html_body: str,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.notification_from
    msg["To"] = ", ".join(client_profile.notification.email_to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _send(config: Config, client_profile: ClientProfile, msg: MIMEMultipart) -> None:
    recipients = client_profile.notification.email_to
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.smtp_user, recipients, msg.as_string())
    logger.info(
        "[%s] Email enviado a %s (asunto: '%s').",
        client_profile.client_id,
        ", ".join(recipients),
        msg["Subject"],
    )


def send_report(
    config: Config,
    client_profile: ClientProfile,
    procesos_clasificados: list[ProcesoClasificado],
    total_revisados: int,
    run_timestamp: datetime,
) -> None:
    """Envía el reporte diario personalizado para un cliente específico."""
    relevantes = [p for p in procesos_clasificados if p.es_relevante]
    dudosos = [
        p for p in procesos_clasificados
        if not p.es_relevante and p.confianza >= client_profile.confidence_threshold
    ]

    # Calcular datos adicionales para el nuevo template
    today = datetime.now(run_timestamp.tzinfo)
    opportunities = []
    closing_this_week = 0

    for proc in relevantes:
        # Calcular match overall: promedio ponderado de los 3 scores
        match_overall = round(
            (proc.match_tecnico * 0.4 + proc.match_categoria * 0.3 + proc.factibilidad * 0.3) * 100
        )

        # Calcular días restantes hasta fecha_limite
        days_left = None
        if proc.fecha_limite:
            delta = (proc.fecha_limite - today).days
            days_left = max(0, delta)
            if days_left <= 7:
                closing_this_week += 1

        # Formatear monto (vacío por ahora)
        estimate = ""
        if proc.monto:
            estimate = f"USD {proc.monto:,.0f}"

        # Formatear fecha de cierre
        close_date_str = ""
        if proc.fecha_limite:
            close_date_str = proc.fecha_limite.strftime("%d %b %Y")

        opportunities.append({
            "institution": proc.institucion,
            "code": proc.numero_referencia,
            "title": proc.descripcion,
            "description": proc.razon,
            "category": proc.categoria_detectada,
            "method": proc.forma_contratacion,
            "estimate": estimate,
            "closeDate": close_date_str,
            "daysLeft": days_left,
            "match": match_overall,
            "brands": proc.marcas_recomendadas,
            "url": proc.url_directa,
        })

    # Ordenar por match descendente
    opportunities.sort(key=lambda x: x["match"], reverse=True)

    notif = client_profile.notification
    prefix = notif.subject_prefix

    # Subject y preheader
    if relevantes:
        min_days = min((o["daysLeft"] for o in opportunities if o["daysLeft"] is not None), default=None)
        subject = f"Licitaciones TI · {len(relevantes)} oportunidades · {closing_this_week} cierran esta semana"
        preheader = (
            f"Hoy revisamos {total_revisados} procesos en COMPRASAL. "
            f"{len(relevantes)} encajan con tu portafolio — "
            f"la más prioritaria cierra en {min_days} días."
        ) if min_days is not None else (
            f"Hoy revisamos {total_revisados} procesos. {len(relevantes)} encajan con tu portafolio."
        )
    else:
        subject = f"{prefix} Sin oportunidades nuevas — {run_timestamp.strftime('%d/%m/%Y %H:%M')}"
        preheader = f"Hoy revisamos {total_revisados} procesos en COMPRASAL. Sin coincidencias."

    env = _get_jinja_env()
    template = env.get_template("report.html")
    html_body = template.render(
        client=client_profile,
        app_name=config.app_name,
        app_full_name=config.app_full_name,
        run_timestamp=run_timestamp,
        total_revisados=total_revisados,
        total_relevantes=len(relevantes),
        closing_this_week=closing_this_week,
        opportunities=opportunities,
        preheader=preheader,
        relevantes=relevantes,
        dudosos=dudosos,
    )

    msg = _build_message(config, client_profile, subject, html_body)
    _send(config, client_profile, msg)


def send_session_expired_alert(config: Config, client_profile: ClientProfile) -> None:
    """Notifica a un cliente que la sesión de COMPRASAL expiró."""
    prefix = client_profile.notification.subject_prefix
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">⚠️ {app}: Sesión de COMPRASAL expirada</h2>
    <p>Hola {client_profile.notification.greeting}</p>
    <p>La sesión de acceso a COMPRASAL ha expirado y necesita ser renovada para que
    {app} pueda continuar monitoreando licitaciones para <strong>{client_profile.client_name}</strong>.</p>
    <h3>Para renovar la sesión:</h3>
    <ol>
      <li>Conéctate al servidor donde corre {app}</li>
      <li>Ejecuta: <code>echo "CODIGO_OTP" | docker compose run -T --rm amalia python -m src.auth.login --manual</code></li>
      <li>{app} retomará su operación normal en la próxima ejecución programada</li>
    </ol>
    <p style="color: #7f8c8d; font-size: 12px;">
      Generado automáticamente por {app} ({config.app_full_name})
    </p>
    </body></html>
    """
    subject = f"{prefix} ⚠️ Acción requerida: Renovar sesión de COMPRASAL"
    msg = _build_message(config, client_profile, subject, html_body)
    _send(config, client_profile, msg)


def send_circuit_breaker_alert(
    config: Config,
    client_profile: ClientProfile,
    error_count: int,
    last_error: str,
) -> None:
    """Notifica a un cliente cuando el agente falla repetidamente."""
    prefix = client_profile.notification.subject_prefix
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">🚨 {app}: Error crítico</h2>
    <p>{app} ha fallado <strong>{error_count} veces consecutivas</strong> y ha pausado su operación
    para el cliente <strong>{client_profile.client_name}</strong>.</p>
    <h3>Último error:</h3>
    <pre style="background: #f8f8f8; padding: 10px; border-left: 4px solid #e74c3c; font-size: 12px; overflow-x: auto;">
{last_error[:1000]}
    </pre>
    <p>Revisa los logs del contenedor: <code>docker compose logs -f amalia</code></p>
    <p style="color: #7f8c8d; font-size: 12px;">
      Generado automáticamente por {app} ({config.app_full_name})
    </p>
    </body></html>
    """
    subject = f"{prefix} 🚨 Circuit breaker activado ({error_count} errores consecutivos)"
    msg = _build_message(config, client_profile, subject, html_body)
    _send(config, client_profile, msg)
