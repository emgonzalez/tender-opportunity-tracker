#!/usr/bin/env python3
"""scripts.comparar_llm — contrasta Claude contra OCI con lo MEDIDO, no con estimaciones.

Lee el JSONL append-only que `src/llm/registro.py` deja en
`{data_dir}/llm_consumo.jsonl` —una línea por llamada al LLM— y contesta cuatro
preguntas que hoy solo se responden con conjeturas:

1. **Cómo factura OCI las imágenes.** Oracle documenta que *"1 character is
   calculated as 1 transaction"* y que en chat se cobran prompt y respuesta,
   pero **no dice qué hace con una imagen**. Medido: una página rasterizada son
   45.329 caracteres de base64 contra 2.491 tokens reportados; al ratio de 5,5
   chars/token eso da un factor ~3,3 entre cobrar el base64 y cobrar los tokens.
   Como no se puede deducir, este informe calcula **las dos hipótesis** y las
   muestra lado a lado. La diferencia entre ambas es lo único que hay que ir a
   contrastar contra la factura real, y el informe dice qué número buscar en
   Cost Analysis y en qué ventana de fechas.
2. **La comparación con Claude, manzana con manzana**: por etapa, costo por
   llamada, tokens de entrada y salida, latencia p50/p95, truncamiento y error.
3. **El razonamiento oculto** de Grok y Gemini: tokens que se facturan y **no**
   aparecen en `completion_tokens`. Con Claude ese costo no existe, así que
   ignorarlo inclinaría la comparación a favor de OCI sin que se note.
4. **Salud operativa**: truncamientos por etapa y clamps del techo de salida
   (`max_tokens_efectivo < max_tokens_pedido`).

NO gasta un solo token ni abre la red: solo lee un archivo que ya existe.

⚠️ **Por qué acá los precios no son los de `src/drilldown/quota.py`.** Esa
guardia tarifa con un factor de seguridad de 1,5× a propósito: su trabajo es
frenar el gasto y una guardia que subestima no es una guardia. Este informe hace
lo contrario —medir para poder comparar con la factura— así que usa la **tarifa
de lista cruda**, sin margen. Mezclar las dos unidades es justamente el error
que este script existe para no cometer.

Uso:
    python -m scripts.comparar_llm
    python -m scripts.comparar_llm --registro data/llm_consumo.jsonl
    python -m scripts.comparar_llm --desde 2026-09-20 --hasta 2026-09-30
    python -m scripts.comparar_llm --json
    python -m scripts.comparar_llm --tarifa-oci-10k 0.0075   # cuando se consiga
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
from datetime import datetime, time, timedelta, timezone

# Ejecutable como script suelto (`python scripts/comparar_llm.py`) además de como
# módulo. No importa nada de `src` a propósito: el registro se analiza en máquinas
# donde el paquete puede no estar instalado, y el informe no debe caerse porque un
# módulo de runtime no importe.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


# ── Vocabulario ──────────────────────────────────────────────────────────────

# Espejo de `src.llm.modelos.ETAPAS`. Está duplicado y no importado por la razón
# de arriba; el orden es el del pipeline, para que el informe se lea de corrido.
ETAPAS = ("triaje", "analisis", "clasificacion", "perfilado")

# Los dos proveedores que la comparación espera encontrar. Si falta alguno, el
# informe lo dice en vez de presentar media comparación como si fuera entera.
PROVEEDORES_ESPERADOS = ("anthropic", "oci")

# Las familias que razonan sin declararlo (espejo de
# `src.llm.modelos.FAMILIAS_CON_RAZONAMIENTO_OCULTO`).
FAMILIAS_CON_RAZONAMIENTO_OCULTO = frozenset({"xai", "google"})


# ── Tarifas ──────────────────────────────────────────────────────────────────

# USD por cada 10.000 transacciones, tarifa on-demand de lista, SIN margen.
#
# `None` significa **no se conoce**, y es un valor de primera clase: el informe
# muestra las transacciones calculadas y dice "tarifa desconocida" en vez de
# inventar un número. Un costo inventado en un informe que existe para comparar
# con la factura real es peor que un hueco: el hueco se nota.
TARIFAS_OCI_10K: dict[str, float | None] = {
    "nativo": 0.0018,   # Llama 4 Scout y Maverick, misma tarifa (doc de Oracle)
    "xai": None,        # Grok: OCI no publica tarifa de reventa
    "google": None,     # Gemini: ídem
}

# Caracteres por token que reporta OCI para texto puro. MEDIDO con
# `scripts/smoke_oci_genai.py` sobre la familia `nativo`; su hogar autoritativo
# es `src/drilldown/quota.py` (`_CHARS_POR_TOKEN_OCI`).
#
# ⚠️ Para `xai` y `google` **no está medido**: se usa el mismo 5,5 como
# aproximación y el informe lo advierte. Solo afecta a la hipótesis optimista,
# que es la que convierte tokens de imagen a transacciones.
CHARS_POR_TOKEN_OCI = 5.5

TRANSACCIONES_POR_UNIDAD_DE_TARIFA = 10_000


# ── Lectura del registro ─────────────────────────────────────────────────────


def _num(d: object, *claves: str) -> float:
    """Navega un dict anidado y devuelve un número, o 0.0 si falta cualquier tramo.

    El esquema del registro es fijo, pero una línea la puede haber escrito una
    versión anterior del código: preferimos un 0 explícito a un KeyError que
    tumba el informe entero por una línea vieja.
    """
    cur: object = d
    for k in claves:
        if not isinstance(cur, dict):
            return 0.0
        cur = cur.get(k)
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return 0.0
    return float(cur)


def _ts(fila: dict) -> datetime | None:
    """El `ts` de la línea como datetime, o None si no se puede parsear."""
    crudo = fila.get("ts")
    if not isinstance(crudo, str):
        return None
    try:
        return datetime.fromisoformat(crudo)
    except ValueError:
        return None


def _limite(texto: str, *, fin: bool) -> datetime:
    """Parsea `--desde` / `--hasta`.

    Una fecha sola (`2026-09-20`) se interpreta como el **día entero**: inicio
    del día para `--desde`, final del día para `--hasta`. Escribir `--hasta
    2026-09-30` y que se excluyera todo lo de ese día sería la sorpresa clásica.
    """
    dt = datetime.fromisoformat(texto)
    if "T" not in texto and " " not in texto:
        dt = datetime.combine(dt.date(), time.max if fin else time.min)
    return dt


def _en_rango(ts: datetime, desde: datetime | None, hasta: datetime | None) -> bool:
    """Compara `ts` contra los límites, alineando tz-awareness.

    El registro escribe timestamps con offset (`-06:00`) y los límites de la
    línea de comandos vienen sin él. Comparar un aware con un naive es un
    TypeError, así que al límite naive se le presta el huso de la línea: el
    operador que escribe `--desde 2026-09-20` quiere decir las 00:00 del huso en
    el que se registró la corrida, no UTC.
    """
    for limite, es_fin in ((desde, False), (hasta, True)):
        if limite is None:
            continue
        if limite.tzinfo is None and ts.tzinfo is not None:
            limite = limite.replace(tzinfo=ts.tzinfo)
        elif limite.tzinfo is not None and ts.tzinfo is None:
            limite = limite.replace(tzinfo=None)
        if es_fin and ts > limite:
            return False
        if not es_fin and ts < limite:
            return False
    return True


def leer_registro(
    path: str, desde: datetime | None = None, hasta: datetime | None = None
) -> tuple[list[dict], dict]:
    """Devuelve `(filas, diagnóstico)` del JSONL, tolerando corrupción.

    El archivo es append-only y lo puede estar escribiendo la corrida de las
    07:00 mientras lo leemos: la última línea puede estar cortada a la mitad.
    Eso **no** puede tumbar el informe — se saltea y se cuenta, porque una línea
    salteada en silencio es una medición que se pierde sin que nadie se entere.
    """
    filas: list[dict] = []
    diag = {"archivo": path, "lineas": 0, "corruptas": 0, "sin_ts": 0, "fuera_de_rango": 0}

    with open(path, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            diag["lineas"] += 1
            try:
                fila = json.loads(linea)
            except json.JSONDecodeError:
                diag["corruptas"] += 1
                continue
            if not isinstance(fila, dict):
                diag["corruptas"] += 1
                continue

            ts = _ts(fila)
            if ts is None:
                diag["sin_ts"] += 1
                if desde or hasta:
                    # Sin fecha no se puede afirmar que caiga en la ventana, y una
                    # ventana que incluye lo que no sabe fechar no es una ventana.
                    continue
            elif not _en_rango(ts, desde, hasta):
                diag["fuera_de_rango"] += 1
                continue

            fila["_ts"] = ts
            filas.append(fila)

    if diag["corruptas"]:
        logger.warning("Se saltearon %s línea(s) ilegible(s) de %s", diag["corruptas"], path)
    return filas, diag


# ── Lectura desde la ADB (fuente primaria con CUOTA_FUENTE=adb) ───────────────

_TZ_ELSALVADOR = timezone(timedelta(hours=-6))
_COLUMNAS_ADB = (
    "ts", "proveedor", "familia", "modelo", "etapa", "ok", "error_msg", "latencia_ms",
    "max_tokens_pedido", "max_tokens_efectivo", "truncada", "caracteres_texto", "caracteres_sistema",
    "caracteres_base64", "bytes_imagen", "caracteres_salida", "input_tokens", "output_tokens",
    "razonamiento_oculto", "costo_estimado_usd", "version",
)


def _fila_de_adb(valores: tuple) -> dict:
    """Una fila de `llm_llamada` con la misma forma que una línea del JSONL."""
    r = dict(zip(_COLUMNAS_ADB, valores))
    ts = r["ts"]
    n = lambda k: r[k] if r[k] is not None else 0  # noqa: E731
    return {
        "ts": ts.replace(tzinfo=_TZ_ELSALVADOR).isoformat() if isinstance(ts, datetime) else None,
        "proveedor": r["proveedor"] or "", "familia": r["familia"] or "", "modelo": r["modelo"] or "",
        "etapa": r["etapa"] or "", "ok": bool(r["ok"]), "error": r["error_msg"],
        "latencia_ms": n("latencia_ms"), "max_tokens_pedido": n("max_tokens_pedido"),
        "max_tokens_efectivo": n("max_tokens_efectivo"), "truncada": bool(r["truncada"]),
        "entrada": {"caracteres_texto": n("caracteres_texto"), "caracteres_sistema": n("caracteres_sistema"),
                    "caracteres_base64": n("caracteres_base64"), "bytes_imagen": n("bytes_imagen")},
        "salida": {"caracteres": n("caracteres_salida")},
        "uso": {"input_tokens": n("input_tokens"), "output_tokens": n("output_tokens")},
        "razonamiento_oculto": n("razonamiento_oculto"),
        "costo_estimado_usd": float(n("costo_estimado_usd")),
        "version": r["version"] or "",
    }


def leer_adb(
    cfg, desde: datetime | None = None, hasta: datetime | None = None
) -> tuple[list[dict], dict] | None:
    """`(filas, diagnóstico)` desde `llm_llamada`, o None si la ADB no está disponible. Nunca lanza."""
    if not (getattr(cfg, "adb_dsn", "") and getattr(cfg, "adb_password", "")):
        return None
    try:
        from src.memoria import db

        with db.connection(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT {', '.join(_COLUMNAS_ADB)} FROM llm_llamada ORDER BY ts")
                crudas = cur.fetchall()
            finally:
                cur.close()
    except Exception as exc:  # noqa: BLE001 — sin ADB se cae al JSONL
        logger.warning("ADB no disponible (%s); se usa el JSONL.", type(exc).__name__)
        return None

    filas: list[dict] = []
    diag = {"archivo": "ADB:llm_llamada", "lineas": len(crudas), "corruptas": 0, "sin_ts": 0, "fuera_de_rango": 0}
    for valores in crudas:
        fila = _fila_de_adb(tuple(valores))
        ts = _ts(fila)
        if ts is None:
            diag["sin_ts"] += 1
            if desde or hasta:
                continue
        elif not _en_rango(ts, desde, hasta):
            diag["fuera_de_rango"] += 1
            continue
        fila["_ts"] = ts
        filas.append(fila)
    return filas, diag


# ── Las dos hipótesis de facturación de OCI ──────────────────────────────────


def transacciones_pesimista(fila: dict) -> float:
    """Hipótesis PESIMISTA: el base64 de la imagen se factura carácter por carácter.

    Es la lectura literal de la documentación de Oracle —*"1 character is
    calculated as 1 transaction"*— aplicada a lo que de verdad viaja por el
    cable: el data-URI. Si Oracle cobra así, una página escaneada cuesta ~3,3
    veces lo que costaría por tokens.
    """
    return (
        _num(fila, "entrada", "caracteres_texto")
        + _num(fila, "entrada", "caracteres_sistema")
        + _num(fila, "entrada", "caracteres_base64")
        + _num(fila, "salida", "caracteres")
    )


def transacciones_optimista(fila: dict, chars_por_token: float = CHARS_POR_TOKEN_OCI) -> float:
    """Hipótesis OPTIMISTA: la imagen se factura por sus tokens, no por su base64.

    El texto se cobra por sus caracteres (ahí la doc es clara) y la imagen por
    los tokens que la API reportó, convertidos a transacciones con el ratio
    chars/token de la familia.

    Los tokens de la imagen **no vienen desglosados**: `prompt_tokens` es el
    total. Se derivan restándole al total los tokens que le corresponden al
    texto según el ratio. Es una resta, no una medición, y por eso:

    - si la llamada no mandó imágenes, las dos hipótesis coinciden por
      construcción (nada que discutir, y así no aparece una diferencia
      fantasma nacida de que el tokenizador no es exactamente 5,5 chars/token);
    - el aporte de la imagen se topa en su propio base64, porque una hipótesis
      "optimista" que dé más caro que la pesimista no es una hipótesis, es un
      error de aritmética filtrándose al informe.
    """
    texto = _num(fila, "entrada", "caracteres_texto") + _num(fila, "entrada", "caracteres_sistema")
    salida = _num(fila, "salida", "caracteres")
    base64_chars = _num(fila, "entrada", "caracteres_base64")

    if base64_chars <= 0:
        return texto + salida

    ratio = chars_por_token or CHARS_POR_TOKEN_OCI
    tokens_texto = texto / ratio
    tokens_imagen = max(0.0, _num(fila, "uso", "input_tokens") - tokens_texto)
    return texto + salida + min(tokens_imagen * ratio, base64_chars)


def _usd(transacciones: float, tarifa_10k: float | None) -> float | None:
    """Transacciones a dólares. `None` si la tarifa de esa familia no se conoce."""
    if tarifa_10k is None:
        return None
    return transacciones / TRANSACCIONES_POR_UNIDAD_DE_TARIFA * tarifa_10k


# ── Agregación ───────────────────────────────────────────────────────────────


def grupo_de(fila: dict) -> str:
    """Etiqueta del grupo comparable: `anthropic` u `oci/<familia>`.

    La familia entra en la clave solo para OCI porque es ahí donde cambia el
    modelo, la tarifa y el comportamiento de razonamiento. Mezclar `oci/nativo`
    con `oci/google` en una sola fila promediaría dos cosas que no se parecen.
    """
    proveedor = str(fila.get("proveedor") or "desconocido")
    familia = str(fila.get("familia") or "")
    if proveedor == "oci" and familia:
        return f"oci/{familia}"
    return proveedor


def familia_de(grupo: str) -> str:
    """La familia de un grupo `oci/<familia>`; cadena vacía para Anthropic."""
    return grupo.split("/", 1)[1] if grupo.startswith("oci/") else ""


def _acumulador() -> dict:
    return {
        "n": 0, "n_ok": 0, "n_error": 0, "n_truncada": 0, "n_clamp": 0,
        "input_tokens": 0.0, "output_tokens": 0.0, "razonamiento_oculto": 0.0,
        "transacciones_pesimista": 0.0, "transacciones_optimista": 0.0,
        "costo_registrado_usd": 0.0,
        "latencias": [], "etapas": set(), "modelos": set(), "versiones": set(),
        "primer_ts": None, "ultimo_ts": None,
    }


def _acumular(acc: dict, fila: dict, chars_por_token: float) -> None:
    acc["n"] += 1
    ok = bool(fila.get("ok"))
    acc["n_ok" if ok else "n_error"] += 1
    if fila.get("truncada"):
        acc["n_truncada"] += 1

    pedido = _num(fila, "max_tokens_pedido")
    efectivo = _num(fila, "max_tokens_efectivo")
    if pedido and efectivo and efectivo < pedido:
        acc["n_clamp"] += 1

    acc["input_tokens"] += _num(fila, "uso", "input_tokens")
    acc["output_tokens"] += _num(fila, "uso", "output_tokens")
    acc["razonamiento_oculto"] += _num(fila, "razonamiento_oculto")
    acc["transacciones_pesimista"] += transacciones_pesimista(fila)
    acc["transacciones_optimista"] += transacciones_optimista(fila, chars_por_token)
    acc["costo_registrado_usd"] += _num(fila, "costo_estimado_usd")

    # La latencia se mide solo sobre las llamadas que salieron bien: un timeout
    # de 60 s y una respuesta de 1,8 s no son la misma magnitud, y promediarlas
    # convierte la tasa de error en un percentil, que es el peor de dos mundos.
    if ok:
        lat = _num(fila, "latencia_ms")
        if lat > 0:
            acc["latencias"].append(lat)

    if fila.get("etapa"):
        acc["etapas"].add(str(fila["etapa"]))
    if fila.get("modelo"):
        acc["modelos"].add(str(fila["modelo"]))
    if fila.get("version"):
        acc["versiones"].add(str(fila["version"]))

    ts = fila.get("_ts")
    if isinstance(ts, datetime):
        clave = ts.timestamp()
        if acc["primer_ts"] is None or clave < acc["primer_ts"].timestamp():
            acc["primer_ts"] = ts
        if acc["ultimo_ts"] is None or clave > acc["ultimo_ts"].timestamp():
            acc["ultimo_ts"] = ts


def _percentil(ordenados: list[float], p: float) -> float | None:
    """Percentil por rango más cercano. Sin interpolar: son milisegundos medidos,
    no una distribución continua, y un valor que existió es más honesto que uno
    promediado entre dos que existieron."""
    if not ordenados:
        return None
    i = min(int(p * (len(ordenados) - 1) + 0.5), len(ordenados) - 1)
    return ordenados[i]


def _cerrar(acc: dict, grupo: str, tarifas: dict[str, float | None]) -> dict:
    """Convierte un acumulador en las métricas del informe. Nunca divide por cero."""
    n = acc["n"]
    familia = familia_de(grupo)
    tarifa = tarifas.get(familia) if grupo.startswith("oci") else None
    lat = sorted(acc["latencias"])

    pes = _usd(acc["transacciones_pesimista"], tarifa)
    opt = _usd(acc["transacciones_optimista"], tarifa)

    # ⚠️ `razonamiento_oculto` NO se le suma a `output_tokens`: ya está adentro.
    # `Uso.desde_oci` (src/llm/contrato.py) calcula `output_tokens = total_tokens
    # - prompt_tokens` cuando esa resta es mayor que `completion_tokens`,
    # justamente para recuperar el razonamiento que las familias externas
    # facturan y no reportan. O sea: con Grok, `output_tokens` ya vale 514 y
    # `razonamiento_oculto` vale 496 de esos 514 — es un DESGLOSE, no un extra.
    # Sumarlos daría 1.010 y sobreestimaría el gasto de las familias externas
    # justo en el eje que esta comparación existe para medir.
    # `facturada` es el total que se paga; `visible` es la parte que el
    # proveedor sí reportó como salida. La fracción invisible se mide contra lo
    # facturado, que es el número que aparece en la factura.
    oculto = acc["razonamiento_oculto"]
    facturada = acc["output_tokens"]
    visible = max(0.0, facturada - oculto)

    return {
        "n": n,
        "n_ok": acc["n_ok"],
        "n_error": acc["n_error"],
        "n_truncada": acc["n_truncada"],
        "n_clamp": acc["n_clamp"],
        "tasa_error": (acc["n_error"] / n) if n else None,
        "tasa_truncamiento": (acc["n_truncada"] / n) if n else None,
        "input_tokens": int(acc["input_tokens"]),
        "output_tokens": int(acc["output_tokens"]),
        "razonamiento_oculto": int(oculto),
        # El desglose se calcula UNA vez acá y viaja en el resultado. Antes cada
        # sitio que lo imprimía lo recomponía por su cuenta, y por ahí entró el
        # doble conteo que este bloque existe para impedir.
        "output_tokens_visible": int(visible),
        "fraccion_razonamiento_oculto": (oculto / facturada) if facturada else None,
        "input_tokens_por_llamada": (acc["input_tokens"] / n) if n else None,
        "output_tokens_por_llamada": (acc["output_tokens"] / n) if n else None,
        "latencia_ms": {
            "n": len(lat),
            "p50": _percentil(lat, 0.50),
            "p95": _percentil(lat, 0.95),
            "media": (sum(lat) / len(lat)) if lat else None,
        },
        "transacciones_pesimista": int(acc["transacciones_pesimista"]),
        "transacciones_optimista": int(acc["transacciones_optimista"]),
        "tarifa_10k": tarifa,
        "costo_pesimista_usd": pes,
        "costo_optimista_usd": opt,
        "costo_registrado_usd": acc["costo_registrado_usd"],
        "usd_por_llamada_pesimista": (pes / n) if (pes is not None and n) else None,
        "usd_por_llamada_optimista": (opt / n) if (opt is not None and n) else None,
        "usd_por_llamada_registrado": (acc["costo_registrado_usd"] / n) if n else None,
        "etapas": sorted(acc["etapas"]),
        "modelos": sorted(acc["modelos"]),
        "versiones": sorted(acc["versiones"]),
        "desde": acc["primer_ts"].isoformat() if acc["primer_ts"] else None,
        "hasta": acc["ultimo_ts"].isoformat() if acc["ultimo_ts"] else None,
    }


# ── Advertencias: cuándo la comparación NO es justa ──────────────────────────

# Debajo de esto un promedio no describe un comportamiento, describe tres
# corridas. No es un umbral estadístico fino: es el punto donde un operador
# leyendo la tabla se merece un cartel.
N_MINIMO_CONFIABLE = 30


def _advertencias(res: dict) -> list[str]:
    """Todo lo que hace que dos columnas de la tabla no sean comparables.

    Existe porque el modo de falla de este informe no es equivocarse en una
    cuenta: es presentar un promedio sobre 3 llamadas con la misma tipografía
    que uno sobre 300 y que alguien decida el proveedor con eso.
    """
    avisos: list[str] = []
    grupos = res["grupos"]

    for proveedor in PROVEEDORES_ESPERADOS:
        presentes = [g for g in grupos if g == proveedor or g.startswith(f"{proveedor}/")]
        if not presentes:
            avisos.append(
                f"SIN DATOS de `{proveedor}`: no hay ninguna llamada registrada. "
                "La comparación entre proveedores no es posible con este registro."
            )

    for grupo, m in sorted(grupos.items()):
        if m["n"] < N_MINIMO_CONFIABLE:
            avisos.append(
                f"`{grupo}` tiene solo {m['n']} llamada(s) (mínimo sugerido "
                f"{N_MINIMO_CONFIABLE}): sus promedios y percentiles son indicativos, "
                "no medidas."
            )
        if grupo.startswith("oci") and m["tarifa_10k"] is None:
            familia = familia_de(grupo) or "?"
            avisos.append(
                f"TARIFA DESCONOCIDA para la familia `{familia}`: OCI no publica el "
                "precio de reventa. Se informan las transacciones pero NO el costo. "
                "Pasar `--tarifa-oci-10k N` cuando se consiga el número."
            )
        if familia_de(grupo) in FAMILIAS_CON_RAZONAMIENTO_OCULTO:
            avisos.append(
                f"`{grupo}` usa el ratio chars/token de la familia `nativo` "
                f"({CHARS_POR_TOKEN_OCI}) porque el suyo no está medido: la hipótesis "
                "optimista de esa fila es más floja que la del resto."
            )

    # Cobertura de etapas: comparar el costo global de dos grupos que corrieron
    # etapas distintas es comparar dos pipelines, no dos proveedores.
    cobertura = {g: set(m["etapas"]) for g, m in grupos.items()}
    if len(cobertura) > 1:
        comunes = set.intersection(*cobertura.values()) if cobertura else set()
        for grupo, etapas in sorted(cobertura.items()):
            faltan = sorted(set().union(*cobertura.values()) - etapas)
            if faltan:
                avisos.append(
                    f"`{grupo}` no tiene datos de: {', '.join(faltan)}. Comparar su "
                    "total contra el de otro grupo suma etapas distintas."
                )
        if not comunes:
            avisos.append(
                "NINGUNA etapa tiene datos de todos los grupos: no hay una sola "
                "comparación manzana con manzana en este registro."
            )

    # Ventanas temporales: si no se solapan, la diferencia puede ser el corpus
    # de ese día y no el proveedor.
    ventanas = {g: (m["desde"], m["hasta"]) for g, m in grupos.items()
                if m["desde"] and m["hasta"]}
    if len(ventanas) > 1:
        items = sorted(ventanas.items())
        for (ga, (da, ha)), (gb, (db, hb)) in zip(items, items[1:]):
            if ha < db or hb < da:
                avisos.append(
                    f"`{ga}` y `{gb}` se midieron en períodos que NO se solapan "
                    f"({da[:10]}…{ha[:10]} vs {db[:10]}…{hb[:10]}): la diferencia "
                    "puede ser el corpus de esos días y no el proveedor."
                )

    versiones = set().union(*(set(m["versiones"]) for m in grupos.values())) if grupos else set()
    if len(versiones) > 1:
        avisos.append(
            f"El registro abarca {len(versiones)} versiones del código "
            f"({', '.join(sorted(versiones))}): parte de la diferencia puede ser un "
            "cambio de prompt y no del modelo."
        )

    if res["registro"]["corruptas"]:
        avisos.append(
            f"Se saltearon {res['registro']['corruptas']} línea(s) ilegible(s) del "
            "registro (el archivo es append-only y puede estar siendo escrito)."
        )
    return avisos


def agregar(
    filas: list[dict],
    diag: dict,
    tarifas: dict[str, float | None] | None = None,
    chars_por_token: float = CHARS_POR_TOKEN_OCI,
) -> dict:
    """El informe entero como estructura de datos. `imprimir` y `--json` la comparten."""
    tarifas = dict(TARIFAS_OCI_10K if tarifas is None else tarifas)

    por_grupo: dict[str, dict] = {}
    por_etapa: dict[str, dict[str, dict]] = {}

    for fila in filas:
        grupo = grupo_de(fila)
        etapa = str(fila.get("etapa") or "desconocida")
        _acumular(por_grupo.setdefault(grupo, _acumulador()), fila, chars_por_token)
        _acumular(
            por_etapa.setdefault(etapa, {}).setdefault(grupo, _acumulador()),
            fila,
            chars_por_token,
        )

    res = {
        "registro": dict(diag),
        "tarifas_oci_10k": tarifas,
        "chars_por_token_oci": chars_por_token,
        "grupos": {g: _cerrar(a, g, tarifas) for g, a in sorted(por_grupo.items())},
        "etapas": {
            e: {g: _cerrar(a, g, tarifas) for g, a in sorted(gs.items())}
            for e, gs in sorted(por_etapa.items(), key=lambda kv: _orden_etapa(kv[0]))
        },
    }
    res["advertencias"] = _advertencias(res)
    res["verificacion_factura"] = _verificacion_factura(res)
    return res


def _orden_etapa(etapa: str) -> tuple[int, str]:
    """Orden del pipeline primero, alfabético para lo que no esté en la tabla."""
    return (ETAPAS.index(etapa) if etapa in ETAPAS else len(ETAPAS), etapa)


def _verificacion_factura(res: dict) -> dict:
    """Qué número hay que ir a buscar a Cost Analysis, y en qué ventana.

    Sin esto el informe es una especulación bien formateada: las dos hipótesis
    solo sirven si alguien va a la consola y ve cuál de las dos se parece a lo
    facturado.
    """
    oci = {g: m for g, m in res["grupos"].items() if g.startswith("oci")}
    if not oci:
        return {"aplica": False, "motivo": "No hay llamadas a OCI en el registro."}

    fechas = [m["desde"][:10] for m in oci.values() if m["desde"]]
    fechas += [m["hasta"][:10] for m in oci.values() if m["hasta"]]
    pes = sum(m["costo_pesimista_usd"] or 0.0 for m in oci.values())
    opt = sum(m["costo_optimista_usd"] or 0.0 for m in oci.values())
    sin_tarifa = sorted({familia_de(g) for g, m in oci.items() if m["tarifa_10k"] is None})

    return {
        "aplica": True,
        "servicio": "Generative AI Service (Cost Analysis → filtrar por servicio)",
        "desde": min(fechas) if fechas else None,
        "hasta": max(fechas) if fechas else None,
        "usd_hipotesis_pesimista": pes,
        "usd_hipotesis_optimista": opt,
        "familias_sin_tarifa": sin_tarifa,
        "transacciones_pesimista": sum(m["transacciones_pesimista"] for m in oci.values()),
        "transacciones_optimista": sum(m["transacciones_optimista"] for m in oci.values()),
    }


# ── Informe legible ──────────────────────────────────────────────────────────


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _ms(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _usd_txt(x: float | None, decimales: int = 4) -> str:
    return "s/tarifa" if x is None else f"{x:.{decimales}f}"


def _titulo(texto: str) -> None:
    print(f"\n{texto}")
    print("─" * min(len(texto), 96))


def imprimir(res: dict) -> None:
    reg = res["registro"]
    print(f"\nREGISTRO: {reg['archivo']}")
    print(f"  líneas leídas: {reg['lineas']}   ilegibles salteadas: {reg['corruptas']}"
          f"   fuera de la ventana: {reg['fuera_de_rango']}   sin fecha: {reg['sin_ts']}")

    if not res["grupos"]:
        print("\nNo quedó ninguna llamada para analizar. Nada que comparar.")
        return

    _imprimir_hipotesis(res)
    _imprimir_por_etapa(res)
    _imprimir_razonamiento(res)
    _imprimir_salud(res)

    _titulo("ADVERTENCIAS — leer antes de decidir nada con las tablas de arriba")
    if not res["advertencias"]:
        print("  Ninguna: los grupos son comparables entre sí.")
    for aviso in res["advertencias"]:
        # Envueltas y sangradas: una advertencia que se sale de la terminal y se
        # parte sola en dos renglones desalineados se lee como ruido, y el punto
        # de esta sección es que NO se saltee.
        print(textwrap.fill(aviso, width=92, initial_indent="  ⚠️  ",
                            subsequent_indent="      "))


def _imprimir_hipotesis(res: dict) -> None:
    _titulo("1 · CÓMO FACTURA OCI LAS IMÁGENES — las dos hipótesis, lado a lado")
    oci = {g: m for g, m in res["grupos"].items() if g.startswith("oci")}
    if not oci:
        print("  No hay llamadas a OCI en este registro: la pregunta no aplica todavía.")
        return

    print("  Oracle documenta «1 character is calculated as 1 transaction» pero no dice")
    print("  qué hace con una imagen. Las dos lecturas posibles del mismo tráfico:\n")
    print(f"  {'grupo':<14}{'llam':>6}{'trans PESIM':>14}{'trans OPTIM':>14}"
          f"{'USD pesim':>12}{'USD optim':>12}{'factor':>8}")
    print("  " + "-" * 80)
    for grupo, m in sorted(oci.items()):
        tp, to = m["transacciones_pesimista"], m["transacciones_optimista"]
        factor = f"{tp / to:.2f}x" if to else "—"
        print(f"  {grupo:<14}{m['n']:>6}{tp:>14,}{to:>14,}"
              f"{_usd_txt(m['costo_pesimista_usd']):>12}"
              f"{_usd_txt(m['costo_optimista_usd']):>12}{factor:>8}")

    v = res["verificacion_factura"]
    print("\n  QUÉ BUSCAR EN LA CONSOLA para saber cuál de las dos es la buena:")
    print("    · Consola OCI → Billing & Cost Management → Cost Analysis")
    print(f"    · Servicio:  {v['servicio']}")
    print(f"    · Ventana:   {v['desde']} … {v['hasta']}  (inclusive, huso del registro)")
    print(f"    · Si el total facturado ≈ ${v['usd_hipotesis_pesimista']:.4f} → "
          "OCI cobra el base64 (pesimista).")
    print(f"    · Si el total facturado ≈ ${v['usd_hipotesis_optimista']:.4f} → "
          "OCI cobra la imagen por tokens (optimista).")
    if v["familias_sin_tarifa"]:
        print(f"    ⚠️  Las familias {', '.join(v['familias_sin_tarifa'])} no tienen tarifa "
              "conocida y NO están en esos totales.")


def _imprimir_por_etapa(res: dict) -> None:
    _titulo("2 · COMPARACIÓN POR ETAPA — manzana con manzana (mirá siempre el N)")
    print("  Para `anthropic` las dos columnas de USD son el MISMO número: su factura es")
    print("  por token y no admite dos hipótesis; se repite para que la fila se lea derecho.")
    cab = (f"  {'grupo':<14}{'n':>5}{'err':>7}{'trunc':>7}{'tok in/ll':>11}"
           f"{'tok out/ll':>11}{'p50 ms':>9}{'p95 ms':>9}{'USD/ll pes':>12}{'USD/ll opt':>12}")
    for etapa, grupos in res["etapas"].items():
        print(f"\n  ETAPA: {etapa}")
        print(cab)
        print("  " + "-" * 95)
        for grupo, m in sorted(grupos.items()):
            # Anthropic no tiene dos hipótesis: su factura es por token y ya está
            # en `costo_estimado_usd`. Se repite el mismo número en las dos
            # columnas para que la fila se lea en la misma línea visual.
            if grupo.startswith("oci"):
                pes = _usd_txt(m["usd_por_llamada_pesimista"], 6)
                opt = _usd_txt(m["usd_por_llamada_optimista"], 6)
            else:
                pes = opt = _usd_txt(m["usd_por_llamada_registrado"], 6)
            print(f"  {grupo:<14}{m['n']:>5}{_pct(m['tasa_error']):>7}"
                  f"{_pct(m['tasa_truncamiento']):>7}"
                  f"{(m['input_tokens_por_llamada'] or 0):>11,.0f}"
                  f"{(m['output_tokens_por_llamada'] or 0):>11,.0f}"
                  f"{_ms(m['latencia_ms']['p50']):>9}{_ms(m['latencia_ms']['p95']):>9}"
                  f"{pes:>12}{opt:>12}")


def _imprimir_razonamiento(res: dict) -> None:
    _titulo("3 · RAZONAMIENTO OCULTO — el gasto que no aparece en `completion_tokens`")
    print("  Grok y Gemini gastan tokens de razonamiento que se facturan y no se")
    print("  reportan. Con Claude este costo no existe, así que ignorarlo inclina la")
    print("  comparación a favor de OCI sin que se note.\n")
    print(f"  {'grupo':<14}{'out visible':>13}{'oculto':>12}{'out real':>12}"
          f"{'% invisible':>14}")
    print("  " + "-" * 65)
    for grupo, m in sorted(res["grupos"].items()):
        # `output_tokens` YA incluye el razonamiento oculto (ver `_metricas`):
        # la columna "out real" es ese total, y la visible es el total menos lo
        # oculto. Sumarlas sería contar el razonamiento dos veces.
        print(f"  {grupo:<14}{m['output_tokens_visible']:>13,}"
              f"{m['razonamiento_oculto']:>12,}{m['output_tokens']:>12,}"
              f"{_pct(m['fraccion_razonamiento_oculto']):>14}")


def _imprimir_salud(res: dict) -> None:
    _titulo("4 · SALUD OPERATIVA — truncamientos y clamp del techo de salida")
    print("  `trunc` = la respuesta se cortó (el techo de Meta es 4.096 y el análisis")
    print("  pide 16.000). `clamp` = max_tokens_efectivo < max_tokens_pedido, o sea el")
    print("  recorte actuando antes de llamar; no es un error, pero explica truncamientos.\n")
    print(f"  {'etapa':<16}{'grupo':<14}{'n':>5}{'trunc':>8}{'clamp':>8}{'errores':>9}")
    print("  " + "-" * 60)
    hubo = False
    for etapa, grupos in res["etapas"].items():
        for grupo, m in sorted(grupos.items()):
            if not (m["n_truncada"] or m["n_clamp"] or m["n_error"]):
                continue
            hubo = True
            print(f"  {etapa:<16}{grupo:<14}{m['n']:>5}{m['n_truncada']:>8}"
                  f"{m['n_clamp']:>8}{m['n_error']:>9}")
    if not hubo:
        print("  Sin truncamientos, sin clamps y sin errores en todo el registro.")


# ── CLI ──────────────────────────────────────────────────────────────────────


def _json_limpio(obj):
    """`set` y `datetime` no son JSON. Se normalizan acá y no en `agregar` para
    que la estructura interna siga siendo cómoda de recorrer en los tests."""
    if isinstance(obj, dict):
        return {k: _json_limpio(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_limpio(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, float):
        return round(obj, 8)
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    data_dir = os.getenv("DATA_DIR", "data")
    ap.add_argument(
        "--registro", default=os.path.join(data_dir, "llm_consumo.jsonl"),
        help="JSONL de consumo (default: $DATA_DIR/llm_consumo.jsonl)",
    )
    ap.add_argument(
        "--fuente", choices=("auto", "adb", "jsonl"), default="auto",
        help="auto: la ADB si CUOTA_FUENTE=adb y responde, si no el JSONL; adb: solo la ADB; jsonl: solo el archivo",
    )
    ap.add_argument("--desde", help="Fecha o timestamp ISO inicial (inclusive)")
    ap.add_argument("--hasta", help="Fecha o timestamp ISO final (inclusive)")
    ap.add_argument("--json", action="store_true", help="Emitir JSON en vez del informe")
    ap.add_argument(
        "--tarifa-oci-10k", type=float, default=None, dest="tarifa_oci_10k",
        help="USD por 10.000 transacciones, para TODAS las familias de OCI. Úsese "
             "cuando se consiga la tarifa de xai/google, que OCI no publica.",
    )
    args = ap.parse_args(argv)

    try:
        desde = _limite(args.desde, fin=False) if args.desde else None
        hasta = _limite(args.hasta, fin=True) if args.hasta else None
    except ValueError as e:
        print(f"Fecha inválida: {e}", file=sys.stderr)
        return 2

    leido = None
    if args.fuente == "adb" or (args.fuente == "auto" and os.getenv("CUOTA_FUENTE", "").strip().lower() == "adb"):
        try:
            from src.config import get_config

            leido = leer_adb(get_config(), desde, hasta)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo consultar la ADB (%s); se usa el JSONL.", type(exc).__name__)
        if leido is None and args.fuente == "adb":
            print("La ADB no está disponible.", file=sys.stderr)
            return 1

    if leido is None and not os.path.exists(args.registro):
        print(f"No existe el registro {args.registro}.\n"
              "Todavía no corrió ninguna llamada instrumentada, o el DATA_DIR es otro.",
              file=sys.stderr)
        return 1

    filas, diag = leido if leido is not None else leer_registro(args.registro, desde, hasta)

    tarifas = dict(TARIFAS_OCI_10K)
    if args.tarifa_oci_10k is not None:
        tarifas = {f: args.tarifa_oci_10k for f in tarifas}

    res = agregar(filas, diag, tarifas)

    if args.json:
        print(json.dumps(_json_limpio(res), indent=2, ensure_ascii=False))
    else:
        imprimir(res)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    raise SystemExit(main())
