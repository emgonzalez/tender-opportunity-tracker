#!/usr/bin/env python3
"""Evalúa el filtro con juicios externos, solo leyendo un JSONL local.

Uso: python scripts/evaluar_filtro_ruido.py --datos muestra.jsonl [--json]
El archivo de entrada no debe versionarse. No carga Config ni usa BD o LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classifier.filtro_ruido import evaluar_ruido


def evaluar_archivo(ruta: Path) -> dict:
    """Cuenta descartes potenciales y su efecto sobre los relevantes del legado.

    Las tasas sin denominador se devuelven como None; no hay evidencia para
    afirmar precisión perfecta sobre un conjunto sin descartes.
    """
    total = filtraria = ruido_eliminado = relevantes_filtrados = 0
    relevantes_legado = falsos_positivos_legado = perdida_legado = 0
    revision = []
    with ruta.open(encoding="utf-8") as archivo:
        for numero, linea in enumerate(archivo, 1):
            if not linea.strip():
                continue
            try:
                fila = json.loads(linea)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Línea {numero}: JSON inválido") from exc
            if (not isinstance(fila, dict) or not isinstance(fila.get("titulo"), str)
                    or type(fila.get("legado")) is not int or fila["legado"] not in (0, 1)
                    or type(fila.get("juicio_relevante")) is not bool
                    or (fila.get("institucion") is not None
                        and not isinstance(fila["institucion"], str))):
                raise ValueError(f"Línea {numero}: se requieren titulo, legado 0|1 y juicio_relevante booleano")
            total += 1
            relevante = fila["juicio_relevante"]
            legado = fila["legado"] == 1
            relevantes_legado += legado and relevante
            falsos_positivos_legado += legado and not relevante
            resultado = evaluar_ruido(fila["titulo"], fila.get("institucion"))
            if not resultado.ruido or resultado.nivel != "claro":
                continue
            filtraria += 1
            ruido_eliminado += legado and not relevante
            relevantes_filtrados += relevante
            perdida_legado += legado and relevante
            if relevante:
                revision.append({"linea": numero, "titulo": fila["titulo"],
                                 "institucion": fila.get("institucion"), "legado": fila["legado"],
                                 "categoria": resultado.categoria, "termino": resultado.termino})
    return {
        "total": total, "filtraria": filtraria,
        "relevantes_filtrados": relevantes_filtrados,
        "relevantes_legado": relevantes_legado, "perdida_recall_casos": perdida_legado,
        "perdida_recall": perdida_legado / relevantes_legado if relevantes_legado else None,
        "falsos_positivos_legado": falsos_positivos_legado,
        "falsos_positivos_eliminados": ruido_eliminado,
        "reduccion_ruido": ruido_eliminado / falsos_positivos_legado if falsos_positivos_legado else None,
        "precision_filtro": (filtraria - relevantes_filtrados) / filtraria if filtraria else None,
        "revision": revision,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datos", type=Path, required=True, help="JSONL local con juicios externos")
    parser.add_argument("--json", action="store_true", help="Imprime el informe como JSON")
    args = parser.parse_args(argv)
    try:
        informe = evaluar_archivo(args.datos)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(informe, ensure_ascii=False, indent=2))
        return 0

    def tasa(clave):
        valor = informe[clave]
        return "sin denominador" if valor is None else f"{valor:.2%}"

    print(f"Procesos: {informe['total']}; filtraría: {informe['filtraria']}")
    print(f"Juicio relevante entre los filtrados: {informe['relevantes_filtrados']}")
    print(f"Pérdida de recall del legado: {informe['perdida_recall_casos']} casos ({tasa('perdida_recall')})")
    print(f"Falsos positivos del legado eliminados: {informe['falsos_positivos_eliminados']} "
          f"de {informe['falsos_positivos_legado']} ({tasa('reduccion_ruido')})")
    print(f"Precisión del filtro: {tasa('precision_filtro')}")
    print("Casos relevantes para revisión:")
    for caso in informe["revision"]:
        print(f"  Línea {caso['linea']}: {caso['titulo']} [{caso['categoria']}: {caso['termino']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
