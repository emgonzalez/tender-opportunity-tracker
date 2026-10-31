"""src.drilldown.quota — Guardia de gasto del drill-down (Fase 2, MVP Track A).

El presupuesto diario se lleva **en dólares, no en tokens**. El drill-down usa
dos modelos con precios distintos (Haiku $1/MTok de entrada, Sonnet $3), así que
un contador único de tokens no se puede traducir a dinero: 2 M de tokens son $2
o $6 según quién los gastó. El dólar es la única unidad que compone.

Dos operaciones, deliberadamente asimétricas:

  cabe(...)    — pre-flight. Usa una ESTIMACIÓN, porque hay que decidir antes de
                 llamar. Su trabajo es frenar, no contabilizar.
  cobrar(...)  — post-flight. Usa el `usage` REAL que devolvió la API. El libro
                 mayor nunca acumula estimaciones: lo que se anota es la factura.

Esa asimetría es lo que habilitó la instrumentación de `UsoModelo`: antes solo
existía la estimación, así que el error del estimador se acumulaba en el saldo.

Todas las funciones toleran errores de IO: loguean y caen a un comportamiento
seguro (nunca deben tumbar el batch nocturno).

── Fase 9: el proveedor entra en la ecuación ────────────────────────────────

Con `LLM_PROVIDER=oci` cambian **las dos** mitades del módulo, y por razones
distintas:

  · el PRECIO, porque OCI cobra por transacción (caracteres) y no por token, y
    no distingue entrada de salida. La conversión, con su aritmética y su
    margen, está arriba de `PRECIOS_USD_MTOK`.
  · la ESTIMACIÓN, porque OCI no tiene bloque `document`: el pliego se traduce
    del lado nuestro a capa de texto o a N imágenes. `ANALISIS_TOKENS_POR_PAGINA`
    describía la rasterización del lado de Anthropic y con OCI no describe nada.

Los IDs de modelo salen de `llm.modelo_de(cfg, etapa)` y no de `cfg.model_*`:
esos campos solo gobiernan a Anthropic, y tarifar el gasto de OCI contra la
tarifa de Sonnet sería el mismo error que llevar el libro en tokens.
"""
import json
import logging
import os
import time
from datetime import date
from typing import Optional

from src import llm

logger = logging.getLogger(__name__)


# ── Precios ──────────────────────────────────────────────────────────────────
#
# ── Cómo se traduce la tarifa de OCI a la unidad de este libro mayor ─────────
#
# OCI Generative AI cobra por TRANSACCIÓN, no por token, y **no distingue
# entrada de salida**: su documentación dice textualmente *"1 character is
# calculated as 1 transaction"*, y en los modelos de chat se cobran el prompt y
# la respuesta con la misma tarifa. Para expresar eso en USD/MTok —la única
# unidad que compone con los modelos de Anthropic— se encadenan dos hechos:
#
#   · 1 transacción = 1 carácter      (definición de Oracle, verificada en la doc)
#   · 1 token ≈ 5,5 caracteres        (MEDIDO con scripts/smoke_oci_genai.py)
#
#   ⇒ USD/MTok = USD_por_10k_transacciones × 550
#
# (1 MTok = 1.000.000 tokens × 5,5 chars = 5.500.000 transacciones = 550 × 10k.)
#
# Y sobre eso, ×_FACTOR_SEGURIDAD_OCI: la conversión sigue siendo una
# ESTIMACIÓN encadenada —una definición documentada y una medición nuestra— y la
# doctrina de este módulo es que una guardia que subestima no es una guardia.
#
# ⚠️ El plan original convertía con ×400 (4 chars/token, supuesto). La Tanda 0
# midió 5,5 y esa suposición subestimaba ~37%. No volver a ×400.

# Caracteres por token que reporta la API de OCI para texto puro. MEDIDO, no
# supuesto; recalibrable con `scripts/smoke_oci_genai.py`, que compara los
# caracteres enviados contra los `prompt_tokens` que devuelve la API.
_CHARS_POR_TOKEN_OCI = 5.5

# Margen sobre la conversión de arriba. Cubre dos riesgos, no uno:
#
#   1. que la cadena transacción→carácter→token esté corrida;
#   2. que la tarifa medida abajo sea la EFECTIVA de esta tenancy —con
#      descuento por compromiso— y no la de lista, que es 2,2× más alta.
#
# Lo que NO cubre es que Oracle cambie el precio publicado: eso se ve mirando
# la factura, no agrandando el margen.
_FACTOR_SEGURIDAD_OCI = 1.5

# Tarifa de los Llama 4 on-demand en OCI, por cada 10.000 transacciones. La
# misma para Scout y para Maverick, y la misma para entrada y salida.
#
# **MEDIDA, no de lista.** Hasta el 2026-09-24 acá decía 0,0018 —el precio
# publicado— porque no había forma de ver la factura: la Usage API respondía
# `404 NotAuthorizedOrNotFound`. Con el permiso `read usage-report` concedido,
# el consumo real del 2026-09-23 da:
#
#     SKU B111035 · 82,2639 × 10k transacciones · $0,066633759
#     SKU B111036 · 61,1645 × 10k transacciones · $0,049543245
#     ⇒ $0,00081 por 10k, idéntico en los dos SKU
#
# Reproducible con:
#     oci --profile MULTICOMPUTOS usage-api usage-summary \
#         request-summarized-usages --from-json '{"tenantId":"<ocid>",
#         "timeUsageStarted":"<desde>","timeUsageEnded":"<hasta>",
#         "granularity":"DAILY","queryType":"COST",
#         "groupBy":["service","skuPartNumber","skuName","unit"]}'
#
# ⚠️ Es la tarifa EFECTIVA de esta tenancy, que puede incluir descuento por
# compromiso; la de lista es 2,2× más alta. Si el descuento caducara, este
# número subestimaría — y ése es uno de los dos trabajos que hace
# `_FACTOR_SEGURIDAD_OCI`. Recalibrar es correr el comando de arriba y cambiar
# esta línea sola.
_USD_POR_10K_TRANSACCIONES_LLAMA4 = 0.00081


def _usd_mtok_oci(usd_por_10k: float) -> float:
    """Convierte la tarifa de OCI (USD/10k transacciones) a USD/MTok, con margen."""
    return usd_por_10k * _CHARS_POR_TOKEN_OCI * 100 * _FACTOR_SEGURIDAD_OCI


# USD por millón de tokens, (entrada, salida), tarifa estándar de la API.
#
# Anthropic: deliberadamente NO se usa el precio introductorio de Sonnet 5
# ($2/$10 hasta el 2026-08-31): un freno de gasto debe sobreestimar, y fijar la
# promoción dejaría la guardia 33% floja el día que expire.
#
# OCI: entrada y salida al MISMO precio, porque OCI no las diferencia. Los IDs
# vienen namespaceados (`meta.`, `cohere.`) así que conviven con los de
# Anthropic sin riesgo de colisión.
#
# ⚠️ **Cohere y los demás modelos de OCI no están acá a propósito.** El
# adaptador solo habla `GenericChatRequest`, y `cohere.command-a-*` lo rechaza
# con `400 Chat request type does not match serving model` (verificado): no es
# alcanzable. Además su tarifa (~$9,4/MTok con esta conversión) superaría a
# `_PRECIO_DESCONOCIDO` y rompería su invariante —"lo que no conozco se cobra
# como lo más caro que conozco"—, que es justo lo que hace segura a la guardia.
PRECIOS_USD_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    # ── OCI Generative AI ──
    "meta.llama-4-scout-17b-16e-instruct": (
        _usd_mtok_oci(_USD_POR_10K_TRANSACCIONES_LLAMA4),
        _usd_mtok_oci(_USD_POR_10K_TRANSACCIONES_LLAMA4),
    ),
    "meta.llama-4-maverick-17b-128e-instruct-fp8": (
        _usd_mtok_oci(_USD_POR_10K_TRANSACCIONES_LLAMA4),
        _usd_mtok_oci(_USD_POR_10K_TRANSACCIONES_LLAMA4),
    ),
}

# Un modelo fuera de la tabla se cobra como el más caro que conocemos: si alguien
# apunta MODEL_ANALYSIS a algo nuevo, la guardia debe apretar, no soltarse.
_PRECIO_DESCONOCIDO = (5.00, 25.00)

# Presupuesto por defecto. Con el corpus medido (~6 procesos/día, de los cuales
# una fracción llega al análisis) el gasto real esperado está muy por debajo;
# esto es un tope de seguridad, no un objetivo.
DEFAULT_DAILY_BUDGET_USD = 5.00


def precio(modelo: str) -> tuple[float, float]:
    """(USD/MTok de entrada, USD/MTok de salida) para `modelo`."""
    p = PRECIOS_USD_MTOK.get(modelo)
    if p is None:
        logger.warning("Modelo sin precio conocido (%s); se cobra como el más caro.", modelo)
        return _PRECIO_DESCONOCIDO
    return p


def costo_usd(modelo: str, input_tokens: int, output_tokens: int = 0) -> float:
    """Convierte tokens a dólares con la tarifa de `modelo`."""
    p_in, p_out = precio(modelo)
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


# ── Estimación pre-flight ────────────────────────────────────────────────────

# Tokens que cuesta UNA página de pliego en la llamada de análisis **con
# Anthropic**.
#
# Con Anthropic el análisis manda el PDF como bloque `document`: quien rasteriza
# es Anthropic, del lado servidor, a su propia resolución. Por eso
# `pdf.tokens_imagen_estimados` NO sirve en esa rama — describe el triaje, que
# rasterizamos nosotros. El rango documentado para PDFs es ~1.500-3.000
# tokens/página según cuánta capa de texto tenga; se toma el extremo alto porque
# una guardia que subestima no es una guardia.
#
# ⚠️ **Esa premisa se rompe con OCI.** OCI no tiene bloque `document`: el
# adaptador traduce el pliego a capa de texto (nativo) o a N imágenes que
# rasterizamos NOSOTROS (escaneado). Nadie rasteriza del lado servidor, así que
# un coeficiente por página no describe nada. De ahí las ramas por proveedor de
# `costo_analisis_estimado_usd`.
#
# ESTE ES EL ÚNICO NÚMERO SIN MEDIR DEL MÓDULO. `scripts/calibrar_cuota.py` lo
# contrasta contra el `uso_analisis` real que ya persiste el historial; ajustarlo
# es cambiar ANALISIS_TOKENS_POR_PAGINA (env var), sin tocar código.
ANALISIS_TOKENS_POR_PAGINA = 3000

# System prompt + contexto del cliente (catálogo, marcas, nota de mercado).
_OVERHEAD_PROMPT_TOKENS = 800

# Salida esperada por llamada. El triaje responde un JSON de una línea; el
# análisis, una matriz de riesgos. A $15/MTok la salida del análisis ronda el
# 10% del total: ignorarla sesga la guardia hacia abajo.
_SALIDA_TRIAJE_TOKENS = 150
_SALIDA_ANALISIS_TOKENS = 2500

# Caracteres por token para estimar un bloque de texto. Es el ratio de
# Anthropic; OCI midió 5,5, que daría MENOS tokens para el mismo texto. Se deja
# en 4 a propósito para los dos proveedores: sobreestimar es la dirección segura
# (ver `_PRECIO_DESCONOCIDO`, misma doctrina).
_CHARS_POR_TOKEN = 4

# Espejo del default de `cfg.oci_analisis_raster_pages` (y de
# `proveedor_oci._RASTER_PAGINAS_DEFAULT`, que es quien de verdad decide cuántas
# páginas se mandan). Está duplicado para no importar el adaptador —que arrastra
# el SDK opcional— desde la guardia de gasto; `tests/test_quota.py` verifica que
# los dos números sigan siendo el mismo.
# 10 desde el 2026-09-23: es el techo de imágenes por request de Llama 4
# (`modelos.TECHO_IMAGENES`), y el adaptador recorta ahí aunque la perilla pida
# más. Si este número quedara en 20, la guardia estimaría el costo de veinte
# páginas para un request que manda diez y frenaría pliegos que sí entraban.
_RASTER_PAGINAS_OCI = 10

# Cuántas páginas de texto extrae el adaptador de OCI de un PDF nativo cuando
# nadie le dijo cuántas tiene. Mismo espejo que el de arriba, misma razón.
_PAGINAS_TEXTO_SI_DESCONOCIDO_OCI = 60

# ⚠️ **PREGUNTA ABIERTA, y es la que decide si conviene mover el drill-down.**
#
# La documentación de Oracle es explícita para texto —"1 character is calculated
# as 1 transaction"— pero **no dice cómo cuenta una imagen**. Lo medido en la
# Tanda 0 sobre UNA página rasterizada (JPEG de 33 KB a 1568 px):
#
#     caracteres del base64 ......... 45.329
#     prompt_tokens que reportó OCI ..  2.491
#     ratio ......................... 18,2 chars/token (el texto puro dio 5,5)
#
# Que el ratio de la imagen sea 3,3× el del texto significa que el TOKENIZADOR
# no trata al base64 como texto. Es razonable suponer que la FACTURACIÓN tampoco
# — pero "razonable" no alcanza para un freno de gasto, y las dos hipótesis se
# separan por un factor 3,3 en el 66% del corpus (los escaneados):
#
#     si OCI factura los caracteres del base64 → 45.329/5,5 = 8.242 tok/página
#     si factura los tokens que reporta ....... →              2.491 tok/página
#
# Con la primera, el triaje de escaneados en OCI sale ~3,3× lo que cuesta hoy en
# Anthropic y el argumento económico de la migración se invierte.
#
# **No se puede medir por API**: la Usage API de OCI está denegada
# (`NotAuthorizedOrNotFound`, verificado el 2026-09-19), el mismo permiso de
# tenancy que ya deja inútil a `cost-watchdog.sh`. Se cierra mirando Cost
# Analysis en la consola después de correr el benchmark — tarea de verificación
# ANTES de encender `llm_provider: oci` en cualquier tenant.
#
# Hasta entonces se adopta la hipótesis PESIMISTA, que es la doctrina del
# módulo. Si la consola dice que OCI factura los tokens reportados, este factor
# pasa a 1.0 y la guardia se afloja 3,3× para los escaneados.
_FACTOR_IMAGEN_B64_OCI = 3.3


def _es_oci(cfg) -> bool:
    return llm.proveedor_activo(cfg) == "oci"


def _tokens_de_texto(texto: str | None) -> int:
    return len(texto or "") // _CHARS_POR_TOKEN


def _tokens_de_paginas_rasterizadas(cfg, pdf_path: str, paginas: int) -> int:
    """Tokens de entrada de `paginas` páginas mandadas COMO IMAGEN.

    Se apoya en `pdf.tokens_imagen_estimados`, que usa **el mismo zoom que el
    renderizador** (`pdf._zoom_de_pagina`): si el estimador y el renderizador
    divergen, la estimación describe un documento que nunca se manda — la
    propiedad que `pdf.py` protege explícitamente.

    ⚠️ Ese estimador tokeniza como **Anthropic** (`_PX_POR_TOKEN = 750`), no
    como Llama. Resulta que para la página medida en la Tanda 0 le pega bastante
    (≈2.530 estimados contra 2.491 `prompt_tokens` reales), así que se usa
    también para OCI como sobreestimación provisoria; lo que sí cambia entre
    proveedores es la FACTURACIÓN, y de eso se encarga `_FACTOR_IMAGEN_B64_OCI`.
    """
    from src.drilldown import pdf as _pdf

    tokens = _pdf.tokens_imagen_estimados(pdf_path, paginas)
    if _es_oci(cfg):
        return int(tokens * _FACTOR_IMAGEN_B64_OCI)
    return tokens


def costo_triaje_estimado_usd(cfg, pdf_path: str, *, scanned: bool, texto: str | None) -> float:
    """Estima en USD lo que costará el triaje, antes de llamar.

    Esta rama sí se puede estimar con precisión, porque el contenido lo armamos
    nosotros (`triage._build_user_content`) y es el MISMO con los dos
    proveedores: o son N páginas rasterizadas con el mismo zoom que usará el
    renderizador, o es texto que ya está en memoria. Lo único que cambia con
    OCI es la tarifa y la hipótesis de facturación de las imágenes.
    """
    if scanned:
        tokens_in = _tokens_de_paginas_rasterizadas(
            cfg, pdf_path, getattr(cfg, "triage_raster_pages", 5)
        )
    else:
        tokens_in = _tokens_de_texto(texto)
    return costo_usd(
        llm.modelo_de(cfg, "triaje"), tokens_in + _OVERHEAD_PROMPT_TOKENS, _SALIDA_TRIAJE_TOKENS
    )


def _tokens_analisis_oci(cfg, pdf_path: str, n_paginas: int, escaneado: bool) -> int:
    """Tokens de entrada del análisis **tal como lo va a armar el adaptador de OCI**.

    Espeja `proveedor_oci._documento_como_texto` / `_documento_como_imagenes`,
    que es lo único que hace honesta a una estimación: si acá se estimara el PDF
    entero y allá se mandaran 10 páginas, la guardia frenaría pliegos que sí
    entraban. Devuelve 0 si no pudo estimar, y el caller decide qué hacer con
    eso (spoiler: frenar, no soltarse).
    """
    if escaneado:
        tope = int(getattr(cfg, "oci_analisis_raster_pages", 0) or _RASTER_PAGINAS_OCI)
        paginas = min(tope, n_paginas) if n_paginas > 0 else tope
        return _tokens_de_paginas_rasterizadas(cfg, pdf_path, paginas)

    from src.drilldown import pdf as _pdf

    try:
        texto = _pdf.extract_text_first_pages(
            pdf_path, n_paginas or _PAGINAS_TEXTO_SI_DESCONOCIDO_OCI
        )
    except Exception as exc:  # noqa: BLE001 - estimar nunca debe tumbar el batch
        logger.warning("gasto: no se pudo leer la capa de texto de %s (%s)", pdf_path, exc)
        return 0
    return _tokens_de_texto(texto)


def costo_analisis_estimado_usd(
    cfg,
    n_paginas: int,
    *,
    pdf_path: str | None = None,
    escaneado: bool | None = None,
) -> float:
    """Estima en USD lo que costará el análisis profundo de un pliego de `n_paginas`.

    Los dos kwargs son opcionales porque el proveedor por defecto no los
    necesita: con Anthropic el pliego viaja como bloque `document` y el costo
    solo depende de cuántas páginas tiene. Con OCI depende de **cómo** se
    traduce el pliego, y para eso hay que mirarlo.

      · anthropic ............... `n_paginas × ANALISIS_TOKENS_POR_PAGINA`
      · oci + escaneado ......... las primeras `oci_analisis_raster_pages`
                                  rasterizadas, con la hipótesis pesimista de
                                  facturación de imágenes
      · oci + nativo ............ la capa de texto real, que es lo que manda el
                                  adaptador (y es mucho más barata)
      · sin `pdf_path` .......... la estimación por página, cualquiera sea el
                                  proveedor: una guardia que no puede estimar
                                  debe FRENAR, no soltarse
    """
    por_pagina = getattr(cfg, "analisis_tokens_por_pagina", ANALISIS_TOKENS_POR_PAGINA)
    tokens_por_pagina = max(int(n_paginas), 0) * por_pagina

    tokens_in = tokens_por_pagina
    if _es_oci(cfg) and pdf_path and escaneado is not None:
        tokens_in = _tokens_analisis_oci(cfg, pdf_path, max(int(n_paginas), 0), escaneado)
        if tokens_in <= 0 and tokens_por_pagina > 0:
            # El PDF no se dejó leer. Se vuelve a la estimación por página, que
            # es la más cara de las dos: frenar de más cuesta un pliego a
            # revisión manual; soltarse de más cuesta dinero real.
            logger.warning(
                "gasto: estimación específica de OCI no disponible para %s; "
                "se usa la estimación por página (más conservadora)", pdf_path,
            )
            tokens_in = tokens_por_pagina

    return costo_usd(
        llm.modelo_de(cfg, "analisis"),
        tokens_in + _OVERHEAD_PROMPT_TOKENS,
        _SALIDA_ANALISIS_TOKENS,
    )


# ── Libro mayor diario ───────────────────────────────────────────────────────

def _libro_vacio() -> dict:
    return {"fecha": date.today().isoformat(), "gasto_usd": 0.0, "por_modelo": {}}


def _cargar_libro(path: str) -> dict:
    """Carga el libro del día desde `path`.

    Devuelve un libro en cero si el archivo no existe, está corrupto, es de otra
    fecha (reset diario) o viene del formato viejo en tokens. Ese último caso no
    necesita migración: el archivo se resetea todos los días de todos modos.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("fecha") != date.today().isoformat():
            return _libro_vacio()
        if "gasto_usd" not in data:
            return _libro_vacio()
        data.setdefault("por_modelo", {})
        data["gasto_usd"] = float(data["gasto_usd"])
        return data
    except FileNotFoundError:
        return _libro_vacio()
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
        logger.warning("gasto: no se pudo leer %s (%s); reseteando el día.", path, e)
        return _libro_vacio()


def _guardar_libro(path: str, data: dict) -> None:
    """Escribe `data` de forma atómica (write-to-tmp + os.replace)."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("gasto: no se pudo guardar %s (%s)", path, e)


def _tope(cfg) -> float:
    return float(getattr(cfg, "daily_budget_usd", DEFAULT_DAILY_BUDGET_USD))


# ── Fuente del libro (archivo | sombra | adb) ────────────────────────────────
#
# `CUOTA_FUENTE` (src/persistencia/fuente.py): `archivo` (default) = el JSON de siempre, sin tocar la ADB.
# `sombra` = manda el archivo; el gasto también se anota en la ADB y se compara, logueando diferencias.
# `adb` = el total del día sale de `cuota_gasto` (incremento atómico en SQL). **Nunca falla abierto**: el archivo
# local se sigue escribiendo en cada cobro y es la caché; si la ADB no responde se usa, y si responde se toma
# el MAYOR de los dos totales. El tope diario aplica siempre, con la ADB arriba o caída.

DATO_FUENTE = "CUOTA"
_ESPERA_TRAS_FALLO = 60.0  # s: con la ADB caída no se reintenta (ni se espera el timeout) en cada llamada
_adb_estado: dict = {"fallo_t": None}
_sombra_avisadas: set[tuple] = set()
_sombra_base: dict[str, float] = {}  # día → diferencia archivo−ADB vista por última vez
_TOLERANCIA_SOMBRA = 0.0005  # USD: el archivo redondea a 6 decimales


def reiniciar_estado() -> None:
    """Olvida el período de espera tras un fallo y los avisos de sombra (tests)."""
    _adb_estado["fallo_t"] = None
    _sombra_avisadas.clear()
    _sombra_base.clear()


def _fuente() -> str:
    from src.persistencia.fuente import fuente
    return fuente(DATO_FUENTE)


def _adb_disponible(cfg) -> bool:
    if not (getattr(cfg, "adb_dsn", "") and getattr(cfg, "adb_password", "")):
        return False
    t = _adb_estado["fallo_t"]
    return t is None or time.monotonic() - t >= _ESPERA_TRAS_FALLO


def _adb_fallo(exc: Exception) -> None:
    primera = _adb_estado["fallo_t"] is None
    _adb_estado["fallo_t"] = time.monotonic()
    if primera:
        logger.warning("gasto: ADB no disponible (%s); el tope sigue aplicando con la caché local.", type(exc).__name__)
    logger.debug("detalle del fallo de la ADB", exc_info=True)


_SQL_TOTAL = "SELECT NVL(SUM(gasto_usd), 0) FROM cuota_gasto WHERE dia = :dia"
_SQL_INCREMENTAR = """
    UPDATE cuota_gasto
       SET gasto_usd = gasto_usd + :gasto,
           input_tokens = input_tokens + :tin,
           output_tokens = output_tokens + :tout,
           llamadas = llamadas + :llamadas,
           actualizado_en = SYSTIMESTAMP
     WHERE dia = :dia AND modelo = :modelo
"""
_SQL_INSERTAR = """
    INSERT INTO cuota_gasto (dia, modelo, gasto_usd, input_tokens, output_tokens, llamadas)
    VALUES (:dia, :modelo, :gasto, :tin, :tout, :llamadas)
"""


def _es_duplicado(exc: Exception) -> bool:
    return "ORA-00001" in str(exc)


def _adb_total(cfg, dia: date) -> Optional[float]:
    """Gasto del día según la ADB, o None si no respondió. Nunca lanza."""
    if not _adb_disponible(cfg):
        return None
    try:
        from src.memoria import db
        with db.connection(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(_SQL_TOTAL, {"dia": dia})
                fila = cur.fetchone()
            finally:
                cur.close()
        _adb_estado["fallo_t"] = None
        return float(fila[0] or 0.0) if fila else 0.0
    except Exception as exc:  # noqa: BLE001 — la ADB nunca tumba la corrida
        _adb_fallo(exc)
        return None


def _adb_incrementar(cfg, dia: date, modelo: str, gasto: float, uso) -> Optional[float]:
    """Suma `gasto` a la fila (día, modelo) con un UPDATE atómico; devuelve el total del día, o None. Nunca lanza."""
    if not _adb_disponible(cfg):
        return None
    binds = {"dia": dia, "modelo": modelo, "gasto": round(gasto, 6), "tin": int(uso.input_tokens),
             "tout": int(uso.output_tokens), "llamadas": int(uso.llamadas)}
    try:
        from src.memoria import db
        with db.connection(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(_SQL_INCREMENTAR, binds)
                if not cur.rowcount:
                    try:
                        cur.execute(_SQL_INSERTAR, binds)
                    except Exception as exc:  # noqa: BLE001 — otro proceso creó la fila entre medio
                        if not _es_duplicado(exc):
                            raise
                        cur.execute(_SQL_INCREMENTAR, binds)
                cur.execute(_SQL_TOTAL, {"dia": dia})
                fila = cur.fetchone()
            finally:
                cur.close()
        _adb_estado["fallo_t"] = None
        return float(fila[0] or 0.0) if fila else None
    except Exception as exc:  # noqa: BLE001
        _adb_fallo(exc)
        return None


def _comparar_sombra(dia: date, local: float, adb: Optional[float]) -> None:
    """Avisa de la diferencia archivo−ADB cuando aparece y cuando CAMBIA, no en cada llamada.

    El día que se enciende la sombra la ADB empieza en cero y el archivo ya traía gasto: esa diferencia es constante
    y no es un error; lo que delata un fallo es que se mueva (un cobro que una fuente no vio).
    """
    if adb is None:
        return
    dif, clave_dia = local - adb, dia.isoformat()
    previa = _sombra_base.get(clave_dia)
    _sombra_base[clave_dia] = dif
    if previa is None:
        if abs(dif) > _TOLERANCIA_SOMBRA:
            logger.warning(
                "gasto[sombra]: el archivo dice $%.4f y la ADB $%.4f para %s (diferencia $%.4f, gasto anterior a la "
                "ADB; no se vuelve a avisar salvo que cambie)", local, adb, dia, dif)
        return
    if abs(dif - previa) <= _TOLERANCIA_SOMBRA:
        return
    clave = (clave_dia, round(dif, 3))
    if clave in _sombra_avisadas:
        return
    _sombra_avisadas.add(clave)
    logger.warning("gasto[sombra]: la diferencia archivo−ADB de %s pasó de $%.4f a $%.4f (archivo $%.4f, ADB $%.4f)",
                   dia, previa, dif, local, adb)


def gasto_del_dia(cfg) -> float:
    """USD ya gastados hoy por el drill-down (nunca menos que lo que sabe el archivo local)."""
    local = _cargar_libro(cfg.budget_path)["gasto_usd"]
    modo = _fuente()
    if modo == "archivo":
        return local
    hoy = date.today()
    total = _adb_total(cfg, hoy)
    if modo == "sombra":
        _comparar_sombra(hoy, local, total)
        return local
    if total is None or total <= local:
        return local
    # La ADB sabe de más gasto que la caché (p. ej. tras un reinicio con la caché perdida): se refresca.
    libro = _cargar_libro(cfg.budget_path)
    libro["gasto_usd"] = round(total, 6)
    _guardar_libro(cfg.budget_path, libro)
    return total


def cabe(cfg, costo_estimado_usd: float) -> bool:
    """True si `costo_estimado_usd` entra en lo que queda del presupuesto de hoy.

    No reserva nada: la anotación la hace `cobrar()` con el consumo real. Entre
    el chequeo y el cobro puede haber una llamada que salga más cara que lo
    estimado, y ese sobrante se descuenta del siguiente pliego — que es lo
    correcto, porque el dinero ya se gastó.
    """
    gastado = gasto_del_dia(cfg)
    tope = _tope(cfg)
    if gastado + costo_estimado_usd > tope:
        logger.warning(
            "gasto: presupuesto diario agotado (gastado=$%.4f, estimado=$%.4f, tope=$%.2f)",
            gastado, costo_estimado_usd, tope,
        )
        return False
    return True


def cobrar(cfg, uso) -> float:
    """Anota el gasto REAL de `uso` en el libro y devuelve cuánto fue (USD).

    Se llama SIEMPRE que hubo llamada al modelo, haya servido o no: un triaje
    NO-GO y un análisis que falló al parsear cuestan igual que uno que funcionó.
    Antes el libro solo se tocaba tras un veredicto GO, así que **todo el gasto
    de triaje quedaba fuera de la cuenta** — y en un corpus mayoritariamente
    escaneado el triaje no es el gasto menor.

    El archivo local es lectura-modificación-escritura sin lock (la Fase B procesa
    un pliego a la vez). Con `CUOTA_FUENTE=adb|sombra` el gasto además se suma en
    `cuota_gasto` con un UPDATE atómico; si la ADB falla, el archivo ya lo tiene.
    """
    if uso is None or not getattr(uso, "llamadas", 0):
        return 0.0

    gasto = costo_usd(uso.modelo, uso.input_tokens, uso.output_tokens)
    libro = _cargar_libro(cfg.budget_path)
    libro["gasto_usd"] = round(libro["gasto_usd"] + gasto, 6)

    m = libro["por_modelo"].setdefault(
        uso.modelo, {"input_tokens": 0, "output_tokens": 0, "llamadas": 0, "gasto_usd": 0.0}
    )
    m["input_tokens"] += uso.input_tokens
    m["output_tokens"] += uso.output_tokens
    m["llamadas"] += uso.llamadas
    m["gasto_usd"] = round(m["gasto_usd"] + gasto, 6)

    _guardar_libro(cfg.budget_path, libro)
    modo = _fuente()
    if modo != "archivo":
        hoy = date.fromisoformat(libro["fecha"])
        total = _adb_incrementar(cfg, hoy, uso.modelo, gasto, uso)
        if modo == "sombra":
            _comparar_sombra(hoy, libro["gasto_usd"], total)
    logger.info(
        "gasto: +$%.4f (%s, %d in / %d out) — acumulado hoy $%.4f de $%.2f",
        gasto, uso.modelo, uso.input_tokens, uso.output_tokens,
        libro["gasto_usd"], _tope(cfg),
    )
    return gasto
