"""src.llm.contrato — el vocabulario neutral entre AMALIA y cualquier proveedor de LLM.

Existe por un problema concreto: AMALIA le habla a Anthropic desde **cinco
sitios**, cada uno con su propia instanciación del cliente, su propio ID de
modelo y su propia semántica de error. Para que esos cinco sitios puedan
apuntar a los modelos generativos de OCI con una sola variable de entorno,
primero hace falta un idioma que no sea el de ninguno de los dos.

Acá vive ese idioma: bloques de contenido, un `Pedido`, un `Uso`, una
`Respuesta` y un único tipo de error. Los adaptadores
(`proveedor_anthropic`, `proveedor_oci`) traducen a y desde estas formas; el
resto de la aplicación no conoce ninguna otra.

**Tres decisiones con intención, que no se pueden cambiar sin romper algo:**

1. **`Uso` expone exactamente `input_tokens` y `output_tokens`.**
   `UsoModelo.acumular()` (`src/scraper/models.py`) los lee por duck typing con
   `getattr(usage, "input_tokens", 0) or 0`. Si los nombres no coinciden, el
   método no falla: **suma cero y el gasto queda contabilizado como CERO en
   silencio**, dejando al sistema sin freno de presupuesto. Elegir estos dos
   nombres convierte ese acoplamiento accidental en contrato explícito, y de
   paso deja `test_pipeline.py` sin enterarse del cambio de capa.

2. **`BloqueDocumento` transporta la RUTA al PDF, no los bytes ni el base64.**
   Hoy `analysis.py` hace `read_pdf_b64(pdf_path)` de forma ansiosa: un pliego
   de 32 MB queda en RAM como base64 (~43 MB) dentro de una VM con
   `mem_limit: 800m`. Con la ruta, el adaptador de Anthropic lee el archivo
   cuando de verdad lo necesita y el de OCI **nunca** lo lee, porque OCI no
   tiene un bloque `document` equivalente y traduce a texto o a imágenes.

3. **Los discriminantes `tipo` son los strings de Anthropic** (`"text"`,
   `"image"`, `"document"`). No es pereza: deja
   `tests/test_triage_raster.py` como un diff de una línea (`b["type"]` →
   `b.tipo`) conservando **verbatim** sus dos aserciones de regresión —
   `tipos.count("image") == 3` y `"document" not in tipos`.

La traducción de nombres de `usage` ocurre en **un único punto**: los
constructores `Uso.desde_anthropic` / `Uso.desde_oci`. Cualquier otro lugar que
traduzca tokens es un segundo punto donde el gasto se puede perder.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Bloques de contenido ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class BloqueTexto:
    """Texto plano. El bloque que entienden los dos proveedores sin traducción."""

    texto: str
    tipo: str = "text"


@dataclass(frozen=True)
class BloqueImagen:
    """Imagen ya codificada en base64 (típicamente una página rasterizada).

    Se transporta el base64 y no la ruta, al revés que `BloqueDocumento`, porque
    la imagen **no existe como archivo**: sale de `pdf.rasterizar_paginas`, que
    renderiza en memoria. Anthropic la manda como `source.data`; OCI, como
    data-URI dentro de un `ImageContent` — medido y funcionando en la Tanda 0.
    """

    datos_b64: str
    media_type: str = "image/jpeg"
    tipo: str = "image"


@dataclass(frozen=True)
class BloqueDocumento:
    """Un PDF **por referencia**: la ruta local, no su contenido.

    `paginas` y `escaneado` los calcula el pipeline y viajan con el bloque
    porque el adaptador de OCI los necesita para decidir cómo traducirlo (capa
    de texto si es nativo, N imágenes si está escaneado) sin volver a abrir el
    PDF para averiguarlo.
    """

    ruta: str
    paginas: int = 0
    escaneado: bool = False
    media_type: str = "application/pdf"
    tipo: str = "document"


Bloque = BloqueTexto | BloqueImagen | BloqueDocumento


# ── Pedido y respuesta ───────────────────────────────────────────────────────


@dataclass
class Pedido:
    """Lo que un call site le pide al LLM, sin nombrar proveedor ni modelo.

    `etapa` es la que resuelve el ID del modelo (ver `src.llm.modelos`): el call
    site dice *para qué* llama, no *a quién*.

    `max_tokens` es el **deseado**, no el efectivo: cada adaptador lo acota a su
    propio techo. OCI rechaza con `400 Invalid 'maxTokens'` cualquier valor por
    encima de 4096, así que el recorte no es una optimización sino un requisito.
    """

    etapa: str
    contenido: list[Bloque]
    sistema: str = ""  # "" = sin system prompt (el caso de trainer.py)
    max_tokens: int = 1024


@dataclass
class Uso:
    """Tokens consumidos por una llamada, ya normalizados.

    ⚠️ **Los nombres de los campos son load-bearing.** `UsoModelo.acumular()`
    (`src/scraper/models.py`) los lee así:

        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0

    Es `getattr` con default, no acceso directo: si el objeto no expone esos dos
    nombres exactos, **no se levanta ninguna excepción**; se suma 0 y el gasto de
    la llamada desaparece del libro mayor sin dejar rastro. OCI reporta
    `prompt_tokens` / `completion_tokens`, así que sin esta normalización el
    presupuesto diario quedaría clavado en cero mientras la factura corre.
    Renombrar estos dos campos rompe el freno de gasto en silencio.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def desde_anthropic(cls, usage) -> Uso | None:
        """Normaliza el bloque `usage` de una respuesta de Anthropic.

        Devuelve `None` si la entrada es `None` — que es lo que espera
        `UsoModelo.acumular`, y significa "esta llamada no reportó consumo",
        distinto de "consumió cero".
        """
        return cls._normalizar(usage, "input_tokens", "output_tokens", proveedor="anthropic")

    @classmethod
    def desde_oci(cls, usage) -> Uso | None:
        """Normaliza el `usage` de OCI Generative AI, recuperando el razonamiento oculto.

        OCI nombra los contadores `prompt_tokens` y `completion_tokens`
        (**medido y confirmado** contra la API real en la Tanda 0). Esta
        traducción es el único punto del sistema donde ocurre.

        **Por qué no alcanza con `completion_tokens`.** Medido el 2026-09-19 en
        `us-chicago-1`, con el mismo prompt y `max_tokens=20`:

        | modelo                  | `prompt` | `completion` | `total` |
        |-------------------------|---------:|-------------:|--------:|
        | `meta.llama-4-scout`    |       39 |           20 |      59 |
        | `xai.grok-4.6`          |       39 |       **18** | **514** |
        | `google.gemini-2.5-flash` |      5 |     **None** |      43 |

        Las familias externas gastan tokens de **razonamiento** que no aparecen
        en `completion_tokens`: Grok facturó ~450 y reportó 18 — una
        subestimación de ~25×. Gemini directamente no reporta el campo. Es la
        misma clase de falla silenciosa de contabilidad que motivó este
        constructor, entrando por otra puerta.

        Por eso la regla:

            output = completion_tokens              si total falta o cierra exacto
            output = total_tokens - prompt_tokens   si la resta es MAYOR

        Con Llama las dos vías dan lo mismo (39+20=59), así que nada de lo que
        ya funciona cambia. **Esto SUBE el gasto contabilizado en las familias
        externas, y ése es el punto**: la guardia de presupuesto
        (`DAILY_BUDGET_USD`) existe para no sorprenderse con la factura, y un
        freno que mira un número 25× menor que el real no frena nada.

        La resta solo se adopta si es mayor, así que un `total_tokens` basura
        —ausente, no numérico, o menor que `prompt_tokens`— **nunca** produce un
        `output_tokens` negativo: se ignora y queda el valor reportado.
        """
        uso = cls._normalizar(usage, "prompt_tokens", "completion_tokens", proveedor="oci")
        if uso is None:
            return None
        uso.output_tokens = cls._salida_con_razonamiento(usage, uso)
        return uso

    @staticmethod
    def _salida_con_razonamiento(usage, uso: Uso) -> int:
        """`total - prompt` si recupera tokens de razonamiento; si no, lo reportado."""
        crudo = getattr(usage, "total_tokens", None)
        if crudo is None:
            return uso.output_tokens
        try:
            total = int(crudo)
        except (TypeError, ValueError):
            # Un `total_tokens` que no es un número es basura del proveedor, no
            # una señal: se descarta en silencio y manda `completion_tokens`.
            return uso.output_tokens

        implicita = total - uso.input_tokens
        if implicita <= uso.output_tokens:
            # Cierra exacto (Meta) o el total vino corrupto/menor: nada que
            # recuperar. La comparación con `<=` es la que evita los negativos.
            return uso.output_tokens

        logger.info(
            "uso: OCI reportó completion_tokens=%s pero total-prompt da %s; se contabilizan "
            "%s tokens de salida (razonamiento no reportado por la familia del modelo)",
            uso.output_tokens, implicita, implicita,
        )
        return implicita

    @classmethod
    def _normalizar(
        cls, usage, campo_entrada: str, campo_salida: str, *, proveedor: str
    ) -> Uso | None:
        if usage is None:
            return None
        entrada = int(getattr(usage, campo_entrada, 0) or 0)
        salida = int(getattr(usage, campo_salida, 0) or 0)
        if entrada == 0 and salida == 0:
            # Endurecimiento barato: el proveedor mandó un `usage` pero no los
            # campos que esperábamos. Sin esta línea, el cambio de nombre de un
            # contador río arriba se vería como "gasto: $0.00" y nadie lo
            # investigaría. Greppable en journalctl, igual que "ALERTA NO
            # ENTREGADA" en el canal de alertas.
            logger.warning(
                "uso: el objeto de %s no expone %s/%s — el gasto de esta llamada NO se "
                "está contabilizando (usage=%r)",
                proveedor, campo_entrada, campo_salida, usage,
            )
        return cls(input_tokens=entrada, output_tokens=salida)


@dataclass
class Respuesta:
    """Lo que devuelve cualquier proveedor, ya traducido.

    `truncada` se normaliza en los dos adaptadores (Anthropic:
    `stop_reason == "max_tokens"`; OCI: `finish_reason` en `{"length",
    "max_tokens"}`) porque hoy solo `engine.py` mira `stop_reason`: en
    `triage` y `analysis` un truncamiento se reporta como `parse_error`, como si
    el modelo hubiera dicho algo inválido. Con un techo de 4096 tokens eso
    dejaría de ser una rareza.
    """

    texto: str
    uso: Uso | None
    modelo: str
    proveedor: str
    truncada: bool = False


# ── Error ────────────────────────────────────────────────────────────────────


class LLMError(Exception):
    """El único error que cruza la frontera del paquete `src.llm`.

    El mapeo desde los errores crudos de cada SDK tiene que ser **total**: si un
    error de OCI se escapa sin traducir, el `except anthropic.APIError` de
    `engine.py` deja de atraparlo y la semántica de fallo cambia en silencio.
    Ése es el riesgo número uno de la migración, y por eso hay un solo tipo.

    `reintentable` lo decide el adaptador según el código HTTP o la clase de
    excepción, no quien lo atrapa: la política de reintento vive donde se conoce
    el protocolo.
    """

    def __init__(
        self,
        mensaje: str,
        *,
        proveedor: str = "",
        etapa: str = "",
        reintentable: bool = False,
    ) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.proveedor = proveedor
        self.etapa = etapa
        self.reintentable = reintentable

    def __str__(self) -> str:
        partes = [self.mensaje]
        campos = (("proveedor", self.proveedor), ("etapa", self.etapa))
        contexto = [f"{k}={v}" for k, v in campos if v]
        if self.reintentable:
            contexto.append("reintentable=sí")
        if contexto:
            partes.append(f"[{', '.join(contexto)}]")
        return " ".join(partes)


__all__ = [
    "Bloque",
    "BloqueDocumento",
    "BloqueImagen",
    "BloqueTexto",
    "LLMError",
    "Pedido",
    "Respuesta",
    "Uso",
]
