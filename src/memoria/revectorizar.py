"""Revectorización E5 manual: dry-run por defecto; --aplicar escribe por lotes.

Ejecutar con escritores detenidos y EMBEDDING_PREFIJOS apagado. Tras completar
ambos esquemas, activar el flag conjuntamente para lectores y escritores.
Los títulos persistidos con vector son ejemplo_marca.titulo_texto; proceso y
licitaciones_metadata no tienen vector de título. No cambia textos ni ejemplos.
El checkpoint se guarda después del commit: una caída entre ambos puede repetir
un lote, de forma idempotente. No compartir checkpoint entre bases o ejecuciones.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from src.memoria.vectorizer import _validar_nombre_modelo, con_prefijo

# Identificadores fijos: jamás vienen del CLI.
TABLAS = (
    ("licitaciones_chunks", "chunk_id", "chunk_text"),
    ("chunk", "chunk_id", "texto"),
    ("ejemplo_marca", "ejemplo_id", "titulo_texto"),
)


def revectorizar(conn, cfg, *, aplicar=False, lote=25, segundos=60,
                 checkpoint=Path("data/revectorizar-e5.json")):
    """Cuenta o recalcula todo el corpus; commits pequeños y continuación por PK.

    El tope cubre también llamadas SQL mediante call_timeout de python-oracledb.
    No crear/borrar tablas ni alterar el esquema. La conexión es de uso exclusivo.
    El checkpoint debe conservarse hasta terminar y eliminarse para una nueva
    migración; no detecta cambios concurrentes en filas ya recorridas.
    """
    if not 1 <= lote <= 100 or segundos <= 0:
        raise ValueError("Lote debe ser 1..100 y tiempo positivo")
    modelo = _validar_nombre_modelo(cfg.embedding_model_name)
    checkpoint = Path(checkpoint)
    estado = {"modelo": modelo, "ultimos": {}}
    if aplicar and checkpoint.exists():
        estado = json.loads(checkpoint.read_text())
        if estado["modelo"] != modelo:
            raise ValueError("Checkpoint de otro modelo")
    inicio = time.monotonic()
    anterior_timeout = getattr(conn, "call_timeout", 0)
    cursor = conn.cursor()
    resumen = {}

    def limitar():
        restante = segundos - (time.monotonic() - inicio)
        if restante <= 0:
            raise TimeoutError("Tope de tiempo alcanzado; reanudar con el mismo checkpoint")
        conn.call_timeout = max(1, int(restante * 1000))

    try:
        for tabla, clave, texto in TABLAS:
            limitar()
            cursor.execute(f"SELECT COUNT(*) FROM {tabla}")
            resumen[tabla] = {"total": int(cursor.fetchone()[0]), "recalculados": 0}
            if not aplicar:
                continue
            while True:
                limitar()
                ultimo = estado["ultimos"].get(tabla)
                where = f"WHERE {clave} > :ultimo" if ultimo is not None else ""
                binds = {"k": lote}
                if ultimo is not None:
                    binds["ultimo"] = ultimo
                cursor.execute(f"SELECT {clave} FROM {tabla} {where} ORDER BY {clave} FETCH FIRST :k ROWS ONLY", binds)
                filas = cursor.fetchall()
                if not filas:
                    break
                for (identificador,) in filas:
                    limitar()
                    cursor.execute(f"UPDATE {tabla} SET embedding = VECTOR_EMBEDDING({modelo} "
                                   f"USING :prefijo || {texto} AS data) WHERE {clave} = :id",
                                   {"prefijo": con_prefijo(SimpleNamespace(embedding_prefijos="on"), "passage", ""),
                                    "id": identificador})
                limitar()
                conn.commit()
                estado["ultimos"][tabla] = filas[-1][0]
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                temporal = checkpoint.with_suffix(".tmp")
                temporal.write_text(json.dumps(estado))
                os.replace(temporal, checkpoint)
                resumen[tabla]["recalculados"] += len(filas)
        return resumen
    except Exception:
        if aplicar:
            conn.rollback()
        raise
    finally:
        conn.call_timeout = anterior_timeout
        cursor.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true")
    parser.add_argument("--lote", type=int, default=25)
    parser.add_argument("--segundos", type=float, default=60)
    parser.add_argument("--checkpoint", type=Path, default=Path("data/revectorizar-e5.json"))
    args = parser.parse_args()
    from src.config import get_config
    from src.memoria.db import connection

    cfg = get_config()
    with connection(cfg) as conn:
        print(json.dumps(revectorizar(conn, cfg, aplicar=args.aplicar, lote=args.lote,
                                      segundos=args.segundos, checkpoint=args.checkpoint)))


if __name__ == "__main__":
    main()
