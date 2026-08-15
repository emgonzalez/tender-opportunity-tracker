#!/usr/bin/env python3
"""Validación E2E Track B — renderizado de precedentes en el email HTML.

Ejecutar localmente (Mac): no requiere conexión a ADB.

    python scripts/validate_trackb_email.py

Valida que el template Jinja2 renderiza correctamente la sección de precedentes
(pliegos similares anteriores) del reporte diario, con los tres niveles de
similitud (Muy similar, Similar, Relacionado) y el caso vacío.

Salida: guarda el HTML renderizado en /tmp/validate_trackb_email.html para
inspección visual, e imprime un resumen JSON con los resultados.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
sys.path.insert(0, str(REPO_ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.classifier.client_profile import (
    Catalog,
    CatalogCategory,
    ClientProfile,
    Intelligence,
    Notification,
    Rules,
)
from src.scraper.models import (
    MarcaRecomendada,
    MatrizRiesgos,
    Precedente,
    ResultadoAnalisis,
    RiesgoItem,
)

_TZ_SV = timezone(timedelta(hours=-6))


def _make_client() -> ClientProfile:
    return ClientProfile(
        client_id="e2e_test",
        client_name="E2E Test Corp",
        client_description="Test client",
        active=True,
        catalog=Catalog(
            categories=[CatalogCategory(id="infra", label="Infraestructura", items=["Servidores"])],
            inclusion_signals=[],
            exclusion_signals=[],
        ),
        rules=Rules(context_note="Test", categories="infraestructura"),
        notification=Notification(
            email_to=["test@example.com"],
            system_admins_email=[],
            report_title="Reporte E2E",
            subject_prefix="[E2E]",
            greeting="Estimado Tester",
            intro_message="",
        ),
        intelligence=Intelligence(generated_at=None, processes_analyzed=0, profile=None),
    )


def _make_precedentes() -> list[Precedente]:
    """3 precedentes con distancias distintas para probar los 3 chips de similitud."""
    return [
        Precedente(
            proceso_id="PREC-001",
            institucion="Ministerio de Hacienda",
            titulo="Adquisición de servidores rack para data center",
            fecha_pub="2026-03-15T00:00:00",
            url_comprasal="https://comprasal.gob.sv/ComprasMenores/compras/10001",
            extracto=(
                "El presente proceso tiene como objetivo la adquisición de servidores tipo rack "
                "con capacidad de virtualización para el centro de datos del Ministerio de Hacienda, "
                "incluyendo licenciamiento de hipervisor, garantía de hardware por 3 años y servicios "
                "de instalación y configuración en sitio."
            ),
            distancia=0.15,  # < 0.25 → "Muy similar"
        ),
        Precedente(
            proceso_id="PREC-002",
            institucion="Ministerio de Salud",
            titulo="Renovación de equipos de red y seguridad perimetral",
            fecha_pub="2026-01-20T00:00:00",
            url_comprasal="https://comprasal.gob.sv/ComprasMenores/compras/10002",
            extracto=(
                "Contratación de equipos de networking (switches core y acceso) y firewall de "
                "nueva generación para la red institucional del Ministerio de Salud, con soporte "
                "técnico especializado y transferencia de conocimiento al personal de TI."
            ),
            distancia=0.35,  # 0.25–0.45 → "Similar"
        ),
        Precedente(
            proceso_id="PREC-003",
            institucion="Corte Suprema de Justicia",
            titulo="Mantenimiento de UPS y aires acondicionados",
            fecha_pub="2025-11-05T00:00:00",
            url_comprasal="",
            extracto=(
                "Prestación de servicios de mantenimiento preventivo y correctivo para "
                "los equipos de respaldo eléctrico (UPS) y sistemas de climatización de "
                "precisión instalados en el centro de datos institucional."
            ),
            distancia=0.55,  # >= 0.45 → "Relacionado"
        ),
    ]


def _make_opportunity(code: str) -> dict:
    return {
        "institution": "Ministerio de Educación",
        "code": code,
        "title": "Adquisición de infraestructura de servidores y almacenamiento",
        "description": "Proceso para adquirir servidores rack y almacenamiento SAN.",
        "category": "Infraestructura",
        "method": "Licitación Pública",
        "estimate": "USD 250,000",
        "closeDate": "25 Ago 2026",
        "daysLeft": 17,
        "match": 92,
        "brands": [
            MarcaRecomendada(marca="Dell", razon="Línea PowerEdge R760", role="Recomendada"),
        ],
        "url": "https://comprasal.gob.sv/ComprasMenores/compras/99999",
    }


def _render(opportunities: list[dict], resultados_analisis: dict | None) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html")
    return template.render(
        client=_make_client(),
        app_name="amalia",
        app_full_name="AMALIA · Tender Opportunity Tracker",
        run_timestamp=datetime.now(_TZ_SV),
        total_revisados=42,
        total_relevantes=len(opportunities),
        closing_this_week=1,
        opportunities=opportunities,
        preheader="Reporte de prueba E2E Track B.",
        relevantes=[],
        dudosos=[],
        resultados_analisis=resultados_analisis,
    )


def validate() -> dict:
    results = {}
    all_pass = True
    code = "E2E-TEST-001"

    # ── Con 3 precedentes ─────────────────────────────────────────────────
    opp = _make_opportunity(code)
    resultado = ResultadoAnalisis(
        proceso_uuid=code,
        estado="ANALIZADO",
        formato="NATIVO",
        triaje="GO",
        triaje_confianza=0.95,
        matriz=MatrizRiesgos(
            items=[RiesgoItem(
                categoria="financiero",
                descripcion="Garantía de cumplimiento alta",
                severidad="media",
            )],
            resumen="Riesgos moderados en plazos de entrega.",
        ),
        modelo_analisis="claude-sonnet-5",
        precedentes=_make_precedentes(),
    )
    html = _render([opp], {code: resultado})

    output_path = "/tmp/validate_trackb_email.html"
    Path(output_path).write_text(html, encoding="utf-8")

    # Check: section header
    ok = "Pliegos similares anteriores" in html and "de la memoria institucional" in html
    results["precedentes_section_rendered"] = ok
    all_pass = all_pass and ok

    # Check: "Muy similar" chip (green #E8F4EC, distancia=0.15)
    ok = "#E8F4EC" in html and "Muy similar" in html
    results["chip_muy_similar"] = ok
    all_pass = all_pass and ok

    # Check: "Similar" chip (yellow #FAF1D6, distancia=0.35)
    ok = "#FAF1D6" in html and "Similar" in html
    results["chip_similar"] = ok
    all_pass = all_pass and ok

    # Check: "Relacionado" chip (neutral #EAE5D9, distancia=0.55)
    ok = "#EAE5D9" in html and "Relacionado" in html
    results["chip_relacionado"] = ok
    all_pass = all_pass and ok

    # Check: each precedente's key fields rendered
    fields_ok = True
    for p in _make_precedentes():
        if p.institucion not in html:
            fields_ok = False
        if p.titulo not in html:
            fields_ok = False
        if p.extracto[:180] not in html:
            fields_ok = False
    results["precedente_fields_rendered"] = fields_ok
    all_pass = all_pass and fields_ok

    # ── Caso vacío: resultados_analisis=None ──────────────────────────────
    html_none = _render([opp], None)
    ok = "Pliegos similares" not in html_none
    results["empty_case_none"] = ok
    all_pass = all_pass and ok

    # ── Caso vacío: precedentes=[] ────────────────────────────────────────
    resultado_vacio = ResultadoAnalisis(
        proceso_uuid=code, estado="ANALIZADO", formato="NATIVO",
        triaje="GO", triaje_confianza=0.95,
    )
    html_empty = _render([opp], {code: resultado_vacio})
    ok = "Pliegos similares" not in html_empty
    results["empty_case_no_precedentes"] = ok
    all_pass = all_pass and ok

    results["all_pass"] = all_pass
    results["output_html"] = output_path
    return results


if __name__ == "__main__":
    results = validate()
    print(json.dumps(results, indent=2))
    sys.exit(0 if results["all_pass"] else 1)
