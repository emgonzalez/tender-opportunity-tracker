"""Dialecto Oracle del SQL de Track B, verificado contra un contenedor local.

── El problema que resuelve ────────────────────────────────────────────────
El ACL de red de la ADB de AMALIA_DEV solo admite la VCN y la IP pública de
la VM (ver ``oci/lib/05-adb.sh``): ni una laptop ni un runner de GitHub
pueden conectarse. Consecuencia: hasta ahora, un error de sintaxis en una de
las sentencias de ``src/memoria/`` se descubría **después** de construir la
imagen, publicarla, desplegarla y correrla en la VM — un ciclo completo de
despliegue para enterarse de un ``ORA-00904``.

Este módulo cierra ese hueco: levanta el mismo motor 23ai en su edición Free
dentro de un contenedor (``db/docker-compose.oracle.yml``) y ejecuta ahí el
SQL real del código. No sustituye a ``tests/integration/test_adb.py`` (que sí
necesita la ADB); ataca la clase de error que no hace falta una ADB para
atrapar, que es la mayoritaria.

── La regla que hace que esto sirva ────────────────────────────────────────
**Ninguna sentencia SQL está escrita en este archivo.** Todas salen de
``src/memoria/`` o de ``db/*.sql``, por dos caminos:

  a) *Ejecución real* — se llama a la función del módulo (``repo.upsert_proceso``,
     ``rag.tendencias``, …) con una conexión de verdad. El SQL es el del código
     por construcción.
  b) *Captura y ejecución* — para las tres sentencias que invocan
     ``VECTOR_EMBEDDING`` (el contenedor no tiene el modelo ONNX cargado), un
     cursor espía graba el SQL que el módulo emite y el test reemplaza SOLO la
     llamada a ``VECTOR_EMBEDDING(...)`` por un literal ``TO_VECTOR``. Todo lo
     demás de la sentencia —columnas, JOIN, ``FETCH FIRST``, binds— se ejecuta
     tal cual salió del código.

Una suite que validara una copia pegada del SQL daría confianza falsa: seguiría
en verde después de que alguien rompiera la consulta de verdad.

── Lo que este contenedor NO reproduce de una ADB ──────────────────────────
``DBMS_CLOUD`` (no está instalado), el wallet mTLS, el modelo ONNX de
embeddings y el dimensionamiento del Vector Memory Pool. Nada que dependa de
eso pertenece a este archivo: eso es lo que verifica ``test_adb.py``.

── Cómo correrlo en local ──────────────────────────────────────────────────
    docker compose -f db/docker-compose.oracle.yml up -d --wait
    pytest -m oracle_local -v
    docker compose -f db/docker-compose.oracle.yml down -v

(La explicación completa, con las variables de entorno y el alcance exacto,
vive en ``db/README.md`` → "Validación local sin ADB".)

Sin contenedor, todos estos tests se saltan solos (mismo patrón que
``test_adb.py``). Si el 1521 local ya está ocupado, se puede apuntar a otro
puerto con ``ORACLE_LOCAL_DSN=localhost:15210/FREEPDB1``. En CI se define
``ORACLE_LOCAL_REQUERIDO=1`` para que la ausencia del contenedor sea un fallo
y no un skip silencioso.
"""
from __future__ import annotations

import os
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import oracledb
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.oracle_local]

_RAIZ = Path(__file__).resolve().parents[2]

# Los valores por defecto son los de db/docker-compose.oracle.yml. Son
# credenciales de un contenedor local y desechable: no hay nada que proteger.
_DSN = os.getenv("ORACLE_LOCAL_DSN", "localhost:1521/FREEPDB1")
_USUARIO = os.getenv("ORACLE_LOCAL_USER", "AMALIA")
_CLAVE = os.getenv("ORACLE_LOCAL_PASSWORD", "Amalia_Local_2026")

_MOTIVO_SKIP = (
    f"No hay un Oracle 23ai local escuchando en {_DSN}. Levantarlo con: "
    "docker compose -f db/docker-compose.oracle.yml up -d --wait"
)

# En CI el contenedor SÍ tiene que estar: si no está, saltarse los tests haría
# que el job pasara en verde sin haber verificado nada — la peor forma de fallo,
# porque nadie la nota. Con esta variable en "1" la ausencia es un error.
_EXIGIDO = os.getenv("ORACLE_LOCAL_REQUERIDO") == "1"


# ═══════════════════════════════════════════════════════════════════════════
# Andamiaje: scripts .sql, cursor espía y sustitución de VECTOR_EMBEDDING
# ═══════════════════════════════════════════════════════════════════════════

def _sentencias_de(ruta: Path) -> list[str]:
    """Parte un script .sql de ``db/`` en sentencias ejecutables por oracledb.

    ``oracledb`` ejecuta UNA sentencia por ``execute()`` y sin el ``;`` final;
    los scripts de ``db/`` están escritos para SQLcl/SQL Developer, que usan
    ``;`` para terminar SQL y una línea con ``/`` sola para terminar un bloque
    PL/SQL. Este helper traduce de un formato al otro. Vive acá y no en
    ``src/`` a propósito: es andamiaje de test, no código de producción — en
    producción los scripts los aplica una persona desde SQL Developer Web.
    """
    sentencias: list[str] = []
    actual: list[str] = []

    for linea in ruta.read_text(encoding="utf-8").splitlines():
        if linea.strip() == "/":
            bloque = "\n".join(actual).strip()
            if bloque:
                sentencias.append(bloque)
            actual = []
            continue
        # Los comentarios de línea se descartan: varios scripts traen bloques
        # enteros comentados (dry-runs, ejemplos) que no deben ejecutarse.
        if linea.strip().startswith("--"):
            continue
        actual.append(linea)

    resto = "\n".join(actual).strip()
    for trozo in resto.split(";"):
        trozo = trozo.strip()
        # COMMIT lo maneja el llamador (la conexión del test), no el script.
        if trozo and trozo.upper() != "COMMIT":
            sentencias.append(trozo)

    return sentencias


class _CursorEspia:
    """Cursor de mentira: no habla con ninguna base, solo anota lo que le piden.

    Sirve para extraer del código el texto exacto de las sentencias que no se
    pueden ejecutar contra el contenedor (las que invocan ``VECTOR_EMBEDDING``,
    porque ahí no hay modelo ONNX cargado). Los ``fetch*`` devuelven lo mínimo
    para que la función bajo prueba llegue hasta el final sin explotar.
    """

    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []
        self.rowcount = 0

    def execute(self, sql: str, binds: Any = None, **kwargs: Any) -> None:
        self.sentencias.append((sql, binds if binds is not None else kwargs))

    def executemany(self, sql: str, binds: Any) -> None:
        self.sentencias.append((sql, binds))

    def fetchone(self) -> tuple:
        return (0,)

    def fetchall(self) -> list:
        return []

    def close(self) -> None:
        pass


class _ConexionEspia:
    def __init__(self) -> None:
        self.espia = _CursorEspia()

    def cursor(self) -> _CursorEspia:
        return self.espia


def _capturar(funcion: Any, *args: Any, **kwargs: Any) -> list[tuple[str, Any]]:
    """Ejecuta ``funcion`` con una conexión espía y devuelve el SQL que emitió."""
    conn = _ConexionEspia()
    funcion(conn, *args, **kwargs)
    return conn.espia.sentencias


# VECTOR_EMBEDDING(<MODELO> USING :bind AS data) — la única construcción que el
# contenedor no puede resolver, porque el modelo ONNX (~118 MB) se carga con
# DBMS_VECTOR.LOAD_ONNX_MODEL solo en la ADB. Se reemplaza por un literal de
# 384 dimensiones (las mismas que declara la columna `embedding` en
# db/schema.sql) para que el resto de la sentencia siga siendo verificable.
_RE_VECTOR_EMBEDDING = re.compile(
    r"VECTOR_EMBEDDING\s*\(\s*\w+\s+USING\s+:\w+\s+AS\s+data\s*\)",
    re.IGNORECASE,
)

# Primer componente en 1 y no todo ceros: la distancia coseno de un vector nulo
# es indefinida y Oracle devolvería NULL, lo que volvería mudo cualquier
# ORDER BY sobre VECTOR_DISTANCE.
_VECTOR_LITERAL = "TO_VECTOR('[1" + ",0" * 383 + "]', 384, FLOAT32)"

_RE_BIND = re.compile(r":(\w+)")


def _sin_modelo_onnx(sql: str) -> str:
    """Sustituye VECTOR_EMBEDDING(...) por un literal; deja el resto intacto."""
    nuevo, n = _RE_VECTOR_EMBEDDING.subn(_VECTOR_LITERAL, sql)
    assert n == 1, (
        "Se esperaba exactamente una llamada a VECTOR_EMBEDDING en la sentencia "
        f"capturada del código, se encontraron {n}. SQL: {sql}"
    )
    return nuevo


def _binds_presentes(sql: str, binds: dict) -> dict:
    """Filtra los binds que ya no aparecen en el SQL tras la sustitución.

    Al reemplazar ``VECTOR_EMBEDDING(... USING :pregunta ...)`` desaparece el
    bind ``:pregunta``, pero el dict que armó el código sigue trayéndolo y
    oracledb rechaza binds sobrantes. Se filtra por lo que quedó en el texto:
    así el test no reescribe los binds del código, solo descarta el que su
    propia sustitución dejó huérfano.
    """
    nombres = set(_RE_BIND.findall(sql))
    return {k: v for k, v in binds.items() if k in nombres}


def _codigo_ora(sql: str, conn: Any) -> int | None:
    """Parsea ``sql`` sin ejecutarlo y devuelve el código ORA, o None si compila.

    ``cursor.parse()`` hace que Oracle valide sintaxis y semántica (existencia
    de tablas y columnas) sin tocar datos: es lo que permite verificar una
    sentencia destructiva o mutada sin consecuencias.
    """
    cursor = conn.cursor()
    try:
        cursor.parse(sql)
        return None
    except oracledb.DatabaseError as exc:
        return exc.args[0].code
    finally:
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

class _CfgLocal:
    """Config mínima para los módulos de ``src.memoria``.

    No se usa ``src.config.Config`` porque exige credenciales de COMPRASAL,
    SMTP y Anthropic que no tienen nada que ver con el dialecto SQL. Los
    valores replican los defaults declarados en ``src/config.py``.
    """

    embedding_model_name = "MULTILINGUAL_E5_SMALL"
    rag_top_k = 5
    precedentes_max = 3
    chunk_tokens = 800
    chunk_overlap = 100


@pytest.fixture(scope="session")
def cfg() -> _CfgLocal:
    return _CfgLocal()


@pytest.fixture(scope="session")
def conn():
    """Conexión al Oracle local, con ``db/schema.sql`` ya aplicado.

    Si no hay nada escuchando, se salta toda la suite (mismo criterio que
    ``test_adb.py``: la falta de infraestructura opcional no es un fallo).
    """
    try:
        conexion = oracledb.connect(
            user=_USUARIO, password=_CLAVE, dsn=_DSN, tcp_connect_timeout=5
        )
    except oracledb.DatabaseError as exc:
        if _EXIGIDO:
            pytest.fail(
                f"ORACLE_LOCAL_REQUERIDO=1 pero no se pudo conectar a {_DSN}: {exc}"
            )
        pytest.skip(_MOTIVO_SKIP)

    cursor = conexion.cursor()
    for sentencia in _sentencias_de(_RAIZ / "db" / "schema.sql"):
        cursor.execute(sentencia)
    # db/schema_v2.sql (esquema multi-tenant/multi-país, fase "Expandir" — ver
    # el plan del portal §3.2/§3.3): coexiste con db/schema.sql mientras dure
    # la migración; nadie lo lee ni lo escribe todavía en producción.
    for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
        cursor.execute(sentencia)
    conexion.commit()
    cursor.close()

    yield conexion
    conexion.close()


@pytest.fixture
def proceso_id(conn) -> str:
    """Inserta una fila mínima en licitaciones_metadata y la borra al final.

    El INSERT es del test, no del código bajo prueba: si el MERGE de
    ``repo.upsert_proceso`` se rompiera, debe fallar SU test y no arrastrar a
    los demás. El ``ON DELETE CASCADE`` del DDL limpia chunks y análisis.
    """
    pid = f"TEST-{_uuid.uuid4().hex[:12]}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO licitaciones_metadata (proceso_id, cliente_id, institucion, "
        "titulo, categoria, fecha_pub, estado) VALUES "
        "(:pid, 'test_client', 'INSTITUCION DE PRUEBA', 'Pliego de prueba', "
        "'infraestructura', SYSDATE, 'DESCUBIERTO')",
        pid=pid,
    )
    conn.commit()

    yield pid

    cursor.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :pid", pid=pid)
    conn.commit()
    cursor.close()


def _proceso(numero_referencia: str):
    """Arma un ``Proceso`` de prueba con los campos que lee el SQL de repo."""
    from src.scraper.models import Proceso

    return Proceso(
        institucion="MINISTERIO DE PRUEBA",
        descripcion="Adquisición de servidores rack y firewall perimetral",
        fecha_publicacion=datetime.now(timezone.utc),
        url_directa="https://example.test/proceso",
        numero_referencia=numero_referencia,
        forma_contratacion="Licitación Pública",
        tipo_actividad="infraestructura",
        monto=12345.67,
        fecha_limite=datetime.now(timezone.utc) + timedelta(days=30),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1) Los scripts de db/ aplican contra el motor real
# ═══════════════════════════════════════════════════════════════════════════

def test_schema_es_idempotente(conn):
    """Re-aplicar db/schema.sql sobre un esquema ya creado no debe fallar.

    Es la propiedad que el DDL promete (bloques PL/SQL que tragan ORA-00955 y
    ORA-01408) y de la que depende poder re-correr el bootstrap sobre una ADB
    viva sin miedo. La fixture ya lo aplicó una vez; esta es la segunda.
    """
    cursor = conn.cursor()
    for sentencia in _sentencias_de(_RAIZ / "db" / "schema.sql"):
        cursor.execute(sentencia)
    conn.commit()

    cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name IN "
        "('LICITACIONES_METADATA','LICITACIONES_CHUNKS','PRESUPUESTO_DIARIO',"
        "'LICITACIONES_ANALISIS','LICITACIONES_REPORTADAS','LICITACIONES_FEEDBACK')"
    )
    assert cursor.fetchone()[0] == 6
    cursor.close()


def test_retention_purga_lo_viejo_y_respeta_lo_vigente(conn, proceso_id):
    """El DELETE de db/retention.sql corre y NO toca procesos vigentes.

    La regla de negocio dice: purgar chunks de procesos cerrados hace más de
    12 meses, conservar los vigentes. Se montan los dos casos y se comprueba
    que la sentencia real del script distingue entre ellos — no solo que
    compila.
    """
    cursor = conn.cursor()
    viejo = f"{proceso_id}-VIEJO"
    cursor.execute(
        "INSERT INTO licitaciones_metadata (proceso_id, cliente_id, fecha_cierre, "
        "estado) VALUES (:pid, 'test_client', ADD_MONTHS(SYSDATE, -18), 'NOTIFICADO')",
        pid=viejo,
    )
    # Vigente: el de la fixture, con fecha_cierre NULL.
    for pid in (viejo, proceso_id):
        cursor.execute(
            "INSERT INTO licitaciones_chunks (chunk_id, proceso_id, seq, chunk_text) "
            "VALUES (:cid, :pid, 0, 'texto de prueba')",
            cid=f"{pid}#0", pid=pid,
        )
    conn.commit()

    try:
        for sentencia in _sentencias_de(_RAIZ / "db" / "retention.sql"):
            cursor.execute(sentencia)
        conn.commit()

        cursor.execute(
            "SELECT proceso_id FROM licitaciones_chunks WHERE proceso_id IN (:a, :b)",
            a=viejo, b=proceso_id,
        )
        sobrevivientes = {fila[0] for fila in cursor.fetchall()}
        assert viejo not in sobrevivientes, "no purgó el proceso cerrado hace 18 meses"
        assert proceso_id in sobrevivientes, "purgó un proceso vigente"
    finally:
        cursor.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :pid", pid=viejo)
        conn.commit()
        cursor.close()


def test_index_ivf_compila_contra_el_motor(conn):
    """El CREATE VECTOR INDEX de db/index_ivf.sql es sintaxis 23ai válida.

    Lo que se verifica es el dialecto (``ORGANIZATION NEIGHBOR PARTITIONS``,
    ``WITH TARGET ACCURACY``), no el rendimiento: el Vector Memory Pool del
    contenedor no está dimensionado como el de una ADB, así que el índice se
    crea sobre una tabla chica y se borra enseguida.
    """
    creates = [
        s for s in _sentencias_de(_RAIZ / "db" / "index_ivf.sql")
        if s.upper().startswith("CREATE VECTOR INDEX")
    ]
    assert creates, "db/index_ivf.sql dejó de tener un CREATE VECTOR INDEX"

    cursor = conn.cursor()
    for sentencia in creates:
        try:
            cursor.execute("DROP INDEX ivf_chunks")
        except oracledb.DatabaseError:
            pass  # ORA-01418: no existía, que es el caso normal.
        cursor.execute(sentencia)
        cursor.execute("DROP INDEX ivf_chunks")
    cursor.close()


def test_retention_v2_purga_lo_viejo_y_respeta_lo_vigente(conn, portal_y_org):
    """Espejo v2 de test_retention_purga_lo_viejo_y_respeta_lo_vigente.

    Más simple que v1 a propósito: `chunk` no tiene `cliente_id`, así que la
    purga no depende de ningún cliente — solo de `proceso.fecha_limite_utc`.
    """
    cursor = conn.cursor()
    viejo = f"TEST-{_uuid.uuid4().hex[:12]}-VIEJO"
    vigente = f"TEST-{_uuid.uuid4().hex[:12]}-VIGENTE"
    gid_viejo = f"{portal_y_org['portal_id']}:{viejo}"
    gid_vigente = f"{portal_y_org['portal_id']}:{vigente}"
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, fecha_limite_utc) "
        "VALUES (:g, :p, :r, ADD_MONTHS(SYSDATE, -18))",
        g=gid_viejo, p=portal_y_org["portal_id"], r=viejo,
    )
    # Vigente: fecha_limite_utc NULL (mismo caso que v1 con fecha_cierre NULL).
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) VALUES (:g, :p, :r)",
        g=gid_vigente, p=portal_y_org["portal_id"], r=vigente,
    )
    for gid, seq in ((gid_viejo, 0), (gid_vigente, 0)):
        cursor.execute(
            "INSERT INTO chunk (proceso_gid, seq, texto) VALUES (:g, :s, 'texto de prueba')",
            g=gid, s=seq,
        )
    conn.commit()

    try:
        for sentencia in _sentencias_de(_RAIZ / "db" / "retention_v2.sql"):
            cursor.execute(sentencia)
        conn.commit()

        cursor.execute(
            "SELECT proceso_gid FROM chunk WHERE proceso_gid IN (:a, :b)",
            a=gid_viejo, b=gid_vigente,
        )
        sobrevivientes = {fila[0] for fila in cursor.fetchall()}
        assert gid_viejo not in sobrevivientes, "no purgó el proceso cerrado hace 18 meses"
        assert gid_vigente in sobrevivientes, "purgó un proceso vigente"

        # proceso/cliente_proceso/analisis nunca se tocan — memoria institucional.
        cursor.execute(
            "SELECT COUNT(*) FROM proceso WHERE proceso_gid IN (:a, :b)",
            a=gid_viejo, b=gid_vigente,
        )
        assert cursor.fetchone()[0] == 2, "el DELETE de chunk no debe borrar filas de proceso"
    finally:
        cursor.execute("DELETE FROM proceso WHERE proceso_gid IN (:a, :b)", a=gid_viejo, b=gid_vigente)
        conn.commit()
        cursor.close()


def test_index_ivf_v2_compila_contra_el_motor(conn):
    """Espejo v2 de test_index_ivf_compila_contra_el_motor, sobre `chunk` con
    el nombre de índice `ivf_chunks_v2` (distinto del de v1 a propósito, ver
    la advertencia de db/index_ivf_v2.sql)."""
    creates = [
        s for s in _sentencias_de(_RAIZ / "db" / "index_ivf_v2.sql")
        if s.upper().startswith("CREATE VECTOR INDEX")
    ]
    assert creates, "db/index_ivf_v2.sql dejó de tener un CREATE VECTOR INDEX"

    cursor = conn.cursor()
    for sentencia in creates:
        try:
            cursor.execute("DROP INDEX ivf_chunks_v2")
        except oracledb.DatabaseError:
            pass  # ORA-01418: no existía, que es el caso normal.
        cursor.execute(sentencia)
        cursor.execute("DROP INDEX ivf_chunks_v2")
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 2) src/memoria/repo.py — el SQL se ejecuta de verdad
# ═══════════════════════════════════════════════════════════════════════════

def test_upsert_proceso_merge_es_idempotente(conn, proceso_id):
    """El MERGE de repo.upsert_proceso corre dos veces y deja UNA fila.

    Y, tal como promete su docstring, la segunda pasada NO pisa el ``estado``:
    reprocesar un proceso ya visto refresca sus datos sin resetear el progreso
    que llevaba en la cola.
    """
    from src.memoria import repo

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE licitaciones_metadata SET estado = 'VECTORIZADO' WHERE proceso_id = :p",
        p=proceso_id,
    )
    conn.commit()

    proceso = _proceso(proceso_id)
    repo.upsert_proceso(conn, proceso, "cliente_x", estado="DESCUBIERTO")
    repo.upsert_proceso(conn, proceso, "cliente_x", estado="DESCUBIERTO")
    conn.commit()

    cursor.execute(
        "SELECT COUNT(*), MAX(estado), MAX(institucion), MAX(monto_estimado) "
        "FROM licitaciones_metadata WHERE proceso_id = :p",
        p=proceso_id,
    )
    filas, estado, institucion, monto = cursor.fetchone()
    assert filas == 1
    assert estado == "VECTORIZADO", "el MERGE pisó el estado en la rama WHEN MATCHED"
    assert institucion == proceso.institucion
    assert float(monto) == pytest.approx(12345.67)
    cursor.close()


def test_upsert_proceso_da_de_alta_uno_nuevo(conn):
    """La rama WHEN NOT MATCHED inserta con el estado indicado."""
    from src.memoria import repo

    pid = f"TEST-{_uuid.uuid4().hex[:12]}"
    try:
        repo.upsert_proceso(conn, _proceso(pid), "cliente_x", estado="DESCARGADO")
        conn.commit()
        assert repo.get_estado(conn, pid) == "DESCARGADO"
    finally:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :p", p=pid)
        conn.commit()
        cursor.close()


def test_set_estado_y_get_estado(conn, proceso_id):
    """set_estado escribe y get_estado lee, en sus dos variantes de SET.

    ``set_estado`` arma el ``SET`` con un f-string según venga o no
    ``worker_id``: son dos sentencias distintas y las dos tienen que compilar.
    """
    from src.memoria import repo

    repo.set_estado(conn, proceso_id, "DESCARGADO", error="algo salió mal")
    conn.commit()
    assert repo.get_estado(conn, proceso_id) == "DESCARGADO"

    repo.set_estado(conn, proceso_id, "ANALIZADO", error=None, worker_id="worker-1")
    conn.commit()
    assert repo.get_estado(conn, proceso_id) == "ANALIZADO"

    cursor = conn.cursor()
    cursor.execute(
        "SELECT worker_id, ultimo_error FROM licitaciones_metadata WHERE proceso_id = :p",
        p=proceso_id,
    )
    worker, error = cursor.fetchone()
    assert worker == "worker-1"
    assert error is None, "el segundo set_estado debía limpiar ultimo_error"
    cursor.close()


def test_set_estado_persiste_triaje_confianza_y_formato(conn, proceso_id):
    """Las columnas triaje/triaje_conf/formato existen en el esquema desde el
    principio pero hasta ahora nunca se escribían desde el código (quedaban
    NULL para siempre). Verifica que el SET dinámico las escribe cuando se
    indican y no las toca cuando no.
    """
    from src.memoria import repo

    repo.set_estado(
        conn, proceso_id, "DESCARTADO_TRIAJE",
        triaje="NO-GO", triaje_conf=0.85, formato="NATIVO",
    )
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT triaje, triaje_conf, formato FROM licitaciones_metadata WHERE proceso_id = :p",
        p=proceso_id,
    )
    triaje, triaje_conf, formato = cursor.fetchone()
    assert triaje == "NO-GO"
    assert float(triaje_conf) == pytest.approx(0.85)
    assert formato == "NATIVO"

    # Un segundo set_estado SIN estas columnas no debe borrarlas (no forma
    # parte del SET dinámico cuando el valor es None).
    repo.set_estado(conn, proceso_id, "ANALIZADO")
    conn.commit()
    cursor.execute(
        "SELECT triaje, triaje_conf, formato FROM licitaciones_metadata WHERE proceso_id = :p",
        p=proceso_id,
    )
    triaje, triaje_conf, formato = cursor.fetchone()
    assert triaje == "NO-GO", "un set_estado sin triaje no debía borrar el anterior"
    assert formato == "NATIVO"
    cursor.close()


def test_set_estado_rechaza_triaje_fuera_del_check(conn, proceso_id):
    """ck_triaje solo admite 'GO'/'NO-GO': el código y el DDL deben coincidir."""
    from src.memoria import repo

    with pytest.raises(oracledb.DatabaseError) as exc:
        repo.set_estado(conn, proceso_id, "ANALIZADO", triaje="TAL_VEZ")
    conn.rollback()
    assert exc.value.args[0].code == 2290  # ORA-02290: check constraint violated


def test_get_estado_devuelve_none_si_no_existe(conn):
    from src.memoria import repo

    assert repo.get_estado(conn, "NO-EXISTE-JAMAS") is None


def test_set_estado_respeta_el_check_de_la_tabla(conn, proceso_id):
    """Un estado fuera del CHECK ck_estado debe rebotar en la base.

    Comprueba que el DDL y el código hablan del mismo vocabulario: si alguien
    agrega un estado nuevo en Python sin tocar db/schema.sql, esto lo atrapa.
    """
    from src.memoria import repo

    with pytest.raises(oracledb.DatabaseError) as exc:
        repo.set_estado(conn, proceso_id, "ESTADO_INVENTADO")
    conn.rollback()
    assert exc.value.args[0].code == 2290  # ORA-02290: check constraint violated


def test_claim_siguiente_reclama_y_avanza_la_cola(conn, proceso_id):
    """El bloque PL/SQL con FOR UPDATE SKIP LOCKED reclama el proceso pendiente.

    Es la sentencia más frágil de todo Track B (cursor explícito + WHERE
    CURRENT OF) y la que más caro costaba verificar: hasta ahora exigía la ADB.
    """
    from src.memoria import repo

    repo.set_estado(conn, proceso_id, "DESCARGADO")
    conn.commit()

    reclamado = repo.claim_siguiente(conn, worker_id="worker-test")
    conn.commit()
    assert reclamado is not None, "había un proceso DESCARGADO y no reclamó ninguno"

    cursor = conn.cursor()
    cursor.execute(
        "SELECT estado, worker_id, intentos FROM licitaciones_metadata "
        "WHERE proceso_id = :p",
        p=reclamado,
    )
    estado, worker, intentos = cursor.fetchone()
    assert estado == "FORMATO_NATIVO"
    assert worker == "worker-test"
    assert intentos >= 1
    cursor.close()


def test_claim_siguiente_sin_pendientes_devuelve_none(conn):
    """Con la cola vacía, el bind OUT del bloque PL/SQL vuelve NULL, no explota.

    OJO: para vaciar la cola toca TODAS las filas en 'DESCARGADO'. Es legítimo
    únicamente porque este módulo habla con un contenedor local y desechable —
    nunca con la ADB, cuyo DSN ni siquiera es alcanzable desde acá.
    """
    from src.memoria import repo

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE licitaciones_metadata SET estado = 'NOTIFICADO' WHERE estado = 'DESCARGADO'"
    )
    conn.commit()
    cursor.close()

    assert repo.claim_siguiente(conn, worker_id="worker-test") is None


def test_claim_siguiente_v2_reclama_y_avanza_la_cola(conn, portal_y_org, dos_clientes_v2):
    """Espejo v2 del test más frágil de todo Track B: cursor explícito +
    FOR UPDATE OF ... SKIP LOCKED + WHERE CURRENT OF, ahora con un JOIN a
    `proceso` (para poder ordenar por fecha_limite_utc, que no vive en
    cliente_proceso) — el riesgo real señalado en el plan de esta fase.
    """
    from src.memoria import repo

    cliente_a, _cliente_b = dos_clientes_v2
    numero_referencia = f"TEST-{_uuid.uuid4().hex[:12]}"
    proceso_gid = f"{portal_y_org['portal_id']}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) VALUES (:g, :p, :r)",
        g=proceso_gid, p=portal_y_org["portal_id"], r=numero_referencia,
    )
    cursor.execute(
        "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, 'DESCARGADO')",
        c=cliente_a, g=proceso_gid,
    )
    conn.commit()
    cursor.close()

    reclamado = repo.claim_siguiente_v2(conn, cliente_a, worker_id="worker-test")
    conn.commit()
    assert reclamado == numero_referencia, "había un proceso DESCARGADO de este cliente y no lo reclamó"

    cursor = conn.cursor()
    cursor.execute(
        "SELECT estado, intentos FROM cliente_proceso WHERE cliente_id = :c AND proceso_gid = :g",
        c=cliente_a, g=proceso_gid,
    )
    estado, intentos = cursor.fetchone()
    assert estado == "FORMATO_NATIVO"
    assert intentos >= 1

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
    conn.commit()
    cursor.close()


def test_claim_siguiente_v2_sin_pendientes_devuelve_none(conn, dos_clientes_v2):
    """A diferencia de la v1, no hace falta vaciar ninguna cola global: cada
    cliente de prueba arranca sin ninguna fila en cliente_proceso."""
    from src.memoria import repo

    cliente_a, _cliente_b = dos_clientes_v2
    assert repo.claim_siguiente_v2(conn, cliente_a, worker_id="worker-test") is None


def test_claim_siguiente_v2_la_cola_es_por_cliente(conn, portal_y_org, dos_clientes_v2):
    """La prueba de que la cola en v2 necesariamente se volvió por-cliente:
    mismo proceso_gid, DESCARGADO para los dos clientes — reclamar para uno
    no debe tocar la fila del otro."""
    from src.memoria import repo

    cliente_a, cliente_b = dos_clientes_v2
    numero_referencia = f"TEST-{_uuid.uuid4().hex[:12]}"
    proceso_gid = f"{portal_y_org['portal_id']}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) VALUES (:g, :p, :r)",
        g=proceso_gid, p=portal_y_org["portal_id"], r=numero_referencia,
    )
    for cid in (cliente_a, cliente_b):
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, 'DESCARGADO')",
            c=cid, g=proceso_gid,
        )
    conn.commit()
    cursor.close()

    reclamado = repo.claim_siguiente_v2(conn, cliente_a, worker_id="worker-a")
    conn.commit()
    assert reclamado == numero_referencia

    cursor = conn.cursor()
    cursor.execute(
        "SELECT estado FROM cliente_proceso WHERE cliente_id = :c AND proceso_gid = :g",
        c=cliente_b, g=proceso_gid,
    )
    estado_b, = cursor.fetchone()
    assert estado_b == "DESCARGADO", "reclamar para cliente_a no debía tocar la fila de cliente_b"

    # Y cliente_b sigue pudiendo reclamar SU propia fila, intacta:
    reclamado_b = repo.claim_siguiente_v2(conn, cliente_b, worker_id="worker-b")
    conn.commit()
    assert reclamado_b == numero_referencia

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
    conn.commit()
    cursor.close()


def test_save_analisis_db_persiste_json_nativo(conn, proceso_id):
    """El MERGE con JSON(:bind) escribe en las columnas JSON de 23ai.

    Verifica la decisión documentada en el docstring de ``save_analisis_db``
    (envolver el bind en la función SQL ``JSON()`` en vez de confiar en la
    conversión implícita VARCHAR2→JSON) contra el motor real, y que re-guardar
    actualiza en vez de duplicar.
    """
    from src.memoria import repo
    from src.scraper.models import MatrizRiesgos, ResultadoAnalisis, RiesgoItem

    resultado = ResultadoAnalisis(
        proceso_uuid=proceso_id,
        estado="ANALIZADO",
        formato="NATIVO",
        modelo_analisis="claude-haiku-4-5",
        matriz=MatrizRiesgos(
            items=[
                RiesgoItem(
                    categoria="plazos",
                    descripcion="multa del 0.5% por día de atraso",
                    severidad="alta",
                )
            ],
            resumen="riesgo principal: penalidades por atraso",
        ),
        anexos={"anexo_a": "garantía de mantenimiento"},
    )

    repo.save_analisis_db(conn, resultado)
    repo.save_analisis_db(conn, resultado)
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM licitaciones_analisis WHERE proceso_id = :p", p=proceso_id
    )
    assert cursor.fetchone()[0] == 1, "el MERGE duplicó en vez de actualizar"

    # Se lee con el operador de path de JSON nativo: si la columna hubiera
    # quedado como texto plano, esto no devolvería nada.
    cursor.execute(
        "SELECT a.anexos.anexo_a, JSON_VALUE(a.matriz_riesgos, '$.items[0].categoria') "
        "FROM licitaciones_analisis a WHERE a.proceso_id = :p",
        p=proceso_id,
    )
    anexo, categoria = cursor.fetchone()
    assert anexo == "garantía de mantenimiento"
    assert categoria == "plazos"
    cursor.close()


def test_stats_por_institucion(conn, proceso_id):
    """La agregación con ROUND/CASE/SUM corre y devuelve el contrato prometido."""
    from src.memoria import repo

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE licitaciones_metadata SET triaje = 'GO' WHERE proceso_id = :p", p=proceso_id
    )
    conn.commit()
    cursor.close()

    filas = repo.stats_por_institucion(conn, dias=90)
    mias = [f for f in filas if f["institucion"] == "INSTITUCION DE PRUEBA"]
    assert mias, "el proceso de prueba no apareció en la agregación"
    assert set(mias[0]) == {"institucion", "categoria", "conteo", "pct_go"}
    assert mias[0]["pct_go"] == pytest.approx(100.0)


# ═══════════════════════════════════════════════════════════════════════════
# 3) src/memoria/rag.py y vectorizer.py — analítica y SQL vectorial
# ═══════════════════════════════════════════════════════════════════════════

def test_tendencias(conn, proceso_id):
    """rag.tendencias no usa vectores: se ejecuta entera, sin sustituir nada."""
    from src.memoria import rag

    filas = rag.tendencias(conn, dias=90)
    mias = [f for f in filas if f["institucion"] == "INSTITUCION DE PRUEBA"]
    assert mias
    assert set(mias[0]) == {"institucion", "categoria", "conteo", "conteo_go"}


def test_contar_chunks(conn, proceso_id):
    """Las dos variantes de vectorizer.contar_chunks (con y sin filtro)."""
    from src.memoria import vectorizer

    assert vectorizer.contar_chunks(conn, proceso_id) == 0
    assert vectorizer.contar_chunks(conn) >= 0


def test_insert_de_vectorizar_proceso_corre_contra_el_motor(conn, cfg, proceso_id):
    """El DELETE + INSERT de vectorizar_proceso, con el embedding sustituido.

    El INSERT sale del código (cursor espía); lo único que cambia es la llamada
    a ``VECTOR_EMBEDDING``, que acá no existe. Se verifica que las columnas, el
    ``executemany`` por lotes y el ``chunk_id`` que arma el módulo entran de
    verdad en la tabla — incluido el UNIQUE(proceso_id, seq).
    """
    from src.memoria import vectorizer

    texto = (
        "CLAUSULA QUINTA - PENALIDADES. El contratista pagará una multa del 0.5% "
        "por cada día de atraso en la entrega de los servidores rack. "
    ) * 40

    capturadas = _capturar(
        vectorizer.vectorizar_proceso, proceso_id, texto, cfg, batch_size=2
    )
    assert len(capturadas) >= 2, "se esperaba al menos el DELETE y un INSERT"

    delete_sql, delete_binds = capturadas[0]
    assert delete_sql.strip().upper().startswith("DELETE")

    cursor = conn.cursor()
    cursor.execute(delete_sql, delete_binds)

    insertados = 0
    for sql, lote in capturadas[1:]:
        cursor.executemany(_sin_modelo_onnx(sql), lote)
        insertados += len(lote)
    conn.commit()

    assert vectorizer.contar_chunks(conn, proceso_id) == insertados
    cursor.execute(
        "SELECT chunk_id, VECTOR_DIMENSION_COUNT(embedding) FROM licitaciones_chunks "
        "WHERE proceso_id = :p AND seq = 0",
        p=proceso_id,
    )
    chunk_id, dims = cursor.fetchone()
    assert chunk_id == f"{proceso_id}#0"
    assert dims == 384, "el literal no entró en la columna VECTOR(384, FLOAT32)"

    # Idempotencia: el DELETE previo deja la tabla como estaba, no acumula.
    cursor.execute(delete_sql, delete_binds)
    conn.commit()
    assert vectorizer.contar_chunks(conn, proceso_id) == 0
    cursor.close()


@pytest.fixture
def con_chunks(conn, cfg, proceso_id):
    """Deja el proceso con chunks vectorizados usando el INSERT real del código."""
    from src.memoria import vectorizer

    texto = "Servidores rack, firewall perimetral y almacenamiento para data center. " * 30
    capturadas = _capturar(vectorizer.vectorizar_proceso, proceso_id, texto, cfg)
    cursor = conn.cursor()
    for sql, lote in capturadas[1:]:
        cursor.executemany(_sin_modelo_onnx(sql), lote)
    conn.commit()
    cursor.close()
    return proceso_id


def test_buscar_chunks_corre_contra_el_motor(conn, cfg, con_chunks):
    """La query intra-documento: VECTOR_DISTANCE + FETCH FIRST :k ROWS ONLY.

    El ``FETCH FIRST`` con bind es justo el tipo de construcción que un dialecto
    equivocado (``LIMIT``) rompería, y que hasta ahora solo se probaba en la VM.
    """
    from src.memoria import rag

    (sql, binds), = _capturar(rag.buscar_chunks, con_chunks, "¿qué multas hay?", cfg)
    sql = _sin_modelo_onnx(sql)

    cursor = conn.cursor()
    cursor.execute(sql, _binds_presentes(sql, binds))
    filas = cursor.fetchall()
    cursor.close()

    assert filas, "la búsqueda no devolvió chunks del proceso"
    assert len(filas) <= cfg.rag_top_k, "FETCH FIRST :k no acotó el resultado"


def test_buscar_chunks_v2_corre_contra_el_motor(conn, cfg, con_chunks_v2, portal_y_org):
    """Espejo v2 de test_buscar_chunks_corre_contra_el_motor — misma
    construcción FETCH FIRST :k ROWS ONLY, ahora sobre `chunk`."""
    from src.memoria import rag

    (sql, binds), = _capturar(
        rag.buscar_chunks_v2, con_chunks_v2, "¿qué multas hay?", cfg,
        portal_id=portal_y_org["portal_id"],
    )
    sql = _sin_modelo_onnx(sql)

    cursor = conn.cursor()
    cursor.execute(sql, _binds_presentes(sql, binds))
    filas = cursor.fetchall()
    cursor.close()

    assert filas, "la búsqueda no devolvió chunks del proceso"
    assert len(filas) <= cfg.rag_top_k, "FETCH FIRST :k no acotó el resultado"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"excluir_proceso_id": "OTRO-PROCESO"},
        {"institucion": "%PRUEBA%"},
        {"excluir_proceso_id": "OTRO-PROCESO", "institucion": "%PRUEBA%"},
    ],
    ids=["sin_filtros", "excluye", "institucion", "ambos"],
)
def test_buscar_precedentes_todas_sus_variantes(conn, cfg, con_chunks, kwargs):
    """buscar_precedentes arma el WHERE por concatenación: las 4 formas compilan.

    El SQL de esta función no es uno sino cuatro, según qué filtros lleguen. Un
    test que solo probara el caso sin filtros dejaría tres sentencias sin
    verificar — que es exactamente lo que pasaba hasta ahora.
    """
    from src.memoria import rag

    (sql, binds), = _capturar(
        rag.buscar_precedentes, "servidores y firewall", cfg, "test_client", **kwargs
    )
    sql = _sin_modelo_onnx(sql)

    cursor = conn.cursor()
    cursor.execute(sql, _binds_presentes(sql, binds))
    filas = cursor.fetchall()
    cursor.close()

    assert any(fila[0] == con_chunks for fila in filas), (
        f"el proceso de prueba debía aparecer con {kwargs}"
    )


def test_buscar_precedentes_excluye_el_proceso_indicado(conn, cfg, con_chunks):
    """El filtro de exclusión funciona en la base, no solo en Python."""
    from src.memoria import rag

    (sql, binds), = _capturar(
        rag.buscar_precedentes, "servidores", cfg, "test_client", excluir_proceso_id=con_chunks
    )
    sql = _sin_modelo_onnx(sql)

    cursor = conn.cursor()
    cursor.execute(sql, _binds_presentes(sql, binds))
    ids = {fila[0] for fila in cursor.fetchall()}
    cursor.close()

    assert con_chunks not in ids


@pytest.fixture
def con_chunks_otro_cliente(conn, cfg):
    """Un segundo proceso, de OTRO cliente, con chunks sobre el mismo tema.

    A propósito usa texto muy similar al de ``con_chunks`` (mismo vocabulario
    de servidores/firewall) para que, si el filtro de ``cliente_id`` faltara,
    este proceso saldría como precedente de ``test_client`` por pura
    cercanía semántica — que es exactamente la fuga que se está cerrando.
    """
    from src.memoria import vectorizer

    pid = f"TEST-{_uuid.uuid4().hex[:12]}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO licitaciones_metadata (proceso_id, cliente_id, institucion, "
        "titulo, categoria, fecha_pub, estado) VALUES "
        "(:pid, 'otro_cliente', 'INSTITUCION DE OTRO CLIENTE', 'Pliego de otro cliente', "
        "'infraestructura', SYSDATE, 'DESCUBIERTO')",
        pid=pid,
    )
    conn.commit()

    texto = "Servidores rack, firewall perimetral y almacenamiento para data center. " * 30
    capturadas = _capturar(vectorizer.vectorizar_proceso, pid, texto, cfg)
    for sql, lote in capturadas[1:]:
        cursor.executemany(_sin_modelo_onnx(sql), lote)
    conn.commit()

    yield pid

    cursor.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :pid", pid=pid)
    conn.commit()
    cursor.close()


def test_buscar_precedentes_no_cruza_clientes(conn, cfg, con_chunks, con_chunks_otro_cliente):
    """La fuga real: sin filtrar por cliente_id, un cliente vería precedentes
    analizados para otro. ``cliente_id`` es obligatorio en la firma
    justamente para que este caso no se pueda omitir por accidente.
    """
    from src.memoria import rag

    (sql, binds), = _capturar(
        rag.buscar_precedentes, "servidores y firewall", cfg, "test_client"
    )
    sql = _sin_modelo_onnx(sql)

    cursor = conn.cursor()
    cursor.execute(sql, _binds_presentes(sql, binds))
    ids = {fila[0] for fila in cursor.fetchall()}
    cursor.close()

    assert con_chunks in ids, "el propio proceso del cliente debía aparecer"
    assert con_chunks_otro_cliente not in ids, (
        "buscar_precedentes filtrado a 'test_client' devolvió un proceso de "
        "'otro_cliente' — fuga entre clientes"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4) Controles negativos — la prueba de que el andamiaje sirve
# ═══════════════════════════════════════════════════════════════════════════
#
# Un test que solo verifica que el SQL bueno pasa no demuestra nada: un
# `assert True` haría lo mismo. Estos tres parten de una consulta REAL del
# código, le aplican la clase de error que se quiere atrapar, y exigen que el
# motor la rechace con el ORA correspondiente. Si alguno dejara de fallar,
# el andamiaje se habría vuelto ciego y el resto de la suite, decorativo.

def test_control_negativo_columna_inexistente(conn):
    """ORA-00904: renombrar una columna en la tabla sin tocar el SQL del código."""
    from src.memoria import repo

    (sql, _), = _capturar(repo.get_estado, "X")
    mutado = sql.replace("estado", "estado_que_no_existe")
    assert _codigo_ora(mutado, conn) == 904


def test_control_negativo_tabla_inexistente(conn):
    """ORA-00942: la tabla del SQL no existe (típico: renombrarla en el DDL)."""
    from src.memoria import repo

    (sql, _), = _capturar(repo.get_estado, "X")
    mutado = sql.replace("licitaciones_metadata", "licitaciones_metadatos")
    assert _codigo_ora(mutado, conn) == 942


def test_control_negativo_dialecto_equivocado(conn, cfg):
    """ORA-03047: escribir ``LIMIT`` (Postgres/MySQL) en vez de ``FETCH FIRST``.

    Es el error que este andamiaje existe para atrapar: sintaxis de otro motor
    que se cuela en una query, compila en la cabeza de quien la escribió y solo
    revienta contra Oracle.

    Se acepta 03047 o 03049 (misma familia, "SQL keyword ... is not syntactically
    valid following ..."): cuál de los dos sale depende de en qué punto de la
    sentencia tropieza el parser, y esa precisión no aporta nada acá.
    """
    from src.memoria import rag

    (sql, _), = _capturar(rag.buscar_chunks, "X", "pregunta", cfg)
    mutado = _sin_modelo_onnx(sql).replace("FETCH  FIRST :k ROWS ONLY", "LIMIT 5")
    assert "LIMIT 5" in mutado, "cambió el formato de buscar_chunks; ajustar la mutación"
    assert _codigo_ora(mutado, conn) in (3047, 3049)


def test_el_sql_bueno_compila(conn, cfg):
    """Contracara de los controles negativos: sin mutar, la misma query compila."""
    from src.memoria import rag

    (sql, _), = _capturar(rag.buscar_chunks, "X", "pregunta", cfg)
    assert _codigo_ora(_sin_modelo_onnx(sql), conn) is None


# ── Mismos controles negativos, sobre los lectores v2 (Fase 4, switch) ──────
#
# El bug más probable en esta migración es justamente el que estos tres
# atrapan: renombrar una tabla/columna al escribir el SQL v2 a mano. Un test
# que solo prueba el SQL v2 "bueno" no demuestra que el andamiaje sigue vivo
# para él — hay que mutarlo también.

def test_control_negativo_v2_columna_inexistente(conn):
    """ORA-00904 sobre cliente_proceso, no solo sobre licitaciones_metadata."""
    from src.memoria import repo

    (sql, _), = _capturar(repo.get_estado_v2, "cli", "X")
    mutado = sql.replace("estado", "estado_que_no_existe")
    assert _codigo_ora(mutado, conn) == 904


def test_control_negativo_v2_tabla_inexistente(conn):
    from src.memoria import repo

    (sql, _), = _capturar(repo.get_estado_v2, "cli", "X")
    mutado = sql.replace("cliente_proceso", "cliente_procesos")
    assert _codigo_ora(mutado, conn) == 942


def test_control_negativo_v2_dialecto_equivocado(conn, cfg):
    from src.memoria import rag

    (sql, _), = _capturar(rag.buscar_chunks_v2, "X", "pregunta", cfg)
    mutado = _sin_modelo_onnx(sql).replace("FETCH  FIRST :k ROWS ONLY", "LIMIT 5")
    assert "LIMIT 5" in mutado, "cambió el formato de buscar_chunks_v2; ajustar la mutación"
    assert _codigo_ora(mutado, conn) in (3047, 3049)


def test_el_sql_bueno_v2_compila(conn, cfg):
    """Contracara: buscar_chunks_v2, buscar_precedentes_v2 y tendencias_v2
    compilan sin mutar — si alguno dejara de compilar, este test lo dice
    antes que un renombre silencioso en producción."""
    from src.memoria import rag

    (sql_chunks, _), = _capturar(rag.buscar_chunks_v2, "X", "pregunta", cfg)
    assert _codigo_ora(_sin_modelo_onnx(sql_chunks), conn) is None

    (sql_precedentes, _), = _capturar(rag.buscar_precedentes_v2, "pregunta", cfg, cliente_id="cli")
    assert _codigo_ora(_sin_modelo_onnx(sql_precedentes), conn) is None

    (sql_tendencias, _), = _capturar(rag.tendencias_v2, "cli")
    assert _codigo_ora(sql_tendencias, conn) is None


# ═══════════════════════════════════════════════════════════════════════════
# Feedback de relevancia (botones del correo): src/feedback/repo.py
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def clave_feedback(conn) -> str:
    """Un proceso_id propio del test; borra sus filas de las dos tablas al final.

    Las tablas de feedback no tienen FK a `licitaciones_metadata` (a propósito),
    así que el CASCADE de `proceso_id` no las limpia: se borran a mano.
    """
    pid = f"FB-{_uuid.uuid4().hex[:12]}"
    yield pid
    cursor = conn.cursor()
    for tabla in ("licitaciones_feedback", "licitaciones_reportadas"):
        cursor.execute(f"DELETE FROM {tabla} WHERE proceso_id = :p", p=pid)
    conn.commit()
    cursor.close()


def _opp_clasificada(numero_referencia: str):
    from src.scraper.models import ProcesoClasificado

    return ProcesoClasificado(
        institucion="MINISTERIO DE PRUEBA",
        descripcion="Adquisición de firewalls perimetrales",
        fecha_publicacion=datetime.now(timezone.utc),
        url_directa="https://example.test/proceso",
        numero_referencia=numero_referencia,
        es_relevante=True,
        categoria_detectada="ciberseguridad",
        match_tecnico=0.9,
        match_categoria=0.8,
        factibilidad=0.7,
        tags=["Firewall", "Ñandú"],
    )


def test_feedback_reportada_se_registra_y_el_reenvio_cuenta(conn, clave_feedback):
    """El MERGE de la foto inserta una vez y en el reenvío cuenta, sin duplicar."""
    from src.feedback import repo

    repo.upsert_reportada(conn, "test_client", _opp_clasificada(clave_feedback))
    repo.upsert_reportada(conn, "test_client", _opp_clasificada(clave_feedback))
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*), MAX(veces_enviada), MAX(JSON_VALUE(tags, '$[1]')) "
        "FROM licitaciones_reportadas WHERE proceso_id = :p", p=clave_feedback,
    )
    filas, veces, segundo_tag = cursor.fetchone()
    cursor.close()
    assert (filas, veces) == (1, 2), "el reenvío duplicó la fila o no la contó"
    assert segundo_tag == "Ñandú", "los tags no quedaron como JSON nativo con sus acentos"

    foto = repo.get_reportada(conn, clave_feedback, "test_client")
    assert foto["titulo"] == "Adquisición de firewalls perimetrales"
    assert repo.get_reportada(conn, clave_feedback, "otro_cliente") is None


def test_feedback_un_voto_por_destinatario_y_cambiar_de_opinion_sobrescribe(conn, clave_feedback):
    from src.feedback import repo

    comunes = dict(proceso_id=clave_feedback, cliente_id="test_client")
    repo.upsert_voto(conn, **comunes, destinatario_id="ana", destinatario_email="ana@x.com",
                     voto="IRRELEVANTE", motivos=["fuera_de_rubro", "otro"],
                     comentario="No vendemos eso — ¿quién lo pidió?")
    repo.upsert_voto(conn, **comunes, destinatario_id="luis", destinatario_email="luis@x.com",
                     voto="RELEVANTE", motivos=[], comentario="")
    conn.commit()

    ana = repo.get_voto(conn, clave_feedback, "test_client", "ana")
    assert ana == {"voto": "IRRELEVANTE", "motivos": ["fuera_de_rubro", "otro"],
                   "comentario": "No vendemos eso — ¿quién lo pidió?"}
    assert repo.get_voto(conn, clave_feedback, "test_client", "luis")["voto"] == "RELEVANTE"

    # Ana cambia de opinión: la fila se sobrescribe y los motivos viejos no sobreviven.
    repo.upsert_voto(conn, **comunes, destinatario_id="ana", destinatario_email="ana@x.com",
                     voto="RELEVANTE", motivos=["fuera_de_rubro"], comentario="me equivoqué")
    conn.commit()
    ana = repo.get_voto(conn, clave_feedback, "test_client", "ana")
    assert ana == {"voto": "RELEVANTE", "motivos": [], "comentario": ""}

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM licitaciones_feedback WHERE proceso_id = :p",
                   p=clave_feedback)
    assert cursor.fetchone()[0] == 2, "cada destinatario debe tener exactamente una fila"
    cursor.close()


def test_feedback_el_check_rechaza_un_voto_desconocido(conn, clave_feedback):
    """La restricción vive en la base, no solo en Python: un INSERT directo tampoco pasa."""
    cursor = conn.cursor()
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        cursor.execute(
            "INSERT INTO licitaciones_feedback (proceso_id, cliente_id, destinatario_id, voto) "
            "VALUES (:p, 'c', 'd', 'TAL VEZ')", p=clave_feedback,
        )
    conn.rollback()
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 5) Datos de mercado: adjudicaciones y resultado propio (plan del portal, §3.5)
# ═══════════════════════════════════════════════════════════════════════════
#
# Aditivo sobre el esquema actual: sin FK a licitaciones_metadata (no depende
# de proceso_gid/cliente_proceso, el esquema v2 todavía sin arrancar).

def _item_adjudicacion(id_, codigo_proceso, monto=100.0, id_proveedor=691, **overrides):
    base = {
        "id": id_,
        "monto": monto,
        "proceso_compra": {
            "id": 138001, "nombre_proceso": "Proceso de prueba", "codigo_proceso": codigo_proceso,
            "fecha_adjudicacion": "2026-09-25",
        },
        "institucion": {"id": 25, "codigo": "300", "nombre": "Institución de prueba"},
        "proveedor": {
            "id": id_proveedor, "id_proveedor": id_proveedor,
            "nombre": f"Proveedor {id_proveedor}", "nombre_comercial": f"Proveedor {id_proveedor}",
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def codigo_adjudicacion():
    """Un código de proceso único por test, con limpieza de ambas tablas al final."""
    codigo = f"TEST-ADJ-{_uuid.uuid4().hex[:12]}"
    yield codigo


@pytest.fixture(autouse=False)
def _limpiar_mercado(conn, codigo_adjudicacion):
    yield
    cursor = conn.cursor()
    cursor.execute("DELETE FROM adjudicacion_item WHERE codigo_proceso = :c", c=codigo_adjudicacion)
    cursor.execute("DELETE FROM proceso_participado WHERE codigo_proceso = :c", c=codigo_adjudicacion)
    conn.commit()
    cursor.close()


def test_upsert_adjudicacion_item_merge_es_idempotente(conn, codigo_adjudicacion, _limpiar_mercado):
    from src.mercado import repo

    item = _item_adjudicacion(id_=900001, codigo_proceso=codigo_adjudicacion, monto=55.5)
    repo.upsert_adjudicacion_item(conn, item, es_multicomputos=False)
    repo.upsert_adjudicacion_item(conn, item, es_multicomputos=False)
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*), MAX(monto), MAX(es_multicomputos), MAX(JSON_VALUE(datos_crudos, '$.id')) "
        "FROM adjudicacion_item WHERE codigo_proceso = :c",
        c=codigo_adjudicacion,
    )
    filas, monto, es_mc, raw_id = cursor.fetchone()
    assert filas == 1, "el MERGE duplicó en vez de actualizar"
    assert float(monto) == pytest.approx(55.5)
    assert es_mc == 0
    assert raw_id == "900001", "el crudo completo debe quedar legible como JSON nativo"
    cursor.close()


def test_upsert_adjudicacion_item_es_multicomputos_true(conn, codigo_adjudicacion, _limpiar_mercado):
    from src.mercado import repo

    item = _item_adjudicacion(id_=900002, codigo_proceso=codigo_adjudicacion, id_proveedor=20371)
    repo.upsert_adjudicacion_item(conn, item, es_multicomputos=True)
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT es_multicomputos, id_proveedor FROM adjudicacion_item WHERE id = 900002"
    )
    es_mc, id_proveedor = cursor.fetchone()
    assert es_mc == 1
    assert id_proveedor == 20371
    cursor.close()


def test_adjudicacion_item_check_rechaza_es_multicomputos_fuera_de_rango(conn):
    """La restricción vive en la base: un INSERT directo con 2 tampoco pasa."""
    cursor = conn.cursor()
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        cursor.execute(
            "INSERT INTO adjudicacion_item (id, codigo_proceso, es_multicomputos) "
            "VALUES (999999999, 'X', 2)"
        )
    conn.rollback()
    cursor.close()


def test_upsert_proceso_participado_no_pisa_resultado_ya_resuelto(conn, codigo_adjudicacion, _limpiar_mercado):
    from src.mercado import repo

    item = {"codigo": codigo_adjudicacion, "nombre": "Proceso de prueba",
            "institucion": "Institución de prueba", "metodo": "Comparación de precios",
            "fecha": "2026-09-01"}
    repo.upsert_proceso_participado(conn, "test_client", item)
    conn.commit()
    assert repo.contar_pendientes(conn, "test_client") >= 1

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE proceso_participado SET resultado = 'GANADO' "
        "WHERE cliente_id = 'test_client' AND codigo_proceso = :c",
        c=codigo_adjudicacion,
    )
    conn.commit()

    # Reprocesar el mismo item (como haría una segunda corrida de sincronización)
    # no debe resetear el resultado ya resuelto a PENDIENTE.
    repo.upsert_proceso_participado(conn, "test_client", item)
    conn.commit()

    cursor.execute(
        "SELECT resultado FROM proceso_participado "
        "WHERE cliente_id = 'test_client' AND codigo_proceso = :c",
        c=codigo_adjudicacion,
    )
    assert cursor.fetchone()[0] == "GANADO"
    cursor.close()


def test_proceso_participado_check_rechaza_resultado_desconocido(conn):
    cursor = conn.cursor()
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        cursor.execute(
            "INSERT INTO proceso_participado (cliente_id, codigo_proceso, resultado) "
            "VALUES ('c', 'X', 'TAL VEZ')"
        )
    conn.rollback()
    cursor.close()


def test_resolver_resultados_gana_pierde_y_deja_pendiente(conn, codigo_adjudicacion, _limpiar_mercado):
    """El escenario completo: tres procesos participados, tres desenlaces distintos."""
    from src.mercado import repo

    ganado = f"{codigo_adjudicacion}-GANADO"
    perdido = f"{codigo_adjudicacion}-PERDIDO"
    pendiente = f"{codigo_adjudicacion}-PENDIENTE"

    # Adjudicaciones de mercado: una nuestra (ganado), una de un competidor (perdido).
    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=900101, codigo_proceso=ganado, monto=1000, id_proveedor=20371),
        es_multicomputos=True,
    )
    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=900102, codigo_proceso=perdido, monto=2000, id_proveedor=555),
        es_multicomputos=False,
    )
    # `pendiente` no tiene ninguna fila en adjudicacion_item todavía (no se ha publicado la adjudicación).

    for codigo in (ganado, perdido, pendiente):
        repo.upsert_proceso_participado(conn, "test_client", {
            "codigo": codigo, "nombre": "x", "institucion": "y", "metodo": "z", "fecha": "2026-09-01",
        })
    conn.commit()

    resultado = repo.resolver_resultados(conn, "test_client")
    conn.commit()
    assert resultado["ganados"] >= 1
    assert resultado["perdidos"] >= 1

    cursor = conn.cursor()
    cursor.execute(
        "SELECT codigo_proceso, resultado, monto_adjudicado_a_nosotros, proveedor_ganador, monto_ganador "
        "FROM proceso_participado WHERE cliente_id = 'test_client' AND codigo_proceso IN (:g, :p, :d)",
        g=ganado, p=perdido, d=pendiente,
    )
    filas = {r[0]: r for r in cursor.fetchall()}

    assert filas[ganado][1] == "GANADO"
    assert float(filas[ganado][2]) == pytest.approx(1000)

    assert filas[perdido][1] == "PERDIDO"
    assert filas[perdido][3] == "Proveedor 555"
    assert float(filas[perdido][4]) == pytest.approx(2000)

    assert filas[pendiente][1] == "PENDIENTE"

    cursor.execute(
        "DELETE FROM adjudicacion_item WHERE codigo_proceso IN (:g, :p)", g=ganado, p=perdido,
    )
    cursor.execute(
        "DELETE FROM proceso_participado WHERE cliente_id = 'test_client' AND codigo_proceso IN (:g, :p, :d)",
        g=ganado, p=perdido, d=pendiente,
    )
    conn.commit()
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 6) Espejo del libro LLM y del historial de corridas (plan del portal, §9)
# ═══════════════════════════════════════════════════════════════════════════

def test_llm_registro_espejo_adb_persiste_via_completar(conn, monkeypatch):
    """De punta a punta: `llm.completar()` con `enable_memoria=True` deja la
    llamada en `llm_llamada`, contra el motor real (no un mock de cursor).
    """
    import types

    from src import llm
    from src.llm.contrato import BloqueTexto, Pedido, Respuesta, Uso
    from src.memoria import db as memoria_db

    cfg = types.SimpleNamespace(
        llm_provider="oci", oci_familia="nativo", data_dir="/tmp",
        llm_registro_habilitado=True, enable_memoria=True, app_version="test",
        adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )
    monkeypatch.setattr(memoria_db, "_pool", None)  # fuerza a que use el DSN de este test
    monkeypatch.setattr(
        llm, "_adaptador",
        lambda proveedor, etapa="": (lambda c, p, cliente=None: Respuesta(
            texto="ok", uso=Uso(input_tokens=100, output_tokens=20),
            modelo="meta.llama-4-scout-17b-16e-instruct", proveedor="oci",
        )),
    )

    pedido = Pedido(etapa="triaje_test_integration", contenido=[BloqueTexto(texto="x")],
                    sistema="sys", max_tokens=256)
    llm.completar(cfg, pedido)
    memoria_db.close_pool()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT proveedor, etapa, ok, input_tokens, output_tokens, "
        "JSON_VALUE(detalle, '$.uso_crudo._reconstruido') "
        "FROM llm_llamada WHERE etapa = 'triaje_test_integration'"
    )
    filas = cursor.fetchall()
    assert len(filas) == 1
    proveedor, etapa, ok, entrada, salida, reconstruido = filas[0]
    assert proveedor == "oci"
    assert ok == 1
    assert entrada == 100
    assert salida == 20
    assert reconstruido == "true"

    cursor.execute("DELETE FROM llm_llamada WHERE etapa = 'triaje_test_integration'")
    conn.commit()
    cursor.close()


def test_ejecucion_insert_y_check_de_success(conn):
    """DDL real: `ejecucion` acepta una corrida y el CHECK rechaza un `success` inválido."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ejecucion (run_timestamp, success, procesos_revisados, clientes_procesados, version) "
        "VALUES (SYSTIMESTAMP, 1, 42, 1, 'test-sha')"
    )
    conn.commit()
    cursor.execute(
        "SELECT procesos_revisados, clientes_procesados, version FROM ejecucion "
        "WHERE version = 'test-sha'"
    )
    assert cursor.fetchone() == (42, 1, "test-sha")

    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        cursor.execute("INSERT INTO ejecucion (run_timestamp, success) VALUES (SYSTIMESTAMP, 2)")
    conn.rollback()

    cursor.execute("DELETE FROM ejecucion WHERE version = 'test-sha'")
    conn.commit()
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 7) Recuperación ante desastre: reimportar JSONL → ADB, sin LLM (plan §2.3)
# ═══════════════════════════════════════════════════════════════════════════

def test_reimportar_cliente_es_idempotente_y_respeta_la_fk(conn, cfg, tmp_path):
    """De punta a punta: JSONL real → reimportar_cliente → ADB real.

    Cubre lo que un mock de `repo` no puede probar: que el UPSERT + SET_ESTADO
    + SAVE_ANALISIS_DB de una reimportación completa corran dos veces sin
    duplicar ni romper la FK real de `licitaciones_analisis`, y que un
    huérfano (análisis sin clasificación) NO dispare un ORA-02291 en vez de
    saltarse en Python.
    """
    import types
    from datetime import datetime, timezone

    from src import history
    from src.memoria import reimportar
    from src.scraper.models import MatrizRiesgos, ProcesoClasificado, ResultadoAnalisis, RiesgoItem

    cliente = f"test_reimport_{_uuid.uuid4().hex[:8]}"
    pid = f"TEST-REIMPORT-{_uuid.uuid4().hex[:8]}"
    data_dir = str(tmp_path)

    proceso = ProcesoClasificado(
        institucion="Institución de prueba", descripcion="Adquisición de prueba",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test/x",
        numero_referencia=pid, es_relevante=True, monto=12345.67,
    )
    resultado = ResultadoAnalisis(
        proceso_uuid=pid, estado="ANALIZADO", formato="NATIVO", triaje="GO", triaje_confianza=0.9,
        matriz=MatrizRiesgos(items=[RiesgoItem("financiero", "multa", "alta", "5.1")], resumen="ok"),
        modelo_analisis="test-model", monto_estimado=12345.67,
    )
    history.save_run(cliente, datetime.now(timezone.utc), [proceso], 1, data_dir)
    history.save_analisis(cliente, resultado, data_dir)

    cfg_reimport = types.SimpleNamespace(
        data_dir=data_dir, adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )
    try:
        from src.memoria import db as memoria_db
        monkeypatch_pool = memoria_db._pool
        memoria_db._pool = None

        conteos1 = reimportar.reimportar_cliente(cfg_reimport, cliente)
        conteos2 = reimportar.reimportar_cliente(cfg_reimport, cliente)  # idempotencia
        memoria_db.close_pool()
        memoria_db._pool = monkeypatch_pool

        assert conteos1 == {"clasificados": 1, "analisis": 1, "huerfanos": 0}
        assert conteos2 == conteos1, "correrlo dos veces no debe cambiar los conteos"

        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*), MAX(estado), MAX(monto_estimado) FROM licitaciones_metadata "
            "WHERE proceso_id = :p", p=pid,
        )
        filas, estado, monto = cursor.fetchone()
        assert filas == 1, "el MERGE no debe duplicar tras la segunda corrida"
        assert estado == "ANALIZADO"
        assert float(monto) == pytest.approx(12345.67)

        cursor.execute("SELECT COUNT(*) FROM licitaciones_analisis WHERE proceso_id = :p", p=pid)
        assert cursor.fetchone()[0] == 1
        cursor.close()
    finally:
        cur = conn.cursor()
        cur.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :p", p=pid)
        conn.commit()
        cur.close()


def test_reimportar_cliente_huerfano_no_dispara_la_fk_real(conn, cfg, tmp_path):
    """Un análisis sin clasificación correspondiente: si se insertara igual en
    `licitaciones_analisis`, la FK real (`fk_analisis_proc`) lo rechazaría con
    ORA-02291. La reimportación debe saltarlo ANTES de llegar a ese punto.
    """
    import types

    from src import history
    from src.memoria import reimportar
    from src.scraper.models import ResultadoAnalisis

    cliente = f"test_huerfano_{_uuid.uuid4().hex[:8]}"
    pid = f"TEST-HUERFANO-{_uuid.uuid4().hex[:8]}"
    data_dir = str(tmp_path)

    history.save_analisis(
        cliente, ResultadoAnalisis(proceso_uuid=pid, estado="ANALIZADO"), data_dir,
    )

    cfg_reimport = types.SimpleNamespace(
        data_dir=data_dir, adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )
    from src.memoria import db as memoria_db
    monkeypatch_pool = memoria_db._pool
    memoria_db._pool = None

    conteos = reimportar.reimportar_cliente(cfg_reimport, cliente)

    memoria_db.close_pool()
    memoria_db._pool = monkeypatch_pool

    assert conteos == {"clasificados": 0, "analisis": 0, "huerfanos": 1}

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM licitaciones_analisis WHERE proceso_id = :p", p=pid)
    assert cursor.fetchone()[0] == 0, "el huérfano no debía llegar a intentarse insertar"
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 8) Esquema v2 — Fase "Expandir" (db/schema_v2.sql)
# ═══════════════════════════════════════════════════════════════════════════
# Esta sección inserta directamente (igual que la fixture `proceso_id` más
# arriba para licitaciones_metadata), sin pasar por src.memoria.repo: lo que
# importa verificar acá no es "compila" sino que la separación
# proceso/cliente_proceso/analisis cierra de verdad el bug de
# licitaciones_metadata (un cliente le pisa el cliente_id/análisis al otro).
# La Fase "Doble escritura" (repo.upsert_proceso_v2/set_estado_v2/
# save_analisis_v2, llamadas ADEMÁS desde src.memoria.service) se valida en
# la sección 9, más abajo, contra las funciones reales.

def test_schema_v2_es_idempotente(conn):
    """Re-aplicar db/schema_v2.sql sobre un esquema ya creado no debe fallar."""
    cursor = conn.cursor()
    for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
        cursor.execute(sentencia)
    conn.commit()

    cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name IN ("
        "'PORTAL','TAXONOMIA_MODALIDAD','FX_TASA','ORGANIZACION','CLIENTE',"
        "'CLIENTE_CONFIG','CLIENTE_PORTAL','PROCESO','CLIENTE_PROCESO','ANALISIS',"
        "'CHUNK','REPORTADA','FEEDBACK','OPORTUNIDAD_ESTADO','OPORTUNIDAD_EVENTO',"
        "'EJECUCION_ETAPA','KPI_SNAPSHOT','USUARIO','MEMBRESIA','SESION',"
        "'MEMBRESIA_ORG','CODIGO_ACCESO','RECUPERACION_MFA','INVITACION','AUDITORIA')"
    )
    assert cursor.fetchone()[0] == 25
    cursor.close()


@pytest.fixture
def portal_y_org(conn):
    """Portal y organización mínimos para las FK de esta sección; limpia al final."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO portal (portal_id, nombre, pais, zona_horaria, moneda_default, base_url) "
        "VALUES ('test_portal', 'Portal de prueba', 'SV', 'America/El_Salvador', 'USD', 'https://example.test')"
    )
    cursor.execute(
        "INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
        "VALUES ('test_org', 'Org de prueba', 'SV', 'America/El_Salvador')"
    )
    conn.commit()
    yield {"portal_id": "test_portal", "org_id": "test_org"}
    cursor.execute("DELETE FROM portal WHERE portal_id = 'test_portal'")
    cursor.execute("DELETE FROM organizacion WHERE org_id = 'test_org'")
    conn.commit()
    cursor.close()


@pytest.fixture
def dos_clientes_v2(conn, portal_y_org):
    """Dos filas de `cliente` sobre la misma organización; limpia al final."""
    cursor = conn.cursor()
    for cid in ("test_cli_a", "test_cli_b"):
        cursor.execute(
            "INSERT INTO cliente (cliente_id, org_id, nombre) VALUES (:c, :o, :c)",
            c=cid, o=portal_y_org["org_id"],
        )
    conn.commit()
    yield ("test_cli_a", "test_cli_b")
    cursor.execute("DELETE FROM cliente WHERE cliente_id IN ('test_cli_a', 'test_cli_b')")
    conn.commit()
    cursor.close()


def test_cliente_proceso_no_pisa_entre_clientes(conn, portal_y_org, dos_clientes_v2):
    """El bug real de licitaciones_metadata (el MERGE del segundo cliente le
    pisa el cliente_id al primero) es estructuralmente imposible en v2: la
    clave de `cliente_proceso` YA incluye `cliente_id`, así que dos clientes
    sobre el mismo proceso son dos filas, no una.
    """
    cliente_a, cliente_b = dos_clientes_v2
    proceso_gid = f"{portal_y_org['portal_id']}:REF-BUG-MULTICLIENTE"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) "
        "VALUES (:gid, :pid, 'REF-BUG-MULTICLIENTE')",
        gid=proceso_gid, pid=portal_y_org["portal_id"],
    )
    for cid, estado in ((cliente_a, "ANALIZADO"), (cliente_b, "DESCUBIERTO")):
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, :e)",
            c=cid, g=proceso_gid, e=estado,
        )
    conn.commit()

    cursor.execute(
        "SELECT cliente_id, estado FROM cliente_proceso WHERE proceso_gid = :g ORDER BY cliente_id",
        g=proceso_gid,
    )
    filas = dict(cursor.fetchall())
    assert filas == {cliente_a: "ANALIZADO", cliente_b: "DESCUBIERTO"}, (
        "cada cliente debe conservar SU estado sobre el mismo proceso"
    )

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada a cliente_proceso
    conn.commit()
    cursor.close()


def test_get_estado_v2_lee_estado_por_cliente(conn, portal_y_org, dos_clientes_v2):
    """Fase switch (plan del portal §3.7 punto 4): get_estado_v2 debe leer el
    estado DE ESE CLIENTE, no el de cualquier otro sobre el mismo proceso —
    la contraparte de lectura de test_cliente_proceso_no_pisa_entre_clientes."""
    from src.memoria import repo

    cliente_a, cliente_b = dos_clientes_v2
    numero_referencia = "REF-GET-ESTADO-V2"
    proceso_gid = f"{portal_y_org['portal_id']}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) "
        "VALUES (:gid, :pid, :ref)",
        gid=proceso_gid, pid=portal_y_org["portal_id"], ref=numero_referencia,
    )
    for cid, estado in ((cliente_a, "ANALIZADO"), (cliente_b, "DESCUBIERTO")):
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, :e)",
            c=cid, g=proceso_gid, e=estado,
        )
    conn.commit()
    cursor.close()

    assert repo.get_estado_v2(conn, cliente_a, numero_referencia,
                               portal_id=portal_y_org["portal_id"]) == "ANALIZADO"
    assert repo.get_estado_v2(conn, cliente_b, numero_referencia,
                               portal_id=portal_y_org["portal_id"]) == "DESCUBIERTO"
    assert repo.get_estado_v2(conn, "cliente_inexistente", numero_referencia,
                               portal_id=portal_y_org["portal_id"]) is None

    cursor = conn.cursor()
    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada a cliente_proceso
    conn.commit()
    cursor.close()


@pytest.fixture
def con_chunks_v2(conn, cfg, dos_clientes_v2, portal_y_org):
    """Proceso v2 con chunks vectorizados, visible SOLO para `test_cli_a`.

    A diferencia de v1 (donde "otro cliente" es un proceso_id separado, con
    su propia fila de metadata), en v2 el chunk cuelga de `proceso`, no de
    `cliente_proceso` — así que la fuga a cerrar es otra: un MISMO proceso,
    con chunks, del que un cliente (`test_cli_a`) SÍ tiene fila en
    `cliente_proceso` y otro (`test_cli_b`) no tiene ninguna.
    """
    from src.memoria import vectorizer

    cliente_a, _cliente_b = dos_clientes_v2
    numero_referencia = f"TEST-{_uuid.uuid4().hex[:12]}"
    proceso_gid = f"{portal_y_org['portal_id']}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, titulo) "
        "VALUES (:gid, :pid, :ref, 'INSTITUCION DE PRUEBA', 'Pliego de prueba')",
        gid=proceso_gid, pid=portal_y_org["portal_id"], ref=numero_referencia,
    )
    cursor.execute(
        "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, 'ANALIZADO')",
        c=cliente_a, g=proceso_gid,
    )
    conn.commit()

    texto = "Servidores rack, firewall perimetral y almacenamiento para data center. " * 30
    capturadas = _capturar(
        vectorizer.vectorizar_proceso_v2, numero_referencia, texto, cfg,
        portal_id=portal_y_org["portal_id"],
    )
    for sql, lote in capturadas[1:]:
        cursor.executemany(_sin_modelo_onnx(sql), lote)
    conn.commit()

    yield numero_referencia

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada a chunk/cliente_proceso
    conn.commit()
    cursor.close()


def test_buscar_precedentes_v2_no_cruza_clientes(conn, cfg, dos_clientes_v2, con_chunks_v2):
    """La fuga en v2 es más sutil que en v1: el chunk/proceso SÍ existen para
    cualquiera (no son por cliente), pero sin fila en `cliente_proceso` el
    cliente no debe poder verlo como precedente — es lo que hace el JOIN
    (en vez de un WHERE) estructuralmente, no solo por convención.
    """
    from src.memoria import rag

    cliente_a, cliente_b = dos_clientes_v2

    (sql_a, binds_a), = _capturar(
        rag.buscar_precedentes_v2, "servidores y firewall", cfg, cliente_id=cliente_a,
    )
    sql_a = _sin_modelo_onnx(sql_a)
    cursor = conn.cursor()
    cursor.execute(sql_a, _binds_presentes(sql_a, binds_a))
    ids_a = {fila[0] for fila in cursor.fetchall()}

    (sql_b, binds_b), = _capturar(
        rag.buscar_precedentes_v2, "servidores y firewall", cfg, cliente_id=cliente_b,
    )
    sql_b = _sin_modelo_onnx(sql_b)
    cursor.execute(sql_b, _binds_presentes(sql_b, binds_b))
    ids_b = {fila[0] for fila in cursor.fetchall()}
    cursor.close()

    assert con_chunks_v2 in ids_a, "el cliente con fila en cliente_proceso debía verlo"
    assert con_chunks_v2 not in ids_b, (
        "buscar_precedentes_v2 devolvió un proceso sin fila en cliente_proceso "
        "para este cliente — fuga entre clientes"
    )


def test_tendencias_v2_agrega_solo_del_cliente_indicado(conn, portal_y_org, dos_clientes_v2):
    """rag.tendencias_v2 no usa vectores: se ejecuta entera, sin sustituir
    nada. A diferencia de v1 (agregado global), el triaje='GO' de un
    cliente no debe contarse en el agregado de otro."""
    from src.memoria import rag

    cliente_a, cliente_b = dos_clientes_v2
    numero_referencia = f"TEST-{_uuid.uuid4().hex[:12]}"
    proceso_gid = f"{portal_y_org['portal_id']}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, "
        "categoria_raw, fecha_pub_utc) VALUES "
        "(:gid, :pid, :ref, 'INSTITUCION TENDENCIAS V2', 'infraestructura', SYSTIMESTAMP)",
        gid=proceso_gid, pid=portal_y_org["portal_id"], ref=numero_referencia,
    )
    cursor.execute(
        "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, triaje) "
        "VALUES (:c, :g, 'ANALIZADO', 'GO')",
        c=cliente_a, g=proceso_gid,
    )
    conn.commit()
    cursor.close()

    filas_a = rag.tendencias_v2(conn, cliente_a)
    filas_b = rag.tendencias_v2(conn, cliente_b)

    mias_a = [f for f in filas_a if f["institucion"] == "INSTITUCION TENDENCIAS V2"]
    mias_b = [f for f in filas_b if f["institucion"] == "INSTITUCION TENDENCIAS V2"]
    assert mias_a and mias_a[0]["conteo_go"] == 1
    assert set(mias_a[0]) == {"institucion", "categoria", "conteo", "conteo_go"}
    assert not mias_b, "el agregado de cliente_b no debe incluir el proceso de cliente_a"

    cursor = conn.cursor()
    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
    conn.commit()
    cursor.close()


def test_stats_por_institucion_v2_pct_go_es_por_cliente(conn, portal_y_org, dos_clientes_v2):
    """La agregación con ROUND/CASE/SUM corre sobre v2 y devuelve el mismo
    contrato que v1 — pct_go acotado al cliente, no global."""
    from src.memoria import repo

    cliente_a, cliente_b = dos_clientes_v2
    numero_referencia = f"TEST-{_uuid.uuid4().hex[:12]}"
    proceso_gid = f"{portal_y_org['portal_id']}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, "
        "categoria_raw, fecha_pub_utc) VALUES "
        "(:gid, :pid, :ref, 'INSTITUCION STATS V2', 'infraestructura', SYSTIMESTAMP)",
        gid=proceso_gid, pid=portal_y_org["portal_id"], ref=numero_referencia,
    )
    cursor.execute(
        "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, triaje) "
        "VALUES (:c, :g, 'ANALIZADO', 'GO')",
        c=cliente_a, g=proceso_gid,
    )
    conn.commit()
    cursor.close()

    filas_a = repo.stats_por_institucion_v2(conn, cliente_a, dias=90)
    filas_b = repo.stats_por_institucion_v2(conn, cliente_b, dias=90)

    mias_a = [f for f in filas_a if f["institucion"] == "INSTITUCION STATS V2"]
    mias_b = [f for f in filas_b if f["institucion"] == "INSTITUCION STATS V2"]
    assert mias_a, "el proceso de prueba no apareció en la agregación de cliente_a"
    assert set(mias_a[0]) == {"institucion", "categoria", "conteo", "pct_go"}
    assert mias_a[0]["pct_go"] == pytest.approx(100.0)
    assert not mias_b, "cliente_b no tiene fila en cliente_proceso para este proceso"

    cursor = conn.cursor()
    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
    conn.commit()
    cursor.close()


def test_analisis_v2_es_por_cliente_no_global(conn, portal_y_org, dos_clientes_v2):
    """`analisis` v2 tiene clave (cliente_id, proceso_gid): dos clientes pueden
    analizar el mismo proceso sin pisarse (a diferencia de
    licitaciones_analisis, que es una fila por proceso_id sin cliente_id).
    """
    cliente_a, cliente_b = dos_clientes_v2
    proceso_gid = f"{portal_y_org['portal_id']}:REF-ANALISIS-MULTICLIENTE"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) "
        "VALUES (:gid, :pid, 'REF-ANALISIS-MULTICLIENTE')",
        gid=proceso_gid, pid=portal_y_org["portal_id"],
    )
    for cid in (cliente_a, cliente_b):
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, 'ANALIZADO')",
            c=cid, g=proceso_gid,
        )
        cursor.execute(
            "INSERT INTO analisis (cliente_id, proceso_gid, resumen) VALUES (:c, :g, :r)",
            c=cid, g=proceso_gid, r=f"resumen de {cid}",
        )
    conn.commit()

    cursor.execute(
        "SELECT cliente_id, resumen FROM analisis WHERE proceso_gid = :g ORDER BY cliente_id",
        g=proceso_gid,
    )
    filas = dict(cursor.fetchall())
    assert filas == {cliente_a: f"resumen de {cliente_a}", cliente_b: f"resumen de {cliente_b}"}

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada doble
    conn.commit()
    cursor.close()


def test_cliente_proceso_check_rechaza_estado_desconocido(conn, portal_y_org, dos_clientes_v2):
    cliente_a, _ = dos_clientes_v2
    proceso_gid = f"{portal_y_org['portal_id']}:REF-ESTADO-INVALIDO"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) VALUES (:gid, :pid, 'REF-ESTADO-INVALIDO')",
        gid=proceso_gid, pid=portal_y_org["portal_id"],
    )
    conn.commit()
    try:
        with pytest.raises(oracledb.IntegrityError):
            cursor.execute(
                "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado) VALUES (:c, :g, 'NO_EXISTE')",
                c=cliente_a, g=proceso_gid,
            )
    finally:
        conn.rollback()
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
        conn.commit()
        cursor.close()


def test_chunk_v2_se_purga_en_cascada_con_el_proceso(conn, portal_y_org):
    """`chunk` cuelga del PROCESO, no del cliente: borrar el proceso arrastra
    sus chunks, igual que licitaciones_chunks hoy con licitaciones_metadata.
    """
    proceso_gid = f"{portal_y_org['portal_id']}:REF-CASCADA-CHUNK"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) VALUES (:gid, :pid, 'REF-CASCADA-CHUNK')",
        gid=proceso_gid, pid=portal_y_org["portal_id"],
    )
    cursor.execute(
        "INSERT INTO chunk (proceso_gid, seq, texto) VALUES (:g, 0, 'texto de prueba')",
        g=proceso_gid,
    )
    conn.commit()

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM chunk WHERE proceso_gid = :g", g=proceso_gid)
    assert cursor.fetchone()[0] == 0, "el chunk debió purgarse en cascada con el proceso"
    cursor.close()


def test_semilla_esquema_v2_es_idempotente(conn, tmp_path):
    """``scripts/semilla_esquema_v2.semilla`` corrida dos veces no duplica filas."""
    import types

    from scripts import semilla_esquema_v2
    from src.memoria import db as memoria_db

    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    cliente_id = f"test_semilla_{_uuid.uuid4().hex[:8]}"
    (clients_dir / f"{cliente_id}.yaml").write_text(
        f"client:\n  id: {cliente_id}\n  name: Cliente de prueba\n  active: true\n"
        "catalog: {categories: []}\n",
        encoding="utf-8",
    )

    cfg = types.SimpleNamespace(
        clients_dir=str(clients_dir), adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )
    monkeypatch_pool = memoria_db._pool
    memoria_db._pool = None
    try:
        conteos1 = semilla_esquema_v2.semilla(cfg)
        conteos2 = semilla_esquema_v2.semilla(cfg)
    finally:
        memoria_db.close_pool()
        memoria_db._pool = monkeypatch_pool

    assert conteos1 == {"portal": 1, "organizacion": 1, "clientes": 1, "configs_nuevas": 1}
    assert conteos2 == {"portal": 1, "organizacion": 1, "clientes": 1, "configs_nuevas": 0}, (
        "la segunda corrida no debe crear una config v1 nueva ni duplicar filas"
    )

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM cliente WHERE cliente_id = :c", c=cliente_id)
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT COUNT(*) FROM cliente_config WHERE cliente_id = :c", c=cliente_id)
    assert cursor.fetchone()[0] == 1
    cursor.execute("DELETE FROM cliente_config WHERE cliente_id = :c", c=cliente_id)
    cursor.execute("DELETE FROM cliente WHERE cliente_id = :c", c=cliente_id)
    conn.commit()
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 9) Espejo v2 desde src.memoria.repo / service — Fase "Doble escritura"
# ═══════════════════════════════════════════════════════════════════════════
# `repo.upsert_proceso_v2`/`set_estado_v2`/`save_analisis_v2` usan el
# `portal_id` fijo `repo.PORTAL_ID_DEFAULT` ("comprasal_sv"): a diferencia de
# la sección 8 (que usa un portal de prueba propio), acá se necesita ESE
# portal en particular. Se siembra de forma idempotente (INSERT solo si no
# existe) y NO se borra al final: es el portal real, permanente en cualquier
# ambiente donde corra `scripts/semilla_esquema_v2`.

@pytest.fixture
def portal_comprasal_real(conn):
    """Garantiza que exista el portal_id real que usa src.memoria.repo."""
    from src.memoria import repo as _repo

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM portal WHERE portal_id = :p", p=_repo.PORTAL_ID_DEFAULT,
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO portal (portal_id, nombre, pais, zona_horaria, moneda_default, base_url) "
            "VALUES (:p, 'COMPRASAL', 'SV', 'America/El_Salvador', 'USD', 'https://www.comprasal.gob.sv')",
            p=_repo.PORTAL_ID_DEFAULT,
        )
        conn.commit()
    cursor.close()
    return _repo.PORTAL_ID_DEFAULT


@pytest.fixture
def dos_clientes_reales(conn, portal_comprasal_real):
    """Dos `cliente` sobre una `organizacion` propia de esta sección; limpia todo al final."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
        "VALUES ('test_org_v2', 'Org de prueba v2', 'SV', 'America/El_Salvador')"
    )
    for cid in ("test_repo_cli_a", "test_repo_cli_b"):
        cursor.execute(
            "INSERT INTO cliente (cliente_id, org_id, nombre) VALUES (:c, 'test_org_v2', :c)",
            c=cid,
        )
    conn.commit()
    yield ("test_repo_cli_a", "test_repo_cli_b")
    cursor.execute("DELETE FROM cliente WHERE cliente_id IN ('test_repo_cli_a', 'test_repo_cli_b')")
    cursor.execute("DELETE FROM organizacion WHERE org_id = 'test_org_v2'")
    conn.commit()
    cursor.close()


def test_upsert_proceso_v2_real_no_pisa_entre_clientes(conn, dos_clientes_reales):
    """La función REAL (no un INSERT manual): dos clientes sobre el mismo
    proceso terminan en dos filas de `cliente_proceso`, cada una con SU
    estado — el bug de `licitaciones_metadata` es estructuralmente imposible.
    """
    from src.memoria import repo
    from src.scraper.models import Proceso

    cliente_a, cliente_b = dos_clientes_reales
    numero_referencia = f"REF-REPO-V2-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio de Prueba", descripcion="Adquisición de prueba",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test",
        numero_referencia=numero_referencia,
    )

    repo.upsert_proceso_v2(conn, proceso, cliente_a, estado="ANALIZADO")
    repo.upsert_proceso_v2(conn, proceso, cliente_b, estado="DESCUBIERTO")
    conn.commit()

    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
    assert cursor.fetchone()[0] == 1, "el HECHO del proceso es uno solo, lo compartan o no dos clientes"

    cursor.execute(
        "SELECT cliente_id, estado FROM cliente_proceso WHERE proceso_gid = :g ORDER BY cliente_id",
        g=proceso_gid,
    )
    filas = dict(cursor.fetchall())
    assert filas == {cliente_a: "ANALIZADO", cliente_b: "DESCUBIERTO"}

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
    conn.commit()
    cursor.close()


def test_set_estado_v2_real_actualiza_solo_la_fila_del_cliente_correcto(conn, dos_clientes_reales):
    from src.memoria import repo
    from src.scraper.models import Proceso

    cliente_a, cliente_b = dos_clientes_reales
    numero_referencia = f"REF-SETESTADO-V2-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio X", descripcion="Test",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test",
        numero_referencia=numero_referencia,
    )
    repo.upsert_proceso_v2(conn, proceso, cliente_a)
    repo.upsert_proceso_v2(conn, proceso, cliente_b)
    conn.commit()

    repo.set_estado_v2(conn, numero_referencia, cliente_a, "ANALIZADO", triaje="GO", triaje_conf=0.85)
    conn.commit()

    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cliente_id, estado, triaje FROM cliente_proceso WHERE proceso_gid = :g ORDER BY cliente_id",
        g=proceso_gid,
    )
    filas = {c: (e, t) for c, e, t in cursor.fetchall()}
    assert filas[cliente_a] == ("ANALIZADO", "GO")
    assert filas[cliente_b] == ("DESCUBIERTO", None), "el otro cliente no debió tocarse"

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
    conn.commit()
    cursor.close()


def test_save_analisis_v2_real_es_por_cliente_via_las_funciones_reales(conn, dos_clientes_reales):
    from src.memoria import repo
    from src.scraper.models import MatrizRiesgos, Proceso, ResultadoAnalisis, RiesgoItem

    cliente_a, cliente_b = dos_clientes_reales
    numero_referencia = f"REF-ANALISIS-V2-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio X", descripcion="Test",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test",
        numero_referencia=numero_referencia,
    )
    for cid in (cliente_a, cliente_b):
        repo.upsert_proceso_v2(conn, proceso, cid, estado="ANALIZADO")
        repo.save_analisis_v2(
            conn,
            ResultadoAnalisis(
                proceso_uuid=numero_referencia, estado="ANALIZADO",
                matriz=MatrizRiesgos(items=[RiesgoItem("financiero", "multa", "media", "5.1")],
                                      resumen=f"resumen de {cid}"),
                modelo_analisis="test-model",
            ),
            cid,
        )
    conn.commit()

    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cliente_id, resumen FROM analisis WHERE proceso_gid = :g ORDER BY cliente_id",
        g=proceso_gid,
    )
    filas = dict(cursor.fetchall())
    assert filas == {cliente_a: f"resumen de {cliente_a}", cliente_b: f"resumen de {cliente_b}"}

    cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada doble
    conn.commit()
    cursor.close()


def test_service_persistir_memoria_con_v2_activado_escribe_ambos_esquemas(conn, dos_clientes_reales):
    """De punta a punta, a través de `src.memoria.service` (no de `repo`
    directamente): con `ENABLE_ESQUEMA_V2` encendido, una corrida real deja
    fila en `licitaciones_metadata`/`licitaciones_analisis` (v1) Y en
    `proceso`/`cliente_proceso`/`analisis` (v2).
    """
    import types

    from src.memoria import db as memoria_db
    from src.memoria import service
    from src.scraper.models import MatrizRiesgos, Proceso, ResultadoAnalisis

    cliente_a, _ = dos_clientes_reales
    numero_referencia = f"REF-SERVICE-V2-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio X", descripcion="Test de servicio",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test",
        numero_referencia=numero_referencia,
    )
    resultado = ResultadoAnalisis(
        proceso_uuid=numero_referencia, estado="ANALIZADO", formato="NATIVO",
        matriz=MatrizRiesgos(items=[], resumen="resumen vía service"),
        modelo_analisis="test-model",
    )
    cfg = types.SimpleNamespace(
        enable_memoria=True, enable_esquema_v2=True,
        adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )

    monkeypatch_pool = memoria_db._pool
    memoria_db._pool = None
    try:
        service.persistir_memoria(cfg, proceso, resultado, "", cliente_a)
    finally:
        memoria_db.close_pool()
        memoria_db._pool = monkeypatch_pool

    from src.memoria import repo
    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{numero_referencia}"
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM licitaciones_metadata WHERE proceso_id = :p", p=numero_referencia,
        )
        assert cursor.fetchone()[0] == 1, "v1 (licitaciones_metadata) debe seguir escribiéndose"
        cursor.execute(
            "SELECT COUNT(*) FROM licitaciones_analisis WHERE proceso_id = :p", p=numero_referencia,
        )
        assert cursor.fetchone()[0] == 1, "v1 (licitaciones_analisis) debe seguir escribiéndose"
        cursor.execute("SELECT COUNT(*) FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
        assert cursor.fetchone()[0] == 1, "v2 (proceso) debe escribirse cuando ENABLE_ESQUEMA_V2=true"
        cursor.execute(
            "SELECT COUNT(*) FROM cliente_proceso WHERE cliente_id = :c AND proceso_gid = :g",
            c=cliente_a, g=proceso_gid,
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT resumen FROM analisis WHERE cliente_id = :c AND proceso_gid = :g",
            c=cliente_a, g=proceso_gid,
        )
        assert cursor.fetchone()[0] == "resumen vía service"
    finally:
        cursor.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :p", p=numero_referencia)
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
        conn.commit()
        cursor.close()


def test_vectorizar_proceso_v2_insert_corre_contra_el_motor(conn, portal_comprasal_real, cfg):
    """Mismo patrón que ``test_insert_de_vectorizar_proceso_corre_contra_el_motor``
    (cursor espía + sustitución de ``VECTOR_EMBEDDING``), pero contra `chunk`:
    requiere que la fila de `proceso` ya exista (FK `fk_chunk_proceso`).
    """
    from src.memoria import vectorizer

    numero_referencia = f"REF-CHUNK-V2-{_uuid.uuid4().hex[:8]}"
    proceso_gid = f"{portal_comprasal_real}:{numero_referencia}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) VALUES (:g, :p, :n)",
        g=proceso_gid, p=portal_comprasal_real, n=numero_referencia,
    )
    conn.commit()

    texto = (
        "CLAUSULA QUINTA - PENALIDADES. El contratista pagará una multa del 0.5% "
        "por cada día de atraso en la entrega de los servidores rack. "
    ) * 40

    try:
        capturadas = _capturar(
            vectorizer.vectorizar_proceso_v2, numero_referencia, texto, cfg, batch_size=2
        )
        assert len(capturadas) >= 2, "se esperaba al menos el DELETE y un INSERT"

        delete_sql, delete_binds = capturadas[0]
        assert delete_sql.strip().upper().startswith("DELETE")
        cursor.execute(delete_sql, delete_binds)

        insertados = 0
        for sql, lote in capturadas[1:]:
            cursor.executemany(_sin_modelo_onnx(sql), lote)
            insertados += len(lote)
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM chunk WHERE proceso_gid = :g", g=proceso_gid)
        assert cursor.fetchone()[0] == insertados

        cursor.execute(
            "SELECT VECTOR_DIMENSION_COUNT(embedding) FROM chunk WHERE proceso_gid = :g AND seq = 0",
            g=proceso_gid,
        )
        assert cursor.fetchone()[0] == 384, "el literal no entró en la columna VECTOR(384, FLOAT32)"

        # Idempotencia: el DELETE previo deja la tabla como estaba, no acumula.
        cursor.execute(delete_sql, delete_binds)
        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM chunk WHERE proceso_gid = :g", g=proceso_gid)
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
        conn.commit()
        cursor.close()


def test_vectorizar_proceso_v2_sin_proceso_padre_viola_la_fk_real(conn, portal_comprasal_real, cfg):
    """Sin la fila de `proceso`, el INSERT de `chunk` debe rechazarse por la
    FK real (`fk_chunk_proceso`), no por un chequeo de Python — es lo que
    permite que `service.py` decida degradar con su propio try/except sin
    que este módulo tenga que replicar esa validación.
    """
    from src.memoria import vectorizer

    numero_referencia = f"REF-CHUNK-HUERFANO-{_uuid.uuid4().hex[:8]}"
    capturadas = _capturar(
        vectorizer.vectorizar_proceso_v2, numero_referencia, "texto de prueba " * 50, cfg,
    )
    inserts = [c for c in capturadas if not c[0].strip().upper().startswith("DELETE")]
    assert inserts, "se esperaba al menos un INSERT capturado"

    cursor = conn.cursor()
    with pytest.raises(oracledb.IntegrityError):
        cursor.executemany(_sin_modelo_onnx(inserts[0][0]), inserts[0][1])
    conn.rollback()
    cursor.close()


def test_service_persistir_memoria_v2_vectorizar_fallando_de_verdad_no_revierte_nada(
    conn, dos_clientes_reales, monkeypatch
):
    """De punta a punta a través de `service.persistir_memoria`, forzando un
    fallo REAL (no simulado) del espejo v2 de la vectorización: el
    contenedor local no tiene el modelo ONNX cargado (ver el docstring del
    módulo), así que una llamada de verdad a `VECTOR_EMBEDDING` sobre
    `chunk` falla con un `ORA-...` genuino.

    `vectorizar_proceso` (v1) se monkeypatchea a un no-op — no porque haga
    falta ocultar el mismo problema (también fallaría sin el modelo), sino
    para que ESTE test aísle una sola variable: que el fallo del espejo v2
    (real, no fabricado) no revierta nada de lo que ya escribieron v1 y v2
    en `proceso`/`cliente_proceso`/`analisis` en la MISMA transacción.
    """
    import types

    from src.memoria import db as memoria_db
    from src.memoria import service
    from src.scraper.models import MatrizRiesgos, Proceso, ResultadoAnalisis

    cliente_a, _ = dos_clientes_reales
    numero_referencia = f"REF-SERVICE-CHUNK-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio X", descripcion="Test de vectorización vía service",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test",
        numero_referencia=numero_referencia,
    )
    resultado = ResultadoAnalisis(
        proceso_uuid=numero_referencia, estado="ANALIZADO", formato="NATIVO",
        matriz=MatrizRiesgos(items=[], resumen="resumen vía service"), modelo_analisis="test-model",
    )
    cfg = types.SimpleNamespace(
        enable_memoria=True, enable_esquema_v2=True,
        adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
        embedding_model_name="MULTILINGUAL_E5_SMALL", chunk_tokens=800, chunk_overlap=100,
    )
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso", lambda *a, **k: 2)

    monkeypatch_pool = memoria_db._pool
    memoria_db._pool = None
    try:
        # Sin `chunk_text` ni el modelo ONNX cargados, esta llamada REAL a
        # `vectorizar_proceso_v2` termina emitiendo un VECTOR_EMBEDDING(...)
        # que el motor local rechaza — el error genuino que motiva el
        # try/except propio del espejo v2.
        n = service.persistir_memoria(cfg, proceso, resultado, "texto de prueba " * 30, cliente_a)
    finally:
        memoria_db.close_pool()
        memoria_db._pool = monkeypatch_pool

    assert n == 2, "el conteo devuelto es el de v1 (monkeypatcheado), intacto"

    from src.memoria import repo
    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{numero_referencia}"
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM licitaciones_metadata WHERE proceso_id = :p", p=numero_referencia,
        )
        assert cursor.fetchone()[0] == 1, "v1 (licitaciones_metadata) debe seguir escrito"
        cursor.execute("SELECT COUNT(*) FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
        assert cursor.fetchone()[0] == 1, "v2 (proceso) debe seguir escrito pese al fallo de chunk"
        cursor.execute(
            "SELECT COUNT(*) FROM analisis WHERE cliente_id = :c AND proceso_gid = :g",
            c=cliente_a, g=proceso_gid,
        )
        assert cursor.fetchone()[0] == 1, "v2 (analisis) debe seguir escrito pese al fallo de chunk"
    finally:
        cursor.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :p", p=numero_referencia)
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
        conn.commit()
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 10) Espejo v2 desde src.feedback.repo / service — Fase "Doble escritura"
# ═══════════════════════════════════════════════════════════════════════════
# `reportada`/`feedback` v2 NO tienen FK (mismo diseño deliberado que v1: ver
# el comentario de `db/schema.sql` §5) — no hace falta sembrar portal, cliente
# ni proceso para probarlas.

@pytest.fixture
def clave_feedback_v2(conn):
    """Un `numero_referencia` propio del test; borra sus filas v2 al final."""
    from src.memoria.repo import PORTAL_ID_DEFAULT

    numero_referencia = f"FBV2-{_uuid.uuid4().hex[:12]}"
    proceso_gid = f"{PORTAL_ID_DEFAULT}:{numero_referencia}"
    yield numero_referencia, proceso_gid
    cursor = conn.cursor()
    cursor.execute("DELETE FROM feedback WHERE proceso_gid = :g", g=proceso_gid)
    cursor.execute("DELETE FROM reportada WHERE proceso_gid = :g", g=proceso_gid)
    conn.commit()
    cursor.close()


def test_upsert_reportada_v2_via_repo_real(conn, clave_feedback_v2):
    from src.feedback import repo as feedback_repo

    numero_referencia, proceso_gid = clave_feedback_v2
    opp = _opp_clasificada(numero_referencia)

    feedback_repo.upsert_reportada_v2(conn, "test_client", opp)
    feedback_repo.upsert_reportada_v2(conn, "test_client", opp)  # reenvío: no debe duplicar
    conn.commit()

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM reportada WHERE cliente_id = :c AND proceso_gid = :g",
        c="test_client", g=proceso_gid,
    )
    assert cursor.fetchone()[0] == 1
    cursor.close()


def test_upsert_voto_v2_via_repo_real_cambiar_de_opinion_sobrescribe(conn, clave_feedback_v2):
    from src.feedback import repo as feedback_repo

    numero_referencia, proceso_gid = clave_feedback_v2
    comunes = dict(proceso_id=numero_referencia, cliente_id="test_client", destinatario_id="ana")

    feedback_repo.upsert_voto_v2(conn, voto="IRRELEVANTE", motivos=["fuera_de_rubro"], comentario="no aplica", **comunes)
    conn.commit()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT voto, motivo, comentario FROM feedback WHERE cliente_id = :c AND proceso_gid = :g "
        "AND destinatario = :d", c="test_client", g=proceso_gid, d="ana",
    )
    assert cursor.fetchone() == ("IRRELEVANTE", "fuera_de_rubro", "no aplica")

    feedback_repo.upsert_voto_v2(conn, voto="RELEVANTE", motivos=[], comentario="", **comunes)
    conn.commit()
    cursor.execute(
        "SELECT COUNT(*), MAX(voto), MAX(motivo) FROM feedback WHERE cliente_id = :c "
        "AND proceso_gid = :g AND destinatario = :d", c="test_client", g=proceso_gid, d="ana",
    )
    filas, voto, motivo = cursor.fetchone()
    assert (filas, voto, motivo) == (1, "RELEVANTE", None), "cambiar de opinión debe sobrescribir, no acumular"
    cursor.close()


def test_feedback_v2_check_rechaza_un_voto_desconocido(conn, clave_feedback_v2):
    """El `CHECK ck_feedback_v2_voto` de la tabla, no la validación de Python
    (`upsert_voto_v2` ya la tiene, pero esto prueba la última línea de
    defensa: la base misma)."""
    _, proceso_gid = clave_feedback_v2
    cursor = conn.cursor()
    with pytest.raises(oracledb.IntegrityError):
        cursor.execute(
            "INSERT INTO feedback (cliente_id, proceso_gid, destinatario, voto) "
            "VALUES ('test_client', :g, 'ana', 'TAL_VEZ')", g=proceso_gid,
        )
    conn.rollback()
    cursor.close()


def test_service_registrar_reportadas_y_guardar_voto_v2_escriben_ambos_esquemas(conn, clave_feedback_v2):
    """De punta a punta a través de `src.feedback.service`, con
    `ENABLE_ESQUEMA_V2=true`: una corrida real deja fila en
    `licitaciones_reportadas`/`licitaciones_feedback` (v1) Y en
    `reportada`/`feedback` (v2).
    """
    import types

    from src.feedback import service as feedback_service
    from src.memoria import db as memoria_db

    numero_referencia, proceso_gid = clave_feedback_v2
    opp = _opp_clasificada(numero_referencia)
    cfg = types.SimpleNamespace(
        enable_feedback=True, feedback_base_url="https://f.example.com",
        feedback_hmac_secret="sec", enable_esquema_v2=True, data_dir="/tmp",
        adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )

    monkeypatch_pool = memoria_db._pool
    memoria_db._pool = None
    try:
        feedback_service.registrar_reportadas(cfg, "test_client", [opp])
        ok = feedback_service.guardar_voto(
            cfg, client_id="test_client", proceso_id=numero_referencia, dest_id="ana",
            email="ana@x.com", voto="IRRELEVANTE", motivos=["fuera_de_rubro"], comentario="no aplica",
        )
    finally:
        memoria_db.close_pool()
        memoria_db._pool = monkeypatch_pool

    assert ok is True
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM licitaciones_reportadas WHERE proceso_id = :p", p=numero_referencia,
        )
        assert cursor.fetchone()[0] == 1, "v1 (licitaciones_reportadas) debe seguir escrito"
        cursor.execute(
            "SELECT COUNT(*) FROM reportada WHERE cliente_id = 'test_client' AND proceso_gid = :g",
            g=proceso_gid,
        )
        assert cursor.fetchone()[0] == 1, "v2 (reportada) debe escribirse con ENABLE_ESQUEMA_V2=true"
        cursor.execute(
            "SELECT COUNT(*) FROM licitaciones_feedback WHERE proceso_id = :p", p=numero_referencia,
        )
        assert cursor.fetchone()[0] == 1, "v1 (licitaciones_feedback) debe seguir escrito"
        cursor.execute(
            "SELECT voto FROM feedback WHERE cliente_id = 'test_client' AND proceso_gid = :g "
            "AND destinatario = 'ana'", g=proceso_gid,
        )
        assert cursor.fetchone()[0] == "IRRELEVANTE"
    finally:
        cursor.execute("DELETE FROM licitaciones_reportadas WHERE proceso_id = :p", p=numero_referencia)
        cursor.execute("DELETE FROM licitaciones_feedback WHERE proceso_id = :p", p=numero_referencia)
        conn.commit()
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 11) Espejo v2 desde src.mercado.repo / service — Fase "Doble escritura"
# ═══════════════════════════════════════════════════════════════════════════
# `oportunidad_estado` no tiene FK (ver db/schema_v2.sql): no hace falta
# sembrar portal/cliente/proceso para probarla.

def test_prepoblar_oportunidad_estado_v2_via_repo_real(conn, codigo_adjudicacion, _limpiar_mercado):
    from src.memoria.repo import PORTAL_ID_DEFAULT
    from src.mercado import repo

    ganado = f"{codigo_adjudicacion}-GANADO"
    perdido = f"{codigo_adjudicacion}-PERDIDO"

    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=900201, codigo_proceso=ganado, monto=1000, id_proveedor=20371),
        es_multicomputos=True,
    )
    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=900202, codigo_proceso=perdido, monto=2000, id_proveedor=555),
        es_multicomputos=False,
    )
    for codigo in (ganado, perdido):
        repo.upsert_proceso_participado(conn, "test_client", {
            "codigo": codigo, "nombre": "x", "institucion": "y", "metodo": "z", "fecha": "2026-09-01",
        })
    conn.commit()
    repo.resolver_resultados(conn, "test_client")
    conn.commit()

    n = repo.prepoblar_oportunidad_estado_v2(conn, "test_client")
    conn.commit()
    assert n >= 2

    proceso_gid_ganado = f"{PORTAL_ID_DEFAULT}:{ganado}"
    proceso_gid_perdido = f"{PORTAL_ID_DEFAULT}:{perdido}"
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT estado, monto_adjudicado, origen FROM oportunidad_estado "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=proceso_gid_ganado,
        )
        estado, monto, origen = cursor.fetchone()
        assert estado == "GANADO"
        assert float(monto) == pytest.approx(1000)
        assert origen == "AUTOMATICO_MERCADO"

        cursor.execute(
            "SELECT estado, origen FROM oportunidad_estado "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=proceso_gid_perdido,
        )
        estado, origen = cursor.fetchone()
        assert estado == "PERDIDO"
        assert origen == "AUTOMATICO_MERCADO"
    finally:
        cursor.execute(
            "DELETE FROM oportunidad_evento WHERE cliente_id = 'test_client' "
            "AND proceso_gid IN (:g, :p)", g=proceso_gid_ganado, p=proceso_gid_perdido,
        )
        cursor.execute(
            "DELETE FROM oportunidad_estado WHERE cliente_id = 'test_client' "
            "AND proceso_gid IN (:g, :p)", g=proceso_gid_ganado, p=proceso_gid_perdido,
        )
        conn.commit()
        cursor.close()


def test_prepoblar_oportunidad_estado_v2_no_pisa_una_fila_manual(conn, codigo_adjudicacion, _limpiar_mercado):
    """Una fila ya capturada a mano desde el portal (`origen='MANUAL'`) no
    debe ser sobrescrita por el cruce automático de mercado, aunque el
    proceso ya se haya resuelto como GANADO/PERDIDO."""
    from src.memoria.repo import PORTAL_ID_DEFAULT
    from src.mercado import repo

    ganado = f"{codigo_adjudicacion}-GANADO-MANUAL"
    proceso_gid = f"{PORTAL_ID_DEFAULT}:{ganado}"

    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen, motivo) "
        "VALUES ('test_client', :g, 'DESCARTADO', 'MANUAL', 'ya lo evaluamos y no aplica')",
        g=proceso_gid,
    )
    conn.commit()

    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=900203, codigo_proceso=ganado, monto=1000, id_proveedor=20371),
        es_multicomputos=True,
    )
    repo.upsert_proceso_participado(conn, "test_client", {
        "codigo": ganado, "nombre": "x", "institucion": "y", "metodo": "z", "fecha": "2026-09-01",
    })
    conn.commit()
    repo.resolver_resultados(conn, "test_client")
    conn.commit()

    repo.prepoblar_oportunidad_estado_v2(conn, "test_client")
    conn.commit()

    try:
        cursor.execute(
            "SELECT estado, origen, motivo FROM oportunidad_estado "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=proceso_gid,
        )
        estado, origen, motivo = cursor.fetchone()
        assert (estado, origen) == ("DESCARTADO", "MANUAL"), (
            "el cruce automático no debe pisar una fila capturada a mano"
        )
        assert motivo == "ya lo evaluamos y no aplica"
    finally:
        cursor.execute(
            "DELETE FROM oportunidad_evento WHERE cliente_id = 'test_client' AND proceso_gid = :g",
            g=proceso_gid,
        )
        cursor.execute(
            "DELETE FROM oportunidad_estado WHERE cliente_id = 'test_client' AND proceso_gid = :g",
            g=proceso_gid,
        )
        conn.commit()
        cursor.close()


def _preparar_resuelto(conn, repo, codigo, id_):
    """Un proceso participado ya resuelto como GANADO en el registro de mercado."""
    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=id_, codigo_proceso=codigo, monto=1000, id_proveedor=20371),
        es_multicomputos=True,
    )
    repo.upsert_proceso_participado(conn, "test_client", {
        "codigo": codigo, "nombre": "x", "institucion": "y", "metodo": "z", "fecha": "2026-09-01",
    })
    conn.commit()
    repo.resolver_resultados(conn, "test_client")
    conn.commit()


def _limpiar_oportunidad(conn, gid):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM oportunidad_evento WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=gid)
    cursor.execute("DELETE FROM oportunidad_estado WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=gid)
    conn.commit()
    cursor.close()


def test_prepoblar_v2_el_resultado_de_mercado_avanza_una_fila_manual_intermedia(
    conn, codigo_adjudicacion, _limpiar_mercado
):
    """P8: la persona dejó el proceso en OFERTADO y COMPRASAL publica GANADO → avanza,
    conserva origen/motivo y deja su evento sin usuario."""
    from src.memoria.repo import PORTAL_ID_DEFAULT
    from src.mercado import repo

    codigo = f"{codigo_adjudicacion}-INTERMEDIO"
    gid = f"{PORTAL_ID_DEFAULT}:{codigo}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen, motivo) "
        "VALUES ('test_client', :g, 'OFERTADO', 'MANUAL', 'presentamos oferta')", g=gid,
    )
    conn.commit()
    _preparar_resuelto(conn, repo, codigo, 900211)
    try:
        repo.prepoblar_oportunidad_estado_v2(conn, "test_client")
        repo.prepoblar_oportunidad_estado_v2(conn, "test_client")        # idempotente: sin segundo evento
        conn.commit()

        cursor.execute(
            "SELECT estado, origen, motivo, monto_adjudicado FROM oportunidad_estado "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=gid,
        )
        estado, origen, motivo, monto = cursor.fetchone()
        assert (estado, origen, motivo) == ("GANADO", "MANUAL", "presentamos oferta")
        assert float(monto) == pytest.approx(1000)

        cursor.execute(
            "SELECT estado_anterior, estado_nuevo, usuario_id FROM oportunidad_evento "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=gid,
        )
        assert cursor.fetchall() == [("OFERTADO", "GANADO", None)]
        assert repo.listar_conflictos_oportunidad_v2(conn, "test_client") == []
    finally:
        _limpiar_oportunidad(conn, gid)
        cursor.close()


def test_prepoblar_v2_una_fila_manual_final_distinta_queda_como_conflicto(
    conn, codigo_adjudicacion, _limpiar_mercado
):
    """P8: la persona marcó PERDIDO y COMPRASAL dice GANADO → no se pisa y se lista el conflicto."""
    from src.memoria.repo import PORTAL_ID_DEFAULT
    from src.mercado import repo

    codigo = f"{codigo_adjudicacion}-CONFLICTO"
    gid = f"{PORTAL_ID_DEFAULT}:{codigo}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen) "
        "VALUES ('test_client', :g, 'PERDIDO', 'MANUAL')", g=gid,
    )
    conn.commit()
    _preparar_resuelto(conn, repo, codigo, 900212)
    try:
        repo.prepoblar_oportunidad_estado_v2(conn, "test_client")
        conn.commit()

        cursor.execute(
            "SELECT estado FROM oportunidad_estado WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=gid,
        )
        assert cursor.fetchone()[0] == "PERDIDO"
        conflictos = repo.listar_conflictos_oportunidad_v2(conn, "test_client")
        assert [(c["proceso_gid"], c["estado_manual"], c["resultado_mercado"]) for c in conflictos] == [
            (gid, "PERDIDO", "GANADO")
        ]
        cursor.execute(
            "SELECT COUNT(*) FROM oportunidad_evento WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=gid,
        )
        assert cursor.fetchone()[0] == 0              # no hubo transición, no hay evento
    finally:
        _limpiar_oportunidad(conn, gid)
        cursor.close()


def test_service_resolver_resultados_v2_activado_prepuebla_end_to_end(
    conn, codigo_adjudicacion, _limpiar_mercado
):
    """De punta a punta a través de `src.mercado.service.resolver_resultados`,
    con `ENABLE_ESQUEMA_V2=true`."""
    import types

    from src.memoria import db as memoria_db
    from src.memoria.repo import PORTAL_ID_DEFAULT
    from src.mercado import repo, service

    ganado = f"{codigo_adjudicacion}-SERVICE"
    proceso_gid = f"{PORTAL_ID_DEFAULT}:{ganado}"

    repo.upsert_adjudicacion_item(
        conn, _item_adjudicacion(id_=900204, codigo_proceso=ganado, monto=500, id_proveedor=20371),
        es_multicomputos=True,
    )
    repo.upsert_proceso_participado(conn, "test_client", {
        "codigo": ganado, "nombre": "x", "institucion": "y", "metodo": "z", "fecha": "2026-09-01",
    })
    conn.commit()

    cfg = types.SimpleNamespace(
        enable_esquema_v2=True, adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
    )
    monkeypatch_pool = memoria_db._pool
    memoria_db._pool = None
    try:
        resultado = service.resolver_resultados(cfg, "test_client")
    finally:
        memoria_db.close_pool()
        memoria_db._pool = monkeypatch_pool

    assert resultado["ganados"] >= 1

    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT estado, origen FROM oportunidad_estado "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=proceso_gid,
        )
        fila = cursor.fetchone()
        assert fila is not None, "el prepoblado v2 debió correr con ENABLE_ESQUEMA_V2=true"
        assert fila == ("GANADO", "AUTOMATICO_MERCADO")
    finally:
        cursor.execute(
            "DELETE FROM oportunidad_evento WHERE cliente_id = 'test_client' AND proceso_gid = :g",
            g=proceso_gid,
        )
        cursor.execute(
            "DELETE FROM oportunidad_estado WHERE cliente_id = 'test_client' AND proceso_gid = :g",
            g=proceso_gid,
        )
        conn.commit()
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 12) src.memoria.backfill_v2 — Fase "Backfill" (plan del portal §3.3 Fase 3)
# ═══════════════════════════════════════════════════════════════════════════
# Reutiliza las fixtures de metadata/análisis/chunks/feedback ya existentes
# más arriba en este archivo (`proceso_id`, `con_chunks`, `clave_feedback`):
# backfill_v2 lee TODO `licitaciones_metadata`/`licitaciones_analisis`/etc.
# sin filtrar por proceso, así que estos tests solo verifican SUS propias
# filas (identificadas por el proceso_id de la fixture), no un conteo total.
#
# `cliente_proceso`/`analisis` v2 tienen FK real hacia `cliente` (a
# diferencia de v1): en producción `scripts/semilla_esquema_v2` ya deja
# `test_client` (y cualquier cliente real) sembrado antes de que corra
# cualquier doble escritura o backfill. Estos tests lo siembran a mano
# porque no dependen de haber corrido esa semilla primero.

@pytest.fixture
def cliente_test_client_v2(conn):
    """Siembra `organizacion`/`cliente` para 'test_client' si no existen ya
    (p. ej. por `scripts/semilla_esquema_v2`) — NO los borra al final: son
    el mismo cliente de prueba que usan `proceso_id`/`con_chunks`/
    `clave_feedback`, con vida más larga que un solo test."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM organizacion WHERE org_id = 'test_org_v2_backfill'")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
            "VALUES ('test_org_v2_backfill', 'Org de prueba', 'SV', 'America/El_Salvador')"
        )
    cursor.execute("SELECT COUNT(*) FROM cliente WHERE cliente_id = 'test_client'")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO cliente (cliente_id, org_id, nombre) "
            "VALUES ('test_client', 'test_org_v2_backfill', 'Cliente de prueba')"
        )
    conn.commit()
    cursor.close()
    return "test_client"


def test_backfill_procesos_y_analisis_de_punta_a_punta(conn, proceso_id, cliente_test_client_v2):
    """Semilla real de licitaciones_metadata + licitaciones_analisis
    (vía repo.upsert_proceso/save_analisis_db, los mismos escritores v1 en
    producción), corre backfill_procesos + backfill_analisis, y verifica
    que proceso/cliente_proceso/analisis (v2) queden con los valores
    correctos — incluida la matriz de riesgos reconstruida desde el JSON.
    """
    from src.memoria import backfill_v2, repo
    from src.scraper.models import MatrizRiesgos, ResultadoAnalisis, RiesgoItem

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE licitaciones_metadata SET triaje = 'GO', triaje_conf = 0.85, "
        "formato = 'NATIVO' WHERE proceso_id = :p", p=proceso_id,
    )
    conn.commit()

    repo.save_analisis_db(conn, ResultadoAnalisis(
        proceso_uuid=proceso_id, estado="ANALIZADO",
        matriz=MatrizRiesgos(items=[RiesgoItem("legal", "penalidad", "alta", "6.1")],
                              resumen="resumen de backfill"),
        modelo_analisis="test-model",
    ))
    conn.commit()

    n_procesos = backfill_v2.backfill_procesos(conn)
    conn.commit()
    n_analisis = backfill_v2.backfill_analisis(conn)
    conn.commit()
    assert n_procesos >= 1
    assert n_analisis >= 1

    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{proceso_id}"
    try:
        cursor.execute(
            "SELECT institucion FROM proceso WHERE proceso_gid = :g", g=proceso_gid,
        )
        assert cursor.fetchone()[0] == "INSTITUCION DE PRUEBA"

        cursor.execute(
            "SELECT estado, triaje, triaje_conf, formato FROM cliente_proceso "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=proceso_gid,
        )
        estado, triaje, triaje_conf, formato = cursor.fetchone()
        assert estado == "DESCUBIERTO"  # el estado de licitaciones_metadata en la fixture proceso_id
        assert triaje == "GO"
        assert float(triaje_conf) == pytest.approx(0.85)
        assert formato == "NATIVO"

        cursor.execute(
            "SELECT resumen, severidad_max FROM analisis "
            "WHERE cliente_id = 'test_client' AND proceso_gid = :g", g=proceso_gid,
        )
        resumen, severidad_max = cursor.fetchone()
        assert resumen == "resumen de backfill"
        assert severidad_max == "alta"

        # Idempotencia: correrlo de nuevo no debe duplicar ni fallar.
        n2 = backfill_v2.backfill_procesos(conn)
        conn.commit()
        assert n2 >= 1
        cursor.execute("SELECT COUNT(*) FROM proceso WHERE proceso_gid = :g", g=proceso_gid)
        assert cursor.fetchone()[0] == 1
    finally:
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
        conn.commit()
        cursor.close()


def test_backfill_chunks_copia_el_vector_sin_recalcularlo(conn, proceso_id, con_chunks):
    """`con_chunks` deja chunks reales (con embedding real de 384 dims) en
    `licitaciones_chunks`. `backfill_chunks` debe copiarlos a `chunk` sin
    pasar de nuevo por `VECTOR_EMBEDDING` — la fila de `proceso` v2 tiene
    que existir primero (FK), así que se crea a mano acá."""
    from src.memoria import backfill_v2, repo

    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{proceso_id}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) "
        "VALUES (:g, :p, :n)", g=proceso_gid, p=repo.PORTAL_ID_DEFAULT, n=proceso_id,
    )
    conn.commit()

    try:
        n = backfill_v2.backfill_chunks(conn)
        conn.commit()
        assert n >= 1

        cursor.execute(
            "SELECT COUNT(*), MAX(VECTOR_DIMENSION_COUNT(embedding)) FROM chunk "
            "WHERE proceso_gid = :g", g=proceso_gid,
        )
        filas, dims = cursor.fetchone()
        assert filas >= 1
        assert dims == 384, "el vector copiado debe conservar sus 384 dimensiones"

        # Idempotencia: correrlo de nuevo no debe duplicar (DELETE previo por proceso_gid).
        backfill_v2.backfill_chunks(conn)
        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM chunk WHERE proceso_gid = :g", g=proceso_gid)
        assert cursor.fetchone()[0] == filas
    finally:
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=proceso_gid)  # cascada
        conn.commit()
        cursor.close()


def test_backfill_reportadas_y_feedback_de_punta_a_punta(conn, clave_feedback):
    from src.feedback import repo as feedback_repo
    from src.memoria import backfill_v2, repo

    feedback_repo.upsert_reportada(conn, "test_client", _opp_clasificada(clave_feedback))
    feedback_repo.upsert_voto(
        conn, proceso_id=clave_feedback, cliente_id="test_client", destinatario_id="ana",
        destinatario_email="ana@x.com", voto="IRRELEVANTE", motivos=["fuera_de_rubro"],
        comentario="no aplica",
    )
    conn.commit()

    n_reportadas = backfill_v2.backfill_reportadas(conn)
    conn.commit()
    n_feedback = backfill_v2.backfill_feedback(conn)
    conn.commit()
    assert n_reportadas >= 1
    assert n_feedback >= 1

    proceso_gid = f"{repo.PORTAL_ID_DEFAULT}:{clave_feedback}"
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM reportada WHERE cliente_id = 'test_client' AND proceso_gid = :g",
            g=proceso_gid,
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT voto, motivo FROM feedback WHERE cliente_id = 'test_client' "
            "AND proceso_gid = :g AND destinatario = 'ana'", g=proceso_gid,
        )
        assert cursor.fetchone() == ("IRRELEVANTE", "fuera_de_rubro")
    finally:
        cursor.execute("DELETE FROM feedback WHERE proceso_gid = :g", g=proceso_gid)
        cursor.execute("DELETE FROM reportada WHERE proceso_gid = :g", g=proceso_gid)
        conn.commit()
        cursor.close()


def test_verificar_conteos_contra_el_motor_real(conn):
    from src.memoria import backfill_v2

    resultado = backfill_v2.verificar_conteos(conn)

    assert set(resultado) == {"proceso", "cliente_proceso", "analisis", "chunk", "reportada", "feedback"}
    for n1, n2 in resultado.values():
        assert isinstance(n1, int) and n1 >= 0
        assert isinstance(n2, int) and n2 >= 0


def test_version_admite_el_sha_completo_del_commit(conn):
    """`app_version` en OCI es un SHA de 40 caracteres: con VARCHAR2(32) fallaba con ORA-12899."""
    sha = "f8e78dc501e69607a0e7d499bb93940c52f39be2"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ejecucion (run_timestamp, success, version) VALUES (SYSTIMESTAMP, 1, :v)",
        v=sha,
    )
    cursor.execute(
        "INSERT INTO llm_llamada (ts, ok, version) VALUES (SYSTIMESTAMP, 1, :v)", v=sha
    )
    for tabla in ("ejecucion", "llm_llamada"):
        cursor.execute(f"SELECT version FROM {tabla} WHERE version = :v", v=sha)  # noqa: S608
        assert cursor.fetchone() == (sha,)
    conn.rollback()
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# 12) Identidad del portal: sesión deslizante (P5) y auditoría (P6)
# ═══════════════════════════════════════════════════════════════════════════

def test_identidad_sesion_deslizante_purga_y_auditoria_contra_oracle(conn):
    from src.identidad import repo as identidad

    org, usuario = "test_org_identidad", "test_usuario_identidad"
    cursor = conn.cursor()
    cursor.execute("DELETE FROM auditoria WHERE org_id = :o", o=org)
    cursor.execute("DELETE FROM sesion WHERE usuario_id = :u", u=usuario)
    cursor.execute("DELETE FROM usuario WHERE usuario_id = :u", u=usuario)
    cursor.execute("DELETE FROM organizacion WHERE org_id = :o", o=org)
    cursor.execute(
        "INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
        "VALUES (:o, 'Org identidad', 'SV', 'America/El_Salvador')", o=org,
    )
    cursor.execute("INSERT INTO usuario (usuario_id, org_id, email) VALUES (:u, :o, 'x@test.invalid')", u=usuario, o=org)
    # viva (login hace 1 h), vencida hace 40 días, vencida hace 5 días, revocada
    cursor.execute(
        "INSERT INTO sesion (sesion_id, usuario_id, creado_en, expira_en) VALUES "
        "('t_viva', :u, SYSTIMESTAMP - INTERVAL '1' HOUR, SYSTIMESTAMP + INTERVAL '1' HOUR)", u=usuario)
    cursor.execute(
        "INSERT INTO sesion (sesion_id, usuario_id, creado_en, expira_en) VALUES "
        "('t_vieja', :u, SYSTIMESTAMP - INTERVAL '41' DAY, SYSTIMESTAMP - INTERVAL '40' DAY)", u=usuario)
    cursor.execute(
        "INSERT INTO sesion (sesion_id, usuario_id, creado_en, expira_en) VALUES "
        "('t_reciente', :u, SYSTIMESTAMP - INTERVAL '6' DAY, SYSTIMESTAMP - INTERVAL '5' DAY)", u=usuario)
    cursor.execute(
        "INSERT INTO sesion (sesion_id, usuario_id, creado_en, expira_en, revocada) VALUES "
        "('t_revocada', :u, SYSTIMESTAMP - INTERVAL '1' HOUR, SYSTIMESTAMP + INTERVAL '1' HOUR, 1)", u=usuario)
    conn.commit()
    try:
        # Renovar: la viva se desliza a ~12 h; la vencida y la revocada no se resucitan.
        assert identidad.renovar_sesion(conn, "t_viva", 12, 7) is True
        assert identidad.renovar_sesion(conn, "t_reciente", 12, 7) is False
        assert identidad.renovar_sesion(conn, "t_revocada", 12, 7) is False
        cursor.execute(
            "SELECT (CAST(expira_en AS DATE) - SYSDATE) * 24 FROM sesion WHERE sesion_id = 't_viva'")
        assert 10 < cursor.fetchone()[0] <= 12.1

        # Tope: una sesión con login hace 6,9 días no puede pasar de los 7 días desde el login.
        cursor.execute(
            "UPDATE sesion SET creado_en = SYSTIMESTAMP - INTERVAL '6' DAY - INTERVAL '22' HOUR WHERE sesion_id = 't_viva'")
        assert identidad.renovar_sesion(conn, "t_viva", 12, 7) is True
        cursor.execute(
            "SELECT (CAST(expira_en AS DATE) - SYSDATE) * 24 FROM sesion WHERE sesion_id = 't_viva'")
        assert 1 < cursor.fetchone()[0] <= 2.1        # 7 días - 6 d 22 h = 2 h

        # Purga: solo la vencida hace 40 días.
        assert identidad.purgar_sesiones_vencidas(conn, 30, dry_run=True) >= 1
        borradas = identidad.purgar_sesiones_vencidas(conn, 30)
        assert borradas >= 1
        cursor.execute("SELECT sesion_id FROM sesion WHERE usuario_id = :u ORDER BY sesion_id", u=usuario)
        assert [r[0] for r in cursor.fetchall()] == ["t_reciente", "t_revocada", "t_viva"]

        # Auditoría: con y sin alcance/usuario.
        identidad.registrar_auditoria(conn, org, "LOGIN_OK", usuario_id=usuario, recurso="/portal",
                                      alcance={"clientes": ["multicomputos"]}, ip_hash="abc")
        identidad.registrar_auditoria(conn, org, "LOGIN_FALLIDO")
        cursor.execute("SELECT accion, usuario_id FROM auditoria WHERE org_id = :o ORDER BY auditoria_id", o=org)
        assert cursor.fetchall() == [("LOGIN_OK", usuario), ("LOGIN_FALLIDO", None)]
    finally:
        conn.rollback()
        cursor.execute("DELETE FROM auditoria WHERE org_id = :o", o=org)
        cursor.execute("DELETE FROM sesion WHERE usuario_id = :u", u=usuario)
        cursor.execute("DELETE FROM usuario WHERE usuario_id = :u", u=usuario)
        cursor.execute("DELETE FROM organizacion WHERE org_id = :o", o=org)
        conn.commit()
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# Identidad — Entrega 1 (DDL): roles en palabras completas, membresia_org,
# codigo_acceso con PK sustituta, purga
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def usuarios_e1(conn, portal_y_org):
    cursor = conn.cursor()
    ids = ("test_e1_a", "test_e1_b")
    for u in ids:
        cursor.execute(
            "INSERT INTO usuario (usuario_id, org_id, email) VALUES (:u, :o, :e)",
            u=u, o=portal_y_org["org_id"], e=f"{u}@test.invalid",
        )
    conn.commit()
    yield ids
    for tabla in ("codigo_acceso", "sesion", "membresia_org"):
        cursor.execute(f"DELETE FROM {tabla} WHERE usuario_id IN (:a, :b)", a=ids[0], b=ids[1])
    cursor.execute("DELETE FROM usuario WHERE usuario_id IN (:a, :b)", a=ids[0], b=ids[1])
    conn.commit()
    cursor.close()


def test_membresia_acepta_rol_completo_y_rechaza_el_inventado_y_el_codigo_corto(
    conn, dos_clientes_v2, usuarios_e1
):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO membresia (usuario_id, cliente_id, rol) VALUES (:u, :c, 'gerente_regional_comercial_oracle')",
        u=usuarios_e1[0], c=dos_clientes_v2[0],
    )
    for malo in ("inventado", "GP"):
        with pytest.raises(oracledb.DatabaseError) as exc:
            cursor.execute(
                "INSERT INTO membresia (usuario_id, cliente_id, rol) VALUES (:u, :c, :r)",
                u=usuarios_e1[0], c=dos_clientes_v2[1], r=malo,
            )
        assert "ORA-02290" in str(exc.value)
    conn.rollback()
    cursor.execute("DELETE FROM membresia WHERE usuario_id = :u", u=usuarios_e1[0])
    conn.commit()
    cursor.close()


def test_membresia_org_solo_roles_de_organizacion(conn, portal_y_org, usuarios_e1):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO membresia_org (usuario_id, org_id, rol) VALUES (:u, :o, 'ceo')",
        u=usuarios_e1[0], o=portal_y_org["org_id"],
    )
    with pytest.raises(oracledb.DatabaseError) as exc:
        cursor.execute(
            "INSERT INTO membresia_org (usuario_id, org_id, rol) VALUES (:u, :o, 'gerente_pais')",
            u=usuarios_e1[1], o=portal_y_org["org_id"],
        )
    assert "ORA-02290" in str(exc.value)
    conn.rollback()
    cursor.close()


def test_codigo_acceso_dos_usuarios_con_el_mismo_hash_no_chocan(conn, usuarios_e1):
    cursor = conn.cursor()
    for u in usuarios_e1:
        cursor.execute(
            "INSERT INTO codigo_acceso (codigo_hash, usuario_id, expira_en) "
            "VALUES ('h' || '0', :u, SYSTIMESTAMP + INTERVAL '10' MINUTE)", u=u,
        )
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM codigo_acceso WHERE codigo_hash = 'h0'")
    assert cursor.fetchone()[0] == 2
    cursor.close()


def test_schema_v2_rehace_los_objetos_de_identidad_de_una_adb_antigua(conn, usuarios_e1):
    """Simula la ADB real (PK sobre `codigo_hash`, rol VARCHAR2(4) con códigos
    cortos), reaplica el DDL y comprueba la migración — y que se niega si hay filas."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM codigo_acceso WHERE usuario_id IN (:a, :b)", a=usuarios_e1[0], b=usuarios_e1[1])
    cursor.execute("ALTER TABLE membresia DROP CONSTRAINT ck_membresia_rol")
    cursor.execute("ALTER TABLE membresia MODIFY (rol VARCHAR2(4))")
    cursor.execute(
        "ALTER TABLE membresia ADD CONSTRAINT ck_membresia_rol CHECK (rol IN ('CEO','GP','EV'))"
    )
    cursor.execute("DROP TABLE codigo_acceso PURGE")
    cursor.execute(
        "CREATE TABLE codigo_acceso (codigo_hash VARCHAR2(64) CONSTRAINT pk_codigo_acceso PRIMARY KEY, "
        "usuario_id VARCHAR2(64) NOT NULL, creado_en TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL, "
        "expira_en TIMESTAMP NOT NULL, usado_en TIMESTAMP, intentos NUMBER(2) DEFAULT 0 NOT NULL, "
        "CONSTRAINT fk_codigo_acceso_usuario FOREIGN KEY (usuario_id) REFERENCES usuario(usuario_id))"
    )
    conn.commit()

    # Con una fila, el DDL se niega y deja la tabla intacta.
    cursor.execute(
        "INSERT INTO codigo_acceso (codigo_hash, usuario_id, expira_en) VALUES ('x', :u, SYSTIMESTAMP)",
        u=usuarios_e1[0],
    )
    conn.commit()
    with pytest.raises(oracledb.DatabaseError) as exc:
        for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
            cursor.execute(sentencia)
    assert "ORA-20002" in str(exc.value)
    cursor.execute("DELETE FROM codigo_acceso")
    conn.commit()

    for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
        cursor.execute(sentencia)
    conn.commit()

    cursor.execute(
        "SELECT COUNT(*) FROM user_tab_columns WHERE table_name = 'CODIGO_ACCESO' AND column_name = 'CODIGO_ID'"
    )
    assert cursor.fetchone()[0] == 1
    cursor.execute(
        "SELECT data_length FROM user_tab_columns WHERE table_name = 'MEMBRESIA' AND column_name = 'ROL'"
    )
    assert cursor.fetchone()[0] == 40
    cursor.execute("SELECT search_condition_vc FROM user_constraints WHERE constraint_name = 'CK_MEMBRESIA_ROL'")
    assert "gerente_pais" in cursor.fetchone()[0]
    cursor.close()


def test_retention_v2_purga_sesiones_y_codigos_vencidos_hace_mas_de_30_dias(conn, usuarios_e1):
    cursor = conn.cursor()
    u = usuarios_e1[0]
    for sid, delta in (("t_e1_vieja", "40"), ("t_e1_reciente", "5")):
        cursor.execute(
            "INSERT INTO sesion (sesion_id, usuario_id, expira_en) VALUES "
            f"(:s, :u, SYSTIMESTAMP - INTERVAL '{delta}' DAY)", s=sid, u=u,
        )
    for h, delta in (("t_e1_cv", "40"), ("t_e1_cr", "5")):
        cursor.execute(
            "INSERT INTO codigo_acceso (codigo_hash, usuario_id, expira_en) VALUES "
            f"(:h, :u, SYSTIMESTAMP - INTERVAL '{delta}' DAY)", h=h, u=u,
        )
    conn.commit()
    for sentencia in _sentencias_de(_RAIZ / "db" / "retention_v2.sql"):
        cursor.execute(sentencia)
    conn.commit()
    cursor.execute("SELECT sesion_id FROM sesion WHERE usuario_id = :u", u=u)
    assert {f[0] for f in cursor.fetchall()} == {"t_e1_reciente"}
    cursor.execute("SELECT codigo_hash FROM codigo_acceso WHERE usuario_id = :u", u=u)
    assert {f[0] for f in cursor.fetchall()} == {"t_e1_cr"}
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# Identidad — Entrega 2: alcance por rol y país, y login de punta a punta
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def limpiar_identidad(conn, portal_y_org):
    yield
    cursor = conn.cursor()
    org = portal_y_org["org_id"]
    cursor.execute("DELETE FROM auditoria WHERE org_id = :o", o=org)
    for tabla in ("codigo_acceso", "sesion", "membresia", "membresia_org", "recuperacion_mfa"):
        cursor.execute(f"DELETE FROM {tabla} WHERE usuario_id IN (SELECT usuario_id FROM usuario WHERE org_id = :o)", o=org)
    cursor.execute("DELETE FROM usuario WHERE org_id = :o", o=org)
    conn.commit()
    cursor.close()


def test_resolver_alcance_gerente_pais_solo_su_pais_y_ceo_todos(
    conn, portal_y_org, dos_clientes_v2, limpiar_identidad
):
    from src.identidad import alcance, service

    org = portal_y_org["org_id"]
    gp = service.alta_usuario(conn, org, "gp@test.invalid", "gerente_pais", dos_clientes_v2[0])
    ceo = service.alta_usuario(conn, org, "ceo@test.invalid", "ceo")
    tecnico = service.alta_usuario(conn, org, "op@test.invalid", "operador")
    conn.commit()

    a = alcance.resolver_alcance(conn, gp)
    assert a.clientes == {dos_clientes_v2[0]} and a.puede_cambiar_estado(dos_clientes_v2[0])
    assert not a.puede_ver(dos_clientes_v2[1])

    a = alcance.resolver_alcance(conn, ceo)
    assert a.clientes == set(dos_clientes_v2) and not a.cambia_estado

    # Un país dado de alta DESPUÉS le aparece al ceo sin tocar su membresía.
    cursor = conn.cursor()
    cursor.execute("INSERT INTO cliente (cliente_id, org_id, nombre) VALUES ('test_cli_c', :o, 'c')", o=org)
    conn.commit()
    try:
        assert "test_cli_c" in alcance.resolver_alcance(conn, ceo).clientes
        cursor.execute("UPDATE cliente SET activo = 0 WHERE cliente_id = 'test_cli_c'")
        conn.commit()
        assert "test_cli_c" not in alcance.resolver_alcance(conn, ceo).clientes
    finally:
        cursor.execute("DELETE FROM cliente WHERE cliente_id = 'test_cli_c'")
        conn.commit()
        cursor.close()

    t = alcance.resolver_alcance(conn, tecnico)
    assert t.ve_operativo and not t.clientes


def test_login_de_punta_a_punta_contra_oracle(conn, portal_y_org, limpiar_identidad):
    import types

    from src.identidad import repo, service

    org = portal_y_org["org_id"]
    cfg = types.SimpleNamespace(identidad_secret="secreto-de-prueba", codigo_ttl_min=10, sesion_ttl_horas=12)
    uid = service.alta_usuario(conn, org, "login@test.invalid", "ceo")
    conn.commit()

    _, codigo = service.emitir_codigo(conn, cfg, "login@test.invalid")
    conn.commit()
    assert service.verificar_codigo(conn, cfg, "login@test.invalid", "AAAAAAAA") is None
    token = service.verificar_codigo(conn, cfg, "login@test.invalid", codigo)
    conn.commit()
    assert token and service.usuario_de_sesion(conn, token) == uid
    assert service.verificar_codigo(conn, cfg, "login@test.invalid", codigo) is None  # un solo uso

    assert service.cerrar_sesion(conn, token) is True
    conn.commit()
    assert service.usuario_de_sesion(conn, token) is None

    cursor = conn.cursor()
    cursor.execute("SELECT accion FROM auditoria WHERE org_id = :o ORDER BY auditoria_id", o=org)
    acciones = [f[0] for f in cursor.fetchall()]
    assert {"USUARIO_ALTA", "MEMBRESIA_ALTA", "LOGIN_FALLIDO", "LOGIN_OK", "SESION_REVOCADA"} <= set(acciones)

    # Baja: no puede pedir más códigos.
    assert service.baja_usuario(conn, org, "login@test.invalid") is True
    conn.commit()
    assert service.emitir_codigo(conn, cfg, "login@test.invalid") is None
    assert repo.obtener_usuario(conn, uid)["activo"] is False
    cursor.close()


def test_el_mismo_numero_de_referencia_en_dos_portales_son_dos_procesos(conn, dos_clientes_reales):
    """Costa Rica: un cliente de `sicop_cr` y uno de `comprasal_sv` pueden ver la misma referencia
    sin pisarse; cada `proceso_gid` lleva su portal."""
    from src.memoria import repo
    from src.scraper.models import Proceso

    cliente_sv, cliente_cr = dos_clientes_reales
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO portal (portal_id, nombre, pais, zona_horaria, moneda_default, base_url) "
        "VALUES ('test_sicop_cr', 'SICOP de prueba', 'CR', 'America/Costa_Rica', 'CRC', 'https://example.test')"
    )
    conn.commit()
    numero = f"REF-CR-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio de Prueba", descripcion="Adquisición de prueba",
        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.test",
        numero_referencia=numero,
    )
    gid_sv, gid_cr = f"{repo.PORTAL_ID_DEFAULT}:{numero}", f"test_sicop_cr:{numero}"
    try:
        repo.upsert_proceso_v2(conn, proceso, cliente_sv, estado="ANALIZADO")
        repo.upsert_proceso_v2(conn, proceso, cliente_cr, estado="DESCUBIERTO", portal_id="test_sicop_cr")
        conn.commit()

        cursor.execute("SELECT proceso_gid, portal_id FROM proceso WHERE proceso_gid IN (:a, :b)", a=gid_sv, b=gid_cr)
        assert dict(cursor.fetchall()) == {gid_sv: repo.PORTAL_ID_DEFAULT, gid_cr: "test_sicop_cr"}
        cursor.execute("SELECT cliente_id, proceso_gid FROM cliente_proceso WHERE proceso_gid IN (:a, :b)",
                       a=gid_sv, b=gid_cr)
        assert dict(cursor.fetchall()) == {cliente_sv: gid_sv, cliente_cr: gid_cr}
    finally:
        cursor.execute("DELETE FROM proceso WHERE proceso_gid IN (:a, :b)", a=gid_sv, b=gid_cr)
        cursor.execute("DELETE FROM portal WHERE portal_id = 'test_sicop_cr'")
        conn.commit()
        cursor.close()


def test_schema_v2_renombra_el_rol_regional_de_la_version_intermedia(conn, usuarios_e1, portal_y_org):
    """La ADB real quedó con `director_regional_ventas_oracle` en los dos CHECK: el DDL
    los lleva a `gerente_regional_comercial_oracle`, y se niega si hay filas con el nombre viejo."""
    cursor = conn.cursor()
    viejo = "director_regional_ventas_oracle"
    lista = ("'ceo','director_estrategia','director_comercial','gerente_marca_oracle',"
             f"'{viejo}','director_servicios_oracle','admin_plataforma','admin_organizacion','operador'")
    cursor.execute("ALTER TABLE membresia_org DROP CONSTRAINT ck_membresia_org_rol")
    cursor.execute(f"ALTER TABLE membresia_org ADD CONSTRAINT ck_membresia_org_rol CHECK (rol IN ({lista}))")
    cursor.execute("ALTER TABLE membresia DROP CONSTRAINT ck_membresia_rol")
    cursor.execute(
        f"ALTER TABLE membresia ADD CONSTRAINT ck_membresia_rol CHECK (rol IN ({lista},'gerente_pais','equipo_ventas'))"
    )
    conn.commit()

    cursor.execute(
        "INSERT INTO membresia_org (usuario_id, org_id, rol) VALUES (:u, :o, :r)",
        u=usuarios_e1[0], o=portal_y_org["org_id"], r=viejo,
    )
    conn.commit()
    with pytest.raises(oracledb.DatabaseError) as exc:
        for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
            cursor.execute(sentencia)
    assert "ORA-20003" in str(exc.value)
    cursor.execute("DELETE FROM membresia_org WHERE usuario_id = :u", u=usuarios_e1[0])
    conn.commit()

    for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
        cursor.execute(sentencia)
    conn.commit()
    for tabla in ("MEMBRESIA", "MEMBRESIA_ORG"):
        cursor.execute("SELECT data_length FROM user_tab_columns WHERE table_name = :t AND column_name = 'ROL'", t=tabla)
        assert cursor.fetchone()[0] == 40, tabla
    for constraint in ("CK_MEMBRESIA_ROL", "CK_MEMBRESIA_ORG_ROL"):
        cursor.execute("SELECT search_condition_vc FROM user_constraints WHERE constraint_name = :c", c=constraint)
        condicion = cursor.fetchone()[0]
        assert "gerente_regional_comercial_oracle" in condicion and viejo not in condicion
    cursor.close()


def test_login_web_de_punta_a_punta_contra_oracle(conn, portal_y_org, limpiar_identidad, monkeypatch):
    """Entrega 3: alta por el servicio, código por correo (capturado), canje, sesión por cookie y salida,
    con el repositorio REAL sobre Oracle y la app FastAPI real."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.identidad import service
    from src.identidad import sesiones as sesiones_mod
    from src.web import acceso, csrf, sesion
    from src.web.app import crear_app

    base, secreto = "https://dev.amalia.test", "secreto-de-prueba"
    cfg = SimpleNamespace(
        enable_portal=True, identidad_secret=secreto, feedback_base_url=base, portal_cliente_id="cli",
        enable_feedback=False, daily_budget_usd=5.0, codigo_ttl_min=10, sesion_ttl_horas=12, sesion_max_dias=7,
    )

    @contextmanager
    def conexion(_cfg):
        yield conn
        conn.commit()

    monkeypatch.setattr(acceso.db, "connection", conexion)
    monkeypatch.setattr(sesion.db, "connection", conexion)
    enviados = []
    monkeypatch.setattr(acceso.email_sender, "send_access_code",
                        lambda c, email, codigo, ttl: enviados.append((email, codigo)))

    org = portal_y_org["org_id"]
    service.alta_usuario(conn, org, "web@test.invalid", "ceo")
    conn.commit()

    c = TestClient(crear_app(cfg), base_url=base, follow_redirects=False)
    origen = {"origin": base}

    r = c.post("/acceso", data={"email": "web@test.invalid", "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    sin_cuenta = c.post("/acceso", data={"email": "nadie@test.invalid", "csrf": csrf.emitir(secreto, "anon")},
                        headers=origen)
    assert r.status_code == sin_cuenta.status_code == 200
    assert [e for e, _ in enviados] == ["web@test.invalid"]
    codigo = enviados[0][1]

    mal = c.post("/acceso/codigo", data={"email": "web@test.invalid", "codigo": "AAAAAAAA",
                                         "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    assert mal.status_code == 401
    ok = c.post("/acceso/codigo", data={"email": "web@test.invalid", "codigo": codigo,
                                        "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    assert ok.status_code == 303 and ok.headers["location"] == "/portal"
    token = ok.cookies.get(sesion.COOKIE) or ok.headers["set-cookie"].split(";")[0].split("=", 1)[1]

    c.cookies.clear()  # el cliente guardó la cookie; las pruebas siguientes la pasan a mano
    # La cookie abre una sesión real: /acceso ya redirige y la base solo guarda el hash.
    assert c.get("/acceso", cookies={sesion.COOKIE: token}).status_code == 303
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sesion WHERE sesion_id = :s", s=sesiones_mod.hash_token(token))
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT COUNT(*) FROM sesion WHERE sesion_id = :s", s=token)
    assert cursor.fetchone()[0] == 0

    # El código no se reutiliza.
    de_nuevo = c.post("/acceso/codigo", data={"email": "web@test.invalid", "codigo": codigo,
                                              "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    assert de_nuevo.status_code == 401

    salir = c.post("/salir", data={"csrf": csrf.emitir(secreto, sesiones_mod.hash_token(token))},
                   headers=origen, cookies={sesion.COOKIE: token})
    assert salir.status_code == 303 and salir.headers["location"] == "/acceso"
    assert c.get("/acceso", cookies={sesion.COOKIE: token}).status_code == 200  # ya sin sesión
    cursor.close()


def test_schema_v2_agrega_las_columnas_mfa_a_una_adb_anterior(conn, usuarios_e1):
    """La ADB real quedó sin las columnas del TOTP: el DDL las agrega, ensancha el secreto y es idempotente."""
    cursor = conn.cursor()
    cursor.execute("DROP TABLE recuperacion_mfa PURGE")
    cursor.execute("ALTER TABLE usuario DROP COLUMN mfa_confirmado_en")
    cursor.execute("ALTER TABLE usuario DROP COLUMN mfa_ultimo_paso")
    cursor.execute("ALTER TABLE usuario MODIFY (mfa_totp_secret VARCHAR2(64))")
    cursor.execute("ALTER TABLE sesion DROP COLUMN mfa_ok CASCADE CONSTRAINTS")
    conn.commit()

    for _ in range(2):  # y de nuevo: idempotente
        for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
            cursor.execute(sentencia)
        conn.commit()

    cursor.execute("SELECT column_name FROM user_tab_columns WHERE table_name = 'USUARIO' "
                   "AND column_name IN ('MFA_CONFIRMADO_EN','MFA_ULTIMO_PASO')")
    assert len(cursor.fetchall()) == 2
    cursor.execute("SELECT data_length FROM user_tab_columns WHERE table_name = 'USUARIO' "
                   "AND column_name = 'MFA_TOTP_SECRET'")
    assert cursor.fetchone()[0] == 200
    cursor.execute("SELECT data_default FROM user_tab_columns WHERE table_name = 'SESION' AND column_name = 'MFA_OK'")
    assert str(cursor.fetchone()[0]).strip() == "1"
    cursor.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'RECUPERACION_MFA'")
    assert cursor.fetchone()[0] == 1
    cursor.close()


def test_mfa_web_de_punta_a_punta_contra_oracle(conn, portal_y_org, limpiar_identidad, monkeypatch):
    """admin_plataforma: correo → sesión pendiente (sin acceso) → enrolamiento del TOTP → entra → segundo
    ingreso con TOTP nuevo y con código de recuperación; el mismo TOTP no sirve dos veces."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    import pyotp
    from fastapi.testclient import TestClient

    from src.identidad import mfa, service
    from src.identidad import sesiones as sesiones_mod
    from src.web import acceso, csrf, sesion
    from src.web.app import crear_app

    base, secreto = "https://dev.amalia.test", "secreto-de-prueba"
    cfg = SimpleNamespace(
        enable_portal=True, identidad_secret=secreto, feedback_base_url=base, portal_cliente_id="cli",
        enable_feedback=False, daily_budget_usd=5.0, codigo_ttl_min=10, sesion_ttl_horas=12, sesion_max_dias=7,
    )

    @contextmanager
    def conexion(_cfg):
        yield conn
        conn.commit()

    monkeypatch.setattr(acceso.db, "connection", conexion)
    monkeypatch.setattr(sesion.db, "connection", conexion)
    enviados = []
    monkeypatch.setattr(acceso.email_sender, "send_access_code",
                        lambda c, email, codigo, ttl: enviados.append((email, codigo)))
    org = portal_y_org["org_id"]
    uid = service.alta_usuario(conn, org, "admin@test.invalid", "admin_plataforma")
    service.alta_usuario(conn, org, "ceo2@test.invalid", "ceo")
    conn.commit()
    origen = {"origin": base}

    def entrar_con_correo(c, email):
        c.post("/acceso", data={"email": email, "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
        codigo = enviados[-1][1]
        r = c.post("/acceso/codigo", data={"email": email, "codigo": codigo, "csrf": csrf.emitir(secreto, "anon")},
                   headers=origen)
        token = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        c.cookies.clear()
        return r, token

    c = TestClient(crear_app(cfg), base_url=base, follow_redirects=False)

    # Quien no tiene rol con MFA entra directo (control).
    r, _ = entrar_con_correo(c, "ceo2@test.invalid")
    assert r.headers["location"] == "/portal"

    # admin_plataforma: va al TOTP y la sesión NO da acceso todavía.
    r, token = entrar_con_correo(c, "admin@test.invalid")
    assert r.headers["location"].startswith("/acceso/mfa")
    cookie = {sesion.COOKIE: token}
    assert service.usuario_de_sesion(conn, token) is None and service.mfa_pendiente(conn, token)
    pagina = c.get("/acceso/mfa", cookies=cookie)
    assert pagina.status_code == 200 and "otpauth://totp/" in pagina.text

    cursor = conn.cursor()
    cursor.execute("SELECT mfa_totp_secret FROM usuario WHERE usuario_id = :u", u=uid)
    cifrado = cursor.fetchone()[0]
    claro = mfa.descifrar(secreto, cifrado)
    assert claro and claro not in cifrado  # en reposo solo vive cifrado

    csrf_mfa = csrf.emitir(secreto, sesiones_mod.hash_token(token))
    malo = c.post("/acceso/mfa", data={"codigo": "000000", "csrf": csrf_mfa}, headers=origen, cookies=cookie)
    assert malo.status_code == 401
    ok = c.post("/acceso/mfa", data={"codigo": pyotp.TOTP(claro).now(), "csrf": csrf_mfa}, headers=origen,
                cookies=cookie)
    assert ok.status_code == 200 and "recuperación" in ok.text
    codigos_rec = [ln.strip()[4:-5] for ln in ok.text.splitlines() if ln.strip().startswith("<li>")]
    assert len(codigos_rec) == 8
    assert service.usuario_de_sesion(conn, token) == uid and not service.mfa_pendiente(conn, token)

    # Segundo ingreso: el MISMO TOTP (mismo paso) no sirve; uno de otro paso sí; la recuperación, una vez.
    r, t2 = entrar_con_correo(c, "admin@test.invalid")
    cookie2 = {sesion.COOKIE: t2}
    csrf2 = csrf.emitir(secreto, sesiones_mod.hash_token(t2))
    repetido = c.post("/acceso/mfa", data={"codigo": pyotp.TOTP(claro).now(), "csrf": csrf2}, headers=origen,
                      cookies=cookie2)
    assert repetido.status_code == 401
    siguiente_paso = pyotp.TOTP(claro).at(__import__("time").time() + 30)
    ok2 = c.post("/acceso/mfa", data={"codigo": siguiente_paso, "csrf": csrf2}, headers=origen, cookies=cookie2)
    assert ok2.status_code == 303 and ok2.headers["location"] == "/portal"

    r, t3 = entrar_con_correo(c, "admin@test.invalid")
    csrf3 = csrf.emitir(secreto, sesiones_mod.hash_token(t3))
    rec = c.post("/acceso/mfa", data={"codigo": codigos_rec[0], "csrf": csrf3}, headers=origen,
                 cookies={sesion.COOKIE: t3})
    assert rec.status_code == 303
    r, t4 = entrar_con_correo(c, "admin@test.invalid")
    csrf4 = csrf.emitir(secreto, sesiones_mod.hash_token(t4))
    de_nuevo = c.post("/acceso/mfa", data={"codigo": codigos_rec[0], "csrf": csrf4}, headers=origen,
                      cookies={sesion.COOKIE: t4})
    assert de_nuevo.status_code == 401

    cursor.execute("SELECT accion FROM auditoria WHERE org_id = :o", o=org)
    acciones = {f[0] for f in cursor.fetchall()}
    assert {"MFA_ENROLADO", "MFA_RECUPERACION_USADA", "LOGIN_OK", "LOGIN_FALLIDO"} <= acciones
    cursor.execute("DELETE FROM recuperacion_mfa WHERE usuario_id = :u", u=uid)
    conn.commit()
    cursor.close()
