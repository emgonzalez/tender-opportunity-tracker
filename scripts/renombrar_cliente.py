#!/usr/bin/env python3
"""Renombra un `cliente_id` en la ADB (p. ej. `multicomputos` → `multicomputos_sv`) en UNA transacción.

Oracle no tiene `ON UPDATE CASCADE` y no se puede actualizar una PK referenciada, así que el orden importa:
  1. se inserta la fila nueva de `cliente` (copia de la vieja);
  2. los hijos que solo cuelgan de `cliente` (config, portal, membresía) se re-apuntan con UPDATE;
  3. los padres de otra FK (`cliente_proceso` ← `analisis`; `oportunidad_estado` ← `oportunidad_evento`) se copian,
     se re-apuntan sus hijos con UPDATE (así conservan sus ids) y se borra el padre viejo;
  4. las tablas sin FK se actualizan directo;
  5. se borra la fila vieja de `cliente`.
Antes de confirmar se compara, tabla por tabla, que las filas del id nuevo sean las que había con el viejo y que
del viejo no quede ninguna: si algo no cuadra, ROLLBACK. Solo DML (sin DDL), así que el ROLLBACK es completo.

Salvaguardas:
  · Por defecto es `--dry-run`: cuenta y no escribe. Escribir exige `--aplicar`.
  · Aborta si el id nuevo ya tiene filas (no mezcla) o si el viejo no existe.
  · Aborta si encuentra una tabla con columna CLIENTE_ID / CLIENT_ID que este script no conoce: mejor parar que
    dejar un dato huérfano con el id viejo.
  · `auditoria` NO se reescribe: es historia de lo que pasó con el id de entonces.
Antes de aplicar en una ADB real: `scripts/copiar_adb.py --exportar` como red. Corre en la VM, con ADB_* del entorno
(la ACL de la ADB solo admite a su VM). No imprime contraseñas ni filas.

    python3 scripts/renombrar_cliente.py --de multicomputos --a multicomputos_sv             # dry-run
    python3 scripts/renombrar_cliente.py --de multicomputos --a multicomputos_sv --aplicar
"""
import argparse
import os
import sys

# (tabla, columna) que se actualizan en el mismo lugar: hijos de `cliente` y tablas sin FK.
ACTUALIZAR = [
    ("CLIENTE_CONFIG", "CLIENTE_ID"), ("CLIENTE_PORTAL", "CLIENTE_ID"), ("MEMBRESIA", "CLIENTE_ID"),
    ("REPORTADA", "CLIENTE_ID"), ("FEEDBACK", "CLIENTE_ID"), ("KPI_SNAPSHOT", "CLIENTE_ID"),
    ("LICITACIONES_METADATA", "CLIENTE_ID"), ("LICITACIONES_REPORTADAS", "CLIENTE_ID"),
    ("LICITACIONES_FEEDBACK", "CLIENTE_ID"), ("PROCESO_PARTICIPADO", "CLIENTE_ID"),
    ("SESION", "VER_COMO_CLIENTE"),
]
# (padre, hijo): se copia el padre, se re-apunta el hijo y se borra el padre viejo.
PADRES = [("CLIENTE_PROCESO", "ANALISIS"), ("OPORTUNIDAD_ESTADO", "OPORTUNIDAD_EVENTO")]
# Tablas con columna de cliente que NO se tocan a propósito.
IGNORAR = {"AUDITORIA"}
COLUMNAS_CLIENTE = ("CLIENTE_ID", "CLIENT_ID")


def _conocidas() -> set[str]:
    t = {"CLIENTE"} | {a for a, _ in ACTUALIZAR} | IGNORAR
    for padre, hijo in PADRES:
        t |= {padre, hijo}
    return t


def desconocidas(cur) -> list[str]:
    """Tablas con una columna de cliente que este script no maneja."""
    cur.execute(
        "SELECT DISTINCT table_name FROM user_tab_cols WHERE column_name IN ('CLIENTE_ID', 'CLIENT_ID') "
        "AND table_name NOT LIKE 'DM$%' AND table_name NOT LIKE 'BIN$%'"
    )
    return sorted(r[0] for r in cur.fetchall() if r[0] not in _conocidas())


def existe(cur, tabla: str) -> bool:
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=tabla)
    return cur.fetchone()[0] == 1


def contar(cur, tabla: str, columna: str, valor: str) -> int:
    cur.execute(f'SELECT COUNT(*) FROM "{tabla}" WHERE "{columna}" = :v', v=valor)
    return cur.fetchone()[0]


def copiar_filas(cur, tabla: str, viejo: str, nuevo: str) -> None:
    """INSERT … SELECT en el servidor (sin pasar los datos por el cliente) con el id nuevo en CLIENTE_ID."""
    cur.execute(
        "SELECT column_name FROM user_tab_cols WHERE table_name = :t AND hidden_column = 'NO' "
        "AND virtual_column = 'NO' AND NVL(identity_column, 'NO') = 'NO' ORDER BY column_id", t=tabla,
    )
    cols = [r[0] for r in cur.fetchall()]
    lista = ", ".join(f'"{c}"' for c in cols)
    sel = ", ".join(":nuevo" if c == "CLIENTE_ID" else f'"{c}"' for c in cols)
    cur.execute(f'INSERT INTO "{tabla}" ({lista}) SELECT {sel} FROM "{tabla}" WHERE "CLIENTE_ID" = :viejo',
                nuevo=nuevo, viejo=viejo)


def pasos(existentes: set[str]) -> list[tuple[str, str, str]]:
    """Plan en orden: (accion, tabla, columna). Acciones: copiar | actualizar | borrar."""
    p = [("copiar", "CLIENTE", "CLIENTE_ID")]
    p += [("actualizar", t, c) for t, c in ACTUALIZAR[:3] if t in existentes]
    for padre, hijo in PADRES:
        if padre in existentes:
            p.append(("copiar", padre, "CLIENTE_ID"))
            if hijo in existentes:
                p.append(("actualizar", hijo, "CLIENTE_ID"))
            p.append(("borrar", padre, "CLIENTE_ID"))
    # las tablas sin FK van después de los padres; el orden entre ellas es indiferente
    p += [("actualizar", t, c) for t, c in ACTUALIZAR[3:] if t in existentes]
    p.append(("borrar", "CLIENTE", "CLIENTE_ID"))
    return p


def tablas_a_verificar(existentes: set[str]) -> list[tuple[str, str]]:
    t = [("CLIENTE", "CLIENTE_ID")] + [(a, c) for a, c in ACTUALIZAR if a in existentes]
    for padre, hijo in PADRES:
        t += [(x, "CLIENTE_ID") for x in (padre, hijo) if x in existentes]
    return t


def renombrar(con, viejo: str, nuevo: str, aplicar: bool, out=print) -> bool:
    """True si quedó (o quedaría) consistente. Con `aplicar=False` no escribe nada."""
    cur = con.cursor()
    if viejo == nuevo:
        out("✗ el id viejo y el nuevo son iguales")
        return False
    extra = desconocidas(cur)
    if extra:
        out(f"✗ tablas con columna de cliente que este script no conoce: {', '.join(extra)}. Agrégalas antes de seguir.")
        return False
    if contar(cur, "CLIENTE", "CLIENTE_ID", viejo) != 1:
        out(f"✗ el cliente {viejo!r} no existe")
        return False
    if contar(cur, "CLIENTE", "CLIENTE_ID", nuevo) != 0:
        out(f"✗ el cliente {nuevo!r} ya existe: no se mezcla")
        return False
    existentes = {t for t in _conocidas() if existe(cur, t)}
    verif = tablas_a_verificar(existentes)
    antes = {(t, c): contar(cur, t, c, viejo) for t, c in verif}
    for (t, c), n in antes.items():
        out(f"{t:28}{c:18}{n:>8} filas")
    if not aplicar:
        out(f"(dry-run) {sum(antes.values())} filas pasarían de {viejo!r} a {nuevo!r}. Usa --aplicar para escribir.")
        return True
    try:
        for accion, tabla, col in pasos(existentes):
            if accion == "copiar":
                copiar_filas(cur, tabla, viejo, nuevo)
            elif accion == "actualizar":
                cur.execute(f'UPDATE "{tabla}" SET "{col}" = :n WHERE "{col}" = :v', n=nuevo, v=viejo)
            else:
                cur.execute(f'DELETE FROM "{tabla}" WHERE "{col}" = :v', v=viejo)
        malas = []
        for (t, c), n in antes.items():
            ahora_nuevo, ahora_viejo = contar(cur, t, c, nuevo), contar(cur, t, c, viejo)
            if ahora_nuevo != n or ahora_viejo != 0:
                malas.append(f"{t}: esperaba {n} con el id nuevo y 0 con el viejo, hay {ahora_nuevo} y {ahora_viejo}")
        if malas:
            con.rollback()
            out("✗ los conteos no cuadran; ROLLBACK, no se cambió nada:\n  " + "\n  ".join(malas))
            return False
        con.commit()
    except Exception as exc:  # noqa: BLE001
        con.rollback()
        out(f"✗ {str(exc).splitlines()[0][:160]}; ROLLBACK, no se cambió nada")
        return False
    out(f"✓ {sum(antes.values())} filas pasaron de {viejo!r} a {nuevo!r}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--de", required=True, metavar="ID_VIEJO")
    ap.add_argument("--a", required=True, metavar="ID_NUEVO")
    ap.add_argument("--aplicar", action="store_true", help="escribe; sin esto solo cuenta (dry-run)")
    a = ap.parse_args()
    import oracledb
    dsn, user, pw = os.getenv("ADB_DSN", ""), os.getenv("ADB_USER", "AMALIA"), os.getenv("ADB_PASSWORD", "")
    if not dsn or not pw:
        sys.exit("Faltan ADB_DSN / ADB_PASSWORD en el entorno")
    con = oracledb.connect(user=user, password=pw, dsn=dsn)
    sys.exit(0 if renombrar(con, a.de, a.a, a.aplicar) else 1)


if __name__ == "__main__":
    main()
