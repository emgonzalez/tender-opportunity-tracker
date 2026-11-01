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


def _score(valor) -> float:
    """Score del LLM como 0.0-1.0. Un modelo que contesta en escala 0-100 se normaliza; todo se acota.

    Desde E11.1 los scores se persisten en columnas `NUMBER(3,2)`: un 85 dejaría de caber (ORA-01438)."""
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    if 1.0 < v <= 100.0:
        v /= 100.0
    return max(0.0, min(1.0, v))


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
            pc.match_tecnico = _score(c.get("match_tecnico", 0.0))
            pc.match_categoria = _score(c.get("match_categoria", 0.0))
            pc.factibilidad = _score(c.get("factibilidad", 0.0))
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
                pc.marcas_ajenas.append({"marca": m.marca, "evidencia": m.evidencia})
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

    modo = getattr(config, "intel_clasif", "off")
    if modo == "activo":
        salida, uso = _clasificar_v2(procesos, config, client_profile)
        from src.drilldown import quota
        quota.cobrar(config, uso)
        return salida

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
    if modo == "sombra":
        sombra, uso = _clasificar_v2(procesos, config, client_profile)
        _sumar_uso(uso_total, uso)
        from dataclasses import asdict

        from src.aprendizaje import service
        for anterior, nuevo in zip(all_clasificados, sombra):
            logger.info("intel_clasif sombra: relevante %s → %s", anterior.es_relevante, nuevo.es_relevante)
            service.guardar_decision(config, client_profile.client_id,
                                     f"{client_profile.portal_id}:{nuevo.numero_referencia}", "clasif",
                                     variante="sombra", salida=asdict(nuevo))
    quota.cobrar(config, uso_total)

    filtrar_marcas_por_portafolio(all_clasificados, client_profile)

    relevantes = sum(1 for p in all_clasificados if p.es_relevante)
    logger.info("Clasificación completa: %d/%d procesos relevantes.", relevantes, len(all_clasificados))
    return all_clasificados


def _terminos_v2(cfg, profile):
    from src.aprendizaje import service
    from src.aprendizaje.semilla import terminos_semilla

    propios = {b.id for cat in profile.catalog.categories for b in cat.brands}
    base = [dict(marca_id=m, termino=t, tipo=tipo, activo=1, estado="semilla")
            for m, t, tipo in terminos_semilla() if (tipo == "ajena" and m not in propios) or (tipo != "ajena" and m in propios)]
    for cat in profile.catalog.categories:
        for b in cat.brands:
            base.extend(dict(marca_id=b.id, termino=t, tipo="fuerte", activo=1, estado="semilla")
                        for t in [b.name] if t)
            if b.id.lower() == "oracle":
                base.extend(dict(marca_id=b.id, termino=t, tipo="fuerte", activo=1, estado="semilla")
                            for t in ["Sun", "Java", "Crystal Ball"])
    aprendidos = service.terminos_activos(cfg, profile.client_id)
    # Una fila vigente reemplaza su equivalente de semilla.
    claves = {(t["marca_id"], t["termino"].casefold()) for t in aprendidos}
    return [t for t in base if (t["marca_id"], t["termino"].casefold()) not in claves] + aprendidos


def _parsear_v2(texto, procesos, profile, terminos):
    from src.classifier import marcas

    datos = parseo.cargar(_strip_markdown_fences(texto))
    filas = {c["indice"]: c for c in datos["clasificaciones"]}
    salida = []
    for i, p in enumerate(procesos):
        c = filas.get(i, {})
        pc = _parsear(json.dumps({"clasificaciones": [dict(c, indice=0)]}), [p])[0][0]
        fuentes = [p.descripcion, p.institucion, p.tipo_actividad]
        completo = " \n".join(fuentes)
        pc.marcas_ajenas = marcas.marcas_ajenas_en(completo, terminos)
        permitidas = _marcas_del_portafolio(profile)
        for m in c.get("marcas_recomendadas", []):
            if _norm_marca(m.get("marca", "")) not in permitidas:
                pc.marcas_ajenas.append({"marca": m.get("marca", ""),
                                         "evidencia": m.get("evidencia", ""), "origen": "llm"})
        for tag in pc.tags:
            if tag not in profile.catalog.tag_taxonomy:
                for ajena in marcas.marcas_ajenas_en(tag, terminos):
                    ajena["evidencia_valida"] = any(marcas.contiene(f, ajena["evidencia"]) for f in fuentes)
                    ajena["origen"] = "llm"
                    pc.marcas_ajenas.append(ajena)
        pc.tags = [t for t in pc.tags if t in profile.catalog.tag_taxonomy]
        sugeridas = { _norm_marca(m.get("marca", "")): m for m in c.get("marcas_recomendadas", [])}
        pc.marcas_recomendadas = []
        for cat in profile.catalog.categories:
            for b in cat.brands:
                m = sugeridas.get(_norm_marca(b.id), sugeridas.get(_norm_marca(b.name), {}))
                estado = marcas.detectar_por_diccionario(completo, terminos, b.id)
                propias = [t for t in terminos if t["marca_id"] == b.id]
                contrapatron = any(t["tipo"] == "excluye" and marcas.contiene(completo, t["termino"])
                                   for t in propias if t.get("activo", 1) and t.get("estado") in {"activo", "semilla"})
                if not m and estado == "DUDOSA" and not contrapatron:
                    continue
                fuertes = [t for t in terminos if t["marca_id"] == b.id and t["tipo"] == "fuerte"]
                cita = m.get("evidencia", "")
                valida = marcas.validar_evidencia(cita, fuentes, fuertes)
                confirmado = estado == "CONFIRMADA"
                pc.marcas_recomendadas.append(MarcaRecomendada(
                    marca=b.name, razon=m.get("razon", ""),
                    role=m.get("role") if m.get("role") in {"Recomendada", "Alternativa"} else "Alternativa",
                    score=1.0 if confirmado else _score(m.get("score", 0)),
                    evidencia=next((f for f in fuentes if marcas.validar_evidencia(f, [f], fuertes)), "")
                    if confirmado and not valida else cita,
                    evidencia_valida=confirmado or valida,
                    origen="diccionario" if estado != "DUDOSA" or contrapatron else "llm",
                    veredicto="confirmada" if confirmado else "fuera" if estado == "DESCARTADA" else "por_confirmar",
                    por_que="Migración fuera de la marca; requiere confirmar alcance" if contrapatron else
                            "Marca ajena explícita, sin término fuerte propio" if estado == "DESCARTADA" else
                            "Contrapatrón o evidencia insuficiente" if not confirmado else "Término fuerte del diccionario",
                ))
        categoria = c.get("evidencia_categoria", "")
        cat_ok = (pc.categoria_detectada in [v.strip() for v in profile.rules.categories.split(",")] and bool(categoria)
                  and any(marcas.normalizar(categoria) in marcas.normalizar(f) for f in fuentes)
                  and any(marcas.normalizar(s) in marcas.normalizar(categoria)
                          for s in profile.catalog.inclusion_signals if s))
        marca_ok = any(m.evidencia_valida and m.veredicto == "confirmada"
                       and m.score >= profile.relevance_threshold for m in pc.marcas_recomendadas)
        pc.es_relevante = bool((marca_ok or cat_ok and pc.match_categoria >= profile.relevance_threshold)
                              and pc.confianza >= profile.confidence_threshold)
        salida.append(pc)
    return salida


def _clasificar_v2(procesos, cfg, profile):
    from src.classifier.rules import (
        esquema_clasificacion_v2,
        get_system_prompt_v2,
        get_user_prompt_v2,
    )

    terminos = _terminos_v2(cfg, profile)
    resultados = []
    uso = UsoModelo(modelo=llm.modelo_de(cfg, "clasificacion"))
    for inicio in range(0, len(procesos), _batch_size(cfg)):
        lote = procesos[inicio:inicio + _batch_size(cfg)]
        datos = _procesos_to_dicts(lote)
        for d, p in zip(datos, lote):
            d["tipo_actividad"] = p.tipo_actividad
        ejemplos = []
        from src.aprendizaje import service, vecinos
        for p in lote:
            for cat in profile.catalog.categories:
                for b in cat.brands:
                    cercanos = service._tolerante(
                        "vecinos", cfg, None, [],
                        lambda conn: vecinos.consultar_vecinos(conn, profile.client_id, b.id, p.descripcion, 8))
                    ejemplos.append({"titulo_consulta": p.descripcion, "marca": b.id,
                                     "score_knn": vecinos.score_knn(cercanos),
                                     "casos": vecinos.casos_few_shot(cercanos)})
        pedido = Pedido(etapa="clasificacion", sistema=get_system_prompt_v2(profile, ejemplos),
                        contenido=[BloqueTexto(texto=get_user_prompt_v2(datos))], max_tokens=MAX_TOKENS,
                        temperatura=llm.temperatura_configurada(cfg),
                        esquema=esquema_clasificacion_v2() if llm.soporta_esquema(uso.modelo) else None,
                        nombre_esquema="clasificacion_marcas")
        try:
            respuesta = llm.completar(cfg, pedido)
            uso.acumular(respuesta.uso)
            resultados.extend(_parsear_v2(respuesta.texto, lote, profile, terminos))
        except Exception as exc:  # La capa opcional jamás detiene la corrida.
            logger.warning("Clasificación v2 degradada: %s", type(exc).__name__)
            resultados.extend(_parsear_v2('{"clasificaciones":[]}', lote, profile, terminos))
    return resultados, uso
