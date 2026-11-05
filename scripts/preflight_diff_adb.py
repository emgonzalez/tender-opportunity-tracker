#!/usr/bin/env python3
"""Preflight de solo lectura DEV → PROD: exporta metadatos y compara dos JSON.

    python3 scripts/preflight_diff_adb.py --exportar --etiqueta DEV > /tmp/dev.json
    python3 scripts/preflight_diff_adb.py --comparar /tmp/dev.json /tmp/prod.json --base DEV --objetivo PROD

No exporta filas de negocio: únicamente ids de cliente y sus conteos agrupados.
La conexión compartida cierra con commit automático; no se ejecuta DML ni DDL.
"""
import argparse
import json
import logging
import sys
from pathlib import Path


def normalizar_check(texto):
    return " ".join((texto or "").lower().split())


def negocio(nombre):
    return not nombre.upper().startswith(("BIN$", "SYS_", "DR$", "ISEQ$$", "DM$"))


def identificador(nombre):
    # Los identificadores vienen del diccionario, incluso si fueron creados entre comillas.
    return '"' + nombre.replace('"', '""') + '"'


def exportar(con, etiqueta=""):
    cur = con.cursor()
    try:
        def leer(sql):
            cur.execute(sql)
            return cur.fetchall()

        tablas = sorted(r[0] for r in leer("SELECT table_name FROM user_tables") if negocio(r[0]))
        columnas = {}
        for fila in leer("SELECT table_name, column_name, data_type, data_length, data_precision, "
                         "data_scale, nullable, char_length, char_used FROM user_tab_columns"):
            tabla, columna, *datos = fila
            if tabla in tablas:
                columnas.setdefault(tabla, {})[columna] = dict(zip(
                    ("tipo", "longitud", "precision", "escala", "nulable", "longitud_char", "unidad_char"), datos,
                ))
        constraints = {}
        for nombre, tabla, tipo, condicion, estado in leer(
            "SELECT constraint_name, table_name, constraint_type, search_condition_vc, status "
            "FROM user_constraints"
        ):
            if tabla in tablas and negocio(nombre):
                constraints[nombre] = {"tabla": tabla, "tipo": tipo,
                                       "check": normalizar_check(condicion), "estado": estado}
        indices = {}
        for nombre, tabla, tipo, unico, estado in leer(
            "SELECT index_name, table_name, index_type, uniqueness, status FROM user_indexes"
        ):
            if tabla in tablas and negocio(nombre):
                indices[nombre] = {"tabla": tabla, "tipo": tipo, "unicidad": unico,
                                   "estado": estado, "columnas": []}
        for nombre, columna, posicion, direccion in leer(
            "SELECT index_name, column_name, column_position, descend FROM user_ind_columns"
        ):
            if nombre in indices:
                indices[nombre]["columnas"].append(
                    {"columna": columna, "posicion": posicion, "direccion": direccion})
        for indice in indices.values():
            indice["columnas"].sort(key=lambda c: (c["posicion"], c["columna"]))
        conteos = {}
        for tabla in tablas:
            for columna in sorted(columnas.get(tabla, {})):
                if columna not in ("CLIENTE_ID", "CLIENT_ID"):
                    continue
                t, c = identificador(tabla), identificador(columna)
                filas = leer(f"SELECT {c}, COUNT(*) FROM {t} GROUP BY {c}")
                conteos.setdefault(tabla, {})[columna] = sorted(
                    [{"cliente_id": clave, "cantidad": int(n)} for clave, n in filas],
                    key=lambda r: (r["cliente_id"] is not None, r["cliente_id"] or ""),
                )
        ids = []
        if "CLIENTE" in tablas:
            columna = next((c for c in ("CLIENTE_ID", "CLIENT_ID")
                            if c in columnas.get("CLIENTE", {})), None)
            if columna is None:
                raise ValueError("La tabla CLIENTE no tiene columna de id reconocida")
            ids = sorted(r[0] for r in leer(f'SELECT {identificador(columna)} FROM "CLIENTE"'))
        return {"etiqueta": etiqueta, "tablas": tablas, "columnas": columnas,
                "constraints": constraints, "indices": indices, "conteos": conteos, "clientes": ids}
    finally:
        cur.close()


def comparar(a, b, base=None, objetivo=None, out=print):
    base, objetivo = base or a.get("etiqueta") or "A", objetivo or b.get("etiqueta") or "B"
    out(f"Preflight: {base} → {objetivo}")
    diferencias = []

    def comparar_mapa(izq, der, clase):
        for nombre in sorted(set(izq) | set(der)):
            if nombre not in izq:
                diferencias.append(f"{clase} {nombre}: falta en {base}")
            elif nombre not in der:
                diferencias.append(f"{clase} {nombre}: falta en {objetivo}")
            elif izq[nombre] != der[nombre]:
                descripcion = "distinto"
                if clase == "Constraint" and izq[nombre].get("check") != der[nombre].get("check"):
                    descripcion = "CHECK distinto"
                diferencias.append(f"{clase} {nombre}: {descripcion} ({base}: "
                                   f"{json.dumps(izq[nombre], ensure_ascii=False, sort_keys=True)}; "
                                   f"{objetivo}: {json.dumps(der[nombre], ensure_ascii=False, sort_keys=True)})")

    comparar_mapa(dict.fromkeys(a.get("tablas", [])), dict.fromkeys(b.get("tablas", [])), "Tabla")
    for tabla in sorted(set(a.get("columnas", {})) | set(b.get("columnas", {}))):
        comparar_mapa(a.get("columnas", {}).get(tabla, {}), b.get("columnas", {}).get(tabla, {}),
                      f"Columna de {tabla}")
    for campo, clase in (("constraints", "Constraint"), ("indices", "Índice")):
        comparar_mapa(a.get(campo, {}), b.get(campo, {}), clase)
    comparar_mapa(dict.fromkeys(a.get("clientes", [])), dict.fromkeys(b.get("clientes", [])), "Id de cliente")
    for linea in diferencias:
        out(linea)
    out("Hay diferencias." if diferencias else "Sin diferencias de esquema ni ids de cliente.")
    for tabla in sorted(set(a.get("conteos", {})) | set(b.get("conteos", {}))):
        x, y = a.get("conteos", {}).get(tabla, {}), b.get("conteos", {}).get(tabla, {})
        for columna in sorted(set(x) | set(y)):
            claves_a = {r["cliente_id"] for r in x.get(columna, [])}
            claves_b = {r["cliente_id"] for r in y.get(columna, [])}
            for claves, lado in ((claves_a - claves_b, base), (claves_b - claves_a, objetivo)):
                for clave in sorted(claves, key=lambda c: (c is not None, c or "")):
                    out(f"Informativo (conteos): {tabla}.{columna}, clave {clave!r} solo en {lado}")
    return 2 if diferencias else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modos = ap.add_mutually_exclusive_group(required=True)
    modos.add_argument("--exportar", action="store_true", help="imprime JSON a stdout desde ADB_*")
    modos.add_argument("--comparar", nargs=2, metavar=("A.json", "B.json"))
    ap.add_argument("--etiqueta", default="")
    ap.add_argument("--base")
    ap.add_argument("--objetivo")
    args = ap.parse_args(argv)
    try:
        if args.comparar:
            datos = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.comparar]
            return comparar(*datos, base=args.base, objetivo=args.objetivo)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.config import Config
        from src.memoria.db import connection

        logger = logging.getLogger("src.memoria.db")
        desactivado = logger.disabled
        logger.disabled = True  # El pool compartido registra el DSN a nivel INFO.
        try:
            with connection(Config()) as con:
                datos = exportar(con, args.etiqueta)
        finally:
            logger.disabled = desactivado
        print(json.dumps(datos, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception:
        # Los errores del driver pueden incluir el DSN: no se imprimen.
        print("No se pudo completar el preflight; revise conexión, permisos o archivos JSON.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
