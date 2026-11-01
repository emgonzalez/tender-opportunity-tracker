#!/usr/bin/env python3
"""Observabilidad manual solo lectura: consumo, calidad, variantes y deriva léxica."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluar_marca import configuracion, leer, objeto


def deriva(titulos_actuales, titulos_previos):
    """Distancia coseno entre conteos de términos; None sin ambas ventanas.

    Ventana actual: últimos 7 días; referencia: los 56 días inmediatamente anteriores.
    No necesita embeddings ni descargar vectores. No interpreta esta medida como alarma.
    """
    def contar(titulos):
        return Counter(t for titulo in titulos for t in re.findall(r"[^\W\d_]{3,}", (titulo or "").lower()))
    a, b = contar(titulos_actuales), contar(titulos_previos)
    norma = math.sqrt(sum(v * v for v in a.values()) * sum(v * v for v in b.values()))
    return max(0.0, 1 - sum(v * b[k] for k, v in a.items()) / norma) if norma else None


def resumir(llamadas, decisiones):
    """Cada llamada suma costo una vez; enlace conservador sin multiplicar por decisiones."""
    grupos = defaultdict(lambda: dict(n=0, costo_usd=0.0, latencia_ms=0.0, errores=0, enlazadas=0))
    indice = set()
    ultimas = {}
    for cliente, gid, capa, variante, salida, entrada_hash in decisiones:
        salida = objeto(salida) or {}
        indice.add((cliente, gid, capa, variante, entrada_hash))
        ultimas[(cliente, gid, capa, variante)] = salida
    for etapa, costo, latencia, ok, error, detalle in llamadas:
        detalle = objeto(detalle) or {}
        ctx = detalle.get("contexto") or {}
        capa, variante = ctx.get("capa", "sin_capa"), ctx.get("variante", "sin_variante")
        g = grupos[(etapa, capa, variante)]
        g["n"] += 1
        g["costo_usd"] += float(costo or 0)
        g["latencia_ms"] += float(latencia or 0)
        g["errores"] += not ok or "ERROR_LLM" in str(error) or "parse_error" in str(error).lower()
        clave = (ctx.get("cliente_id"), ctx.get("proceso_gid"), capa, variante, detalle.get("prompt_hash"))
        g["enlazadas"] += clave in indice
    consumo = []
    for (etapa, capa, variante), g in sorted(grupos.items(), key=lambda x: str(x[0])):
        consumo.append(dict(etapa=etapa, capa=capa, variante=variante, **g,
                            latencia_media_ms=g["latencia_ms"] / g["n"], tasa_error=g["errores"] / g["n"]))
    def veredictos(salida):
        if isinstance(salida, list):
            return {str(m.get("marca_id", m.get("marca", ""))): m.get("veredicto")
                    for m in salida if isinstance(m, dict)}
        if isinstance(salida, dict):
            if "marcas" in salida or "marcas_recomendadas" in salida:
                return veredictos(salida.get("marcas", salida.get("marcas_recomendadas")))
            return {str(salida.get("marca_id", salida.get("marca", ""))): salida.get("veredicto")}
        return {}
    pares = cambios = inciertos = total = errores_decision = 0
    for (cliente, gid, capa, variante), salida in ultimas.items():
        vs = veredictos(salida)
        validos = [v for v in vs.values() if v is not None]
        total += len(validos)
        inciertos += sum(str(v).lower() in {"incierto", "por_confirmar"} for v in validos)
        errores_decision += "parse_error" in json.dumps(salida).lower() or "ERROR_LLM" in json.dumps(salida)
        if variante == "activa" and (cliente, gid, capa, "sombra") in ultimas:
            sombra = veredictos(ultimas[(cliente, gid, capa, "sombra")])
            for marca in vs.keys() & sombra.keys():
                if vs[marca] is not None and sombra[marca] is not None:
                    pares += 1
                    cambios += vs[marca] != sombra[marca]
    return {"consumo": consumo, "veredictos": total, "porcentaje_incierto": 100 * inciertos / total if total else None,
            "pares": pares, "tasa_cambio": cambios / pares if pares else None,
            "decisiones": len(ultimas), "tasa_error_decision": errores_decision / len(ultimas) if ultimas else None}


def informe(conn, dias=7):
    llamadas = leer(conn, "SELECT etapa, costo_estimado_usd, latencia_ms, ok, error_msg, "
                    "JSON_SERIALIZE(detalle RETURNING CLOB) FROM llm_llamada "
                    "WHERE ts >= SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')", {"dias": dias})
    decisiones = leer(conn, "SELECT cliente_id, proceso_gid, capa, variante, JSON_SERIALIZE(salida RETURNING CLOB), "
                      "entrada_hash FROM decision_capa WHERE creado_en >= SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY') "
                      "ORDER BY creado_en, id", {"dias": dias})
    titulos = leer(conn, "SELECT titulo, CASE WHEN fecha_pub_utc >= SYS_EXTRACT_UTC(SYSTIMESTAMP) - INTERVAL '7' DAY "
                   "THEN 1 ELSE 0 END FROM proceso WHERE fecha_pub_utc >= "
                   "SYS_EXTRACT_UTC(SYSTIMESTAMP) - INTERVAL '63' DAY")
    resultado = resumir(llamadas, decisiones)
    resultado["deriva"] = {"metodo": "coseno_conteos_terminos", "dias_actuales": 7, "semanas_referencia": 8,
                           "distancia": deriva([t for t, actual in titulos if actual],
                                                [t for t, actual in titulos if not actual])}
    return resultado


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dias", type=int, default=7)
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.dias <= 0:
        p.error("--dias debe ser positivo")
    if args.dry_run:
        print("Solo lectura: consumo por etapa/capa, enlace por hash/contexto, variantes y deriva; sin conexión.")
        return 0
    try:
        from src.memoria.db import connection
        with connection(configuracion()) as conn:
            resultado = informe(conn, args.dias)
        if args.json:
            print(json.dumps(resultado, ensure_ascii=False, allow_nan=False))
        else:
            print("Etapa / capa / variante: llamadas costo_USD latencia_media_ms tasa_error enlazadas")
            for g in resultado.pop("consumo"):
                print(g["etapa"], g["capa"], g["variante"], g["n"], round(g["costo_usd"], 6),
                      round(g["latencia_media_ms"], 1), g["tasa_error"], g["enlazadas"])
            print(json.dumps(resultado, ensure_ascii=False))
        return 0
    except Exception:
        print("Métricas no disponibles: revise configuración ADB, tablas y formato del registro.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
