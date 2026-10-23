#!/usr/bin/env python3
"""Validación E2E Track B — renderizado de precedentes en el email HTML.

Ejecutar localmente (Mac): no requiere conexión a ADB.

    python scripts/validate_trackb_email.py

Desde P0/P5 los precedentes (pliegos similares anteriores) y la matriz de riesgos
ya NO van en el correo: se siguen calculando y guardando, pero el correo diario es
un resumen. Este script pinta el correo con el mismo armado que `send_report`
(`email_sender.componer_correo`) y verifica que no reaparezcan, con precedentes,
sin ellos y sin análisis, y que la oportunidad sí esté en «Para decidir».

Salida: guarda el HTML renderizado en /tmp/validate_trackb_email.html para
inspección visual, e imprime un resumen JSON con los resultados.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
sys.path.insert(0, str(REPO_ROOT))

from src.classifier.client_profile import (
    Catalog,
    CatalogCategory,
    ClientProfile,
    Intelligence,
    Notification,
    Rules,
)
from src.notifier import email_sender
from src.scraper.models import (
    MarcaRecomendada,
    MatrizRiesgos,
    Precedente,
    ProcesoClasificado,
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


def _make_opportunity(code: str) -> ProcesoClasificado:
    ahora = datetime.now(_TZ_SV)
    return ProcesoClasificado(
        institucion="Ministerio de Educación (MINED)",
        descripcion="Adquisición de infraestructura de servidores y almacenamiento",
        fecha_publicacion=ahora,
        url_directa="https://comprasal.gob.sv/ComprasMenores/compras/99999",
        numero_referencia=code,
        forma_contratacion="Licitación Pública",
        monto=250_000.0,
        fecha_limite=ahora + timedelta(days=17),
        es_relevante=True,
        categoria_detectada="infra",
        razon="Proceso para adquirir servidores rack y almacenamiento SAN.",
        match_tecnico=0.92, match_categoria=0.92, factibilidad=0.92,
        marcas_recomendadas=[
            MarcaRecomendada(marca="Dell", razon="Línea PowerEdge R760", role="Recomendada"),
        ],
    )


def _render(opp: ProcesoClasificado, resultados_analisis: dict | None) -> str:
    """Pinta el correo con el MISMO armado que `send_report` (sin enviar nada)."""
    cfg = SimpleNamespace(app_name="AMALIA", app_full_name="AMALIA · Tender Opportunity Tracker")
    ahora = datetime.now(_TZ_SV)
    with patch.object(email_sender, "TEMPLATES_DIR", TEMPLATES_DIR):
        _, render = email_sender.componer_correo(
            cfg, _make_client(), [opp], 42, ahora, resultados_analisis, ahora)
        html, _ = render(lambda o: None)
    return html


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
    html = _render(opp, {code: resultado})

    output_path = "/tmp/validate_trackb_email.html"
    Path(output_path).write_text(html, encoding="utf-8")

    # P0 (correo liviano): los precedentes ya NO van en el correo — pesaban el 32 %
    # del texto y todos decían «Muy similar». Se verifica que no reaparezcan.
    ok = "Pliegos similares" not in html and "de la memoria institucional" not in html
    results["precedentes_fuera_del_correo"] = ok
    all_pass = all_pass and ok

    # P5: la matriz tampoco va (vive fuera del correo); la oportunidad sí, en su grupo.
    ok = "Garantía de cumplimiento alta" not in html
    results["matriz_fuera_del_correo"] = ok
    all_pass = all_pass and ok
    ok = "Para decidir · 1" in html and opp.descripcion in html
    results["oportunidad_en_para_decidir"] = ok
    all_pass = all_pass and ok

    # Los colores (#E8F4EC…) son tokens que la plantilla reutiliza en otros chips:
    # lo que delata un precedente es el texto de la etiqueta.
    ok = not any(etiqueta in html for etiqueta in ("Muy similar", "Relacionado"))
    results["sin_chips_de_similitud"] = ok
    all_pass = all_pass and ok

    fields_ok = True
    for p in _make_precedentes():
        if p.extracto[:180] in html:
            fields_ok = False
    results["sin_extractos_de_precedentes"] = fields_ok
    all_pass = all_pass and fields_ok

    # ── Caso vacío: resultados_analisis=None ──────────────────────────────
    html_none = _render(opp, None)
    ok = "Pliegos similares" not in html_none
    results["empty_case_none"] = ok
    all_pass = all_pass and ok

    # ── Caso vacío: precedentes=[] ────────────────────────────────────────
    resultado_vacio = ResultadoAnalisis(
        proceso_uuid=code, estado="ANALIZADO", formato="NATIVO",
        triaje="GO", triaje_confianza=0.95,
    )
    html_empty = _render(opp, {code: resultado_vacio})
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
