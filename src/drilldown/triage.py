"""src.drilldown.triage — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md.

Triaje GO/NO-GO sobre un extracto del pliego:
  - PDF nativo    -> texto de las primeras paginas (barato)
  - PDF escaneado -> las primeras paginas RASTERIZADAS a JPEG (<=1568 px, q80)

Nunca se manda el PDF completo: en un corpus mayoritariamente escaneado eso
convertia al triaje en el mayor consumidor de tokens del pipeline.

Desde la Fase 9 el modelo lo elige `src.llm`: este modulo ya no nombra a ningun
proveedor, solo declara que la etapa es `"triaje"` y deja que la tabla de
despacho resuelva a Claude Haiku o a Llama Scout segun `LLM_PROVIDER`.
"""

from __future__ import annotations

import json
import logging
import re

from src import llm
from src.llm import parseo
from src.llm.contrato import Bloque, BloqueImagen, BloqueTexto, LLMError, Pedido
from src.scraper.models import UsoModelo

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
    """Se lanza cuando la llamada al LLM falla (red o error del proveedor).

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
) -> list[Bloque]:
    content: list[Bloque] = []

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
            content.append(BloqueImagen(datos_b64=img_b64))
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

    content.append(BloqueTexto(texto=text_block))
    return content


def _extract_json(raw_text: str) -> dict:
    """Igual que en `analysis.py`: `llm.parseo.cargar`, no `json.loads`.

    El triaje pide un `motivo` en prosa, asi que tambien puede arrastrar barras
    invertidas del pliego. Y acá el costo de fallar es peor que en el analisis:
    un parseo roto devuelve NO-GO, o sea que descarta el proceso en silencio.
    """
    try:
        return parseo.cargar(raw_text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(raw_text)
        if match:
            return parseo.cargar(match.group(0))
        raise


def triage(
    cfg,
    contexto_cliente: str,
    *,
    texto: str | None = None,
    pdf_path: str | None = None,
    scanned: bool = False,
    client=None,
) -> tuple[str, float, str, UsoModelo]:
    """Ejecuta el triaje GO/NO-GO contra el proveedor de LLM activo.

    Devuelve (veredicto, confianza, motivo, uso) donde veredicto ∈ {"GO",
    "NO-GO"}, confianza ∈ [0, 1] y `uso` es el consumo real de tokens que
    reportó el proveedor (acumulando el reintento si lo hubo).

    `client` es el cliente **crudo del proveedor activo** y es opcional: sirve
    para inyectar uno falso en tests (smoke tests sin red). Si es None, el
    adaptador correspondiente construye —y cachea— el suyo; la instanciación
    dejó de vivir acá.
    """
    # El modelo sale de la tabla de despacho, NO de `cfg.model_triage`: ese
    # campo solo gobierna a Anthropic. `quota.cobrar` tarifa por este string,
    # así que con LLM_PROVIDER=oci tiene que decir el modelo de OCI o el gasto
    # se contabiliza contra la tarifa equivocada.
    uso = UsoModelo(modelo=llm.modelo_de(cfg, "triaje"))

    user_content = _build_user_content(
        contexto_cliente, texto=texto, pdf_path=pdf_path, scanned=scanned,
        raster_pages=getattr(cfg, "triage_raster_pages", 5),
    )

    try:
        respuesta = llm.completar(
            cfg,
            Pedido(
                etapa="triaje",
                contenido=user_content,
                sistema=_SYSTEM_PROMPT,
                max_tokens=512,
            ),
            cliente=client,
        )
        uso.acumular(respuesta.uso)
    except LLMError as e:
        logger.error("Error del proveedor de LLM durante el triaje: %s", e)
        raise TriageError(str(e)) from e
    except Exception as e:  # noqa: BLE001 — todo fallo del triaje es TriageError
        logger.error("Error de red/inesperado durante el triaje: %s", e)
        raise TriageError(str(e)) from e

    try:
        data = _extract_json(respuesta.texto)
    except (json.JSONDecodeError, AttributeError):
        logger.warning(
            "No se pudo parsear el JSON del triaje (truncada=%s); reintentando: %r",
            respuesta.truncada, respuesta.texto,
        )
        retry_content = _build_user_content(
            contexto_cliente,
            texto=texto,
            pdf_path=pdf_path,
            scanned=scanned,
            raster_pages=getattr(cfg, "triage_raster_pages", 5),
            extra_instruction=_RETRY_SUFFIX,
        )
        try:
            respuesta = llm.completar(
                cfg,
                Pedido(
                    etapa="triaje",
                    contenido=retry_content,
                    sistema=_SYSTEM_PROMPT,
                    max_tokens=512,
                ),
                cliente=client,
            )
            uso.acumular(respuesta.uso)
        except LLMError as e:
            logger.error("Error del proveedor de LLM durante el reintento de triaje: %s", e)
            raise TriageError(str(e)) from e
        except Exception as e:  # noqa: BLE001 — todo fallo del triaje es TriageError
            logger.error("Error de red/inesperado durante el reintento de triaje: %s", e)
            raise TriageError(str(e)) from e

        try:
            data = _extract_json(respuesta.texto)
        except (json.JSONDecodeError, AttributeError):
            # `truncada` separa dos causas que hasta la Fase 9 se veían iguales:
            # el modelo dijo algo inválido, o se quedó sin tokens a mitad del
            # JSON. Con el techo de 4096 de OCI el segundo caso pasa de rareza a
            # evento esperado, y el operador necesita distinguirlos en el log.
            logger.error(
                "Parseo del triaje fallo tras reintento (truncada=%s): %r",
                respuesta.truncada, respuesta.texto,
            )
            return "NO-GO", 0.0, "parse_error", uso

    viabilidad = str(data.get("viabilidad", "")).upper()
    veredicto = "GO" if viabilidad == "GO" else "NO-GO"

    try:
        confianza = float(data.get("confianza", 0.0))
    except (TypeError, ValueError):
        confianza = 0.0
    confianza = max(0.0, min(1.0, confianza))

    motivo = str(data.get("motivo", ""))

    return veredicto, confianza, motivo, uso
