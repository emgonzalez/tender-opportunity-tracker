"""src.drilldown.triage — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md.

Triaje GO/NO-GO con Claude Haiku sobre un extracto del pliego:
  - PDF nativo    -> texto de las primeras paginas (barato)
  - PDF escaneado -> las primeras paginas RASTERIZADAS a JPEG (<=1568 px, q80)

Nunca se manda el PDF completo: en un corpus mayoritariamente escaneado eso
convertia al triaje en el mayor consumidor de tokens del pipeline.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = (
    "Eres un evaluador técnico de licitaciones de tecnología de la información "
    "(TI) en El Salvador. Con base en el extracto del pliego y el contexto del "
    "cliente, determina si el alcance de la contratación requiere "
    "servicios/productos de infraestructura TI, ciberseguridad, software o "
    "desarrollo que el cliente pueda ofertar. Responde ÚNICAMENTE con un "
    'objeto JSON válido, sin texto adicional, con el formato: {"viabilidad": '
    '"GO" o "NO-GO", "confianza": número entre 0 y 1, "motivo": "breve '
    'justificación"}.'
)

_RETRY_SUFFIX = "\n\nResponde solo el JSON, sin markdown."


class TriageError(Exception):
    """Se lanza cuando la llamada a la API de Claude falla (red o error de API).

    El pipeline debe mapear esta excepción a estado ERROR_LLM — nunca se
    silencia como NO-GO.
    """

    pass


def _build_user_content(
    contexto_cliente: str,
    *,
    texto: str | None,
    pdf_path: str | None,
    scanned: bool,
    raster_pages: int = 5,
    extra_instruction: str = "",
) -> list[dict]:
    content: list[dict] = []

    if scanned and pdf_path:
        # Pliego escaneado: NO se manda el PDF completo. Se rasterizan solo las
        # primeras paginas (donde vive el "Objeto de la Contratacion"), que es
        # lo unico que el triaje necesita para decidir GO/NO-GO.
        #
        # Historia: mandar el PDF entero hacia Haiku hizo que el triaje fuera el
        # mayor consumidor de tokens de todo el pipeline (validacion 2026-08-03:
        # ~66% de los pliegos son escaneados y se pagaba el documento completo
        # para descartar solo el 9%). Ademas el mismo PDF viajaba dos veces:
        # una al triaje y otra al analisis.
        from src.drilldown.pdf import rasterizar_paginas

        paginas = rasterizar_paginas(pdf_path, raster_pages)
        for img_b64 in paginas:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_b64,
                    },
                }
            )
        text_block = (
            f"Contexto del cliente:\n{contexto_cliente}\n\n"
            f"Se adjuntan las primeras {len(paginas)} pagina(s) de un pliego "
            "escaneado. Determina si es GO o NO-GO para este cliente."
            + extra_instruction
        )
    else:
        text_block = (
            f"Contexto del cliente:\n{contexto_cliente}\n\n"
            f"Extracto del pliego:\n{texto or ''}" + extra_instruction
        )

    content.append({"type": "text", "text": text_block})
    return content


def _extract_json(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(raw_text)
        if match:
            return json.loads(match.group(0))
        raise


def _call_model(client: anthropic.Anthropic, model: str, user_content: list[dict]) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text_block = next((b for b in response.content if b.type == "text"), None)
    if text_block is None:
        return ""
    return text_block.text


def triage(
    cfg,
    contexto_cliente: str,
    *,
    texto: str | None = None,
    pdf_path: str | None = None,
    scanned: bool = False,
    client: "anthropic.Anthropic | None" = None,
) -> tuple[str, float, str]:
    """Ejecuta el triaje GO/NO-GO con Claude Haiku.

    Devuelve (veredicto, confianza, motivo) donde veredicto ∈ {"GO", "NO-GO"}
    y confianza ∈ [0, 1].

    `client` es opcional y sirve para inyectar un cliente falso en tests
    (smoke tests sin red); si es None se construye uno real a partir de
    `cfg.anthropic_api_key`.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=4)

    user_content = _build_user_content(
        contexto_cliente, texto=texto, pdf_path=pdf_path, scanned=scanned,
        raster_pages=getattr(cfg, "triage_raster_pages", 5),
    )

    try:
        raw_text = _call_model(client, cfg.model_triage, user_content)
    except anthropic.APIError as e:
        logger.error("Error de API de Anthropic durante el triaje: %s", e)
        raise TriageError(str(e)) from e
    except Exception as e:
        logger.error("Error de red/inesperado durante el triaje: %s", e)
        raise TriageError(str(e)) from e

    try:
        data = _extract_json(raw_text)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("No se pudo parsear el JSON del triaje; reintentando: %r", raw_text)
        retry_content = _build_user_content(
            contexto_cliente,
            texto=texto,
            pdf_path=pdf_path,
            scanned=scanned,
            raster_pages=getattr(cfg, "triage_raster_pages", 5),
            extra_instruction=_RETRY_SUFFIX,
        )
        try:
            raw_text = _call_model(client, cfg.model_triage, retry_content)
        except anthropic.APIError as e:
            logger.error("Error de API de Anthropic durante el reintento de triaje: %s", e)
            raise TriageError(str(e)) from e
        except Exception as e:
            logger.error("Error de red/inesperado durante el reintento de triaje: %s", e)
            raise TriageError(str(e)) from e

        try:
            data = _extract_json(raw_text)
        except (json.JSONDecodeError, AttributeError):
            logger.error("Parseo del triaje fallo tras reintento: %r", raw_text)
            return "NO-GO", 0.0, "parse_error"

    viabilidad = str(data.get("viabilidad", "")).upper()
    veredicto = "GO" if viabilidad == "GO" else "NO-GO"

    try:
        confianza = float(data.get("confianza", 0.0))
    except (TypeError, ValueError):
        confianza = 0.0
    confianza = max(0.0, min(1.0, confianza))

    motivo = str(data.get("motivo", ""))

    return veredicto, confianza, motivo
