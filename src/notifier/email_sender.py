"""
Envío de reportes por email usando SMTP (Gmail con App Password).

Cada email usa la configuración de notificación del ClientProfile:
  - Destinatario (notification.email_to)
  - Título del reporte (notification.report_title)
  - Saludo y mensaje introductorio personalizados

Las credenciales SMTP (usuario, contraseña, host) permanecen en .env
ya que son compartidas por todos los clientes.

Los REPORTES salen solo por aquí. Las ALERTAS de operación (sesión expirada,
circuit breaker) delegan en `notifier.alerts`, que las manda por todos los
canales configurados: si el correo está caído, el aviso igual sale por otro lado.
"""
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.classifier.client_profile import ClientProfile
from src.config import Config
from src.feedback import service as feedback
from src.notifier import alerts
from src.scraper.models import ProcesoClasificado

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path("/app/templates")


def _get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def resolve_recipients(config: Config, recipients: list[str]) -> tuple[list[str], list[str]]:
    """Punto ÚNICO de resolución de destinatarios.

    Devuelve `(destinatarios_efectivos, destinatarios_originales)`.

    Si `NOTIFICATION_OVERRIDE_TO` está configurado (modo validación), **todo** el
    correo se redirige ahí y los destinatarios reales de `clients/*.yaml` se
    ignoran por completo. Toda función de envío pasa por aquí: no hay forma de
    escribirle a un cliente real con el override activo.
    """
    override = config.override_recipients
    if not override:
        return list(recipients), list(recipients)
    logger.warning(
        "MODO VALIDACIÓN: correo redirigido a %s (destinatarios reales omitidos: %s)",
        ", ".join(override), ", ".join(recipients) or "—",
    )
    return override, list(recipients)


def _componer_mensaje(
    config: Config,
    efectivos: list[str],
    originales: list[str],
    subject: str,
    html_body: str,
    para: list[str] | None = None,
) -> MIMEMultipart:
    """Arma el mensaje con una lista efectiva YA resuelta (no vuelve a resolver).

    `para` fija el `To` de un envío individual; el asunto y `X-Original-To` se
    deciden con la lista COMPLETA: que una sola persona reciba el correo no lo
    vuelve "no validación".

    Existe aparte de `_build_message` para el envío por destinatario: ahí la lista
    se resuelve UNA vez y se itera sobre ella. Si cada iteración re-resolviera con
    `[addr]`, el override mandaría los N correos a la misma dirección y perderíamos
    a quién correspondía cada uno.
    """
    msg = MIMEMultipart("alternative")
    # Con override activo, el asunto lo deja explícito para no confundir un
    # correo de prueba con uno real en la bandeja de entrada.
    msg["Subject"] = f"[VALIDACIÓN] {subject}" if efectivos != originales else subject
    msg["From"] = config.notification_from
    msg["To"] = ", ".join(efectivos if para is None else para)
    if efectivos != originales:
        # Trazabilidad: a quién habría llegado en producción.
        msg["X-Original-To"] = ", ".join(originales) or "(sin destinatarios)"
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _build_message(
    config: Config,
    recipients: list[str],
    subject: str,
    html_body: str,
) -> MIMEMultipart:
    efectivos, originales = resolve_recipients(config, recipients)
    return _componer_mensaje(config, efectivos, originales, subject, html_body)


def _send(config: Config, client_id: str, recipients: list[str], msg: MIMEMultipart) -> None:
    efectivos, _ = resolve_recipients(config, recipients)
    if not efectivos:
        logger.warning("[%s] Sin destinatarios; no se envía nada.", client_id)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.smtp_user, efectivos, msg.as_string())
    logger.info(
        "[%s] Email enviado a %s (asunto: '%s').",
        client_id,
        ", ".join(efectivos),
        msg["Subject"],
    )


def _send_individual(
    config: Config,
    client_id: str,
    efectivos: list[str],
    originales: list[str],
    subject: str,
    render,
) -> None:
    """Un mensaje por dirección efectiva, sobre UNA sola conexión SMTP.

    `render(addr)` devuelve el HTML de esa persona (sus enlaces de feedback son
    suyos). Un fallo con una dirección no debe dejar sin reporte a las demás:
    se registra y se sigue, y el primer error se relanza AL FINAL para que quien
    llama (`main.py`) lo trate igual que antes: log, alerta, circuit breaker.
    """
    errores: list[tuple[str, Exception]] = []
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        for addr in efectivos:
            try:
                msg = _componer_mensaje(
                    config, efectivos, originales, subject, render(addr), para=[addr]
                )
                server.sendmail(config.smtp_user, [addr], msg.as_string())
                logger.info("[%s] Email enviado a %s (asunto: '%s').",
                            client_id, addr, msg["Subject"])
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] Falló el envío a %s: %s", client_id, addr, exc)
                errores.append((addr, exc))
    if errores:
        if len(errores) > 1:
            logger.error("[%s] Fallaron %d de %d envíos: %s", client_id, len(errores),
                         len(efectivos), ", ".join(a for a, _ in errores))
        raise errores[0][1]


def send_report(
    config: Config,
    client_profile: ClientProfile,
    procesos_clasificados: list[ProcesoClasificado],
    total_revisados: int,
    run_timestamp: datetime,
    resultados_analisis: dict | None = None,
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

        # Formatear monto: solo se llena si el drill-down lo extrajo del pliego
        # (COMPRASAL no publica un monto estimado en ningún endpoint; ver
        # `drilldown/analysis.py::_extraer_monto`). Vacío para todo proceso sin
        # análisis profundo, o cuyo pliego no lo declara.
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

    def render(opps: list[dict]) -> str:
        return template.render(
            client=client_profile,
            app_name=config.app_name,
            app_full_name=config.app_full_name,
            run_timestamp=run_timestamp,
            total_revisados=total_revisados,
            total_relevantes=len(relevantes),
            closing_this_week=closing_this_week,
            opportunities=opps,
            preheader=preheader,
            relevantes=relevantes,
            dudosos=dudosos,
            resultados_analisis=resultados_analisis,
        )

    if not feedback.activo(config):
        # Sin feedback nada cambia: un solo mensaje con todos en `To`, sin botones
        # y sin tocar la ADB.
        msg = _build_message(
            config, client_profile.notification.email_to, subject, render(opportunities)
        )
        _send(config, client_profile.client_id, client_profile.notification.email_to, msg)
        return

    # Con feedback cada persona necesita enlaces propios → un correo por destinatario
    # EFECTIVO. Se resuelve una vez: en MODO VALIDACIÓN los efectivos son el override
    # y ningún destinatario real recibe nada (`X-Original-To` conserva los reales).
    efectivos, originales = resolve_recipients(config, client_profile.notification.email_to)
    if not efectivos:
        logger.warning("[%s] Sin destinatarios; no se envía nada.", client_profile.client_id)
        return

    # La foto del proceso tiene que existir ANTES de que alguien haga clic.
    feedback.registrar_reportadas(config, client_profile.client_id, relevantes)

    def render_para(addr: str) -> str:
        return render([
            {**o, "feedback": feedback.enlaces_para(
                config, client_profile.client_id, o["code"], addr)}
            for o in opportunities
        ])

    _send_individual(
        config, client_profile.client_id, efectivos, originales, subject, render_para
    )


def send_session_expired_alert(config: Config, client_profile: ClientProfile) -> list[str]:
    """Notifica a system admins que la sesión de COMPRASAL expiró.

    Devuelve los canales por los que salió. Ya no aborta cuando no hay
    `system_admins_email`: el correo es *un* canal, no el único, y Telegram puede
    estar configurado aunque no haya ninguna dirección. Si no hay ninguno,
    `notify` lo deja dicho en el log.
    """
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">⚠️ {app}: Sesión de COMPRASAL expirada</h2>
    <p>Hola,</p>
    <p>La sesión de acceso a COMPRASAL ha expirado para el cliente <strong>{client_profile.client_name}</strong>
    y necesita ser renovada para que {app} pueda continuar monitoreando licitaciones.</p>
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
    subject = f"[{app}] ⚠️ Acción requerida: Renovar sesión de COMPRASAL — {client_profile.client_name}"
    # Texto plano para Telegram/webhook. Sin Markdown: los guiones bajos del
    # comando harían fallar el envío justo cuando hay algo que reportar.
    texto = (
        f"La sesión de COMPRASAL expiró para el cliente {client_profile.client_name} "
        f"y {app} no puede seguir monitoreando licitaciones.\n\n"
        "Para renovarla, en el servidor:\n"
        '  echo "CODIGO_OTP" | docker compose run -T --rm amalia python -m src.auth.login --manual'
    )
    return alerts.notify(
        config, subject, texto,
        email_recipients=client_profile.notification.system_admins_email,
        email_html=html_body,
        client_id=client_profile.client_id,
    )


def send_circuit_breaker_alert(
    config: Config,
    client_profile: ClientProfile,
    error_count: int,
    last_error: str,
) -> list[str]:
    """Notifica a system admins cuando el agente falla repetidamente.

    Este es EL caso que justifica el multi-canal: entre las causas de que la
    corrida falle N veces seguidas está que el propio SMTP esté caído, y
    entonces el aviso por correo tampoco sale.
    """
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">🚨 {app}: Error crítico</h2>
    <p>{app} ha fallado <strong>{error_count} veces consecutivas</strong> para el cliente
    <strong>{client_profile.client_name}</strong> y ha pausado su operación.</p>
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
    subject = f"[{app}] 🚨 Circuit breaker activado ({error_count} errores consecutivos) — {client_profile.client_name}"
    texto = (
        f"{app} falló {error_count} veces consecutivas para el cliente "
        f"{client_profile.client_name} y pausó su operación.\n\n"
        f"Último error:\n{last_error[:1000]}\n\n"
        "Revisar: docker compose logs -f amalia"
    )
    return alerts.notify(
        config, subject, texto,
        email_recipients=client_profile.notification.system_admins_email,
        email_html=html_body,
        client_id=client_profile.client_id,
    )
