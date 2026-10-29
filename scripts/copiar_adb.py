#!/usr/bin/env python3
"""Copia tablas de una ADB a otra (DEV → PROD): una ADB Always Free no se puede clonar.

Cada ADB solo acepta conexiones desde la IP de su propia VM (ACL), así que la copia va en dos pasos y NUNCA hay
una máquina hablando con las dos bases ni se toca ninguna ACL:

    # 1) en la VM de ORIGEN (ADB_DSN/ADB_USER/ADB_PASSWORD = la base de origen; solo lee)
    python3 scripts/copiar_adb.py --exportar /tmp/copia.pkl.gz
    # 2) llevar el archivo a la VM de DESTINO y, allí (ADB_* = la base de destino; el esquema ya debe existir)
    python3 scripts/copiar_adb.py --importar /tmp/copia.pkl.gz --solo-contar     # no escribe
    python3 scripts/copiar_adb.py --importar /tmp/copia.pkl.gz

Salvaguardas:
  · Al importar, solo escribe en tablas de destino VACÍAS: nunca mezcla ni pisa. Una tabla con filas se omite y se avisa.
  · Orden por dependencias (padres primero) y una sola transacción por tabla.
  · Las columnas `GENERATED ALWAYS AS IDENTITY` no se copian (Oracle no admite valores explícitos): se regeneran.
  · `usuario.mfa_*` se anula: el secreto TOTP va cifrado con `IDENTIDAD_SECRET`, que es distinto en cada ambiente;
    copiarlo dejaría un TOTP imposible de validar. Cada admin vuelve a enrolar su autenticador en el destino.
  · NO se copian: sesiones, códigos de acceso y códigos de recuperación (efímeros y atados al secreto del origen),
    auditoría y libro de consumo del LLM (son historia del origen), ni las tablas del modelo ONNX (`DM$…`).
  · El archivo es un pickle comprimido que produce y consume el mismo operador entre sus propias VM: no importar
    uno que venga de otra fuente. Contiene datos de negocio y correos: borrarlo al terminar.
No imprime contraseñas ni contenido de filas.
"""
import argparse
import gzip
import os
import pickle
import sys

import oracledb

# Padres primero. v2 (esquema vigente) y luego las tablas v1 que aún conviven (ADR del plan del portal).
TABLAS = [
    "ORGANIZACION", "PORTAL", "CLIENTE", "CLIENTE_CONFIG", "CLIENTE_PORTAL",
    "USUARIO", "MEMBRESIA_ORG", "MEMBRESIA",
    "PROCESO", "CLIENTE_PROCESO", "CHUNK", "ANALISIS", "REPORTADA", "FEEDBACK", "EJECUCION",
    "LICITACIONES_METADATA", "LICITACIONES_ANALISIS", "LICITACIONES_CHUNKS",
    "LICITACIONES_REPORTADAS", "LICITACIONES_FEEDBACK",
]
ANULAR = {"USUARIO": {"MFA_TOTP_SECRET", "MFA_CONFIRMADO_EN", "MFA_ULTIMO_PASO"}}


def conectar():
    dsn, user, pw = os.getenv("ADB_DSN", ""), os.getenv("ADB_USER", "AMALIA"), os.getenv("ADB_PASSWORD", "")
    if not dsn or not pw:
        sys.exit("Faltan ADB_DSN / ADB_PASSWORD en el entorno")
    return oracledb.connect(user=user, password=pw, dsn=dsn)


def existe(cur, tabla: str) -> bool:
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=tabla)
    return cur.fetchone()[0] == 1


def contar(cur, tabla: str) -> int:
    cur.execute(f'SELECT COUNT(*) FROM "{tabla}"')
    return cur.fetchone()[0]


def columnas_copiables(cur, tabla: str) -> list[str]:
    """Columnas del destino, sin virtuales ni de identidad `ALWAYS`, en el orden de la tabla."""
    cur.execute("""SELECT column_name FROM user_tab_cols
                   WHERE table_name = :t AND hidden_column = 'NO' AND virtual_column = 'NO'
                     AND NVL(identity_column, 'NO') = 'NO' ORDER BY column_id""", t=tabla)
    return [r[0] for r in cur.fetchall()]


def exportar(con, tabla: str, lote: int) -> dict:
    cur = con.cursor()
    cols = columnas_copiables(cur, tabla)
    cur.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{tabla}"')
    tipos = ["JSON" if d.type == oracledb.DB_TYPE_JSON else "VECTOR" if d.type == oracledb.DB_TYPE_VECTOR else ""
             for d in cur.description]
    filas = []
    while True:
        bloque = cur.fetchmany(lote)
        if not bloque:
            break
        filas.extend(bloque)
    return {"cols": cols, "tipos": tipos, "filas": filas}


def importar(con, tabla: str, datos: dict, lote: int) -> int:
    cur = con.cursor()
    cols_dst = set(columnas_copiables(cur, tabla))
    idx = [i for i, c in enumerate(datos["cols"]) if c in cols_dst]
    cols = [datos["cols"][i] for i in idx]
    anular = {i for i, c in enumerate(cols) if c in ANULAR.get(tabla, set())}
    ins = f'INSERT INTO "{tabla}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join(f":{i + 1}" for i in range(len(cols)))})'
    especiales = [{"JSON": oracledb.DB_TYPE_JSON, "VECTOR": oracledb.DB_TYPE_VECTOR}.get(datos["tipos"][i]) for i in idx]
    if any(especiales):
        cur.setinputsizes(*especiales)
    filas = [tuple(None if j in anular else f[i] for j, i in enumerate(idx)) for f in datos["filas"]]
    for k in range(0, len(filas), lote):
        cur.executemany(ins, filas[k:k + lote])
    con.commit()
    return len(filas)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--exportar", metavar="ARCHIVO", help="lee las tablas de ADB_* y las guarda en ARCHIVO")
    g.add_argument("--importar", metavar="ARCHIVO", help="carga ARCHIVO en las tablas vacías de ADB_*")
    ap.add_argument("--solo-contar", action="store_true", help="con --importar: no escribe, solo compara filas")
    ap.add_argument("--lote", type=int, default=200)
    a = ap.parse_args()
    oracledb.defaults.fetch_lobs = False  # CLOB → str, BLOB → bytes
    con = conectar()
    cur = con.cursor()
    if a.exportar:
        paquete, tot = {}, 0
        for t in TABLAS:
            if not existe(cur, t):
                print(f"{t:28}  no existe en el origen (se omite)")
                continue
            paquete[t] = exportar(con, t, a.lote)
            tot += len(paquete[t]["filas"])
            print(f"{t:28}{len(paquete[t]['filas']):>8} filas")
        with gzip.open(a.exportar, "wb") as f:
            pickle.dump(paquete, f)
        os.chmod(a.exportar, 0o600)
        print(f"✓ exportadas {tot} filas de {len(paquete)} tablas")
        return
    with gzip.open(a.importar, "rb") as f:
        paquete = pickle.load(f)  # noqa: S301 — archivo propio, ver la cabecera
    print(f"{'tabla':28}{'archivo':>8}{'destino':>9}  resultado")
    errores = 0
    for t in TABLAS:
        if t not in paquete:
            continue
        n_arc = len(paquete[t]["filas"])
        if not existe(cur, t):
            print(f"{t:28}{n_arc:>8}{'—':>9}  ✗ no existe en el destino (corré adb_bootstrap y schema_v2)")
            errores += 1
            continue
        n_dst = contar(cur, t)
        if a.solo_contar:
            print(f"{t:28}{n_arc:>8}{n_dst:>9}")
            continue
        if n_dst > 0:
            print(f"{t:28}{n_arc:>8}{n_dst:>9}  destino con filas: se omite (no se mezcla)")
            continue
        try:
            n = importar(con, t, paquete[t], a.lote)
            print(f"{t:28}{n_arc:>8}{contar(cur, t):>9}  ✓ copiadas {n}")
        except oracledb.DatabaseError as e:
            con.rollback()
            errores += 1
            print(f"{t:28}{n_arc:>8}{n_dst:>9}  ✗ {str(e.args[0].message).splitlines()[0][:110]}")
    sys.exit(1 if errores else 0)


if __name__ == "__main__":
    main()
