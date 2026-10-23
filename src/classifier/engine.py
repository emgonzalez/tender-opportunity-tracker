"""
Motor de clasificación semántica: manda los procesos al LLM y parsea el veredicto.

Envía todos los procesos de una ejecución en un único prompt (batch)
para minimizar el número de llamadas a la API y aprovechar el contexto completo.

Habla con el modelo por `src.llm`, no por el SDK de Anthropic: el proveedor lo
elige `LLM_PROVIDER` y el ID del modelo sale de `llm.modelo_de(cfg,
"clasificacion")`. Eso tiene dos consecuencias visibles acá abajo: el tamaño del
lote depende del proveedor (OCI corta la salida en 4096 tokens) y el único error
que hay que atrapar es `LLMError`.
"""
import json
import logging
import unicodedata

from src import llm
from src.classifier.client_profile import ClientProfile
from src.classifier.rules import get_system_prompt, get_user_prompt
from src.config import Config
from src.llm import modelos, parseo
from src.llm.contrato import BloqueTexto, LLMError, Pedido
from src.scraper.models import MarcaRecomendada, Proceso, ProcesoClasificado, UsoModelo

logger = logging.getLogger(__name__)

# Retrocompat: `tests/test_quota.py` importa este nombre para verificar que tiene
# precio en `PRECIOS_USD_MTOK`. Es el default de Anthropic para esta etapa; el
# modelo REAL lo resuelve `llm.modelo_de()`, que respeta LLM_PROVIDER.
MODEL = modelos.DEFAULTS[("anthropic", "clasificacion")]

MAX_TOKENS = 16384
BATCH_SIZE = 40       # Procesos por llamada con Anthropic (protege del truncamiento)
# Tokens de JSON que cuesta clasificar UN proceso. Medido sobre el esquema de
# `rules.py`: indice, es_relevante, categoria_detectada, razon, tres scores, tags
# y marcas_recomendadas con su razón. Es el divisor que traduce "techo de salida
# del modelo" a "cuántos procesos entran en una llamada".
TOKENS_POR_PROCESO = 350

# Piso del lote. Por debajo de esto el split-y-reintento no tiene de dónde
# agarrarse y cada proceso pagaría su propio overhead de prompt.
BATCH_SIZE_MIN = 5

# Profundidad máxima del split-y-reintento de `_classify_batch`: se parte el lote
# UNA vez. Más niveles multiplicarían las llamadas —y el gasto— persiguiendo un
# modelo que ya demostró no caber en su propio techo de salida.
MAX_NIVEL_SPLIT = 1


def _batch_size(cfg) -> int:
    """Procesos por llamada, derivados del techo de salida del MODELO activo.

    El JSON de una clasificación cuesta ~350 tokens por proceso: 40 × 350 ≈
    14.000, que es exactamente para lo que existe `MAX_TOKENS = 16384`.

    **Por qué se deriva y no es una constante.** Hasta la Fase 9b esto devolvía
    10 fijo para todo OCI, porque 4096 era el techo de los únicos modelos que se
    usaban (Meta Llama 4). Pero el techo es **por modelo**, no del proveedor, y
    la diferencia es enorme: Gemini acepta 65.536 y Grok 200.000. Con una
    constante, cambiar `OCI_FAMILIA` a una familia con 16× más espacio habría
    seguido partiendo en lotes de 10 —cuatro veces más llamadas y más gasto que
    los necesarios— sin un solo error que lo delatara.

    `cfg.oci_classifier_batch_size` sigue pisando el cálculo, para poder bajarlo
    a mano si un modelo resulta más verboso de lo medido.
    """
    if llm.proveedor_activo(cfg) != "oci":
        return BATCH_SIZE

    manual = int(getattr(cfg, "oci_classifier_batch_size", 0) or 0)
    if manual > 0:
        return manual

    techo = modelos.techo_de(modelos.modelo_de(cfg, "clasificacion"))
    # Nunca por encima de los 40 de Anthropic: más procesos por llamada no
    # mejoran nada y empeoran lo que se pierde si esa llamada falla.
    return max(BATCH_SIZE_MIN, min(BATCH_SIZE, techo // TOKENS_POR_PROCESO))


def _sumar_uso(total: UsoModelo, parcial: UsoModelo) -> None:
    """Fusiona `parcial` dentro de `total`, preservando el conteo de llamadas.

    No se usa `UsoModelo.acumular()` porque ese método suma 1 a `llamadas` sin
    mirar cuántas trae el objeto fusionado: sumar así dos lotes —o las dos
    mitades de un split— perdería llamadas del libro mayor.
    """
    total.modelo = total.modelo or parcial.modelo
    total.input_tokens += parcial.input_tokens
    total.output_tokens += parcial.output_tokens
    total.llamadas += parcial.llamadas


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
    """Remueve envolturas ```json ... ``` que el modelo agrega a pesar de la instrucción."""
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


def _parsear(response_text: str, procesos: list[Proceso]) -> tuple[list[ProcesoClasificado], bool]:
    """Parsea la respuesta JSON del modelo y devuelve `(clasificados, parseo_ok)`.

    La segunda mitad de la tupla es la que habilita el split-y-reintento: sin
    ella, un truncamiento y un modelo que dijo una pavada se ven idénticos desde
    afuera (una lista entera de procesos sin clasificar).
    """
    try:
        cleaned = _strip_markdown_fences(response_text)
        # `parseo.cargar` y no `json.loads`: el modelo copia texto del proceso
        # en `razon` y en los tags, y una barra invertida suelta tira TODO el
        # lote —no un proceso— al fallback de "no clasificados".
        data = parseo.cargar(cleaned)
        clasificaciones = data.get("clasificaciones", [])
    except json.JSONDecodeError as exc:
        logger.error("Respuesta del modelo no es JSON válido: %s\nContenido: %s", exc, response_text[:500])
        # Fallback: marcar todos como no clasificados
        return [ProcesoClasificado.from_proceso(p) for p in procesos], False

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

    return result, True


def _norm_marca(nombre: str) -> str:
    """Minúsculas, sin acentos ni espacios/puntuación: 'Pure Storage' == 'purestorage'."""
    sin_acentos = unicodedata.normalize("NFKD", nombre or "")
    sin_acentos = "".join(c for c in sin_acentos if not unicodedata.combining(c))
    return "".join(c for c in sin_acentos.casefold() if c.isalnum())


def _marcas_del_portafolio(client_profile: ClientProfile) -> set[str]:
    """Nombres e ids normalizados de las marcas que el cliente declara en su catálogo."""
    permitidas: set[str] = set()
    for cat in client_profile.catalog.categories:
        for b in cat.brands:
            permitidas.update(n for n in (_norm_marca(b.name), _norm_marca(b.id)) if n)
    return permitidas


def filtrar_marcas_por_portafolio(
    clasificados: list[ProcesoClasificado],
    client_profile: ClientProfile,
) -> int:
    """Descarta de `marcas_recomendadas` las marcas ajenas al portafolio del cliente.

    El LLM a veces sugiere marcas que el cliente no vende (DJI, VMware...). Si el
    catálogo no declara ninguna marca no se filtra nada (otros clientes). Muta
    `clasificados` y devuelve cuántas marcas descartó.
    """
    permitidas = _marcas_del_portafolio(client_profile)
    if not permitidas:
        return 0
    descartadas: list[str] = []
    for pc in clasificados:
        conservadas = []
        for m in pc.marcas_recomendadas:
            if _norm_marca(m.marca) in permitidas:
                conservadas.append(m)
            else:
                descartadas.append(m.marca)
        pc.marcas_recomendadas = conservadas
    if descartadas:
        logger.info(
            "Marcas fuera del portafolio descartadas: %d (%s).",
            len(descartadas), ", ".join(sorted(set(descartadas))),
        )
    return len(descartadas)


def _parse_clasificaciones(response_text: str, procesos: list[Proceso]) -> list[ProcesoClasificado]:
    """Parsea la respuesta JSON del modelo y construye los ProcesoClasificado.

    Envoltorio de `_parsear` que descarta la señal de éxito, conservado con esta
    firma porque es la superficie que testea `tests/test_classifier.py`.
    """
    return _parsear(response_text, procesos)[0]


def _classify_batch(
    procesos: list[Proceso],
    cfg: Config,
    system_prompt: str,
    *,
    cliente=None,
    _nivel: int = 0,
) -> tuple[list[ProcesoClasificado], UsoModelo]:
    """Clasifica un lote de procesos en una llamada y devuelve `(clasificados, uso)`.

    Devuelve el consumo junto con el resultado porque quien cobra es
    `classify_procesos`, una sola vez, con la suma de todos los lotes.

    **Split-y-reintento.** Si la respuesta vino truncada *y* además no se pudo
    parsear, se parte el lote a la mitad y se reintenta una vez (`_nivel`
    guarda la recursión en profundidad 1). Sin esto, con un techo de salida de
    4096 tokens un truncamiento devolvería el lote entero sin clasificar: una
    pérdida sistemática, no un caso raro.
    """
    user_prompt = get_user_prompt(_procesos_to_dicts(procesos))
    pedido = Pedido(
        etapa="clasificacion",
        contenido=[BloqueTexto(texto=user_prompt)],
        sistema=system_prompt,
        max_tokens=MAX_TOKENS,
    )
    respuesta = llm.completar(cfg, pedido, cliente=cliente)

    uso = UsoModelo(modelo=respuesta.modelo)
    uso.acumular(respuesta.uso)

    logger.debug("Respuesta de %s (primeros 500 chars): %s", respuesta.modelo, respuesta.texto[:500])
    clasificados, parseo_ok = _parsear(respuesta.texto, procesos)

    if not respuesta.truncada or parseo_ok:
        return clasificados, uso

    logger.warning(
        "Respuesta truncada (max_tokens=%d) y sin JSON parseable: lote de %d proceso(s) con %s/%s.",
        MAX_TOKENS, len(procesos), respuesta.proveedor, respuesta.modelo,
    )
    if _nivel >= MAX_NIVEL_SPLIT or len(procesos) < 2:
        logger.error(
            "El lote de %d proceso(s) no se pudo recuperar: quedan sin clasificar.", len(procesos),
        )
        return clasificados, uso

    mitad = len(procesos) // 2
    logger.info(
        "Reintentando el lote partido en dos (%d + %d procesos).", mitad, len(procesos) - mitad,
    )
    recuperados: list[ProcesoClasificado] = []
    for parte in (procesos[:mitad], procesos[mitad:]):
        sub_clasificados, sub_uso = _classify_batch(
            parte, cfg, system_prompt, cliente=cliente, _nivel=_nivel + 1,
        )
        recuperados.extend(sub_clasificados)
        _sumar_uso(uso, sub_uso)
    return recuperados, uso


def classify_procesos(
    procesos: list[Proceso],
    config: Config,
    client_profile: ClientProfile,
) -> list[ProcesoClasificado]:
    """
    Clasifica una lista de procesos con el proveedor de LLM activo.
    Si hay más procesos que el lote del proveedor, los divide para evitar
    truncamiento de la respuesta por límite de tokens.
    """
    if not procesos:
        logger.info("No hay procesos para clasificar.")
        return []

    system_prompt = get_system_prompt(client_profile, config.app_name)
    proveedor = llm.proveedor_activo(config)
    modelo = llm.modelo_de(config, "clasificacion")
    batch_size = _batch_size(config)
    uso_total = UsoModelo(modelo=modelo)
    all_clasificados: list[ProcesoClasificado] = []

    if len(procesos) <= batch_size:
        logger.info("Clasificando %d proceso(s) con %s (%s)...", len(procesos), modelo, proveedor)
        try:
            all_clasificados, uso = _classify_batch(procesos, config, system_prompt)
        except LLMError as exc:
            # Asimetría deliberada, la de siempre: con un solo lote no queda
            # nada que reportar, así que el error tumba la corrida entera.
            logger.error("Error del proveedor de LLM (%s): %s", proveedor, exc)
            raise
        _sumar_uso(uso_total, uso)
    else:
        n_batches = (len(procesos) + batch_size - 1) // batch_size
        logger.info(
            "Clasificando %d proceso(s) en %d lote(s) de ~%d con %s (%s)...",
            len(procesos), n_batches, batch_size, modelo, proveedor,
        )

        for batch_num in range(n_batches):
            start = batch_num * batch_size
            end = min(start + batch_size, len(procesos))
            batch = procesos[start:end]

            logger.info(
                "Lote %d/%d: procesos %d–%d (%d)",
                batch_num + 1, n_batches, start, end - 1, len(batch),
            )
            try:
                clasificados, uso = _classify_batch(batch, config, system_prompt)
            except LLMError as exc:
                # ...y la otra mitad de la asimetría: con varios lotes, los que
                # ya se clasificaron valen un reporte, así que solo se degrada
                # ÉSTE y la corrida sigue.
                logger.error("Error del proveedor de LLM en lote %d (%s): %s", batch_num + 1, proveedor, exc)
                all_clasificados.extend(ProcesoClasificado.from_proceso(p) for p in batch)
                continue
            _sumar_uso(uso_total, uso)
            all_clasificados.extend(clasificados)

    # Import perezoso a propósito: `quota` vive en el paquete `drilldown`, que es
    # una feature opcional (flag ENABLE_DRILLDOWN). Importarlo arriba acoplaría
    # el clasificador —la ruta que corre siempre— a algo que puede estar apagado.
    from src.drilldown import quota

    # `cobrar()` SÍ, `cabe()` NUNCA: el drill-down se frena por presupuesto; el
    # clasificador solo se contabiliza. Frenar el clasificador es apagar el
    # sistema, y eso lo decide una persona, no un contador.
    quota.cobrar(config, uso_total)

    filtrar_marcas_por_portafolio(all_clasificados, client_profile)

    relevantes = sum(1 for p in all_clasificados if p.es_relevante)
    logger.info("Clasificación completa: %d/%d procesos relevantes.", relevantes, len(all_clasificados))
    return all_clasificados
