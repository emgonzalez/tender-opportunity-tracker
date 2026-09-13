"""src.drilldown.analysis — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md.

Analisis profundo de pliegos con Claude Sonnet: genera una matriz de riesgos
financieros, tecnicos, legales y de plazos a partir del PDF del pliego, con
severidad y cita textual de la clausula relevante, mas un resumen ejecutivo.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import anthropic

from src.scraper.models import MatrizRiesgos, ResultadoAnalisis, RiesgoItem, UsoModelo

logger = logging.getLogger(__name__)

CATEGORIAS_VALIDAS = {"financiero", "tecnico", "legal", "plazos"}
SEVERIDADES_VALIDAS = {"alta", "media", "baja"}

SYSTEM_PROMPT = """Eres un analista experto en licitaciones publicas (COMPRASAL, El Salvador).

Se te adjunta el pliego de bases de un proceso de contratacion en formato PDF. \
Tu tarea es leerlo con cuidado y extraer una MATRIZ DE RIESGOS financieros, \
tecnicos, legales y de plazos para una empresa integradora de tecnologia que \
evalua si participar.

Para cada riesgo detectado:
- Clasifica la categoria como una de: "financiero", "tecnico", "legal", "plazos".
- Describe el riesgo de forma clara y accionable para quien decide si ofertar.
- Asigna una severidad: "alta", "media" o "baja".
- Incluye una CITA TEXTUAL (copiada literalmente del pliego) de la clausula \
que origina el riesgo, en "clausula_ref".

Ademas, redacta un resumen ejecutivo breve (2-4 oraciones) de los hallazgos \
mas relevantes para la decision de participar o no.

Responde UNICAMENTE con JSON valido, sin texto adicional antes ni despues, \
con este formato EXACTO:
{"resumen": "...", "items": [{"categoria": "financiero|tecnico|legal|plazos", \
"descripcion": "...", "severidad": "alta|media|baja", "clausula_ref": "cita textual"}]}"""


def _extraer_json(texto: str) -> dict:
    """Parsea el primer objeto JSON de `texto`, con regex de respaldo."""
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("No se encontro un objeto JSON valido en la respuesta del modelo.")


def _construir_matriz(data: dict) -> MatrizRiesgos:
    """Construye MatrizRiesgos tolerando items con claves faltantes o invalidas."""
    items = []
    for it in data.get("items", []) or []:
        if not isinstance(it, dict):
            continue

        categoria = it.get("categoria", "tecnico")
        if categoria not in CATEGORIAS_VALIDAS:
            categoria = "tecnico"

        severidad = it.get("severidad", "media")
        if severidad not in SEVERIDADES_VALIDAS:
            severidad = "media"

        items.append(RiesgoItem(
            categoria=categoria,
            descripcion=it.get("descripcion", ""),
            severidad=severidad,
            clausula_ref=it.get("clausula_ref", ""),
        ))

    return MatrizRiesgos(items=items, resumen=data.get("resumen", ""))


def analizar_pliego(
    pdf_path: str,
    cfg,
    contexto_cliente: str,
    proceso_uuid: str,
    formato: str = "",
    client=None,
) -> ResultadoAnalisis:
    """Analiza un pliego con Claude Sonnet y devuelve la matriz de riesgos.

    En caso de error de la API de Anthropic, timeout o parseo irrecuperable,
    NO relanza la excepcion — devuelve un ResultadoAnalisis con
    estado="ERROR_LLM" para que el pipeline continue con el siguiente pliego.
    """
    from src.drilldown.pdf import read_pdf_b64

    # Se declara fuera del try para que un fallo de parseo POSTERIOR a la
    # llamada siga reportando los tokens que ya se pagaron.
    uso = UsoModelo(modelo=cfg.model_analysis)

    try:
        if client is None:
            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=4)

        pdf_b64 = read_pdf_b64(pdf_path)

        user_text = (
            f"Contexto del cliente que evalua este proceso:\n{contexto_cliente}\n\n"
            "Analiza el pliego adjunto y devuelve la matriz de riesgos en el "
            "formato JSON indicado en las instrucciones."
        )

        with client.messages.stream(
            model=cfg.model_analysis,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        ) as stream:
            msg = stream.get_final_message()

        uso.acumular(getattr(msg, "usage", None))

        text_block = next((b.text for b in msg.content if b.type == "text"), None)
        if text_block is None:
            raise ValueError("La respuesta del modelo no contiene un bloque de texto.")

        data = _extraer_json(text_block)
        matriz = _construir_matriz(data)

        return ResultadoAnalisis(
            proceso_uuid=proceso_uuid,
            estado="ANALIZADO",
            formato=formato,
            matriz=matriz,
            modelo_analisis=cfg.model_analysis,
            uso_analisis=uso,
            generado_en=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        logger.error("Error en analizar_pliego para proceso_uuid=%s: %s", proceso_uuid, e)
        return ResultadoAnalisis(
            proceso_uuid=proceso_uuid,
            estado="ERROR_LLM",
            error=str(e),
            modelo_analisis=cfg.model_analysis,
            uso_analisis=uso,
            generado_en=datetime.now(timezone.utc).isoformat(),
        )
