"""Presupuesto de peso del correo diario (`templates/report.html`).

Gmail recorta todo HTML que pase de ~102 KB y muestra «[Mensaje recortado]». La corrida
del 2026-10-22 pesó ~372 KB: el asunto anunciaba 16 oportunidades y solo se veían 4, sin
los botones de feedback del resto. P0 lo bajó a ~53 KB y P5 a ~17 KB; el estilo visual
(tarjetas, chips y botones con CSS en línea) lo subió a ~37 KB en una corrida normal
(~4,8 KB por tarjeta, ~1,6 KB por fila). Estos tests renderizan corridas realistas —todas
con la matriz de riesgos completa, citas del pliego y 3 pliegos similares, con enlaces de
feedback de largo real, más pesadas que las reales— y exigen < 80 KB con 16. Con 30
«para decidir» el correo se DEGRADA solo (tarjetas → filas, menos baja prioridad) para no
pasar de `email_sender.PRESUPUESTO_HTML` (90 KB). Si fallan, no subas el umbral: aligera la
plantilla.
"""
import email
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.feedback import tokens
from src.notifier import email_sender
from src.notifier.email_sender import send_report
from src.scraper.models import (
    MarcaRecomendada,
    MatrizRiesgos,
    Precedente,
    ProcesoClasificado,
    ResultadoAnalisis,
    RiesgoItem,
)

REPO_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
PRESUPUESTO_16 = 80_000
PRESUPUESTO_30 = email_sender.PRESUPUESTO_HTML
N_OPORTUNIDADES = 16
DESTINO = "gerente@cliente.com"

# Textos con el largo de los reales (medias de la corrida del 2026-10-22: institución ~37,
# título ~93, razón ~78, resumen de la matriz ~325, descripción de riesgo ~154 caracteres).
INSTITUCION = "Registro Nacional de las Personas Naturales (RNPN) {i}"
TITULO = ("Renovación de suscripciones de sistema operativo y soporte técnico "
          "especializado para servidores — lote {i}")
RAZON = "Renovación de licencias de software de infraestructura que el portafolio cubre."
RESUMEN = ("El proceso de contratación es por subasta inversa electrónica para la renovación "
           "de licencias de sistema operativo para la institución {i}. El pliego establece "
           "condiciones específicas para la presentación de propuestas y los criterios de "
           "evaluación. No se declara un monto estimado para la contratación en el documento.")
RIESGO = ("El pliego exige que el oferente presente certificación vigente del fabricante y "
          "experiencia comprobable en contratos similares durante los últimos tres años ({sev}).")
CITA = "CITA-LITERAL-DEL-PLIEGO {i}-{j}: " + "«Los documentos contractuales se regirán por la LCP» " * 4
EXTRACTO = "EXTRACTO-PRECEDENTE {i}-{k}: " + "declaración jurada del ofertante sobre su solvencia " * 8
AHORA = datetime(2026, 10, 22, 7, tzinfo=timezone(timedelta(hours=-6)))


def _proceso(i: int, ahora: datetime) -> ProcesoClasificado:
    ref = f"{1000 + i}-2026-P{i:04d}"
    return ProcesoClasificado(
        institucion=INSTITUCION.format(i=i),
        descripcion=TITULO.format(i=i),
        fecha_publicacion=ahora,
        url_directa=f"https://www.comprasal.gob.sv/pac-procesos/etapas/{121000 + i}",
        numero_referencia=ref,
        forma_contratacion="Subasta Inversa",
        fecha_limite=ahora + timedelta(days=2 + i),
        es_relevante=True,
        categoria_detectada="infraestructura",
        razon=RAZON,
        match_tecnico=0.95, match_categoria=0.9, factibilidad=0.85,
        marcas_recomendadas=[
            MarcaRecomendada(marca="Red Hat", razon="Renovación de suscripciones de sistema operativo"),
            MarcaRecomendada(marca="Dell", razon="Servidores y soporte de hardware del portafolio"),
        ],
    )


def _analisis(i: int, ref: str, ahora: datetime) -> ResultadoAnalisis:
    sevs = ["media", "alta", "baja", "alta"]
    return ResultadoAnalisis(
        proceso_uuid=ref, estado="ANALIZADO", formato="ESCANEADO", triaje="GO",
        triaje_confianza=0.9,
        matriz=MatrizRiesgos(
            resumen=RESUMEN.format(i=i),
            items=[
                RiesgoItem(categoria=cat, severidad=sev,
                           descripcion=f"[{ref}#{j}] " + RIESGO.format(sev=sev),
                           clausula_ref=CITA.format(i=i, j=j))
                for j, (cat, sev) in enumerate(zip(["plazos", "legal", "financiero", "tecnico"], sevs))
            ],
        ),
        precedentes=[
            Precedente(proceso_id=f"OLD-{i}-{k}", institucion="Ministerio de Hacienda",
                       titulo=f"Renovación de licencias {k} (precedente)",
                       fecha_pub=(ahora - timedelta(days=300)).isoformat(),
                       url_comprasal=f"https://www.comprasal.gob.sv/pac-procesos/etapas/{9000 + k}",
                       extracto=EXTRACTO.format(i=i, k=k), distancia=0.12)
            for k in range(3)
        ],
    )


def _enlaces(cfg, client_id, proceso_id, addr):
    # Igual que `feedback.enlaces_para` pero sin tocar la ADB: tokens firmados de verdad,
    # con el largo de producción (~150 caracteres por enlace).
    tok = tokens.firmar(cfg.feedback_hmac_secret, proceso_id, client_id, tokens.destinatario_id(addr))
    return {v: tokens.url(cfg.feedback_base_url, tok, v) for v in tokens.VOTOS}


def _parte(msg, tipo: str) -> str:
    for parte in msg.walk():
        if parte.get_content_type() == tipo:
            return parte.get_payload(decode=True).decode("utf-8")
    raise AssertionError(f"el mensaje no trae parte {tipo}")


def _render(n: int) -> tuple[str, list[ProcesoClasificado]]:
    """Renderiza y 'envía' (SMTP simulado) una corrida de `n`; devuelve (html, procesos)."""
    procesos = [_proceso(i, AHORA) for i in range(n)]
    resultados = {p.numero_referencia: _analisis(i, p.numero_referencia, AHORA)
                  for i, p in enumerate(procesos)}
    cfg = SimpleNamespace(
        notification_override_to="", override_recipients=[],
        notification_from="AMALIA <bot@gmail.com>",
        smtp_host="smtp.test", smtp_port=587, smtp_user="bot@gmail.com", smtp_password="x",
        app_name="AMALIA", app_full_name="AMALIA — Agente de Monitoreo de Licitaciones",
        enable_feedback=True, feedback_base_url="https://dev.amalia.multicomputos.com",
        feedback_hmac_secret="secreto-de-prueba",
    )
    perfil = SimpleNamespace(
        client_id="multicomputos", client_name="Multicómputos", confidence_threshold=0.5,
        notification=SimpleNamespace(
            email_to=[DESTINO], subject_prefix="[AMALIA]", greeting="", report_title="",
            intro_message="", system_admins_email=[],
        ),
    )
    enviados = []
    server = MagicMock()
    server.__enter__.return_value = server
    server.sendmail.side_effect = lambda _f, _t, cuerpo: enviados.append(cuerpo)
    with patch.object(email_sender, "TEMPLATES_DIR", REPO_TEMPLATES), \
         patch.object(email_sender.smtplib, "SMTP", return_value=server), \
         patch.object(email_sender.feedback, "registrar_reportadas"), \
         patch.object(email_sender.feedback, "enlaces_para", side_effect=_enlaces):
        send_report(cfg, perfil, procesos, 71, AHORA, resultados)
    assert len(enviados) == 1
    return _parte(email.message_from_string(enviados[0]), "text/html"), procesos


@pytest.fixture
def correo():
    return _render(N_OPORTUNIDADES)


def test_correo_de_16_oportunidades_cabe_en_el_presupuesto(correo):
    html, _ = correo
    peso = len(html.encode("utf-8"))
    assert peso < PRESUPUESTO_16, (
        f"El HTML del correo pesa {peso:,} bytes con {N_OPORTUNIDADES} oportunidades "
        f"analizadas; el presupuesto es {PRESUPUESTO_16:,} (Gmail recorta a ~102 KB). "
        "Aligera templates/report.html en vez de subir el umbral."
    )


def test_correo_de_30_oportunidades_se_degrada_para_no_pasar_el_presupuesto():
    """El caso pesado: 30 «para decidir». Sin degradar pesaría ~100 KB y Gmail lo recortaría."""
    html, procesos = _render(30)
    peso = len(html.encode("utf-8"))
    assert peso <= PRESUPUESTO_30, f"{peso:,} bytes con 30 oportunidades (presupuesto {PRESUPUESTO_30:,})"
    # Degradó: menos tarjetas que el tope, pero NINGUNA oportunidad se pierde ni pierde sus votos.
    assert html.count('class="cd fr-dec"') < email_sender.MAX_DETALLADAS
    assert all(f'href="{p.url_directa}"' in html for p in procesos)
    assert html.count(">Irrelevante</a>") == 30


def test_aparecen_las_16_con_sus_botones_de_feedback(correo):
    html, procesos = correo
    botones = re.findall(r'href="[^"]*/f/([^"/]+)/(relevante|irrelevante)"[^>]*>(Relevante|Irrelevante)</a>', html)
    votos_por_proceso: dict[str, set[str]] = {}
    for token, voto, texto in botones:
        assert voto == texto.lower()
        payload = tokens.verificar("secreto-de-prueba", token)
        assert payload is not None, f"token inválido en el botón {texto}"
        votos_por_proceso.setdefault(payload.proceso_id, set()).add(voto)
    for p in procesos:
        ref = p.numero_referencia
        # Pasado MAX_DETALLADAS van en una línea con el título recortado: el enlace sí es único.
        assert f'href="{p.url_directa}"' in html, f"falta la oportunidad {ref}"
        assert votos_por_proceso.get(ref) == set(tokens.VOTOS), f"faltan los botones de feedback de {ref}"
    assert html.count(">Relevante</a>") == html.count(">Irrelevante</a>") == N_OPORTUNIDADES


def test_sin_pliegos_similares_ni_matriz_ni_citas(correo):
    """La matriz completa y los precedentes viven fuera del correo (P0/P5)."""
    html, procesos = correo
    assert "Pliegos similares" not in html
    assert "memoria institucional" not in html
    assert "EXTRACTO-PRECEDENTE" not in html
    assert "CITA-LITERAL-DEL-PLIEGO" not in html
    for p in procesos:
        assert f"[{p.numero_referencia}#" not in html  # ningún ítem de la matriz


def test_preheader_es_lo_primero_del_body(correo):
    html, _ = correo
    cuerpo = html.split("<body", 1)[1].split(">", 1)[1].lstrip()
    assert cuerpo.startswith('<div style="display:none;'), cuerpo[:120]
    assert "71 procesos revisados" in cuerpo.split(">", 1)[1].split("&#8199;", 1)[0]
    assert "&#8199;&#847;" in cuerpo[:3000]
