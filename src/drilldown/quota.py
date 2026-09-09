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
"""
import json
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)


# ── Precios ──────────────────────────────────────────────────────────────────

# USD por millón de tokens, (entrada, salida), tarifa estándar de la API.
# Deliberadamente NO se usa el precio introductorio de Sonnet 5 ($2/$10 hasta el
# 2026-08-31): un freno de gasto debe sobreestimar, y fijar la promoción dejaría
# la guardia 33% floja el día que expire.
PRECIOS_USD_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
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

# Tokens que cuesta UNA página de pliego en la llamada de análisis.
#
# El análisis manda el PDF como bloque `document`: quien rasteriza es Anthropic,
# del lado servidor, a su propia resolución. Por eso `pdf.tokens_imagen_estimados`
# NO sirve acá — describe el triaje, que rasterizamos nosotros, no esta ruta.
# El rango documentado para PDFs es ~1.500-3.000 tokens/página según cuánta capa
# de texto tenga; se toma el extremo alto porque una guardia que subestima no es
# una guardia.
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

_CHARS_POR_TOKEN = 4


def costo_triaje_estimado_usd(cfg, pdf_path: str, *, scanned: bool, texto: str | None) -> float:
    """Estima en USD lo que costará el triaje, antes de llamar.

    Esta rama sí se puede estimar con precisión, porque el contenido lo armamos
    nosotros (`triage._build_user_content`): o son N páginas rasterizadas con el
    mismo zoom que usará el renderizador, o es texto que ya está en memoria.
    """
    if scanned:
        from src.drilldown import pdf as _pdf
        tokens_in = _pdf.tokens_imagen_estimados(
            pdf_path, getattr(cfg, "triage_raster_pages", 5)
        )
    else:
        tokens_in = len(texto or "") // _CHARS_POR_TOKEN
    return costo_usd(
        cfg.model_triage, tokens_in + _OVERHEAD_PROMPT_TOKENS, _SALIDA_TRIAJE_TOKENS
    )


def costo_analisis_estimado_usd(cfg, n_paginas: int) -> float:
    """Estima en USD lo que costará el análisis profundo de un pliego de `n_paginas`."""
    por_pagina = getattr(cfg, "analisis_tokens_por_pagina", ANALISIS_TOKENS_POR_PAGINA)
    tokens_in = max(int(n_paginas), 0) * por_pagina
    return costo_usd(
        cfg.model_analysis, tokens_in + _OVERHEAD_PROMPT_TOKENS, _SALIDA_ANALISIS_TOKENS
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


def gasto_del_dia(cfg) -> float:
    """USD ya gastados hoy por el drill-down."""
    return _cargar_libro(cfg.budget_path)["gasto_usd"]


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

    Lectura-modificación-escritura sin lock: la Fase B procesa un pliego a la
    vez por diseño de memoria, así que no hay concurrencia que perder.
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
    logger.info(
        "gasto: +$%.4f (%s, %d in / %d out) — acumulado hoy $%.4f de $%.2f",
        gasto, uso.modelo, uso.input_tokens, uso.output_tokens,
        libro["gasto_usd"], _tope(cfg),
    )
    return gasto
