#!/usr/bin/env python3
"""Resume cambios de MARCA_VEREDICTO desde JSONL o una foto de la ADB, solo lectura.

Uso: python -m scripts.sombra_marca --registro data/history/demo.marca_sombra.jsonl
     python -m scripts.sombra_marca --fuente adb --cliente demo
La foto ADB compara el filtro por marca; el JSONL diario incluye también los grupos.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def leer_jsonl(ruta):
    """Ignora líneas incompletas de un registro que todavía podría estar escribiéndose."""
    filas = []
    with Path(ruta).open(encoding="utf-8") as archivo:
        for linea in archivo:
            try:
                fila = json.loads(linea)
            except (ValueError, TypeError):
                continue
            if isinstance(fila, dict):
                filas.append(fila)
    return filas


def leer_adb(config, cliente):
    """Solo SELECT; no usa el context manager que confirma transacciones al salir."""
    from src.memoria.db import get_pool
    from src.notifier.por_marca import es_de_marca_proceso, veredicto_de_proceso
    from src.scraper.models import MarcaRecomendada

    filas = []
    pool = get_pool(config)
    conn = pool.acquire()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT cp.proceso_gid, p.titulo,
                JSON_SERIALIZE(cp.marcas RETURNING CLOB)
                FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid
                WHERE cp.cliente_id = :cliente""", cliente=cliente)
            for gid, titulo, crudo in cur:
                crudo = crudo.read() if hasattr(crudo, "read") else crudo
                marcas = [MarcaRecomendada.desde_dict(m) for m in json.loads(crudo or "[]")]
                proc = SimpleNamespace(descripcion=titulo, marcas_recomendadas=marcas)
                legado = "visible" if es_de_marca_proceso(proc, {"oracle"}) else "fuera"
                nivel = veredicto_de_proceso(proc, {"oracle"})
                nuevo = "fuera" if nivel == "fuera" else "visible"
                if legado != nuevo or nivel in ("por_confirmar", "no_competible"):
                    filas.append({"perfil": ["oracle"], "proceso": gid,
                                  "legado": legado, "nuevo": nivel})
    finally:
        pool.release(conn)
    return filas


def resumir(filas):
    conteos = Counter((", ".join(f.get("perfil", [])), f.get("legado", ""), f.get("nuevo", ""))
                      for f in filas)
    return [{"perfil": perfil, "legado": legado, "nuevo": nuevo, "procesos": cantidad}
            for (perfil, legado, nuevo), cantidad in sorted(conteos.items())]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fuente", choices=("jsonl", "adb"), default="jsonl")
    parser.add_argument("--registro", help="Ruta del JSONL diario")
    parser.add_argument("--cliente", help="Identificador de cliente para la foto ADB")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.fuente == "jsonl" and not args.registro:
        parser.error("--registro es obligatorio con --fuente jsonl")
    if args.fuente == "adb" and not args.cliente:
        parser.error("--cliente es obligatorio con --fuente adb")
    try:
        if args.fuente == "adb":
            from src.config import get_config
            filas = leer_adb(get_config(), args.cliente)
        else:
            filas = leer_jsonl(args.registro)
        resumen = resumir(filas)
    except Exception as exc:  # noqa: BLE001 — no mostrar datos de conexión ni contenido
        print(f"No se pudo leer la fuente ({type(exc).__name__}).", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
    else:
        print(f"Diferencias registradas: {len(filas)}")
        for fila in resumen:
            print(f"{fila['perfil']}: {fila['legado']} → {fila['nuevo']}: {fila['procesos']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
