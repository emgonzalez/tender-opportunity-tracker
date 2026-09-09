#!/usr/bin/env python3
"""Bootstrap completo de la ADB: usuario, esquema y modelo ONNX.

Automatiza lo que hasta ahora era manual por navegador (Database Actions). Los
tres pasos que faltaban después de `adb_setup`:

  1. Crear el usuario de aplicación AMALIA con sus grants. `oci/lib/05-adb.sh`
     solo genera la contraseña de ADMIN; el usuario se creaba a mano.
  2. Correr `db/schema.sql` (idempotente).
  3. Bajar el .onnx a DATA_PUMP_DIR vía el PAR y cargarlo con
     DBMS_VECTOR.LOAD_ONNX_MODEL, más las verificaciones de `db/load_onnx.sql`.

Es re-ejecutable: cada paso detecta si ya se hizo y sigue. Eso importa porque la
carga del modelo es lo más frágil de la secuencia y conviene poder reintentar sin
rehacer el resto.

Uso:
    python3 scripts/adb_bootstrap.py <tenant> [--dsn DSN] [--par-url URL]

Sin flags, lee todo de oci/state/<tenant>.json y de los archivos de contraseña
que dejan los scripts de oci/lib/.
"""
import argparse
import json
import os
import re
import secrets
import string
import sys
from pathlib import Path

import oracledb

REPO = Path(__file__).resolve().parent.parent
MODELO = "MULTILINGUAL_E5_SMALL"
ARCHIVO_ONNX = "multilingual_e5_small.onnx"

# Errores que significan "ya estaba hecho". Se toleran para que el script pueda
# reintentarse sin limpiar nada a mano.
YA_EXISTE = {
    955,    # ORA-00955: el nombre ya lo usa otro objeto
    1920,   # ORA-01920: el usuario ya existe
    -955,
}


def log(msg, estado="·"):
    print(f"  {estado}  {msg}", flush=True)


def _state(tenant: str, obligatorio: bool = True) -> dict:
    """Lee oci/state/<tenant>.json, que NO existe cuando esto corre en la VM.

    `app_deploy` excluye oci/state/ del rsync a propósito (ahí viven las llaves
    SSH). Por eso el DSN y el PAR pueden llegar por flag en vez de por archivo.
    """
    f = REPO / "oci" / "state" / f"{tenant}.json"
    if not f.exists():
        if obligatorio:
            sys.exit(f"No existe {f} — ¿corriste adb_setup para este tenant?")
        return {}
    return json.loads(f.read_text())


def _leer_pw(path: Path, env_var: str) -> str:
    """Contraseña por variable de entorno o por archivo, en ese orden.

    El entorno es lo que se usa al correr desde la VM: los .pw viven solo en la
    laptop y no tiene sentido copiarlos a la máquina remota para un one-shot.
    """
    if os.getenv(env_var):
        return os.environ[env_var].strip()
    if not path.exists():
        sys.exit(f"Falta {path} y tampoco está {env_var} en el entorno")
    return path.read_text().strip()


def _generar_pw() -> str:
    # Reglas de ADB: 12-30 caracteres, con mayúscula, minúscula, dígito, y sin
    # comillas dobles ni el literal del nombre de usuario.
    alfabeto = string.ascii_letters + string.digits
    cuerpo = "".join(secrets.choice(alfabeto) for _ in range(20))
    return f"Am{cuerpo}9#"


def _ejecutar_script(cur, sql_path: Path):
    """Ejecuta un .sql partiendo en las líneas que son solo '/'.

    Es el separador que usa SQL*Plus para bloques PL/SQL, y `db/schema.sql` está
    escrito entero como bloques anónimos (para que el DDL sea idempotente), así
    que partir por ';' rompería cada bloque por la mitad.
    """
    texto = sql_path.read_text()
    hechos = fallidos = 0
    for bloque in re.split(r"(?m)^\s*/\s*$", texto):
        stmt = bloque.strip()
        # Un bloque que solo trae comentarios no es una sentencia.
        util = [ln for ln in stmt.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if not util:
            continue

        # El punto y coma final se trata al revés según el tipo de sentencia, y
        # confundirlos es un ORA-06550 garantizado: en un bloque PL/SQL anónimo
        # el `;` de `END;` es parte de la sintaxis y quitarlo lo rompe, mientras
        # que en SQL suelto el driver no acepta el terminador.
        es_plsql = util[0].strip().upper().startswith(("BEGIN", "DECLARE"))
        if not es_plsql:
            stmt = stmt.rstrip().rstrip(";")

        try:
            cur.execute(stmt)
            hechos += 1
        except oracledb.DatabaseError as e:
            if e.args[0].code in YA_EXISTE:
                hechos += 1
                continue
            fallidos += 1
            detalle = " / ".join(e.args[0].message.splitlines()[:3])
            log(f"falló un bloque: {detalle}", "✗")
    return hechos, fallidos


def paso_usuario(dsn, admin_pw, amalia_pw):
    """Crea AMALIA con los grants que pide db/load_onnx.sql."""
    log("Conectando como ADMIN...")
    with oracledb.connect(user="ADMIN", password=admin_pw, dsn=dsn) as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM all_users WHERE username = 'AMALIA'")
        existe = cur.fetchone()[0] > 0
        if existe:
            # Solo se resetea la contraseña si la que tenemos NO sirve. Hacerlo
            # siempre falla con ORA-28007 ("password cannot be reused") cuando el
            # usuario ya quedó creado con esta misma contraseña en un intento
            # anterior — que es justo el caso al reintentar el bootstrap.
            try:
                oracledb.connect(user="AMALIA", password=amalia_pw, dsn=dsn).close()
                log("El usuario AMALIA ya existe y la contraseña que tenemos sirve", "✓")
            except oracledb.DatabaseError:
                log("El usuario AMALIA existe pero la contraseña no coincide — se resetea", "⚠")
                cur.execute(f'ALTER USER AMALIA IDENTIFIED BY "{amalia_pw}"')
        else:
            cur.execute(f'CREATE USER AMALIA IDENTIFIED BY "{amalia_pw}"')
            log("Usuario AMALIA creado", "✓")

        for g in (
            "GRANT DB_DEVELOPER_ROLE TO AMALIA",
            "GRANT CREATE MINING MODEL TO AMALIA",
            "GRANT READ, WRITE ON DIRECTORY DATA_PUMP_DIR TO AMALIA",
            "GRANT EXECUTE ON DBMS_CLOUD TO AMALIA",
            "GRANT EXECUTE ON DBMS_VECTOR TO AMALIA",
            "ALTER USER AMALIA QUOTA UNLIMITED ON DATA",
        ):
            try:
                cur.execute(g)
            except oracledb.DatabaseError as e:
                log(f"{g.split(' ON ')[0][:40]}… → ORA-{e.args[0].code}", "⚠")
        con.commit()
        log("Grants aplicados", "✓")


def paso_esquema(dsn, amalia_pw):
    log("Ejecutando db/schema.sql como AMALIA...")
    with oracledb.connect(user="AMALIA", password=amalia_pw, dsn=dsn) as con:
        cur = con.cursor()
        hechos, fallidos = _ejecutar_script(cur, REPO / "db" / "schema.sql")
        con.commit()
        cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
        tablas = [r[0] for r in cur.fetchall()]
    log(f"{hechos} bloques OK, {fallidos} con error", "✓" if not fallidos else "⚠")
    log(f"Tablas presentes: {', '.join(tablas) or '(ninguna)'}", "✓" if tablas else "✗")
    return not fallidos and bool(tablas)


def paso_modelo(dsn, amalia_pw, par_url):
    with oracledb.connect(user="AMALIA", password=amalia_pw, dsn=dsn) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM user_mining_models WHERE model_name = :m", m=MODELO
        )
        if cur.fetchone()[0]:
            log(f"El modelo {MODELO} ya está cargado — se omite la descarga", "✓")
        else:
            if not par_url:
                sys.exit("Falta el PAR: corré onnx_stage o pasá --par-url")
            log("Copiando el .onnx a DATA_PUMP_DIR (117 MB, tarda)...")
            cur.execute(
                """
                BEGIN
                  DBMS_CLOUD.GET_OBJECT(
                    credential_name => NULL,
                    object_uri      => :uri,
                    directory_name  => 'DATA_PUMP_DIR',
                    file_name       => :fn);
                END;""",
                uri=par_url, fn=ARCHIVO_ONNX,
            )
            log("Archivo en DATA_PUMP_DIR", "✓")

            log("Cargando el modelo (DBMS_VECTOR.LOAD_ONNX_MODEL)...")
            cur.execute(
                """
                BEGIN
                  DBMS_VECTOR.LOAD_ONNX_MODEL(
                    directory  => 'DATA_PUMP_DIR',
                    file_name  => :fn,
                    model_name => :mn,
                    metadata   => JSON('{"function":"embedding","input":{"input":["DATA"]}}'));
                END;""",
                fn=ARCHIVO_ONNX, mn=MODELO,
            )
            con.commit()
            log("Modelo cargado", "✓")

        # ── Verificación (db/load_onnx.sql §3) ────────────────────────────────
        cur.execute(
            f"SELECT VECTOR_DIMENSION_COUNT("
            f"VECTOR_EMBEDDING({MODELO} USING 'servidor rack firewall' AS data)) FROM dual"
        )
        dims = cur.fetchone()[0]
        log(f"Dimensiones del embedding: {dims}", "✓" if dims == 384 else "✗")

        # Que discrimine en español es lo que de verdad importa para COMPRASAL:
        # un modelo cargado pero sin señal semántica pasaría las otras pruebas.
        cur.execute(f"""
            SELECT VECTOR_DISTANCE(
                     VECTOR_EMBEDDING({MODELO} USING 'Adquisición de servidores rack y firewall' AS data),
                     VECTOR_EMBEDDING({MODELO} USING 'Compra de equipos de red para data center' AS data), COSINE),
                   VECTOR_DISTANCE(
                     VECTOR_EMBEDDING({MODELO} USING 'Adquisición de servidores rack y firewall' AS data),
                     VECTOR_EMBEDDING({MODELO} USING 'Servicio de catering para eventos' AS data), COSINE)
            FROM dual""")
        cerca, lejos = cur.fetchone()
        ok = cerca < lejos
        log(f"Discriminación semántica: similar={cerca:.4f} < distinto={lejos:.4f}",
            "✓" if ok else "✗")
        return dims == 384 and ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tenant")
    ap.add_argument("--dsn")
    ap.add_argument("--par-url")
    args = ap.parse_args()

    st = _state(args.tenant, obligatorio=not args.dsn)
    dsn = args.dsn or st.get("adb_dsn")
    par = args.par_url or st.get("onnx_par_url")
    if not dsn:
        sys.exit("No hay adb_dsn en el state file ni --dsn")

    admin_pw = _leer_pw(REPO / "oci" / "state" / f"{args.tenant}_adb_admin.pw",
                        "ADB_ADMIN_PASSWORD")
    amalia_f = REPO / "oci" / "state" / f"{args.tenant}_adb_amalia.pw"
    if os.getenv("ADB_AMALIA_PASSWORD") or amalia_f.exists():
        amalia_pw = _leer_pw(amalia_f, "ADB_AMALIA_PASSWORD")
    else:
        amalia_pw = _generar_pw()
        amalia_f.write_text(amalia_pw)
        amalia_f.chmod(0o600)
        log(f"Contraseña de AMALIA generada y guardada en {amalia_f.name}", "✓")

    print("\n▶ Paso 1 — usuario de aplicación")
    paso_usuario(dsn, admin_pw, amalia_pw)
    print("\n▶ Paso 2 — esquema")
    ok_esquema = paso_esquema(dsn, amalia_pw)
    print("\n▶ Paso 3 — modelo de embeddings")
    ok_modelo = paso_modelo(dsn, amalia_pw, par)

    print("\n" + ("✓ Bootstrap completo" if ok_esquema and ok_modelo else "✗ Bootstrap incompleto"))
    print("\nVariables para el .env:")
    print("  ADB_USER=AMALIA")
    print(f"  ADB_PASSWORD=<oci/state/{args.tenant}_adb_amalia.pw>")
    print(f"  ADB_DSN={dsn}")
    return 0 if (ok_esquema and ok_modelo) else 1


if __name__ == "__main__":
    sys.exit(main())
