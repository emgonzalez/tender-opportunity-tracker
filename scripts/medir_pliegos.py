"""Mide la distribución real del corpus de pliegos de COMPRASAL. SIN LLM.

Motivación: la heurística de cuota del pipeline (`n_paginas * 2000 + 5000`)
nunca se contrastó contra datos. Este script baja una muestra de pliegos y
reporta páginas, formato (nativo/escaneado), tamaño y el costo en tokens que
tendrían como imagen — todo con PyMuPDF, **sin una sola llamada a la API de
Claude**, así que correrlo no consume presupuesto.

Uso:
    python -m scripts.medir_pliegos --limite 40 --dias 7
    python -m scripts.medir_pliegos --limite 40 --salida data/medicion.json

El script deja los PDFs borrados al terminar (`--conservar` para inspeccionarlos).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Solo `pdf` a nivel de módulo: es puro PyMuPDF. Lo que arrastra Playwright o
# credenciales se importa dentro de _correr(), para que la lógica de agregación
# de este script sea testeable sin navegador ni .env.
from src.drilldown import pdf  # noqa: E402

logger = logging.getLogger("medir_pliegos")

# Precios por millón de tokens de entrada (ver docs de Anthropic).
PRECIO_IN = {"haiku": 1.00, "sonnet": 3.00}


def _percentil(valores: list[int], p: float) -> float:
    """Percentil `p` (0-100) por interpolación lineal. [] → 0."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    if len(ordenados) == 1:
        return float(ordenados[0])
    pos = (len(ordenados) - 1) * (p / 100.0)
    bajo = int(pos)
    alto = min(bajo + 1, len(ordenados) - 1)
    return ordenados[bajo] + (ordenados[alto] - ordenados[bajo]) * (pos - bajo)


def medir_pdf(path: str, cfg) -> dict:
    """Extrae las métricas de UN pliego. No llama a ningún modelo."""
    m: dict = {"tamano_bytes": 0, "n_paginas": 0, "formato": "?", "error": ""}
    try:
        m["tamano_bytes"] = os.path.getsize(path)
    except OSError:
        pass

    try:
        m["n_paginas"] = pdf.page_count(path)
        m["formato"] = "ESCANEADO" if pdf.is_scanned(path) else "NATIVO"
    except pdf.PdfCorruptoError as exc:
        m["error"] = f"pdf_corrupto: {exc}"
        return m

    raster_n = getattr(cfg, "triage_raster_pages", 5)
    # Costo real del triaje de un escaneado: solo las primeras `raster_n` páginas.
    m["tokens_triaje_img"] = pdf.tokens_imagen_estimados(path, raster_n)
    # Costo real del análisis: hoy se manda el documento COMPLETO.
    m["tokens_doc_completo_img"] = pdf.tokens_imagen_estimados(path, None)
    # Lo que la heurística vigente predice para este pliego.
    m["heuristica_actual"] = m["n_paginas"] * 2000 + 5000

    if m["formato"] == "NATIVO":
        try:
            texto = pdf.extract_text_first_pages(path, m["n_paginas"])
            m["chars_texto"] = len(texto)
            # ~4 chars por token es la regla de dedo para español.
            m["tokens_texto_aprox"] = len(texto) // 4
        except pdf.PdfCorruptoError:
            m["chars_texto"] = 0
            m["tokens_texto_aprox"] = 0
    return m


def resumir(filas: list[dict]) -> dict:
    """Agrega las métricas por formato y compara contra la heurística vigente."""
    ok = [f for f in filas if not f.get("error") and f.get("n_paginas")]
    if not ok:
        return {"medidos": 0}

    paginas = [f["n_paginas"] for f in ok]
    escaneados = [f for f in ok if f["formato"] == "ESCANEADO"]
    nativos = [f for f in ok if f["formato"] == "NATIVO"]

    def _tokens_por_pagina(grupo: list[dict]) -> float:
        pares = [(f["tokens_doc_completo_img"], f["n_paginas"])
                 for f in grupo if f["n_paginas"]]
        if not pares:
            return 0.0
        return statistics.mean(t / n for t, n in pares)

    resumen = {
        "medidos": len(ok),
        "con_error": len(filas) - len(ok),
        "paginas": {
            "min": min(paginas), "max": max(paginas),
            "mediana": statistics.median(paginas),
            "media": round(statistics.mean(paginas), 1),
            "p90": round(_percentil(paginas, 90), 1),
            "p99": round(_percentil(paginas, 99), 1),
        },
        "sobre_80_paginas": sum(1 for p in paginas if p > 80),
        "sobre_100_paginas": sum(1 for p in paginas if p > 100),
        "formato": {
            "ESCANEADO": len(escaneados),
            "NATIVO": len(nativos),
            "pct_escaneado": round(100 * len(escaneados) / len(ok), 1),
        },
        "tokens_por_pagina_img": {
            "ESCANEADO": round(_tokens_por_pagina(escaneados)),
            "NATIVO": round(_tokens_por_pagina(nativos)),
        },
    }

    # ¿Cuánto se desvía la heurística vigente del costo real del análisis?
    desvios = [
        f["tokens_doc_completo_img"] / f["heuristica_actual"]
        for f in ok if f.get("heuristica_actual")
    ]
    if desvios:
        resumen["heuristica_vs_real"] = {
            "ratio_mediano": round(statistics.median(desvios), 2),
            "ratio_max": round(max(desvios), 2),
            "subestima_en": sum(1 for d in desvios if d > 1.0),
            "de_un_total_de": len(desvios),
        }

    # Costo real del corpus con los precios de cada modelo.
    tot_triaje = sum(f.get("tokens_triaje_img", 0) for f in escaneados)
    tot_analisis = sum(f.get("tokens_doc_completo_img", 0) for f in ok)
    resumen["costo_usd_muestra"] = {
        "triaje_haiku": round(tot_triaje * PRECIO_IN["haiku"] / 1e6, 4),
        "analisis_sonnet": round(tot_analisis * PRECIO_IN["sonnet"] / 1e6, 4),
    }
    return resumen


def imprimir(resumen: dict, filas: list[dict]) -> None:
    r = resumen
    if not r.get("medidos"):
        print("\nNo se pudo medir ningún pliego.")
        return

    print("\n" + "=" * 62)
    print(f"  MUESTRA: {r['medidos']} pliegos medidos "
          f"({r['con_error']} con error)")
    print("=" * 62)

    p = r["paginas"]
    print(f"\nPÁGINAS   min={p['min']}  mediana={p['mediana']}  "
          f"media={p['media']}  p90={p['p90']}  máx={p['max']}")
    print(f"          > 80 páginas: {r['sobre_80_paginas']}    "
          f"> 100 (rechazados): {r['sobre_100_paginas']}")

    f = r["formato"]
    print(f"\nFORMATO   escaneados={f['ESCANEADO']} ({f['pct_escaneado']}%)   "
          f"nativos={f['NATIVO']}")

    t = r["tokens_por_pagina_img"]
    print(f"\nTOKENS/PÁGINA (como imagen)   escaneado={t['ESCANEADO']}   "
          f"nativo={t['NATIVO']}")

    if "heuristica_vs_real" in r:
        h = r["heuristica_vs_real"]
        print(f"\nHEURÍSTICA `n*2000+5000` vs COSTO REAL DEL ANÁLISIS")
        print(f"          ratio mediano={h['ratio_mediano']}x   "
              f"peor caso={h['ratio_max']}x")
        print(f"          subestima en {h['subestima_en']}/{h['de_un_total_de']} pliegos")

    c = r["costo_usd_muestra"]
    print(f"\nCOSTO DE ESTA MUESTRA (solo entrada)")
    print(f"          triaje (Haiku)     ${c['triaje_haiku']}")
    print(f"          análisis (Sonnet)  ${c['analisis_sonnet']}")

    grandes = sorted((x for x in filas if x.get("n_paginas")),
                     key=lambda x: -x["n_paginas"])[:5]
    if grandes:
        print(f"\nLOS 5 MÁS GRANDES")
        for g in grandes:
            print(f"          {g['n_paginas']:>4} págs  {g['formato']:<10} "
                  f"{g['tamano_bytes']/1e6:>5.1f} MB  {g.get('uuid','')}")
    print()


async def _correr(args) -> int:
    from src.auth.session import is_session_valid
    from src.config import get_config
    from src.drilldown import pipeline
    from src.scraper.extractor import extract_recent_procesos

    cfg = get_config()
    if args.dias:
        cfg.days_lookback = args.dias

    if not await is_session_valid(cfg):
        logger.error("Sesión de COMPRASAL inválida. Corré el login antes:")
        logger.error("  docker compose run -T --rm amalia python -m src.auth.login")
        return 1

    procesos = await extract_recent_procesos(cfg)
    logger.info("Procesos extraídos: %d (ventana: %d días)",
                len(procesos), cfg.days_lookback)
    if not procesos:
        logger.warning("No hay procesos en la ventana. Probá con --dias mayor.")
        return 1

    muestra = procesos[: args.limite]
    logger.info("Midiendo una muestra de %d pliegos (sin llamadas a Claude).",
                len(muestra))

    rutas = await pipeline.descargar_pliegos(muestra, cfg)
    logger.info("Descargados: %d/%d", len(rutas), len(muestra))

    filas = []
    for proceso in muestra:
        uuid = proceso.numero_referencia
        path = rutas.get(uuid)
        if not path:
            filas.append({"uuid": uuid, "error": "sin_pliego",
                          "institucion": proceso.institucion})
            continue
        fila = medir_pdf(path, cfg)
        fila["uuid"] = uuid
        fila["institucion"] = proceso.institucion
        filas.append(fila)

    if not args.conservar:
        pipeline.limpiar_pliegos(rutas)
        logger.info("PDFs temporales borrados.")
    else:
        logger.info("PDFs conservados en %s", cfg.drilldown_tmp_dir)

    resumen = resumir(filas)
    salida = {
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "ventana_dias": cfg.days_lookback,
        "procesos_en_ventana": len(procesos),
        "muestra_solicitada": args.limite,
        "resumen": resumen,
        "pliegos": filas,
    }

    os.makedirs(os.path.dirname(args.salida) or ".", exist_ok=True)
    with open(args.salida, "w", encoding="utf-8") as fh:
        json.dump(salida, fh, ensure_ascii=False, indent=2)
    logger.info("Reporte escrito en %s", args.salida)

    imprimir(resumen, filas)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mide páginas/formato/tokens del corpus de pliegos. No usa LLM.")
    parser.add_argument("--limite", type=int, default=40,
                        help="cuántos pliegos descargar y medir (default: 40)")
    parser.add_argument("--dias", type=int, default=7,
                        help="ventana de días hacia atrás (default: 7)")
    parser.add_argument("--salida", default="data/medicion_pliegos.json",
                        help="ruta del reporte JSON")
    parser.add_argument("--conservar", action="store_true",
                        help="no borrar los PDFs descargados")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sys.exit(asyncio.run(_correr(args)))


if __name__ == "__main__":
    main()
