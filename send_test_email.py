#!/usr/bin/env python3
"""
Script para enviar un reporte de prueba del nuevo diseño a una dirección de correo.
Genera datos de prueba y envía el email con el nuevo template.

Uso:
  python3 send_test_email.py [email_destino] [num_oportunidades]

Ejemplo:
  python3 send_test_email.py emilio.gonzalez@multicomputos.com 4
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import Config


def create_test_opportunities(count: int) -> list[dict]:
    """Create mock opportunities for testing."""
    opportunities = []
    base_date = datetime.now(timezone(timedelta(hours=-6)))

    for i in range(count):
        # Vary match scores
        match_score = 95 - (i % 3) * 10  # 95, 85, 75, 95, 85, 75...

        # Vary closing dates
        days_offset = 3 + (i % 20)  # 3 to 22 days from now
        close_date = base_date + timedelta(days=days_offset)

        opportunity = {
            "institution": f"Institución {chr(65 + (i % 26))} - El Salvador",
            "code": f"2026-P{1000 + i:04d}",
            "title": f"Contratación de Servicio de {'Infraestructura' if i % 3 == 0 else 'Ciberseguridad' if i % 3 == 1 else 'Software'}",
            "description": "Esta es una oportunidad relevante que coincide con tu portafolio. Requiere experiencia en múltiples áreas de IT.",
            "category": ["Infraestructura", "Ciberseguridad", "Software"][i % 3],
            "method": ["Licitación", "Subasta Inversa", "Comparación de precios"][i % 3],
            "estimate": f"USD {20000 * (i + 1):,.0f}" if i % 2 == 0 else "",
            "closeDate": close_date.strftime("%d %b %Y"),
            "daysLeft": days_offset,
            "match": match_score,
            "brands": [
                {
                    "marca": "Fortinet",
                    "razon": "Soluciones de seguridad perimetral y evaluación de vulnerabilidades",
                    "role": "Recomendada"
                },
                {
                    "marca": "HPE",
                    "razon": "Infraestructura de servidores y almacenamiento",
                    "role": "Alternativa"
                }
            ] if i % 2 == 0 else [],
            "url": "https://www.comprasal.gob.sv/pac-procesos/etapas/12345"
        }
        opportunities.append(opportunity)

    return opportunities


def send_test_email(recipient_email: str, num_opportunities: int = 4):
    """Generate and send test email report."""
    config = Config()

    # Mock client profile
    class MockNotification:
        email_to = [recipient_email]
        report_title = "Licitaciones TI"
        greeting = "Estimado Cliente"
        intro_message = "Reporte de prueba del nuevo diseño"

    class MockClient:
        client_id = "test-client"
        client_name = "MULTICOMPUTOS"
        notification = MockNotification()

    today = datetime.now(timezone(timedelta(hours=-6)))
    opportunities = create_test_opportunities(num_opportunities)
    closing_this_week = sum(1 for o in opportunities if o["daysLeft"] and o["daysLeft"] <= 7)

    # Render template
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html")

    html_body = template.render(
        client=MockClient(),
        app_name=config.app_name,
        app_full_name=config.app_full_name,
        run_timestamp=today,
        total_revisados=78,
        total_relevantes=num_opportunities,
        closing_this_week=closing_this_week,
        opportunities=opportunities,
        preheader=f"Reporte de prueba con {num_opportunities} oportunidades.",
        relevantes=[None] * num_opportunities if num_opportunities > 0 else [],
        dudosos=[],
    )

    # Build email
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[TEST] Licitaciones TI · {num_opportunities} oportunidades · {closing_this_week} cierran esta semana"
    msg["From"] = config.notification_from
    msg["To"] = recipient_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Send email
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(config.smtp_user, config.smtp_password)
            server.sendmail(config.smtp_user, [recipient_email], msg.as_string())

        print(f"✅ Email de prueba enviado exitosamente a {recipient_email}")
        print(f"   Oportunidades: {num_opportunities}")
        print(f"   Cierran esta semana: {closing_this_week}")
        return True
    except Exception as e:
        print(f"❌ Error enviando email: {e}")
        return False


if __name__ == "__main__":
    recipient = "emilio.gonzalez@multicomputos.com"
    num_opp = 4

    if len(sys.argv) > 1:
        recipient = sys.argv[1]

    if len(sys.argv) > 2:
        try:
            num_opp = int(sys.argv[2])
        except ValueError:
            print("❌ Error: número de oportunidades debe ser un entero")
            sys.exit(1)

    print("🧪 Generando reporte de prueba...")
    print(f"   Destino: {recipient}")
    print(f"   Oportunidades: {num_opp}\n")

    success = send_test_email(recipient, num_opp)
    sys.exit(0 if success else 1)
