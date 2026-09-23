"""src.llm.proveedor_anthropic — traductor entre el contrato neutral y el SDK de Anthropic.

Este módulo es el **único** lugar del sistema que sabe cómo se le habla a
Anthropic. Antes eran cinco: `drilldown/triage.py`, `drilldown/analysis.py`,
`classifier/engine.py` y dos llamadas en `classifier/trainer.py`, cada una
armando su propio cliente, su propio payload y su propia captura de errores.
Cinco copias de la misma decisión son cinco oportunidades de que una se quede
atrás; acá queda una sola.

Tres cosas que este adaptador hace **distinto** de los call sites que reemplaza,
y que son el motivo de que valga la pena la capa:

1. **El PDF se lee tarde, no temprano.** `analysis.py` hacía
   ``read_pdf_b64(pdf_path)`` apenas entraba a la función: un pliego de 32 MB
   quedaba en RAM como base64 (~43 MB) en una VM con ``mem_limit: 800m``, aunque
   la llamada terminara fallando antes de usarlo. Acá el `BloqueDocumento` viaja
   como **ruta** y recién se lee al traducir ese bloque en particular. El
   adaptador de OCI nunca lo lee.

2. **Timeouts obligatorios.** Ninguna de las cinco llamadas originales pasaba
   ``timeout=``, lo que viola la regla del repo —la misma que
   ``notifier/alerts.py`` documenta en su ``HTTP_TIMEOUT_SECONDS``— y deja a la
   corrida diaria a merced del default del SDK. Acá el timeout es explícito y
   configurable (``LLM_TIMEOUT_S``).

3. **El mapeo de errores es TOTAL.** Nada de `anthropic` sale de esta función:
   todo se traduce a `LLMError`. Si un error se escapara sin traducir, el
   ``except LLMError`` de los call sites dejaría de atraparlo y la semántica de
   fallo cambiaría **en silencio** — el riesgo número uno de la migración.

**No se envuelve con `tenacity`.** El SDK de Anthropic ya reintenta solo
(conexión, 408, 409, 429 y ≥500) con backoff exponencial; agregarle otra capa
multiplicaría las esperas hasta comerse el ``TimeoutStartSec`` de systemd. Se
unifica ``max_retries=4`` para las cuatro etapas: hoy `triage` y `analysis` usan
4 y `engine`/`trainer` el default de 2, y esa asimetría no responde a ninguna
decisión, solo a que se escribieron en momentos distintos.
"""
from __future__ import annotations

import logging

import anthropic

from src.llm.contrato import (
    BloqueDocumento,
    BloqueImagen,
    BloqueTexto,
    LLMError,
    Pedido,
    Respuesta,
    Uso,
)
from src.llm.modelos import max_tokens_de, modelo_de

logger = logging.getLogger(__name__)

PROVEEDOR = "anthropic"


# ── Constantes de transporte ─────────────────────────────────────────────────

# Reintentos internos del SDK. Cuatro y no el default de 2 porque es lo que ya
# usaban las dos etapas del drill-down —las que corren contra la red de
# COMPRASAL en la misma ventana— y porque el adaptador de OCI se calibra contra
# este número: si los dos proveedores no tienen el mismo sobre de fallo,
# "cambiar de proveedor" también cambia la tasa de error observada y no se puede
# comparar nada.
MAX_REINTENTOS_SDK = 4

# Timeout por request, en segundos (el SDK de Python los cuenta en segundos).
# Se puede pisar con `LLM_TIMEOUT_S`. El valor alto es para el análisis, que
# manda el pliego entero y pide hasta 16.000 tokens de salida; las etapas cortas
# terminan mucho antes y el timeout nunca las toca.
#
# ⚠️ El tiempo de pared real es `timeout × (MAX_REINTENTOS_SDK + 1)`: el SDK
# reintenta también los timeouts. Es lo que hay; la alternativa —no poner
# timeout— es la que estaba y es peor.
TIMEOUT_DEFAULT_S = 600

# Por encima de este `max_tokens` se usa `messages.stream()` en vez de
# `messages.create()`. Ver `_invocar` para el porqué.
_UMBRAL_STREAM_TOKENS = 8192

# Códigos HTTP que vale la pena reintentar. Es el mismo conjunto que reintenta
# el SDK internamente, más 529 (`overloaded_error`), que es específico de
# Anthropic. Un 4xx que no esté acá es un problema de la request, no del
# momento: reintentarlo solo gasta cuota.
_HTTP_REINTENTABLES = frozenset({408, 409, 429, 500, 502, 503, 529})


# ── Caché de clientes ────────────────────────────────────────────────────────

# `anthropic.Anthropic()` construye un pool de conexiones httpx; reconstruirlo
# en cada llamada de un lote tira el keep-alive y vuelve a pagar el TLS
# handshake contra la API en cada proceso. La clave incluye el timeout porque es
# lo único del cliente que puede variar entre llamadas dentro de una corrida.
#
# La API key forma parte de la clave. No se loguea nunca —ni acá ni en el
# `repr` de un error— y no sale de este módulo; es el mismo secreto que ya vive
# en `cfg` y en el entorno del proceso.
_clientes: dict[tuple[str, float], anthropic.Anthropic] = {}


def _cliente(cfg) -> anthropic.Anthropic:
    """Devuelve el cliente de Anthropic para `cfg`, construyéndolo una sola vez."""
    api_key = str(getattr(cfg, "anthropic_api_key", "") or "")
    timeout = float(getattr(cfg, "llm_timeout_s", 0) or TIMEOUT_DEFAULT_S)

    clave = (api_key, timeout)
    cliente = _clientes.get(clave)
    if cliente is None:
        cliente = anthropic.Anthropic(
            api_key=api_key,
            max_retries=MAX_REINTENTOS_SDK,
            timeout=timeout,
        )
        _clientes[clave] = cliente
        logger.debug(
            "llm/anthropic: cliente nuevo (timeout=%ss, max_retries=%s)",
            timeout, MAX_REINTENTOS_SDK,
        )
    return cliente


# ── Traducción del contenido ─────────────────────────────────────────────────


def _bloque_documento(bloque: BloqueDocumento, etapa: str) -> dict:
    """Traduce un `BloqueDocumento` leyendo el PDF **en este momento** y no antes.

    El import de `src.drilldown.pdf` es perezoso a propósito: arrastra PyMuPDF,
    que es la dependencia más pesada del repo, y el clasificador —la ruta que
    corre todos los días— no manda documentos nunca. Importarlo a nivel de
    módulo le cobraría ese arranque a todas las etapas para que lo use una.

    Este es el único sitio del sistema que sigue mandando el PDF **entero** al
    modelo, y el motivo de que `BloqueDocumento` transporte la ruta y no los
    bytes: acá el base64 vive el tiempo de armar el request, no el de toda la
    función que lo rodea.
    """
    from src.drilldown.pdf import read_pdf_b64

    try:
        datos_b64 = read_pdf_b64(bloque.ruta)
    except OSError as exc:
        # Un pliego que no se puede leer es un error de configuración o de
        # disco, no de la API. Se traduce igual a LLMError para que el
        # `except LLMError` del call site siga siendo la única captura que
        # necesita conocer.
        raise LLMError(
            f"no se pudo leer el PDF {bloque.ruta!r} para mandarlo al modelo: {exc}",
            proveedor=PROVEEDOR,
            etapa=etapa,
        ) from exc

    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": bloque.media_type,
            "data": datos_b64,
        },
    }


def _traducir_contenido(pedido: Pedido) -> list[dict]:
    """Convierte los bloques neutrales del pedido al formato de Anthropic.

    El orden se preserva tal cual: en `triage` las imágenes van **antes** del
    texto y en `analysis` el documento va **antes** del texto, y ese orden es
    parte del prompt, no un detalle de serialización.
    """
    contenido: list[dict] = []
    for bloque in pedido.contenido:
        if isinstance(bloque, BloqueTexto):
            contenido.append({"type": "text", "text": bloque.texto})
        elif isinstance(bloque, BloqueImagen):
            contenido.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": bloque.media_type,
                        "data": bloque.datos_b64,
                    },
                }
            )
        elif isinstance(bloque, BloqueDocumento):
            contenido.append(_bloque_documento(bloque, pedido.etapa))
        else:
            raise LLMError(
                f"tipo de bloque desconocido en el pedido: {type(bloque).__name__}",
                proveedor=PROVEEDOR,
                etapa=pedido.etapa,
            )
    return contenido


# ── Invocación ───────────────────────────────────────────────────────────────


def _invocar(cliente, kwargs: dict, max_tokens: int):
    """Ejecuta el request, eligiendo `create` o `stream` según `max_tokens`.

    **El criterio es `max_tokens`, no la etapa ni la presencia de un documento.**
    Hoy `analysis.py` usa `messages.stream(...)` + `get_final_message()` sin
    consumir un solo delta: el streaming no está ahí para mostrar texto, está
    para que la conexión no quede idle mientras el modelo genera una respuesta
    larga. Un request **no streameado** que tarda demasiado se corta por timeout
    HTTP —del lado del SDK o de cualquier proxy en el medio— y el SDK
    desaconseja explícitamente pedir salidas grandes sin streaming.

    Si la causa es la duración de la generación, el predictor correcto es cuánta
    salida se pidió. La etapa no lo es (mañana una etapa nueva pide 16k y nadie
    se acuerda de agregarla a la lista) y el `BloqueDocumento` tampoco (mide el
    tamaño de la **entrada**, que no es lo que hace largo al request: el triaje
    manda cinco imágenes y devuelve 512 tokens en segundos).

    El umbral de 8192 deja el corte con margen antes de los ~16.000 tokens a
    partir de los cuales el request no streameado es riesgoso, y reparte las
    cuatro etapas sin ambigüedad: triaje (512) y perfilado (300/1200) por
    `create`; análisis (16.000) y clasificación (16.384) por `stream`.

    ⚠️ La clasificación **gana** esta protección, que hoy no tiene: `engine.py`
    pide 16.384 tokens con `messages.create`. No es un efecto colateral que haya
    que corregir, es el mismo bug latente que `analysis` ya tenía resuelto.

    `get_final_message()` devuelve un `Message` con la misma forma que
    `create()` —`content`, `usage`, `stop_reason`—, así que de acá para abajo
    los dos caminos son indistinguibles.
    """
    if max_tokens < _UMBRAL_STREAM_TOKENS:
        return cliente.messages.create(**kwargs)

    with cliente.messages.stream(**kwargs) as flujo:
        return flujo.get_final_message()


def _texto_de(mensaje) -> str:
    """Primer bloque `type == "text"` de la respuesta, o cadena vacía.

    Vacío y no excepción: los tres call sites que parsean JSON ya tratan la
    respuesta vacía como un fallo de parseo con su propio mensaje, y lanzar acá
    los obligaría a aprender un segundo modo de falla para el mismo síntoma.
    """
    for bloque in getattr(mensaje, "content", None) or []:
        if getattr(bloque, "type", "") == "text":
            return getattr(bloque, "text", "") or ""
    return ""


# ── Fachada del adaptador ────────────────────────────────────────────────────


def completar(cfg, pedido: Pedido, *, cliente=None) -> Respuesta:
    """Ejecuta `pedido` contra Anthropic y devuelve una `Respuesta` normalizada.

    `cliente` viene en `None` en producción y se construye —y cachea— desde
    `cfg.anthropic_api_key`. Los tests inyectan un doble acá: cualquier objeto
    con `.messages.create(...)` (y `.messages.stream(...)` si el pedido supera
    el umbral de streaming) sirve, y se usa **tal cual**, sin tocar la caché.

    `pedido.sistema == ""` significa *sin system prompt*, que es el caso de
    `trainer.py`: manda todo en el turno del usuario. Se omite el parámetro en
    vez de pasar `system=""`, porque una cadena vacía no es lo mismo que la
    ausencia del campo y cambiaría el prefijo del prompt.
    """
    modelo = modelo_de(cfg, pedido.etapa)
    max_tokens = max_tokens_de(cfg, pedido.etapa, pedido.max_tokens)

    # Fuera del try: un fallo traduciendo el contenido ya sale como LLMError por
    # su cuenta y no tiene nada que ver con la API.
    contenido = _traducir_contenido(pedido)

    kwargs: dict = {
        "model": modelo,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": contenido}],
    }
    if pedido.sistema:
        kwargs["system"] = pedido.sistema

    if cliente is None:
        cliente = _cliente(cfg)

    try:
        mensaje = _invocar(cliente, kwargs, max_tokens)
    except anthropic.APIStatusError as exc:
        # La API respondió algo que no es 2xx. El código decide si tiene sentido
        # volver a intentar más tarde: un 429 sí, un 400 por un prompt mal
        # armado no va a mejorar por insistir.
        codigo = getattr(exc, "status_code", 0) or 0
        raise LLMError(
            f"la API de Anthropic respondió {codigo}: {exc}",
            proveedor=PROVEEDOR,
            etapa=pedido.etapa,
            reintentable=codigo in _HTTP_REINTENTABLES,
        ) from exc
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        # No hubo respuesta: red caída, DNS, o el request se pasó del timeout.
        # (`APITimeoutError` hereda de `APIConnectionError`; se nombran las dos
        # para que la intención quede escrita y no dependa de la jerarquía del
        # SDK, que puede cambiar entre versiones.)
        raise LLMError(
            f"no se pudo hablar con la API de Anthropic: {exc}",
            proveedor=PROVEEDOR,
            etapa=pedido.etapa,
            reintentable=True,
        ) from exc
    except anthropic.APIError as exc:
        # Red de contención del resto de la jerarquía. No debería entrar nada
        # acá, y justamente por eso existe: si el SDK agrega una subclase nueva,
        # sale como LLMError igual en vez de atravesar la capa y romper el
        # `except LLMError` de los call sites.
        raise LLMError(
            f"error inesperado del SDK de Anthropic ({type(exc).__name__}): {exc}",
            proveedor=PROVEEDOR,
            etapa=pedido.etapa,
        ) from exc

    return Respuesta(
        texto=_texto_de(mensaje),
        uso=Uso.desde_anthropic(getattr(mensaje, "usage", None)),
        modelo=modelo,
        proveedor=PROVEEDOR,
        truncada=getattr(mensaje, "stop_reason", None) == "max_tokens",
        # Sin normalizar, para que el registro de consumo pueda auditar la
        # traducción de arriba en vez de confiar en ella.
        uso_crudo=getattr(mensaje, "usage", None),
    )


def validar_configuracion(cfg) -> None:
    """Falla si falta la credencial de Anthropic. La llama `llm.validar_configuracion`.

    Hace falta desde que `Config.anthropic_api_key` pasó de ``os.environ[...]``
    a ``os.getenv(..., "")``: ese cambio era obligatorio —con
    ``LLM_PROVIDER=oci`` el `KeyError` mataba la app antes de poder elegir
    proveedor— pero movió el fallo desde la construcción de `Config` hasta la
    primera llamada al modelo. Sin esta función, un `.env` sin la clave se
    descubre a las 07:00, con la corrida del día ya perdida.
    """
    if not str(getattr(cfg, "anthropic_api_key", "") or "").strip():
        raise LLMError(
            "ANTHROPIC_API_KEY está vacía y LLM_PROVIDER=anthropic. Definila en el .env "
            "(ver .env.example) o cambiá LLM_PROVIDER para usar otro proveedor.",
            proveedor=PROVEEDOR,
        )


__all__ = ["PROVEEDOR", "completar", "validar_configuracion"]
