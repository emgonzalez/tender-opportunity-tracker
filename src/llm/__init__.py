"""src.llm — la única puerta por la que AMALIA le habla a un modelo de lenguaje.

Antes de este paquete había **cinco** puertas: `drilldown/triage.py`,
`drilldown/analysis.py`, `classifier/engine.py` y dos llamadas en
`classifier/trainer.py`, cada una instanciando su propio cliente, con su propio
ID de modelo (tres de ellos hardcodeados) y su propia forma de atrapar errores.
Acá quedan todas reducidas a `llm.completar(cfg, pedido)`, y cambiar de
proveedor pasa a ser una variable de entorno.

**Funciones y una tabla de despacho, no `Protocol` ni ABC ni factory.** Dos
razones, las dos del repo y no de gusto:

- No hay un solo `Protocol` en todo `src/`. El molde más cercano es
  `notifier/alerts.py`: callables resueltos por nombre en el punto de uso, sin
  jerarquía de clases. La única diferencia semántica es que `alerts` **itera
  todos** los canales y el LLM **elige uno** — por eso acá es un `dict` y no una
  tupla de pares.
- El idioma de la suite es `monkeypatch.setattr` sobre la función propia de más
  alto nivel, tal como la ve el módulo que la importa (`pipeline.triage.triage`,
  catorce veces en `test_pipeline.py`). Si los call sites hacen
  `from src import llm` y llaman `llm.completar(...)`, un test parchea
  `triage.llm.completar` y encaja sin aprender nada nuevo. Un `Protocol`
  obligaría a construir instancias en los tests: fricción nueva a cambio de
  nada, porque mypy no está en CI y el `select` de ruff es `E4/E7/E9/F/I`.

**Los adaptadores se importan perezosamente**, dentro de `completar`, y el
`dict` guarda nombres de módulo y de función en vez de callables. `proveedor_oci`
arrastra el SDK de OCI, que es un **extra opcional** (~100 MB de clientes
generados + cryptography/pyOpenSSL): importarlo a nivel de módulo rompería el
arranque en cualquier imagen construida sin ese extra, que son todas menos la de
dev. Los módulos se llaman `proveedor_*` y no `anthropic.py`/`oci.py` para no
sombrear los paquetes pip homónimos desde adentro de `src/llm/`.

**Sin fallback, a diferencia de `memoria/objstore.py`** — y esto va escrito acá
porque la divergencia es deliberada: `objstore` degrada a disco local si falta
el SDK de OCI. El LLM no. Si `LLM_PROVIDER=oci` y el SDK no está, eso es un
error de configuración, no una degradación, y degradar a Anthropic significaría
gastar en el proveedor que el operador creyó haber apagado.
`validar_configuracion(cfg)` lo detecta **al arrancar**, no en la primera
llamada del pipeline a las 07:00.
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

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
from src.llm.modelos import ETAPAS, PROVEEDORES, max_tokens_de, modelo_de, proveedor_activo

logger = logging.getLogger(__name__)

# proveedor → (módulo, función). Nombres, no callables: la resolución es
# perezosa (ver docstring del módulo) y así la tabla se puede leer, testear y
# extender sin importar ningún SDK.
_PROVEEDORES: dict[str, tuple[str, str]] = {
    "anthropic": ("src.llm.proveedor_anthropic", "completar"),
    "oci": ("src.llm.proveedor_oci", "completar"),
}

_PISTA_EXTRA = {
    "oci": 'falta el SDK de OCI. Instalalo con: pip install ".[oci]"',
    "anthropic": "falta el paquete `anthropic`. Instalalo con: pip install .",
}

# Familia de OCI → el tercero, con el nombre que usa para facturar y para
# aparecer en una política de privacidad. El log tiene que nombrarlo: "familia
# externa" no le dice nada a nadie; "servidores de xAI" sí.
_TERCERO_DE_FAMILIA: dict[str, str] = {
    "xai": "xAI",
    "google": "Google",
}


# ── Resolución perezosa del adaptador ────────────────────────────────────────


def _importar(proveedor: str, etapa: str = ""):
    """Importa el módulo del adaptador, traduciendo cualquier fallo a `LLMError`.

    Distingue dos causas que se ven igual desde afuera y se arreglan distinto:
    que **falte el módulo del adaptador** (código, no entorno) y que falte una
    dependencia que ese módulo importa — típicamente el SDK de OCI, que es un
    extra opcional. El operador lee `journalctl` y tiene que saber si le toca
    instalar algo o si el despliegue vino incompleto, sin abrir el código.
    """
    nombre_modulo = _PROVEEDORES[proveedor][0]
    try:
        return importlib.import_module(nombre_modulo)
    except ImportError as exc:
        if getattr(exc, "name", "") == nombre_modulo:
            pista = f"el adaptador {nombre_modulo} no está en esta instalación"
        else:
            pista = _PISTA_EXTRA.get(proveedor, "")
        raise LLMError(
            f"no se pudo cargar el adaptador de {proveedor} ({nombre_modulo}): {exc}. {pista}",
            proveedor=proveedor,
            etapa=etapa,
        ) from exc


def _adaptador(proveedor: str, etapa: str = "") -> Callable[..., Respuesta]:
    """Devuelve la función `completar` del proveedor, importándola al vuelo."""
    if proveedor not in _PROVEEDORES:
        raise LLMError(
            f"proveedor de LLM desconocido: {proveedor!r}. "
            f"LLM_PROVIDER admite {', '.join(sorted(_PROVEEDORES))}",
            proveedor=proveedor,
            etapa=etapa,
        )

    nombre_modulo, nombre_funcion = _PROVEEDORES[proveedor]
    modulo = _importar(proveedor, etapa)
    funcion = getattr(modulo, nombre_funcion, None)
    if funcion is None:
        raise LLMError(
            f"{nombre_modulo} no expone {nombre_funcion}(): el adaptador está incompleto",
            proveedor=proveedor,
            etapa=etapa,
        )
    return funcion


# ── Gobernanza del dato: adónde salen los pliegos ────────────────────────────


def _familia_de_oci(cfg) -> str:
    """La familia de modelos de OCI activa, o `""` si esta rama todavía no la tiene.

    Se resuelve con `getattr` sobre el módulo y no con un `import` directo a
    propósito: `modelos.familia_activa` llega con la Tanda A de la Fase 9b y
    este archivo se escribió en paralelo. Mientras no exista, la gobernanza no
    tiene nada que decir —solo hay una familia, la nativa— y el arranque no
    debe romperse por eso.
    """
    resolver = getattr(modelos, "familia_activa", None)
    if resolver is None:
        return ""
    return resolver(cfg)


def _validar_familia(cfg, proveedor: str) -> None:
    """Rechaza una `OCI_FAMILIA` desconocida y avisa si la familia es externa.

    **Solo cuando el proveedor activo es OCI.** Es la misma regla que gobierna
    el resto de `validar_configuracion`: una VM que eligió Anthropic no tiene
    por qué caerse porque en su `.env` quedó una `OCI_FAMILIA` vieja de un
    experimento que ya no corre.

    La familia desconocida **falla**, igual que un `LLM_PROVIDER` desconocido y
    por la misma razón: si `OCI_FAMILIA=gogle` se resolviera en silencio a
    `nativo`, el operador creería estar midiendo Gemini durante semanas
    mirando números de Llama.

    La familia externa **no falla**: es una decisión informada del operador, no
    un error. Lo que hace falta es que quede escrita en el log de la corrida.
    """
    if proveedor != "oci":
        return

    familia = _familia_de_oci(cfg)
    if not familia:
        return

    conocidas = tuple(getattr(modelos, "FAMILIAS", ()) or ())
    if conocidas and familia not in conocidas:
        raise LLMError(
            f"OCI_FAMILIA={familia!r} no es una familia conocida. "
            f"Valores admitidos: {', '.join(sorted(conocidas))}",
            proveedor=proveedor,
        )

    externas = frozenset(getattr(modelos, "FAMILIAS_EXTERNAS", frozenset()))
    if familia not in externas:
        return

    tercero = _TERCERO_DE_FAMILIA.get(familia, familia)
    default = getattr(modelos, "FAMILIA_DEFAULT", "nativo")
    logger.warning(
        "GOBERNANZA DE DATOS: OCI_FAMILIA=%s — la inferencia NO corre en la infraestructura "
        "de OCI. Los pliegos de los clientes (su texto completo, y las páginas escaneadas "
        "como imágenes) salen por llamadas externas a servidores de %s, un tercero que "
        "ningún contrato con los clientes contempla. Es una decisión del operador y no se "
        "bloquea; si no era la intención, poné OCI_FAMILIA=%s y volvé a desplegar.",
        familia, tercero, default,
    )


# ── Fachada ──────────────────────────────────────────────────────────────────


def completar(cfg, pedido: Pedido, *, cliente=None) -> Respuesta:
    """Ejecuta `pedido` contra el proveedor activo y devuelve una `Respuesta`.

    `cliente` es la inyección para los tests y para reutilizar un cliente ya
    construido; cada adaptador sabe qué hacer con el suyo y lo construye si
    viene en `None`.
    """
    proveedor = proveedor_activo(cfg)
    funcion = _adaptador(proveedor, pedido.etapa)
    logger.debug("llm: etapa=%s proveedor=%s", pedido.etapa, proveedor)
    return funcion(cfg, pedido, cliente=cliente)


def validar_configuracion(cfg) -> None:
    """Falla rápido y ruidoso si el proveedor activo no puede funcionar.

    Se llama **al arrancar** (`src/main.py`), no en el primer uso: el costo de
    descubrir a las 07:00 que falta una credencial es una corrida diaria
    perdida, y las corridas diarias no se repiten.

    Valida **solo el proveedor activo**. Exigir las credenciales de los dos
    obligaría a tener una clave de Anthropic en una VM que decidió no usarlo.

    Un `LLM_PROVIDER` desconocido **falla acá**, nunca degrada en silencio a
    Anthropic: un typo que se resolviera solo dejaría al sistema gastando en el
    proveedor equivocado durante semanas, sin ningún síntoma visible más que la
    factura.

    También es el lugar donde se deja constancia de **adónde salen los pliegos**:
    con `LLM_PROVIDER=oci` y una familia externa (`xai`, `google`) la inferencia
    no corre en OCI sino en servidores de un tercero. Ver `_validar_familia`.
    """
    proveedor = proveedor_activo(cfg)
    if proveedor not in _PROVEEDORES:
        raise LLMError(
            f"LLM_PROVIDER={proveedor!r} no es un proveedor conocido. "
            f"Valores admitidos: {', '.join(sorted(_PROVEEDORES))}",
            proveedor=proveedor,
        )

    _validar_familia(cfg, proveedor)

    modulo_nombre = _PROVEEDORES[proveedor][0]
    modulo = _importar(proveedor)
    validador = getattr(modulo, "validar_configuracion", None)
    if validador is None:
        logger.debug(
            "%s no define validar_configuracion(); no hay nada que chequear", modulo_nombre
        )
        return
    validador(cfg)
    logger.info("llm: configuración validada para el proveedor %s", proveedor)


__all__ = [
    "ETAPAS",
    "PROVEEDORES",
    "Bloque",
    "BloqueDocumento",
    "BloqueImagen",
    "BloqueTexto",
    "LLMError",
    "Pedido",
    "Respuesta",
    "Uso",
    "completar",
    "max_tokens_de",
    "modelo_de",
    "proveedor_activo",
    "validar_configuracion",
]
