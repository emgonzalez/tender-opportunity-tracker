"""src.llm.proveedor_oci — el adaptador de OCI Generative AI.

Traduce el vocabulario neutral de `src.llm.contrato` al `GenericChatRequest` de
OCI y devuelve el camino de vuelta. Es el otro extremo de la perilla
`LLM_PROVIDER`; el gemelo de `proveedor_anthropic`.

── Lo que este adaptador NO puede hacer, dicho sin adornos ─────────────────────

**OCI no tiene un bloque `document`.** Anthropic recibe el PDF entero en base64
y lo rasteriza del lado servidor a su propia resolución. Acá hay dos caminos y
ninguno es equivalente:

| `BloqueDocumento` | Traducción | Herramienta |
|---|---|---|
| `escaneado=False` (NATIVO)   | **un** `BloqueTexto` con la capa de texto | `pdf.extract_text_first_pages` |
| `escaneado=True` (ESCANEADO) | **N** `BloqueImagen` data-URI JPEG         | `pdf.rasterizar_paginas` |

- Para **nativos** probablemente **mejora**: la cita textual (`clausula_ref`)
  sale de la capa de texto real y no de una transcripción visual, y cuesta una
  fracción.
- Para **escaneados es una PÉRDIDA REAL**: un pliego de 60 páginas se analiza
  viendo las primeras `OCI_ANALISIS_RASTER_PAGES` (20 por default). Lo que esté
  en la página 40 no existe para el modelo. Subir la perilla sube el costo y la
  presión de RAM linealmente, en una VM con `mem_limit: 800m`. Esto no es un
  detalle de implementación: es el riesgo #6 del plan y se mide con
  `validate_tracka_e2e.py --comparar` antes de encender el flag en cualquier
  tenant.

**`DocumentContent` queda FUERA de v1**, y no por pereza: toma una **URL**, no
base64. Usarlo obligaría a subir cada pliego a Object Storage y emitir un PAR
por análisis — justo la maquinaria que en este tenant está a medio camino
(`memoria/objstore.py` es código muerto, asume un Instance Principal que está
denegado, y `OBJSTORE_BUCKET` ni figura en `.env.example`). Sería apilar dos
incógnitas sobre una.

── Tres hechos MEDIDOS que condicionan el código ──────────────────────────────

1. **`import oci` NO lanza `ModuleNotFoundError` en este repo.** Hay un
   directorio `oci/` en la raíz (los scripts de despliegue) sin `__init__.py`,
   y `pyproject.toml` pone la raíz en `sys.path`. Sin el SDK instalado,
   `import oci` devuelve **la carpeta de scripts** y no falla; el error aparece
   después, como un `AttributeError` opaco en el primer submódulo. Por eso
   `_sdk()` importa **submódulos** y atrapa `ImportError` **y** `AttributeError`.
2. **La región de inferencia es `us-chicago-1`, no `us-ashburn-1`** (donde viven
   las VMs). GenAI no sirve inferencia en Ashburn y responde
   `404 Entity with key <modelo> not found`, que se lee igual que un ID mal
   escrito. `cfg.oci_region` ya trae el default correcto.
3. **El tope de salida es 4096 y se valida en el REQUEST**, con
   `400 Invalid 'maxTokens': Value is greater than maximum: 4096`. El clamp de
   `modelos.max_tokens_de` no es una mejora: sin él, `analisis` (16.000) y
   `clasificacion` (16.384) fallan en la primera llamada.

── Por qué `tenacity` acá y no en el adaptador de Anthropic ───────────────────

El SDK de Anthropic reintenta solo (`max_retries`); envolverlo multiplicaría las
esperas. El de OCI no tiene equivalente, y Oracle recomienda backoff exponencial
explícitamente para los modelos on-demand, cuyo throttling es dinámico.
`stop_after_attempt(4)` iguala el sobre de fallo de Anthropic **a propósito**:
si los dos proveedores no reintentaran lo mismo, cambiar `LLM_PROVIDER` también
cambiaría la tasa de errores observada y el benchmark no compararía nada.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.llm import modelos
from src.llm.contrato import (
    Bloque,
    BloqueDocumento,
    BloqueImagen,
    BloqueTexto,
    LLMError,
    Pedido,
    Respuesta,
    Uso,
)

logger = logging.getLogger(__name__)

PROVEEDOR = "oci"

# ── Vocabulario de alambre de OCI ────────────────────────────────────────────
#
# Los discriminantes de OCI van en MAYÚSCULAS ("TEXT", "IMAGE"), al revés que
# los de `contrato` (que son los de Anthropic, en minúsculas, por la razón que
# explica su docstring). La traducción entre ambos vocabularios ocurre acá y en
# ningún otro lado.

TIPO_TEXTO = "TEXT"
TIPO_IMAGEN = "IMAGE"

ROL_SISTEMA = "SYSTEM"
ROL_USUARIO = "USER"

API_FORMAT_GENERIC = "GENERIC"

# `finish_reason` que significan "me quedé sin presupuesto de salida". OCI
# devuelve "length"; se acepta también "max_tokens" porque el valor viene del
# modelo servido, no de OCI, y Llama y Cohere no lo escriben igual.
_FINISH_TRUNCADA = frozenset({"length", "max_tokens"})

# ── Reintentos y timeouts ────────────────────────────────────────────────────

_HTTP_REINTENTABLES = frozenset({429, 500, 502, 503, 504})

_INTENTOS = 4

# Módulo-global y no un literal en el decorador: los tests lo pisan con
# `wait_none()` para no dormir 30 segundos en CI. Por eso el `@retry` se arma
# dentro de la función y no al importar — un decorador aplicado a nivel de
# módulo congelaría este valor en el import y el monkeypatch no haría nada.
_ESPERA = wait_exponential_jitter(initial=1, max=30)

# (connect, read) en segundos. El connect corto distingue "la red no está" de
# "el modelo está pensando"; el read sale de `cfg.llm_timeout_s` porque el
# análisis es la llamada larga del pipeline.
_TIMEOUT_CONEXION_S = 15
_TIMEOUT_LECTURA_S = 600

# Cuántas páginas de texto se extraen de un PDF nativo cuando el pipeline no
# supo decir cuántas tiene. Generoso a propósito: la capa de texto es barata
# comparada con una imagen, y quedarse corto es perder el pliego.
_PAGINAS_TEXTO_SI_DESCONOCIDO = 60

# Espejo del default de `cfg.oci_analisis_raster_pages`, para que un cfg falso
# de test (un `SimpleNamespace` con tres campos) no tenga que declararlo.
_RASTER_PAGINAS_DEFAULT = 20


# ── Carga perezosa del SDK ───────────────────────────────────────────────────

_sdk_cache: Any = None


def _sdk():
    """Devuelve los símbolos del SDK de OCI, o lanza `LLMError` accionable.

    ⚠️ **No uses `import oci` a secas para detectar que falta el SDK.** El
    directorio `oci/` de la raíz del repo (scripts de despliegue, sin
    `__init__.py`) lo sombrea como *namespace package* PEP 420 y el import
    tiene éxito devolviendo la carpeta equivocada. Hay que importar un
    **submódulo** y atrapar también `AttributeError`, que es como se manifiesta
    el sombreado cuando el SDK real no está.

    Sin fallback, a diferencia de `memoria/objstore.py`: si `LLM_PROVIDER=oci`
    y el SDK no está, eso es un error de configuración, no una degradación.
    Degradar a Anthropic sería gastar en el proveedor que el operador creyó
    haber apagado.
    """
    global _sdk_cache
    if _sdk_cache is not None:
        return _sdk_cache

    try:
        import oci.config
        from oci.exceptions import ConnectTimeout, RequestException, ServiceError
        from oci.generative_ai_inference import GenerativeAiInferenceClient
        from oci.generative_ai_inference.models import (
            ChatDetails,
            GenericChatRequest,
            ImageContent,
            ImageUrl,
            OnDemandServingMode,
            SystemMessage,
            TextContent,
            UserMessage,
        )
        from oci.signer import Signer
    except (ImportError, AttributeError) as exc:
        # AttributeError: el `oci/` del repo sombrea al SDK (ver docstring).
        raise LLMError(
            "falta el SDK de OCI; instalalo con: pip install '.[oci]' "
            "— o volvé a LLM_PROVIDER=anthropic. "
            f"(causa: {type(exc).__name__}: {exc})",
            proveedor=PROVEEDOR,
        ) from exc

    import types

    _sdk_cache = types.SimpleNamespace(
        config=oci.config,
        Signer=Signer,
        Cliente=GenerativeAiInferenceClient,
        ChatDetails=ChatDetails,
        GenericChatRequest=GenericChatRequest,
        ImageContent=ImageContent,
        ImageUrl=ImageUrl,
        OnDemandServingMode=OnDemandServingMode,
        SystemMessage=SystemMessage,
        TextContent=TextContent,
        UserMessage=UserMessage,
        ServiceError=ServiceError,
        RequestException=RequestException,
        ConnectTimeout=ConnectTimeout,
    )
    return _sdk_cache


# ── Higiene de secretos ──────────────────────────────────────────────────────


def _sin_secretos(texto: str, *secretos: str) -> str:
    """Enmascara credenciales antes de que toquen el log.

    Mismo molde que `notifier/alerts._sin_secretos`, y por la misma razón: los
    logs de la VM se leen por `journalctl` sin ninguna protección. Acá el
    secreto es una **clave privada PEM** (`cfg.oci_key_content`), que el SDK
    podría llegar a incluir en el texto de una excepción de firma. Una clave
    privada no puede aparecer en un log ni truncada.
    """
    for secreto in secretos:
        if secreto and len(secreto) > 8:
            texto = texto.replace(secreto, "***")
    return texto


def _secretos_de(cfg) -> tuple[str, ...]:
    bruto = str(getattr(cfg, "oci_key_content", "") or "")
    # Las dos formas en que la clave puede existir en memoria: como viene del
    # `.env` (con `\n` literales) y ya desescapada.
    return (bruto, bruto.replace("\\n", "\n")) if bruto else ()


# ── Credenciales: dos caminos, con precedencia ───────────────────────────────


def _config_y_signer(cfg) -> tuple[dict, Any]:
    """Resuelve cómo firmar. Devuelve `(config_dict, signer_o_None)`.

    Precedencia deliberada, y es la misma que mira
    `Config.oci_credenciales_presentes`:

    1. **`OCI_KEY_CONTENT`** → `Signer(private_key_content=…)`. Es el camino de
       **la VM**: `04-app.sh` inyecta las cuatro variables en el `.env` desde
       `oci/state/`. No se eligió un archivo montado porque `botuser` es usuario
       de sistema en `Dockerfile` (uid < 1000) y usuario normal en
       `Dockerfile.oci` (uid 1000): un archivo `0600` con dueño fijo sería
       legible en una imagen y no en la otra.
    2. **`OCI_CONFIG_FILE`** (o `~/.oci/config`) → `oci.config.from_file(...)`.
       Es el camino de **la laptop del operador**, que ya tiene los perfiles
       `EMILIOGONZALEZ` y `MULTICOMPUTOS`.

    ⚠️ **`OCI_KEY_CONTENT` llega con `\\n` LITERALES.** `04-app.sh` escribe el
    PEM en una sola línea del `.env`, sin comillas; está verificado de punta a
    punta que ni `python-dotenv` ni `docker run --env-file` lo desescapan. Sin
    el `.replace()` de abajo, `Signer` recibe una línea sola y falla al parsear
    la clave con un error que no dice nada de esto.

    `cfg.oci_region` **pisa** siempre la región del archivo de perfiles: el
    tenant vive en Ashburn y la inferencia solo existe en Chicago, así que el
    perfil de la laptop casi nunca apunta a donde hay que llamar.
    """
    sdk = _sdk()
    region = str(getattr(cfg, "oci_region", "") or "").strip()

    clave = str(getattr(cfg, "oci_key_content", "") or "").strip()
    if clave:
        pem = clave.replace("\\n", "\n")
        signer = sdk.Signer(
            tenancy=str(getattr(cfg, "oci_tenancy_ocid", "") or ""),
            user=str(getattr(cfg, "oci_user_ocid", "") or ""),
            fingerprint=str(getattr(cfg, "oci_fingerprint", "") or ""),
            private_key_content=pem,
        )
        logger.debug("oci: firmando con OCI_KEY_CONTENT (camino de la VM), región=%s", region)
        return ({"region": region}, signer)

    archivo = str(getattr(cfg, "oci_config_file", "") or "").strip()
    perfil = str(getattr(cfg, "oci_config_profile", "") or "DEFAULT").strip() or "DEFAULT"
    desde_archivo: dict
    if archivo:
        desde_archivo = dict(sdk.config.from_file(file_location=archivo, profile_name=perfil))
    else:
        desde_archivo = dict(sdk.config.from_file(profile_name=perfil))
    if region:
        desde_archivo["region"] = region
    logger.debug(
        "oci: firmando con el perfil %s de %s, región=%s",
        perfil, archivo or "~/.oci/config", desde_archivo.get("region"),
    )
    return (desde_archivo, None)


# ── Cliente cacheado ─────────────────────────────────────────────────────────

# Clave: (región, timeout de lectura, huella de la identidad). El plan pedía
# cachear por (región, timeout) para no reconstruir el signer en cada llamada de
# un lote —construirlo parsea la clave RSA—; se le agrega la identidad porque
# `validate_tracka_e2e.py --comparar` corre dos configuraciones en el mismo
# proceso y un cliente cacheado por región sola las mezclaría en silencio. La
# huella es el fingerprint o el nombre del perfil: **nunca la clave**.
_CLIENTES: dict[tuple[str, int, str], Any] = {}


def _timeout_de(cfg) -> int:
    return int(getattr(cfg, "llm_timeout_s", 0) or _TIMEOUT_LECTURA_S)


def _huella_de(cfg) -> str:
    return str(
        getattr(cfg, "oci_fingerprint", "")
        or getattr(cfg, "oci_config_profile", "")
        or "DEFAULT"
    )


def _cliente(cfg):
    """Devuelve el `GenerativeAiInferenceClient`, construyéndolo una sola vez."""
    sdk = _sdk()
    clave = (
        str(getattr(cfg, "oci_region", "") or ""),
        _timeout_de(cfg),
        _huella_de(cfg),
    )
    existente = _CLIENTES.get(clave)
    if existente is not None:
        return existente

    config, signer = _config_y_signer(cfg)
    timeout = (_TIMEOUT_CONEXION_S, clave[1])
    if signer is not None:
        cliente = sdk.Cliente(config, signer=signer, timeout=timeout)
    else:
        cliente = sdk.Cliente(config, timeout=timeout)
    _CLIENTES[clave] = cliente
    return cliente


# ── Traducción de contenido: FUNCIONES PURAS, sin el SDK ─────────────────────
#
# La traducción devuelve **dicts en la forma de alambre de OCI**, no objetos del
# SDK, y la conversión dict → objeto vive aparte (`_a_objetos`). No es un rodeo:
# es lo que hace inspeccionable al adaptador. Del lado de Anthropic,
# `tests/test_triage_raster.py` puede afirmar `tipos.count("image") == 3` y
# `"document" not in tipos` porque el payload es una lista de dicts; sin esta
# separación, la misma pregunta del lado de OCI exigiría el SDK instalado y
# reflexión sobre objetos generados. El equivalente de `_build_user_content`.


def _data_uri(media_type: str, datos_b64: str) -> str:
    """`data:<media_type>;base64,<b64>` — la forma que come `ImageUrl.url`.

    Medido en la Tanda 0: los dos modelos Meta del tenant **transcribieron** la
    frase de un PDF rasterizado mandado así. Era el riesgo #1 del plan (sin
    esto, el 66% del corpus que está escaneado no es portable a OCI, porque no
    hay OCR en el repo) y quedó descartado.
    """
    return f"data:{media_type};base64,{datos_b64}"


def _pdf():
    """Import perezoso de `src.drilldown.pdf`.

    Perezoso porque `pdf` arrastra PyMuPDF, y la mayor parte de los call sites
    de este adaptador —el clasificador, el perfilado— no tocan un PDF en toda
    su vida. Importarlo a nivel de módulo le cobraría a `src.llm` una
    dependencia pesada que casi nunca usa.
    """
    from src.drilldown import pdf

    return pdf


def _documento_como_texto(cfg, bloque: BloqueDocumento) -> list[dict]:
    """PDF NATIVO → **un** bloque de texto con su capa de texto real."""
    paginas = bloque.paginas if bloque.paginas > 0 else _PAGINAS_TEXTO_SI_DESCONOCIDO
    texto = _pdf().extract_text_first_pages(bloque.ruta, paginas)
    if not (texto or "").strip():
        # No se lanza: puede ser un PDF mal clasificado como nativo. El modelo
        # verá un bloque vacío y responderá lo que pueda; el log deja la pista.
        logger.warning(
            "oci: %s se declaró NATIVO pero su capa de texto vino vacía; "
            "el modelo va a recibir un bloque sin contenido",
            bloque.ruta,
        )
    return [{"type": TIPO_TEXTO, "text": texto or ""}]


def _documento_como_imagenes(cfg, bloque: BloqueDocumento) -> list[dict]:
    """PDF ESCANEADO → N imágenes data-URI JPEG. Ver la pérdida en el docstring."""
    tope = int(getattr(cfg, "oci_analisis_raster_pages", 0) or _RASTER_PAGINAS_DEFAULT)
    paginas = min(tope, bloque.paginas) if bloque.paginas > 0 else tope
    if bloque.paginas > tope:
        logger.warning(
            "oci: %s tiene %s páginas escaneadas y solo se mandan las primeras %s "
            "(OCI_ANALISIS_RASTER_PAGES); lo que esté más adelante no existe para el modelo",
            bloque.ruta, bloque.paginas, tope,
        )
    imagenes = _pdf().rasterizar_paginas(bloque.ruta, paginas)
    return [
        {"type": TIPO_IMAGEN, "image_url": {"url": _data_uri("image/jpeg", b64)}}
        for b64 in imagenes
    ]


# Política declarativa por formato, en el estilo de la tabla `_PRIORIDAD` de
# `drilldown/locator.py`: la decisión se lee de un vistazo y no está enterrada
# en un `if` a mitad de una función.
_POLITICA_DOCUMENTO: dict[bool, Callable[[Any, BloqueDocumento], list[dict]]] = {
    False: _documento_como_texto,  # NATIVO   → capa de texto
    True: _documento_como_imagenes,  # ESCANEADO → páginas rasterizadas
}


def traducir_bloque(cfg, bloque: Bloque) -> list[dict]:
    """Un bloque neutral → los bloques de alambre de OCI que lo representan.

    Devuelve una **lista** porque la correspondencia no es 1:1: un
    `BloqueDocumento` escaneado se abre en N imágenes.
    """
    if isinstance(bloque, BloqueTexto):
        return [{"type": TIPO_TEXTO, "text": bloque.texto}]
    if isinstance(bloque, BloqueImagen):
        return [
            {
                "type": TIPO_IMAGEN,
                "image_url": {"url": _data_uri(bloque.media_type, bloque.datos_b64)},
            }
        ]
    if isinstance(bloque, BloqueDocumento):
        return _POLITICA_DOCUMENTO[bool(bloque.escaneado)](cfg, bloque)
    raise LLMError(
        f"bloque de contenido no soportado por el adaptador de OCI: {type(bloque).__name__}",
        proveedor=PROVEEDOR,
    )


def traducir_contenido(cfg, contenido: list[Bloque]) -> list[dict]:
    """Aplana la traducción de todos los bloques de un pedido."""
    salida: list[dict] = []
    for bloque in contenido or []:
        salida.extend(traducir_bloque(cfg, bloque))
    return salida


def traducir_pedido(cfg, pedido: Pedido) -> dict:
    """`Pedido` → el request de OCI entero, en forma de alambre.

    Sale un dict y no un `ChatDetails` a propósito (ver el comentario de la
    sección): esto se puede afirmar en un test sin el SDK instalado.

    `pedido.sistema` vacío ⇒ **no se emite mensaje de sistema**. No es un
    detalle: es el caso de `classifier/trainer.py`, que llama sin `system`, y
    mandar un `SystemMessage` con `""` cambia el prompt de una etapa que hoy
    funciona.
    """
    mensajes: list[dict] = []
    sistema = (pedido.sistema or "").strip()
    if sistema:
        mensajes.append(
            {"role": ROL_SISTEMA, "content": [{"type": TIPO_TEXTO, "text": pedido.sistema}]}
        )
    mensajes.append({"role": ROL_USUARIO, "content": traducir_contenido(cfg, pedido.contenido)})

    return {
        "api_format": API_FORMAT_GENERIC,
        "compartment_id": str(getattr(cfg, "oci_genai_compartment_ocid", "") or ""),
        "model_id": modelos.modelo_de(cfg, pedido.etapa),
        # El clamp a 4096 es OBLIGATORIO: por encima la API responde 400 al
        # enviar. `max_tokens_de` es el único lugar donde vive ese número.
        "max_tokens": modelos.max_tokens_de(cfg, pedido.etapa, pedido.max_tokens),
        "messages": mensajes,
    }


# ── Forma de alambre → objetos del SDK ───────────────────────────────────────


def _a_contenido_sdk(bloques: list[dict]) -> list:
    sdk = _sdk()
    salida = []
    for b in bloques:
        if b["type"] == TIPO_TEXTO:
            salida.append(sdk.TextContent(text=b["text"]))
        elif b["type"] == TIPO_IMAGEN:
            salida.append(sdk.ImageContent(image_url=sdk.ImageUrl(url=b["image_url"]["url"])))
        else:  # pragma: no cover — `traducir_bloque` no puede producir otra cosa
            raise LLMError(
                f"tipo de bloque de alambre desconocido: {b['type']!r}", proveedor=PROVEEDOR
            )
    return salida


def _a_objetos(peticion: dict):
    """El dict de `traducir_pedido` → el `ChatDetails` que come `cliente.chat()`."""
    sdk = _sdk()
    mensajes = []
    for m in peticion["messages"]:
        contenido = _a_contenido_sdk(m["content"])
        if m["role"] == ROL_SISTEMA:
            mensajes.append(sdk.SystemMessage(role=ROL_SISTEMA, content=contenido))
        else:
            mensajes.append(sdk.UserMessage(role=ROL_USUARIO, content=contenido))

    chat_request = sdk.GenericChatRequest(
        api_format=sdk.GenericChatRequest.API_FORMAT_GENERIC,
        messages=mensajes,
        max_tokens=peticion["max_tokens"],
    )
    return sdk.ChatDetails(
        compartment_id=peticion["compartment_id"],
        serving_mode=sdk.OnDemandServingMode(
            serving_type="ON_DEMAND", model_id=peticion["model_id"]
        ),
        chat_request=chat_request,
    )


# ── Respuesta ────────────────────────────────────────────────────────────────


def _texto_de(chat_response) -> str:
    """Concatena el texto de todos los bloques de todos los `choices`.

    ⚠️ **`choice.message` puede venir `None`, y `choices` puede venir `None` o
    vacío.** No es hipotético: medido el 2026-09-19 contra
    `google.gemini-2.5-flash` con `max_tokens=20`, que devuelve
    `finish_reason=max_tokens`, `message=None` y texto vacío — gastó el
    presupuesto entero en tokens de razonamiento antes de emitir un solo
    carácter. Un acceso directo a `choice.message.content` explota con
    `AttributeError` justo ahí.

    Por eso los tres niveles se leen con `getattr(..., None)` y se siguen de
    largo: el caso correcto es una `Respuesta(texto="", truncada=True)`, no una
    excepción. Los call sites ya saben qué hacer con texto vacío (el triaje
    reintenta, el análisis cae a `ERROR_LLM`), y `truncada=True` hace que el log
    diga la verdad —se cortó por tope— en vez de culpar al parseo.
    """
    partes: list[str] = []
    for choice in getattr(chat_response, "choices", None) or []:
        mensaje = getattr(choice, "message", None)
        # `getattr(None, "content", None)` es None, no una excepción: un choice
        # sin mensaje no interrumpe la lectura de los que sí lo traen.
        for bloque in (getattr(mensaje, "content", None) or []):
            texto = getattr(bloque, "text", None)
            if texto:
                partes.append(texto)
    return "".join(partes)


def _truncada(chat_response) -> bool:
    """¿El modelo cortó por tope de salida?

    Se normaliza acá, igual que Anthropic normaliza `stop_reason ==
    "max_tokens"`, porque hoy solo `engine.py` mira el motivo de corte: en
    `triage` y `analysis` un truncamiento se reporta como `parse_error`, como si
    el modelo hubiera dicho algo inválido. Con un techo de 4096 tokens eso
    dejaría de ser una rareza y se volvería el modo de falla habitual.
    """
    for choice in getattr(chat_response, "choices", None) or []:
        motivo = getattr(choice, "finish_reason", None)
        if motivo and str(motivo).strip().lower() in _FINISH_TRUNCADA:
            return True
    return False


def _respuesta_de(datos, *, modelo: str, etapa: str) -> Respuesta:
    """`resp.data.chat_response` → `Respuesta` neutral."""
    chat_response = getattr(getattr(datos, "data", None), "chat_response", None)
    if chat_response is None:
        raise LLMError(
            "la respuesta de OCI no trae `data.chat_response`: "
            f"llegó {type(datos).__name__}",
            proveedor=PROVEEDOR,
            etapa=etapa,
        )
    return Respuesta(
        texto=_texto_de(chat_response),
        # Único punto del sistema donde prompt_tokens/completion_tokens se
        # traducen a input/output_tokens. Sin esto el libro mayor queda en cero
        # mientras la factura corre (ver el docstring de `contrato.Uso`).
        uso=Uso.desde_oci(getattr(chat_response, "usage", None)),
        modelo=modelo,
        proveedor=PROVEEDOR,
        truncada=_truncada(chat_response),
    )


# ── Errores: el mapeo tiene que ser TOTAL ────────────────────────────────────


def _traducir_error(exc: BaseException, *, cfg, etapa: str) -> LLMError:
    """Cualquier excepción cruda → `LLMError`. Sin excepciones, literalmente.

    Si un error de OCI se escapa sin traducir, el `except anthropic.APIError` de
    `engine.py` deja de atraparlo y la semántica de fallo del sistema cambia en
    silencio. Es el riesgo #1 de la migración, y por eso el `except Exception`
    final no es pereza: es la red de contención que lo vuelve total.
    """
    secretos = _secretos_de(cfg)
    detalle = _sin_secretos(f"{type(exc).__name__}: {exc}", *secretos)

    sdk = _sdk_cache
    if sdk is not None:
        if isinstance(exc, sdk.ServiceError):
            estado = getattr(exc, "status", 0) or 0
            return LLMError(
                f"OCI GenAI respondió HTTP {estado}: {detalle}",
                proveedor=PROVEEDOR,
                etapa=etapa,
                reintentable=estado in _HTTP_REINTENTABLES,
            )
        if isinstance(exc, (sdk.ConnectTimeout, sdk.RequestException)):
            # ConnectTimeout NO hereda de RequestException en este SDK
            # (verificado): van las dos en la tupla, no una sola.
            return LLMError(
                f"no se pudo hablar con OCI GenAI: {detalle}",
                proveedor=PROVEEDOR,
                etapa=etapa,
                reintentable=True,
            )

    return LLMError(
        f"fallo no clasificado al llamar a OCI GenAI: {detalle}",
        proveedor=PROVEEDOR,
        etapa=etapa,
        reintentable=False,
    )


def _es_reintentable(exc: BaseException) -> bool:
    return isinstance(exc, LLMError) and exc.reintentable


def _chat_con_reintentos(cliente, detalles, *, cfg, etapa: str):
    """Llama a `cliente.chat()` con backoff exponencial y jitter.

    El `@retry` se arma acá adentro y no en el decorador del módulo para que
    `_ESPERA` y `_INTENTOS` se lean **en cada llamada**: así un test los puede
    pisar con `wait_none()` y no dormir medio minuto en CI.
    """

    @retry(
        retry=retry_if_exception(_es_reintentable),
        stop=stop_after_attempt(_INTENTOS),
        wait=_ESPERA,
        reraise=True,
    )
    def _intento():
        try:
            return cliente.chat(detalles)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 — el mapeo a LLMError debe ser TOTAL
            raise _traducir_error(exc, cfg=cfg, etapa=etapa) from exc

    return _intento()


# ── Fachada del adaptador ────────────────────────────────────────────────────


def completar(cfg, pedido: Pedido, *, cliente=None) -> Respuesta:
    """Ejecuta `pedido` contra OCI Generative AI y devuelve una `Respuesta`.

    `cliente` es la inyección de los tests y de quien ya tenga un cliente
    construido; si viene en `None` se usa el cacheado por (región, timeout,
    identidad).
    """
    etapa = pedido.etapa
    modelo = modelos.modelo_de(cfg, etapa)
    peticion = traducir_pedido(cfg, pedido)

    try:
        detalles = _a_objetos(peticion)
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 — el mapeo a LLMError debe ser TOTAL
        raise _traducir_error(exc, cfg=cfg, etapa=etapa) from exc

    cli = cliente if cliente is not None else _cliente(cfg)

    logger.debug(
        "oci: etapa=%s modelo=%s bloques=%s max_tokens=%s",
        etapa, modelo, len(peticion["messages"][-1]["content"]), peticion["max_tokens"],
    )

    datos = _chat_con_reintentos(cli, detalles, cfg=cfg, etapa=etapa)

    try:
        return _respuesta_de(datos, modelo=modelo, etapa=etapa)
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 — el mapeo a LLMError debe ser TOTAL
        raise _traducir_error(exc, cfg=cfg, etapa=etapa) from exc


def validar_configuracion(cfg) -> None:
    """Falla al arrancar si esta VM no puede hablarle a OCI GenAI.

    Chequea **presencia**, no validez: una clave vencida pasa por acá y falla en
    la primera llamada, que es donde corresponde enterarse. Lo que se evita es
    lo otro — descubrir a las 07:00, con la corrida diaria ya empezada, que
    nunca se inyectó el compartment o que la imagen se construyó sin el extra
    `oci`. Las corridas diarias no se repiten.
    """
    _sdk()  # lanza LLMError accionable si falta el extra

    faltantes: list[str] = []
    if not str(getattr(cfg, "oci_genai_compartment_ocid", "") or "").strip():
        faltantes.append("OCI_GENAI_COMPARTMENT_OCID")
    if not str(getattr(cfg, "oci_region", "") or "").strip():
        faltantes.append("OCI_REGION (us-chicago-1 — GenAI NO sirve inferencia en Ashburn)")

    clave = str(getattr(cfg, "oci_key_content", "") or "").strip()
    if clave:
        for campo, var in (
            ("oci_tenancy_ocid", "OCI_TENANCY_OCID"),
            ("oci_user_ocid", "OCI_USER_OCID"),
            ("oci_fingerprint", "OCI_FINGERPRINT"),
        ):
            if not str(getattr(cfg, campo, "") or "").strip():
                faltantes.append(f"{var} (obligatoria junto con OCI_KEY_CONTENT)")
    else:
        ruta = str(getattr(cfg, "oci_config_file", "") or "").strip() or os.path.expanduser(
            "~/.oci/config"
        )
        if not os.path.isfile(ruta):
            faltantes.append(
                f"OCI_KEY_CONTENT o un archivo de perfiles en {ruta} "
                "(no hay con qué firmar la llamada)"
            )

    if faltantes:
        raise LLMError(
            "LLM_PROVIDER=oci pero falta configuración: " + "; ".join(faltantes),
            proveedor=PROVEEDOR,
        )

    logger.info(
        "llm: OCI GenAI configurado — región=%s compartment=…%s credencial=%s",
        getattr(cfg, "oci_region", ""),
        str(getattr(cfg, "oci_genai_compartment_ocid", ""))[-6:],
        "OCI_KEY_CONTENT" if clave else f"perfil {getattr(cfg, 'oci_config_profile', 'DEFAULT')}",
    )


__all__ = [
    "PROVEEDOR",
    "completar",
    "traducir_bloque",
    "traducir_contenido",
    "traducir_pedido",
    "validar_configuracion",
]
