"""
Motor de clasificación semántica usando Claude Sonnet vía Anthropic API.

Envía todos los procesos de una ejecución en un único prompt (batch)
para minimizar el número de llamadas a la API y aprovechar el contexto completo.
"""
import json
import logging

import anthropic

from src.config import Config
from src.scraper.models import Proceso, ProcesoClasificado, MarcaRecomendada
from src.classifier.client_profile import ClientProfile
from src.classifier.rules import get_system_prompt, get_user_prompt

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 16384
BATCH_SIZE = 40  # Procesos por llamada API (protección contra truncamiento)


def _procesos_to_dicts(procesos: list[Proceso]) -> list[dict]:
    return [
        {
            "referencia": p.numero_referencia,
            "institucion": p.institucion,
            "descripcion": p.descripcion,
            "metodo": p.forma_contratacion,
            "fecha": p.fecha_publicacion.strftime("%Y-%m-%d %H:%M"),
        }
        for p in procesos
    ]


def _strip_markdown_fences(text: str) -> str:
    """Remueve envolturas ```json ... ``` que Claude agrega a pesar de la instrucción."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Remover primera línea (```json) y última (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        elif lines[0].strip().startswith("```"):
            lines = lines[1:]
        stripped = "\n".join(lines)
    return stripped


def _parse_clasificaciones(response_text: str, procesos: list[Proceso]) -> list[ProcesoClasificado]:
    """Parsea la respuesta JSON de Claude y construye los ProcesoClasificado."""
    try:
        cleaned = _strip_markdown_fences(response_text)
        data = json.loads(cleaned)
        clasificaciones = data.get("clasificaciones", [])
    except json.JSONDecodeError as exc:
        logger.error("Respuesta de Claude no es JSON válido: %s\nContenido: %s", exc, response_text[:500])
        # Fallback: marcar todos como no clasificados
        return [ProcesoClasificado.from_proceso(p) for p in procesos]

    result: list[ProcesoClasificado] = []
    clasificaciones_by_idx = {c["indice"]: c for c in clasificaciones if "indice" in c}

    for i, proceso in enumerate(procesos):
        pc = ProcesoClasificado.from_proceso(proceso)
        if i in clasificaciones_by_idx:
            c = clasificaciones_by_idx[i]
            pc.es_relevante = bool(c.get("es_relevante", False))
            pc.categoria_detectada = c.get("categoria_detectada", "otro")
            pc.razon = c.get("razon", "")

            # Scoring multidimensional (v2)
            pc.match_tecnico = float(c.get("match_tecnico", 0.0))
            pc.match_categoria = float(c.get("match_categoria", 0.0))
            pc.factibilidad = float(c.get("factibilidad", 0.0))
            # Backward compat: confianza = factibilidad
            pc.confianza = pc.factibilidad

            # Tags dinámicos (v2)
            pc.tags = list(c.get("tags", []))

            # Recomendación de marcas (v2)
            pc.marcas_recomendadas = [
                MarcaRecomendada(
                    marca=m.get("marca", ""),
                    razon=m.get("razon", ""),
                )
                for m in c.get("marcas_recomendadas", [])
            ]
        else:
            logger.warning("No se encontró clasificación para el proceso %d ('%s')", i, proceso.descripcion[:50])
        result.append(pc)

    return result


def _classify_batch(
    procesos: list[Proceso],
    client: anthropic.Anthropic,
    system_prompt: str,
) -> list[ProcesoClasificado]:
    """Clasifica un lote de procesos en una sola llamada API."""
    procesos_dicts = _procesos_to_dicts(procesos)
    user_prompt = get_user_prompt(procesos_dicts)

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    response_text = message.content[0].text

    if message.stop_reason == "max_tokens":
        logger.warning(
            "Respuesta truncada por max_tokens (%d). Lote de %d procesos.",
            MAX_TOKENS, len(procesos),
        )

    logger.debug("Respuesta de Claude (primeros 500 chars): %s", response_text[:500])
    return _parse_clasificaciones(response_text, procesos)


def classify_procesos(
    procesos: list[Proceso],
    config: Config,
    client_profile: ClientProfile,
) -> list[ProcesoClasificado]:
    """
    Clasifica una lista de procesos usando Claude Sonnet.
    Si hay más de BATCH_SIZE procesos, los divide en lotes para evitar
    truncamiento de la respuesta por límite de tokens.
    """
    if not procesos:
        logger.info("No hay procesos para clasificar.")
        return []

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    system_prompt = get_system_prompt(client_profile, config.app_name)

    if len(procesos) <= BATCH_SIZE:
        logger.info("Clasificando %d proceso(s) con Claude %s...", len(procesos), MODEL)
        try:
            clasificados = _classify_batch(procesos, client, system_prompt)
            relevantes = sum(1 for p in clasificados if p.es_relevante)
            logger.info("Clasificación completa: %d/%d procesos relevantes.", relevantes, len(clasificados))
            return clasificados
        except anthropic.APIError as exc:
            logger.error("Error de API Anthropic: %s", exc)
            raise

    # Dividir en lotes
    n_batches = (len(procesos) + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info(
        "Clasificando %d proceso(s) en %d lote(s) de ~%d con Claude %s...",
        len(procesos), n_batches, BATCH_SIZE, MODEL,
    )

    all_clasificados: list[ProcesoClasificado] = []
    for batch_num in range(n_batches):
        start = batch_num * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(procesos))
        batch = procesos[start:end]

        logger.info("Lote %d/%d: procesos %d–%d (%d)", batch_num + 1, n_batches, start, end - 1, len(batch))
        try:
            clasificados = _classify_batch(batch, client, system_prompt)
            all_clasificados.extend(clasificados)
        except anthropic.APIError as exc:
            logger.error("Error de API en lote %d: %s", batch_num + 1, exc)
            # Fallback: marcar este lote como no clasificado
            all_clasificados.extend(ProcesoClasificado.from_proceso(p) for p in batch)

    relevantes = sum(1 for p in all_clasificados if p.es_relevante)
    logger.info("Clasificación completa: %d/%d procesos relevantes.", relevantes, len(all_clasificados))
    return all_clasificados
