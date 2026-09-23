"""src.llm.modelos — resolución de `(proveedor, familia, etapa)` → modelo y techo de salida.

Tablas declarativas y tres funciones. El call site dice **para qué** llama
(`"triaje"`, `"analisis"`, `"clasificacion"`, `"perfilado"`) y acá se decide a
qué modelo concreto le toca, según el proveedor y —dentro de OCI— la **familia**
activa.

**Por qué variables de entorno separadas por proveedor.** Hoy `.env.example`
dice `MODEL_TRIAGE=claude-haiku-4-5`. Si esa misma variable gobernara los dos
proveedores, poner `LLM_PROVIDER=oci` en una VM ya desplegada mandaría
`"claude-haiku-4-5"` como `model_id` a OCI y fallaría con un 404 confuso —
justo el modo de falla que la abstracción existe para evitar. Son cuatro
variables más, todas con default funcional y ninguna obligatoria; a cambio
`LLM_PROVIDER` es de verdad una perilla sola.

**`cfg` manda, la tabla es el piso.** Se lee siempre con `getattr(cfg, campo,
"")`, nunca acceso directo a atributo: es el idioma de `src/memoria/service.py`
y permite que los tests pasen un `types.SimpleNamespace` con solo los tres
campos que les importan, sin construir un `Config` entero.

── Dos ejes ortogonales: proveedor y familia ───────────────────────────────────

`LLM_PROVIDER` elige **quién sirve la inferencia** (`anthropic` | `oci`).
`OCI_FAMILIA` elige, dentro de OCI, **qué familia de modelos** se usa por
defecto en las cuatro etapas (`nativo` | `xai` | `google`). Son perillas
independientes a propósito: `OCI_FAMILIA` no hace nada mientras
`LLM_PROVIDER=anthropic`, y flipearla cambia las cuatro etapas de una vez sin
tocar ninguna otra cosa.

Los `OCI_MODEL_<etapa>` siguen ganándole a la tabla, que es lo que permite
**mezclar**: triaje en Llama (barato, alto volumen) y análisis en Gemini (techo
de salida 16× más alto) es una combinación legítima y no requiere código nuevo.

── Elección de los modelos de OCI (medida, no supuesta) ────────────────────────

**Disponibilidad real del tenant**, verificada con
`oci generative-ai model-collection list-models`: los únicos modelos Meta que
sirve este tenant son `meta.llama-4-scout-17b-16e-instruct` y
`meta.llama-4-maverick-17b-128e-instruct-fp8`. ⚠️ **`meta.llama-3.3-70b-instruct`
NO existe acá** — la documentación lo lista, el tenant no lo sirve. No
reintroducirlo.

- **Triaje, clasificación y perfilado → Scout.** El triaje necesita visión (dos
  de cada tres pliegos del corpus están escaneados) y es la etapa de más
  volumen, así que manda el precio; 16 expertos alcanzan de sobra para un
  GO/NO-GO sobre el "Objeto de la Contratación". Clasificación y perfilado son
  texto puro y de alto volumen: mismo argumento.
- **Análisis → Maverick.** Multimodal, más razonamiento, y **mismo
  `api_format="GENERIC"`** que Scout: un solo camino de traducción para los dos.

**Descartado a propósito, por evidencia y no por conjetura:**

- `cohere.command-a-*`: responde `400 Chat request type does not match serving
  model` a un `GenericChatRequest` (verificado en la Tanda 0). Exige
  `CohereChatRequest`, con `message` + `chat_history` en lugar de `messages`, lo
  que duplicaría el traductor entero para ganar un modelo más caro. **Sigue
  fuera de alcance**: no es una preferencia, es que no es alcanzable.

── Familias externas: `xai` y `google` ─────────────────────────────────────────

Hasta la Fase 9b estas dos estaban vetadas acá mismo *"porque la inferencia sale
por llamadas externas"*. **El veto se levantó y pasó a ser una advertencia de
gobernanza del dato**: son configurables, el default sigue siendo `nativo`, y la
decisión de encenderlas es del operador, informada.

Lo que hay que saber antes de encenderlas:

- ⚠️ **Los pliegos salen de la infraestructura de OCI.** `xai.grok-*` y
  `google.gemini-*` los expone OCI, pero resuelve la inferencia llamando a
  servidores de xAI y de Google. Encenderlas pone documentos de clientes en
  manos de un tercero que ningún contrato contempla. Por eso
  `FAMILIAS_EXTERNAS` existe como conjunto: `llm.validar_configuracion()` lo usa
  para loguear un WARNING nombrando al tercero al arrancar.
- **No exigen cambios de formato** (medido 2026-09-19): las dos hablan el mismo
  `GenericChatRequest` que Meta y leen imágenes base64 por data-URI. Eso es lo
  que hace acotado el trabajo, y lo que las separa de Cohere.
- **Su techo de salida es mucho más alto** (ver `TECHO_SALIDA`): 65.536 en
  Gemini y ≥200.000 en Grok contra los 4.096 de Meta. Con Gemini el análisis
  puede pedir de verdad los 16.000 tokens que pide el call site, que hasta ahora
  se recortaban a 4.096.
- **Su costo no está medido.** Ver el comentario de precios en
  `src/drilldown/quota.py`: quedan fuera de `PRECIOS_USD_MTOK` a propósito, así
  que la guardia de presupuesto las cobra como el modelo más caro conocido. Es
  el comportamiento correcto mientras la tarifa real no esté medida.
"""
from __future__ import annotations

import logging

from src.llm.contrato import LLMError

logger = logging.getLogger(__name__)

# ── Vocabulario ──────────────────────────────────────────────────────────────

ETAPAS: tuple[str, ...] = ("triaje", "analisis", "clasificacion", "perfilado")

PROVEEDORES: tuple[str, ...] = ("anthropic", "oci")

PROVEEDOR_DEFAULT = "anthropic"

# Familias de modelos DENTRO del proveedor `oci`. `nativo` son los Meta que
# OCI sirve con su propia infraestructura; las otras dos las expone OCI pero las
# resuelve llamando a un tercero (ver el docstring).
FAMILIAS: tuple[str, ...] = ("nativo", "xai", "google")

FAMILIA_DEFAULT = "nativo"

# Las familias cuya inferencia SALE de OCI. No es decorativo: `src/llm/__init__`
# lo usa para el WARNING de gobernanza del dato al arrancar, y así el aviso no
# depende de que alguien se acuerde de mantener una lista paralela.
FAMILIAS_EXTERNAS = frozenset({"xai", "google"})

# ── Techo de salida: es POR MODELO, no del proveedor ─────────────────────────

# Topes **MEDIDOS** (2026-09-19) por bisección sobre el mayor `maxTokens` que el
# request acepta. OCI los valida **en el request**: por encima responde
#   400 Invalid 'maxTokens': Value is greater than maximum: <techo>
# Es un error duro, no un truncamiento silencioso. Por eso el clamp de
# `max_tokens_de` no es una optimización sino un requisito: sin él, `analisis`
# (16.000) y `clasificacion` (16.384) fallan en la primera llamada contra Meta.
#
# Hasta la Fase 9b esto era UNA constante del proveedor (`OCI_MAX_TOKENS_SALIDA
# = 4096`). Con Gemini o Grok esa constante recortaba el análisis a 4.096 sin
# ninguna razón: su techo es 16× y ≥48× más alto.
TECHO_SALIDA: dict[str, int] = {
    "meta.llama-4-scout-17b-16e-instruct": 4096,
    "meta.llama-4-maverick-17b-128e-instruct-fp8": 4096,
    "google.gemini-2.5-flash": 65536,
    # Grok aceptó 200.000 en la bisección; se fija ahí y no más arriba porque es
    # lo medido, no lo que la documentación promete.
    "xai.grok-4.6": 200000,
}

# El más restrictivo de los medidos. Un modelo que no esté en la tabla —uno
# nuevo, o un `OCI_MODEL_*` apuntado a mano— se **recorta** en vez de reventar
# con un 400 en la corrida de las 07:00. Degradar es preferible a caer.
TECHO_DESCONOCIDO = 4096


# ── Piso de salida para las familias que razonan sin decirlo ─────────────────
#
# Grok y Gemini gastan tokens de razonamiento que **cuentan contra `maxTokens`**
# pero NO aparecen en `completion_tokens`. Medido el 2026-09-19 en el triaje del
# E2E: Gemini consumió 508 tokens para emitir 21 de respuesta visible. Con el
# `max_tokens=512` del triaje —calibrado para Claude Haiku, que no razona
# invisiblemente— el JSON quedó cortado a la mitad:
#
#     '```json\n{\n  "viabilidad": "GO",\n  "confianza": 0'
#
# y el triaje devolvió `NO-GO` con confianza 0.0 por `parse_error`. O sea: un
# GO se convirtió en NO-GO, que es el peor modo de falla del sistema porque no
# alerta — el pliego se descarta en silencio.
#
# Es INTERMITENTE: con un prompt más corto la respuesta entra y todo anda. Por
# eso no alcanza con documentarlo, hay que darle aire al presupuesto.
#
# 2048 sale de la medición con margen: ~500 de razonamiento observado, y el
# triaje necesita ~150 de JSON. Sigue muy por debajo del techo de las dos
# familias (65.536 y 200.000), así que no arriesga el 400.
FAMILIAS_CON_RAZONAMIENTO_OCULTO = frozenset({"xai", "google"})
PISO_SALIDA_CON_RAZONAMIENTO = 2048

# ── Tabla de despacho ────────────────────────────────────────────────────────

# Dentro de OCI la resolución es en dos pasos: primero la familia activa,
# después la etapa. Se escriben las **cuatro entradas explícitas** de cada
# familia en vez de un comodín: es más obvio de leer y deja el lugar preparado
# si el E2E muestra que conviene partirlas como en Meta.
DEFAULTS_OCI: dict[tuple[str, str], str] = {
    # `nativo`: Scout es barato y de sobra para GO/NO-GO y para texto de alto
    # volumen; Maverick razona mejor y se reserva para el análisis.
    ("nativo", "triaje"): "meta.llama-4-scout-17b-16e-instruct",
    ("nativo", "analisis"): "meta.llama-4-maverick-17b-128e-instruct-fp8",
    ("nativo", "clasificacion"): "meta.llama-4-scout-17b-16e-instruct",
    ("nativo", "perfilado"): "meta.llama-4-scout-17b-16e-instruct",
    # `xai` y `google`: el mismo modelo en las cuatro etapas. Los dos son
    # multimodales y sin tope restrictivo, así que no hay razón para partirlos.
    ("xai", "triaje"): "xai.grok-4.6",
    ("xai", "analisis"): "xai.grok-4.6",
    ("xai", "clasificacion"): "xai.grok-4.6",
    ("xai", "perfilado"): "xai.grok-4.6",
    ("google", "triaje"): "google.gemini-2.5-flash",
    ("google", "analisis"): "google.gemini-2.5-flash",
    ("google", "clasificacion"): "google.gemini-2.5-flash",
    ("google", "perfilado"): "google.gemini-2.5-flash",
}

DEFAULTS: dict[tuple[str, str], str] = {
    ("anthropic", "triaje"): "claude-haiku-4-5",
    ("anthropic", "analisis"): "claude-sonnet-5",
    ("anthropic", "clasificacion"): "claude-haiku-4-5-20251001",
    ("anthropic", "perfilado"): "claude-haiku-4-5-20251001",
}

# Las entradas `("oci", etapa)` se **generan** como espejo de la familia por
# defecto, no se transcriben: dos tablas con los mismos IDs escritos a mano se
# desincronizan el día que alguien cambie uno solo.
#
# Siguen existiendo por dos motivos concretos, los dos vigentes:
#   1. Call sites que leen `DEFAULTS[("oci", etapa)]` para saber qué modelo se
#      va a usar sin pasar por `cfg` (tests de paridad, de triaje rasterizado).
#   2. La guardia de `tests/test_quota.py`, que exige tarifa conocida para todo
#      valor de esta tabla. Que solo espeje `nativo` es lo correcto: grok y
#      gemini quedan fuera de `PRECIOS_USD_MTOK` a propósito —no hay tarifa
#      medida— y se cobran como el modelo más caro conocido.
DEFAULTS.update(
    {("oci", etapa): DEFAULTS_OCI[(FAMILIA_DEFAULT, etapa)] for etapa in ETAPAS}
)

# `(proveedor, etapa)` → nombre del campo de `Config` que lo pisa. Los nombres
# de Anthropic son los que ya existen (`model_triage`, `model_analysis`) más los
# dos que faltaban; los de OCI llevan prefijo por la razón del docstring.
_CAMPO_CFG: dict[tuple[str, str], str] = {
    ("anthropic", "triaje"): "model_triage",
    ("anthropic", "analisis"): "model_analysis",
    ("anthropic", "clasificacion"): "model_classifier",
    ("anthropic", "perfilado"): "model_trainer",
    ("oci", "triaje"): "oci_model_triage",
    ("oci", "analisis"): "oci_model_analysis",
    ("oci", "clasificacion"): "oci_model_classifier",
    ("oci", "perfilado"): "oci_model_trainer",
}


# ── Resolución ───────────────────────────────────────────────────────────────


def proveedor_activo(cfg) -> str:
    """Devuelve el proveedor configurado, normalizado, SIN validarlo.

    Vive acá y no en la fachada para que `modelo_de` no tenga que importar
    `src.llm`, que importaría `src.llm.modelos` de vuelta. `src.llm` lo
    reexporta, así que el resto del código sigue llamando `llm.proveedor_activo`.

    No valida a propósito: quien valida es `llm.validar_configuracion`, que se
    llama **una vez al arrancar** y falla ruidoso. Un valor desconocido que se
    resolviera en silencio a Anthropic haría que un typo (`LLM_PROVIDER=ocl`)
    gastara en el proveedor equivocado durante semanas sin que nadie lo note.
    """
    return str(getattr(cfg, "llm_provider", "") or PROVEEDOR_DEFAULT).strip().lower()


def familia_activa(cfg) -> str:
    """Devuelve la familia de modelos de OCI configurada, normalizada, SIN validarla.

    Mismo contrato que `proveedor_activo`, y por la misma razón: quien valida es
    `llm.validar_configuracion()`, que corre una vez al arrancar y falla
    ruidoso. Una `OCI_FAMILIA=googel` que se resolviera en silencio a `nativo`
    haría que el operador crea que encendió Gemini durante semanas sin ningún
    síntoma — exactamente el modo de falla que la validación existe para evitar.

    Solo tiene efecto con `LLM_PROVIDER=oci`; con Anthropic se ignora.
    """
    # Se normaliza ANTES de aplicar el default —al revés que `proveedor_activo`—
    # para que un `OCI_FAMILIA=" "` caiga en `nativo` y no en la cadena vacía,
    # que después fallaría como "familia desconocida: ''" sin decirle nada útil
    # a nadie. Vacío significa "no la configuré", y eso es el default.
    return str(getattr(cfg, "oci_familia", "") or "").strip().lower() or FAMILIA_DEFAULT


def _familia(cfg, etapa: str) -> str:
    """Familia activa, ya validada. Lanza `LLMError` si no existe."""
    familia = familia_activa(cfg)
    if familia not in FAMILIAS:
        raise LLMError(
            f"familia de modelos de OCI desconocida: {familia!r}. "
            f"OCI_FAMILIA admite {', '.join(FAMILIAS)}",
            proveedor="oci",
            etapa=etapa,
        )
    return familia


def _clave(cfg, etapa: str) -> tuple[str, str]:
    proveedor = proveedor_activo(cfg)
    if etapa not in ETAPAS:
        raise LLMError(
            f"etapa desconocida: {etapa!r}. Las válidas son {', '.join(ETAPAS)}",
            proveedor=proveedor,
            etapa=etapa,
        )
    if proveedor not in PROVEEDORES:
        raise LLMError(
            f"proveedor de LLM desconocido: {proveedor!r}. "
            f"LLM_PROVIDER admite {', '.join(PROVEEDORES)}",
            proveedor=proveedor,
            etapa=etapa,
        )
    return proveedor, etapa


def modelo_de(cfg, etapa: str) -> str:
    """ID del modelo para `etapa` con el proveedor activo.

    Precedencia, sin cambios desde la Fase 9: el campo de `cfg` si trae algo;
    si viene vacío, la tabla. Un campo vacío es lo normal — el `.env` de
    producción no define los cuatro modelos, se apoya en la tabla.

    Con OCI la tabla se resuelve en dos pasos, `(familia, etapa)`. Que el campo
    de `cfg` siga ganando es lo que permite **mezclar** familias por etapa.

    La familia se valida aunque después gane el campo de `cfg`: un typo en
    `OCI_FAMILIA` es un error de configuración se use o no, y descubrirlo recién
    el día que alguien borre un `OCI_MODEL_*` sería la peor versión del mismo
    problema.
    """
    proveedor, etapa = _clave(cfg, etapa)
    valor = str(getattr(cfg, _CAMPO_CFG[(proveedor, etapa)], "") or "").strip()
    if proveedor != "oci":
        return valor or DEFAULTS[(proveedor, etapa)]
    familia = _familia(cfg, etapa)
    return valor or DEFAULTS_OCI[(familia, etapa)]


def techo_de(modelo: str) -> int:
    """Tokens de salida que ese modelo acepta en el request.

    Lo desconocido se trata como lo más restrictivo medido (`TECHO_DESCONOCIDO`)
    y queda en el log: un ID nuevo se recorta, no revienta con un 400.
    """
    techo = TECHO_SALIDA.get(modelo)
    if techo is None:
        logger.info(
            "modelo %s sin techo de salida medido; se usa el más restrictivo (%s)",
            modelo, TECHO_DESCONOCIDO,
        )
        return TECHO_DESCONOCIDO
    return techo


def max_tokens_de(cfg, etapa: str, deseado: int) -> int:
    """Acota `deseado` al techo de salida del **modelo** que resuelve esa etapa.

    Anthropic **no lleva clamp**: sus topes están muy por encima de los 16.384
    que pide el clasificador, y recortar acá escondería un cambio de política
    del proveedor detrás de una constante nuestra.

    OCI sí, y es obligatorio (ver `TECHO_SALIDA`). El techo es por modelo y no
    por proveedor: con Meta son 4.096, con Gemini 65.536 y con Grok 200.000, así
    que el mismo `deseado=16000` del análisis se recorta con una familia y pasa
    entero con las otras dos.

    El techo efectivo se puede bajar con `cfg.oci_max_tokens_salida`, pero
    **nunca subir** por encima del máximo medido del modelo: la API rechaza el
    request con un 400, así que permitir un valor mayor solo serviría para
    romper la corrida de las 07:00.

    Recorta y loguea; no lanza. Una respuesta más corta de lo ideal es
    degradación; un `LLMError` acá sería una caída.
    """
    proveedor, etapa = _clave(cfg, etapa)
    if proveedor != "oci":
        return deseado

    modelo = modelo_de(cfg, etapa)
    maximo = techo_de(modelo)

    # Piso para las familias que razonan sin reportarlo: parte del presupuesto
    # se consume antes de emitir el primer carácter (ver la constante).
    if familia_activa(cfg) in FAMILIAS_CON_RAZONAMIENTO_OCULTO and deseado < PISO_SALIDA_CON_RAZONAMIENTO:
        logger.info(
            "etapa %s: max_tokens %s → %s; %s gasta tokens de razonamiento que "
            "cuentan contra el presupuesto y no se reportan",
            etapa, deseado, PISO_SALIDA_CON_RAZONAMIENTO, modelo,
        )
        deseado = PISO_SALIDA_CON_RAZONAMIENTO

    # ⚠️ El override solo cuenta si viene POSITIVO. `cfg.oci_max_tokens_salida`
    # vale 0 por defecto y 0 significa "sin override: usá el techo del modelo".
    # Que el default fuera 4096 —el techo de Meta, de cuando era el techo del
    # proveedor entero— haría `min(65536, 4096) = 4096` con Gemini y mataría la
    # funcionalidad al nacer, en silencio: nadie que lea `OCI_MAX_TOKENS_SALIDA
    # =4096` en un `.env` sospecharía que está anulando un techo de 65.536.
    override = int(getattr(cfg, "oci_max_tokens_salida", 0) or 0)
    techo = override if override > 0 else maximo
    if techo > maximo:
        logger.warning(
            "oci_max_tokens_salida=%s supera el máximo que acepta %s (%s); se usa el máximo",
            techo, modelo, maximo,
        )
        techo = maximo

    if deseado > techo:
        logger.info(
            "etapa %s: max_tokens %s recortado a %s (tope de salida de %s)",
            etapa, deseado, techo, modelo,
        )
        return techo
    return deseado
