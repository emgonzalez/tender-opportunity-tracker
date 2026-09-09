#!/usr/bin/env python3
"""Calibra el coeficiente de la guardia de gasto contra el consumo REAL.

Lee `data/history/*.analisis.jsonl` —que desde la instrumentación de costo ya
guarda `n_paginas`, `uso_triaje` y `uso_analisis`— y contrasta lo que la guardia
*estima* contra lo que la API *cobró*. Cierra el lazo del paso 3: la estimación
del triaje la controlamos nosotros y debería dar casi exacta; la del análisis
depende de a qué resolución rasteriza Anthropic del lado servidor, y por eso su
coeficiente (ANALISIS_TOKENS_POR_PAGINA) es el único número que hay que medir.

NO gasta un solo token: solo lee archivos que ya existen.

Uso:
    python -m scripts.calibrar_cuota                       # data/ por defecto
    python -m scripts.calibrar_cuota --data-dir ./data
    python -m scripts.calibrar_cuota --json                # salida procesable
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# Ejecutable como script suelto (`python scripts/calibrar_cuota.py`) además de
# como módulo: sin esto, `src` no está en el path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.drilldown.quota import ANALISIS_TOKENS_POR_PAGINA, costo_usd  # noqa: E402


def leer_analisis(data_dir: str) -> list[dict]:
    """Todas las líneas de todos los `*.analisis.jsonl`, tolerando corrupción."""
    filas: list[dict] = []
    patron = os.path.join(data_dir, "history", "*.analisis.jsonl")
    for path in sorted(glob.glob(patron)):
        cliente = os.path.basename(path).split(".")[0]
        with open(path, "r", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    d = json.loads(linea)
                except json.JSONDecodeError:
                    continue  # una línea rota no invalida el historial
                d["_cliente"] = cliente
                filas.append(d)
    return filas


def _uso(fila: dict, clave: str) -> dict | None:
    """Bloque de uso con llamadas > 0, o None si esa etapa no llegó a correr."""
    u = fila.get(clave)
    if isinstance(u, dict) and u.get("llamadas", 0) > 0:
        return u
    return None


def _stats(valores: list[float]) -> dict:
    """min / mediana / p90 / max. La mediana manda: el promedio lo secuestra
    un pliego de 100 páginas entre veinte de cinco."""
    if not valores:
        return {}
    s = sorted(valores)
    def pct(p: float) -> float:
        return s[min(int(p * (len(s) - 1) + 0.5), len(s) - 1)]
    return {"n": len(s), "min": s[0], "mediana": pct(0.5), "p90": pct(0.9), "max": s[-1]}


# Unidad en la que se normaliza el consumo de cada etapa. NO es cosmética: el
# análisis manda el pliego completo, así que su costo escala con las páginas; el
# triaje manda a lo sumo `triage_raster_pages`, así que su costo es plano y
# dividirlo por páginas produciría un "tok/pág" que baja con pliegos más largos
# — justo la lectura al revés.
UNIDADES = {"triaje": "llamada", "analisis": "pagina"}


def analizar(filas: list[dict]) -> dict:
    """Agrupa el consumo real por etapa y formato, normalizado por su unidad."""
    etapas: dict[str, dict] = {}
    gasto_total = 0.0
    modelos: dict[str, dict] = {}

    for clave, nombre in (("uso_triaje", "triaje"), ("uso_analisis", "analisis")):
        for fila in filas:
            u = _uso(fila, clave)
            if not u:
                continue

            modelo = u.get("modelo", "")
            tin = u.get("input_tokens", 0)
            tout = u.get("output_tokens", 0)
            gasto = costo_usd(modelo, tin, tout)
            gasto_total += gasto

            m = modelos.setdefault(modelo, {"llamadas": 0, "input_tokens": 0,
                                            "output_tokens": 0, "gasto_usd": 0.0})
            m["llamadas"] += u.get("llamadas", 0)
            m["input_tokens"] += tin
            m["output_tokens"] += tout
            m["gasto_usd"] += gasto

            unidad = UNIDADES[nombre]
            if unidad == "pagina":
                # Las corridas anteriores a la instrumentación traen n_paginas = 0
                # y no se pueden normalizar; su gasto ya quedó contado arriba.
                n_pag = fila.get("n_paginas", 0) or 0
                if n_pag <= 0:
                    continue
                normalizado = tin / n_pag
            else:
                normalizado = tin / max(u.get("llamadas", 1), 1)

            formato = fila.get("formato") or "DESCONOCIDO"
            e = etapas.setdefault(
                f"{nombre}/{formato}",
                {"por_unidad": [], "gasto_usd": [], "modelo": modelo, "unidad": unidad})
            e["por_unidad"].append(normalizado)
            e["gasto_usd"].append(gasto)

    return {
        "filas": len(filas),
        "gasto_total_usd": round(gasto_total, 4),
        "modelos": {k: {**v, "gasto_usd": round(v["gasto_usd"], 6)}
                    for k, v in modelos.items()},
        "etapas": {k: {"modelo": v["modelo"],
                       "unidad": v["unidad"],
                       "tokens_por_unidad": _stats(v["por_unidad"]),
                       "usd_por_pliego": _stats(v["gasto_usd"])}
                   for k, v in sorted(etapas.items())},
    }


def _recomendacion(res: dict) -> tuple[int | None, str]:
    """Coeficiente sugerido para ANALISIS_TOKENS_POR_PAGINA, o None si no hay datos.

    Se usa el p90 y no la mediana: la guardia debe cubrir el pliego caro, no el
    típico. Se redondea hacia arriba a la centena para no dar una falsa precisión.
    """
    muestras = [e["tokens_por_unidad"] for k, e in res["etapas"].items()
                if k.startswith("analisis/") and e["tokens_por_unidad"]]
    if not muestras:
        return None, ("Sin datos de análisis todavía. El coeficiente sigue siendo el "
                      "prior documentado; hace falta una corrida con pliegos que "
                      "lleguen a ANALIZADO.")
    n = sum(m["n"] for m in muestras)
    p90 = max(m["p90"] for m in muestras)
    sugerido = int(-(-p90 // 100) * 100)
    nota = f"basado en {n} análisis reales (p90 = {p90:,.0f} tok/pág)"
    if n < 10:
        nota += " — muestra chica, tratar como provisional"
    return sugerido, nota


def imprimir(res: dict) -> None:
    print(f"\nRegistros de análisis leídos: {res['filas']}")
    if not res["modelos"]:
        print("\nNo hay consumo instrumentado todavía: el historial es anterior a la\n"
              "instrumentación de costo, o ningún pliego llegó a llamar al modelo.")
        return

    print(f"Gasto real acumulado en el historial: ${res['gasto_total_usd']:.4f}\n")

    print(f"{'modelo':<24}{'llamadas':>10}{'tok in':>12}{'tok out':>10}{'USD':>10}")
    print("-" * 66)
    for modelo, m in sorted(res["modelos"].items()):
        print(f"{modelo:<24}{m['llamadas']:>10}{m['input_tokens']:>12,}"
              f"{m['output_tokens']:>10,}{m['gasto_usd']:>10.4f}")

    # Dos tablas y no una: el triaje se normaliza por LLAMADA (su costo está
    # topado en triage_raster_pages y no crece con el pliego) y el análisis por
    # PÁGINA (manda el documento entero). Mezclarlas invita a leer mal el triaje.
    for etapa, unidad, titulo in (("triaje", "llamada", "tok/llamada"),
                                  ("analisis", "pagina", "tok/pág")):
        filas = [(n, e) for n, e in res["etapas"].items()
                 if n.startswith(f"{etapa}/") and e["tokens_por_unidad"]]
        if not filas:
            continue
        print(f"\n{etapa.upper()} — entrada normalizada por {unidad}")
        print(f"{'formato':<26}{'n':>5}{titulo + ' med':>16}{'p90':>10}"
              f"{'USD/pliego med':>17}")
        print("-" * 74)
        for nombre, e in filas:
            tp, usd = e["tokens_por_unidad"], e["usd_por_pliego"]
            print(f"{nombre.split('/', 1)[1]:<26}{tp['n']:>5}{tp['mediana']:>16,.0f}"
                  f"{tp['p90']:>10,.0f}{usd['mediana']:>17.4f}")

    sugerido, nota = _recomendacion(res)
    print(f"\nANALISIS_TOKENS_POR_PAGINA actual: {ANALISIS_TOKENS_POR_PAGINA:,}")
    if sugerido is None:
        print(f"  {nota}")
    else:
        print(f"  sugerido: {sugerido:,}  ({nota})")
        if sugerido > ANALISIS_TOKENS_POR_PAGINA:
            print("  ⚠️  El coeficiente actual SUBESTIMA: la guardia deja pasar\n"
                  "      análisis más caros de lo que cree. Subirlo.")
        elif sugerido < ANALISIS_TOKENS_POR_PAGINA * 0.6:
            print("  El coeficiente actual sobreestima con holgura. Bajarlo solo\n"
                  "  amplía cuántos pliegos caben por día; no es urgente.")
        else:
            print("  El coeficiente actual es razonable.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.getenv("DATA_DIR", "data"),
                    help="Directorio de datos que contiene history/ (default: data)")
    ap.add_argument("--json", action="store_true", help="Emitir JSON en vez de tabla")
    args = ap.parse_args()

    filas = leer_analisis(args.data_dir)
    if not filas:
        print(f"No se encontró ningún *.analisis.jsonl en {args.data_dir}/history/",
              file=sys.stderr)
        return 1

    res = analizar(filas)
    res["coeficiente_actual"] = ANALISIS_TOKENS_POR_PAGINA
    res["coeficiente_sugerido"], res["nota"] = _recomendacion(res)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        imprimir(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
