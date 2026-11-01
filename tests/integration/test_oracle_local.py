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


def test_leer_analisis_adb_deriva_competible_de_la_matriz_y_la_modalidad(conn, dos_clientes_reales, monkeypatch):
    """`competible` no se guarda: `_leer_analisis_adb` lo deriva de `analisis` + `proceso.modalidad_raw`."""
    import contextlib
    import types

    from src import history
    from src.memoria import repo
    from src.scraper.models import MatrizRiesgos, Proceso, ResultadoAnalisis, RiesgoItem

    cliente_a, _ = dos_clientes_reales
    numero_referencia = f"REF-COMPETIBLE-{_uuid.uuid4().hex[:8]}"
    proceso = Proceso(
        institucion="Ministerio X", descripcion="Test", fecha_publicacion=datetime.now(timezone.utc),
        url_directa="https://example.test", numero_referencia=numero_referencia,
        forma_contratacion="Contratación Directa",
    )
    repo.upsert_proceso_v2(conn, proceso, cliente_a, estado="ANALIZADO")
    repo.save_analisis_v2(
        conn,
        ResultadoAnalisis(
            proceso_uuid=numero_referencia, estado="ANALIZADO",
            matriz=MatrizRiesgos(items=[RiesgoItem("legal", "Contratación directa a proveedor único", "alta", "3.1")],
                                 resumen="proveedor único"),
            modelo_analisis="test-model",
        ),
        cliente_a,
    )
    conn.commit()

    @contextlib.contextmanager
    def _misma_conexion(cfg):
        yield conn

    cfg = types.SimpleNamespace(adb_dsn=_DSN, adb_user=_USUARIO, adb_password=_CLAVE,
                                enable_memoria=True, enable_esquema_v2=True)
    monkeypatch.setattr(history, "_cfg", lambda: cfg)
    monkeypatch.setattr("src.memoria.db.connection", _misma_conexion)
    try:
        out = history._leer_analisis_adb(cliente_a, {numero_referencia}, ("HPE",))
        assert out[numero_referencia].competible is False
        assert out[numero_referencia].motivo_bloqueo
    finally:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g",
                       g=f"{repo.PORTAL_ID_DEFAULT}:{numero_referencia}")
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

    r = c.post("/login", data={"email": "web@test.invalid", "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    sin_cuenta = c.post("/login", data={"email": "nadie@test.invalid", "csrf": csrf.emitir(secreto, "anon")},
                        headers=origen)
    assert r.status_code == sin_cuenta.status_code == 200
    assert [e for e, _ in enviados] == ["web@test.invalid"]
    codigo = enviados[0][1]

    mal = c.post("/login/code", data={"email": "web@test.invalid", "codigo": "AAAAAAAA",
                                         "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    assert mal.status_code == 401
    ok = c.post("/login/code", data={"email": "web@test.invalid", "codigo": codigo,
                                        "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    assert ok.status_code == 303 and ok.headers["location"] == "/portal"
    token = ok.cookies.get(sesion.COOKIE) or ok.headers["set-cookie"].split(";")[0].split("=", 1)[1]

    c.cookies.clear()  # el cliente guardó la cookie; las pruebas siguientes la pasan a mano
    # La cookie abre una sesión real: /acceso ya redirige y la base solo guarda el hash.
    assert c.get("/login", cookies={sesion.COOKIE: token}).status_code == 303
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sesion WHERE sesion_id = :s", s=sesiones_mod.hash_token(token))
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT COUNT(*) FROM sesion WHERE sesion_id = :s", s=token)
    assert cursor.fetchone()[0] == 0

    # El código no se reutiliza.
    de_nuevo = c.post("/login/code", data={"email": "web@test.invalid", "codigo": codigo,
                                              "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
    assert de_nuevo.status_code == 401

    salir = c.post("/logout", data={"csrf": csrf.emitir(secreto, sesiones_mod.hash_token(token))},
                   headers=origen, cookies={sesion.COOKIE: token})
    assert salir.status_code == 303 and salir.headers["location"] == "/login"
    assert c.get("/login", cookies={sesion.COOKIE: token}).status_code == 200  # ya sin sesión
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
        c.post("/login", data={"email": email, "csrf": csrf.emitir(secreto, "anon")}, headers=origen)
        codigo = enviados[-1][1]
        r = c.post("/login/code", data={"email": email, "codigo": codigo, "csrf": csrf.emitir(secreto, "anon")},
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
    assert r.headers["location"].startswith("/login/mfa")
    cookie = {sesion.COOKIE: token}
    assert service.usuario_de_sesion(conn, token) is None and service.mfa_pendiente(conn, token)
    pagina = c.get("/login/mfa", cookies=cookie)
    assert pagina.status_code == 200 and "otpauth://totp/" in pagina.text

    cursor = conn.cursor()
    cursor.execute("SELECT mfa_totp_secret FROM usuario WHERE usuario_id = :u", u=uid)
    cifrado = cursor.fetchone()[0]
    claro = mfa.descifrar(secreto, cifrado)
    assert claro and claro not in cifrado  # en reposo solo vive cifrado

    csrf_mfa = csrf.emitir(secreto, sesiones_mod.hash_token(token))
    malo = c.post("/login/mfa", data={"codigo": "000000", "csrf": csrf_mfa}, headers=origen, cookies=cookie)
    assert malo.status_code == 401
    ok = c.post("/login/mfa", data={"codigo": pyotp.TOTP(claro).now(), "csrf": csrf_mfa}, headers=origen,
                cookies=cookie)
    assert ok.status_code == 200 and "recuperación" in ok.text
    codigos_rec = __import__("re").findall(r"[A-Z0-9]{5}-[A-Z0-9]{5}", ok.text)
    assert len(codigos_rec) == 8
    assert service.usuario_de_sesion(conn, token) == uid and not service.mfa_pendiente(conn, token)

    # Segundo ingreso: el MISMO TOTP (mismo paso) no sirve; uno de otro paso sí; la recuperación, una vez.
    r, t2 = entrar_con_correo(c, "admin@test.invalid")
    cookie2 = {sesion.COOKIE: t2}
    csrf2 = csrf.emitir(secreto, sesiones_mod.hash_token(t2))
    repetido = c.post("/login/mfa", data={"codigo": pyotp.TOTP(claro).now(), "csrf": csrf2}, headers=origen,
                      cookies=cookie2)
    assert repetido.status_code == 401
    siguiente_paso = pyotp.TOTP(claro).at(__import__("time").time() + 30)
    ok2 = c.post("/login/mfa", data={"codigo": siguiente_paso, "csrf": csrf2}, headers=origen, cookies=cookie2)
    assert ok2.status_code == 303 and ok2.headers["location"] == "/portal"

    r, t3 = entrar_con_correo(c, "admin@test.invalid")
    csrf3 = csrf.emitir(secreto, sesiones_mod.hash_token(t3))
    rec = c.post("/login/mfa", data={"codigo": codigos_rec[0], "csrf": csrf3}, headers=origen,
                 cookies={sesion.COOKIE: t3})
    assert rec.status_code == 303
    # (El tope es de 3 códigos por hora y usuario: la cuarta sesión pendiente se crea directo.)
    t4 = sesiones_mod.nuevo_token()
    from src.identidad import repo as repo_identidad
    repo_identidad.crear_sesion(conn, sesiones_mod.hash_token(t4), uid, 12, mfa_ok=False)
    conn.commit()
    csrf4 = csrf.emitir(secreto, sesiones_mod.hash_token(t4))
    de_nuevo = c.post("/login/mfa", data={"codigo": codigos_rec[0], "csrf": csrf4}, headers=origen,
                      cookies={sesion.COOKIE: t4})
    assert de_nuevo.status_code == 401

    cursor.execute("SELECT accion FROM auditoria WHERE org_id = :o", o=org)
    acciones = {f[0] for f in cursor.fetchall()}
    assert {"MFA_ENROLADO", "MFA_RECUPERACION_USADA", "LOGIN_OK", "LOGIN_FALLIDO"} <= acciones
    cursor.execute("DELETE FROM recuperacion_mfa WHERE usuario_id = :u", u=uid)
    conn.commit()
    cursor.close()


def test_ningun_kpi_de_pais_cruza_clientes_por_introspeccion(conn, portal_y_org, dos_clientes_v2):
    """Entrega 4 (T6): TODA función pública de `src.analytics.pais` — también las que se agreguen
    mañana — recibe un `cliente_id` y no devuelve filas del otro. El test recorre el módulo, no una
    lista escrita a mano: una función nueva sin ese filtro rompe esto antes de llegar a producción."""
    import inspect

    from src.analytics import pais, regional

    cliente_a, cliente_b = dos_clientes_v2
    marcas = {cliente_a: "INST-MARCA-AAA", cliente_b: "INST-MARCA-BBB"}
    cursor = conn.cursor()
    gids = []
    for cliente, marca in marcas.items():
        ref = f"TEST-{_uuid.uuid4().hex[:12]}"
        gid = f"{portal_y_org['portal_id']}:{ref}"
        gids.append(gid)
        cursor.execute(
            "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, categoria_raw, "
            "fecha_pub_utc) VALUES (:g, :p, :r, :i, 'infraestructura', SYSTIMESTAMP)",
            g=gid, p=portal_y_org["portal_id"], r=ref, i=marca,
        )
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, triaje) VALUES (:c, :g, 'ANALIZADO', 'GO')",
            c=cliente, g=gid,
        )
    conn.commit()
    try:
        publicas = [f for n, f in inspect.getmembers(pais, inspect.isfunction)
                    if not n.startswith("_") and f.__module__ == pais.__name__]
        assert len(publicas) >= 5, "el test debe estar recorriendo los KPIs del módulo"
        for fn in publicas:
            assert "cliente_id" in inspect.signature(fn).parameters, f"{fn.__name__} no recibe cliente_id"
            for cliente, otro in ((cliente_a, cliente_b), (cliente_b, cliente_a)):
                resultado = repr(fn(conn, cliente))
                assert marcas[otro] not in resultado, f"{fn.__name__} devolvió filas de {otro} para {cliente}"

        # Lo que sí es del cliente aparece (el test no pasa por estar vacío).
        assert marcas[cliente_a] in repr(pais.instituciones_top(conn, cliente_a))
        assert pais.oportunidades_del_mes(conn, cliente_a)["valor"] == 1

        # Vista regional: solo los clientes que se le dan.
        solo_a = regional.oportunidades_por_pais(conn, [cliente_a])
        assert solo_a == [{"cliente_id": cliente_a, "conteo": 1}]
        ambos = regional.oportunidades_por_pais(conn, [cliente_a, cliente_b])
        assert {f["cliente_id"]: f["conteo"] for f in ambos} == {cliente_a: 1, cliente_b: 1}
        assert regional.oportunidades_por_pais(conn, []) == []
        assert regional.nombres_clientes(conn, [cliente_a]) == {cliente_a: cliente_a}
    finally:
        for gid in gids:
            cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=gid)
        conn.commit()
        cursor.close()


@pytest.fixture
def pipeline_e5(conn, portal_y_org, dos_clientes_v2):
    """Un proceso relevante por cliente; limpia estados, eventos y auditoría al final."""
    cliente_a, cliente_b = dos_clientes_v2
    cursor = conn.cursor()
    gids = {}
    for cliente in (cliente_a, cliente_b):
        ref = f"TEST-E5-{_uuid.uuid4().hex[:10]}"
        gid = f"{portal_y_org['portal_id']}:{ref}"
        gids[cliente] = gid
        cursor.execute(
            "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, titulo, fecha_pub_utc) "
            "VALUES (:g, :p, :r, 'INSTITUCION E5', 'Servidores', SYSTIMESTAMP)",
            g=gid, p=portal_y_org["portal_id"], r=ref)
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, triaje) VALUES (:c, :g, 'ANALIZADO', 'GO')",
            c=cliente, g=gid)
    conn.commit()
    yield gids
    for cliente, gid in gids.items():
        cursor.execute("DELETE FROM oportunidad_evento WHERE cliente_id = :c", c=cliente)
        cursor.execute("DELETE FROM oportunidad_estado WHERE cliente_id = :c", c=cliente)
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=gid)
    cursor.execute("DELETE FROM auditoria WHERE org_id = 'test_org' OR org_id = 'org_e5'")
    conn.commit()
    cursor.close()


def _alcance_e5(cliente, cambia=True, org="org_e5"):
    from src.identidad.alcance import Alcance
    return Alcance(usuario_id="u-e5", org_id=org, roles=frozenset({"gerente_pais"}), clientes=frozenset({cliente}),
                   cambia_estado=frozenset({cliente} if cambia else set()), ve_operativo=False)


def test_pipeline_cambiar_estado_contra_oracle(conn, dos_clientes_v2, pipeline_e5):
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    cliente_a, cliente_b = dos_clientes_v2
    gid = pipeline_e5[cliente_a]
    alcance = _alcance_e5(cliente_a)
    cursor = conn.cursor()

    # Lista: aparece como NUEVO, sin fila y sin token.
    fila = next(f for f in orepo.listar(conn, cliente_a) if f["proceso_gid"] == gid)
    assert (fila["estado"], fila["origen"], fila["token"]) == ("NUEVO", None, "")

    # 1) primera captura: NUEVO → EVALUANDO.
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "EVALUANDO", "", competidor="ACME") == service.OK
    conn.commit()
    cursor.execute("SELECT estado, origen, responsable_usuario_id, competidor FROM oportunidad_estado "
                   "WHERE cliente_id = :c AND proceso_gid = :g", c=cliente_a, g=gid)
    assert cursor.fetchone() == ("EVALUANDO", "MANUAL", "u-e5", "ACME")
    cursor.execute("SELECT estado_anterior, estado_nuevo, usuario_id FROM oportunidad_evento WHERE cliente_id = :c",
                   c=cliente_a)
    assert cursor.fetchall() == [("NUEVO", "EVALUANDO", "u-e5")]
    cursor.execute("SELECT accion, usuario_id FROM auditoria WHERE org_id = 'org_e5'")
    assert cursor.fetchall() == [("ESTADO_CAMBIADO", "u-e5")]

    # 2) con el formulario VIEJO (token vacío) ya hay conflicto: nadie pisa lo que no vio.
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "OFERTADO", "") == service.CONFLICTO
    token = orepo.obtener(conn, cliente_a, gid)["token"]
    assert token and token != ""

    # 3) transición inválida y falta de motivo: no escriben.
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "GANADO", token) == service.INVALIDO
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "DESCARTADO", token) == service.INVALIDO

    # 4) el token se renueva con cada cambio: el anterior deja de servir (concurrencia).
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "OFERTADO", token,
                                  monto=__import__("decimal").Decimal("1500.25")) == service.OK
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "GANADO", token) == service.CONFLICTO
    conn.commit()
    cursor.execute("SELECT monto_ofertado FROM oportunidad_estado WHERE cliente_id = :c", c=cliente_a)
    assert float(cursor.fetchone()[0]) == 1500.25
    cursor.execute("SELECT estado_anterior, estado_nuevo FROM oportunidad_evento WHERE cliente_id = :c "
                   "ORDER BY evento_id", c=cliente_a)
    assert cursor.fetchall() == [("NUEVO", "EVALUANDO"), ("EVALUANDO", "OFERTADO")]

    # 5) IDOR y permisos: el proceso del OTRO país, o sin permiso de cambio, es NO_ENCONTRADO.
    assert service.cambiar_estado(conn, alcance, cliente_a, pipeline_e5[cliente_b], "EVALUANDO", "") == service.NO_ENCONTRADO
    assert service.cambiar_estado(conn, alcance, cliente_b, pipeline_e5[cliente_b], "EVALUANDO", "") == service.NO_ENCONTRADO
    solo_lee = _alcance_e5(cliente_a, cambia=False)
    nuevo_token = orepo.obtener(conn, cliente_a, gid)["token"]
    assert service.cambiar_estado(conn, solo_lee, cliente_a, gid, "GANADO", nuevo_token) == service.NO_ENCONTRADO
    cursor.close()


def test_pipeline_rollback_si_falla_el_evento(conn, dos_clientes_v2, pipeline_e5, monkeypatch):
    """El estado y el evento entran juntos o no entran: si el evento falla y se hace rollback, no queda estado."""
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    cliente_a = dos_clientes_v2[0]
    gid = pipeline_e5[cliente_a]

    def boom(*a, **k):
        raise RuntimeError("evento no escrito")

    monkeypatch.setattr(orepo, "insertar_evento", boom)
    with pytest.raises(RuntimeError):
        service.cambiar_estado(conn, _alcance_e5(cliente_a), cliente_a, gid, "EVALUANDO", "")
    conn.rollback()  # lo que hace `db.connection` ante una excepción
    assert orepo.obtener(conn, cliente_a, gid) is None
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM auditoria WHERE org_id = 'org_e5'")
    assert cursor.fetchone()[0] == 0
    cursor.close()


def test_pipeline_la_persona_toma_la_fila_que_movio_el_mercado(conn, dos_clientes_v2, pipeline_e5):
    """P8: corregir un estado que puso COMPRASAL exige motivo y deja la fila como MANUAL, con su evento."""
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    cliente_a = dos_clientes_v2[0]
    gid = pipeline_e5[cliente_a]
    cursor = conn.cursor()
    cursor.execute("INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen) "
                   "VALUES (:c, :g, 'GANADO', 'AUTOMATICO_MERCADO')", c=cliente_a, g=gid)
    conn.commit()
    token = orepo.obtener(conn, cliente_a, gid)["token"]
    alcance = _alcance_e5(cliente_a)

    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "PERDIDO", token) == service.INVALIDO  # sin motivo
    assert service.cambiar_estado(conn, alcance, cliente_a, gid, "PERDIDO", token,
                                  motivo="COMPRASAL lo publicó mal; ganó otro") == service.OK
    conn.commit()
    cursor.execute("SELECT estado, origen, motivo FROM oportunidad_estado WHERE cliente_id = :c", c=cliente_a)
    assert cursor.fetchone() == ("PERDIDO", "MANUAL", "COMPRASAL lo publicó mal; ganó otro")
    cursor.execute("SELECT estado_anterior, estado_nuevo FROM oportunidad_evento WHERE cliente_id = :c", c=cliente_a)
    assert cursor.fetchall() == [("GANADO", "PERDIDO")]
    cursor.close()


def test_kpi_15_y_16_leen_solo_los_eventos_del_cliente(conn, dos_clientes_v2, pipeline_e5):
    from src.analytics import pais
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    cliente_a, cliente_b = dos_clientes_v2
    for cliente in (cliente_a, cliente_b):
        assert service.cambiar_estado(conn, _alcance_e5(cliente), cliente, pipeline_e5[cliente], "EVALUANDO", "") == service.OK
    token = orepo.obtener(conn, cliente_a, pipeline_e5[cliente_a])["token"]
    assert service.cambiar_estado(conn, _alcance_e5(cliente_a), cliente_a, pipeline_e5[cliente_a], "OFERTADO", token) == service.OK
    conn.commit()

    conv = pais.conversion_comercial(conn, cliente_a)
    assert conv["detectadas"] == 1
    assert {e["estado"]: e["conteo"] for e in conv["etapas"]} == {"EVALUANDO": 1, "OFERTADO": 1, "GANADO": 0}
    assert conv["conv_ofertado_pct"] == 100.0 and conv["conv_ganado_pct"] == 0.0  # 1 oferta, 0 ganadas: 0 %
    conv_b = pais.conversion_comercial(conn, cliente_b)
    assert {e["estado"]: e["conteo"] for e in conv_b["etapas"]} == {"EVALUANDO": 1, "OFERTADO": 0, "GANADO": 0}
    assert conv_b["conv_ganado_pct"] is None  # sin ofertas no hay tasa: None, no un 0 % falso

    # KPI 16: EVALUANDO ya terminó (hay un OFERTADO después) en A; en B sigue abierto.
    tiempos_a = {t["etapa"]: t for t in pais.tiempo_por_etapa(conn, cliente_a)}
    assert tiempos_a["EVALUANDO"]["muestra"] == 1 and tiempos_a["EVALUANDO"]["dias_promedio"] >= 0
    assert "OFERTADO" not in tiempos_a  # todavía abierta
    assert pais.tiempo_por_etapa(conn, cliente_b) == []


# ═══════════════════════════════════════════════════════════════════════════
# Identidad — E10 «Ver como»: columnas y SQL de la simulación en la sesión
# ═══════════════════════════════════════════════════════════════════════════

def test_ver_como_fijar_obtener_limpiar_y_ttl_contra_oracle(conn, usuarios_e1):
    from src.identidad import repo as identidad

    usuario = usuarios_e1[0]
    cursor = conn.cursor()
    identidad.crear_sesion(conn, "vc_viva", usuario, 12)
    identidad.crear_sesion(conn, "vc_pendiente", usuario, 12, mfa_ok=False)
    identidad.crear_sesion(conn, "vc_revocada", usuario, 12)
    identidad.revocar_sesion(conn, "vc_revocada")
    cursor.execute(
        "INSERT INTO sesion (sesion_id, usuario_id, expira_en) "
        "VALUES ('vc_vencida', :u, SYSTIMESTAMP - INTERVAL '1' HOUR)", u=usuario)
    conn.commit()

    # Una sesión nueva nace sin simulación.
    assert identidad.obtener_ver_como(conn, "vc_viva", 30) is None

    # fijar → obtener; el cliente es opcional y fijar de nuevo reemplaza
    assert identidad.fijar_ver_como(conn, "vc_viva", "gerente_pais", "cli_x") is True
    rol, cliente, desde = identidad.obtener_ver_como(conn, "vc_viva", 30)
    assert (rol, cliente) == ("gerente_pais", "cli_x") and desde is not None
    assert identidad.fijar_ver_como(conn, "vc_viva", "ceo", None) is True
    assert identidad.obtener_ver_como(conn, "vc_viva", 30)[:2] == ("ceo", None)

    # TTL vencido: se fuerza ver_como_desde al pasado
    cursor.execute("UPDATE sesion SET ver_como_desde = SYSTIMESTAMP - INTERVAL '31' MINUTE "
                   "WHERE sesion_id = 'vc_viva'")
    assert identidad.obtener_ver_como(conn, "vc_viva", 30) is None
    assert identidad.obtener_ver_como(conn, "vc_viva", 60) is not None

    # renovar la sesión NO reinicia la simulación
    assert identidad.renovar_sesion(conn, "vc_viva", 12, 7) is True
    assert identidad.obtener_ver_como(conn, "vc_viva", 30) is None

    # sesión pendiente de TOTP, revocada, vencida o inexistente: no se fija ni se lee
    for sid in ("vc_pendiente", "vc_revocada", "vc_vencida", "no_existe"):
        assert identidad.fijar_ver_como(conn, sid, "ceo", None) is False
        assert identidad.obtener_ver_como(conn, sid, 30) is None

    # el CHECK se evalúa en el motor: rol sin desde y desde sin rol se rechazan; rol + desde se admite
    for rol_x, desde_x in (("'ceo'", "NULL"), ("NULL", "SYSTIMESTAMP")):
        with pytest.raises(Exception, match="ORA-02290"):
            cursor.execute(f"UPDATE sesion SET ver_como_rol = {rol_x}, ver_como_desde = {desde_x} "
                           "WHERE sesion_id = 'vc_pendiente'")
    cursor.execute("UPDATE sesion SET ver_como_rol = 'ceo', ver_como_desde = SYSTIMESTAMP "
                   "WHERE sesion_id = 'vc_pendiente'")

    # limpiar es idempotente
    assert identidad.limpiar_ver_como(conn, "vc_viva") is True
    assert identidad.limpiar_ver_como(conn, "vc_viva") is False
    assert identidad.limpiar_ver_como(conn, "no_existe") is False

    # la purga de sesiones vencidas sigue funcionando con las columnas nuevas
    cursor.execute("UPDATE sesion SET expira_en = SYSTIMESTAMP - INTERVAL '40' DAY WHERE sesion_id = 'vc_vencida'")
    assert identidad.purgar_sesiones_vencidas(conn, 30) >= 1
    cursor.execute("SELECT COUNT(*) FROM sesion WHERE sesion_id = 'vc_vencida'")
    assert cursor.fetchone()[0] == 0
    conn.commit()
    cursor.close()


def test_schema_v2_agrega_ver_como_a_una_adb_anterior_y_conserva_las_sesiones(conn, usuarios_e1):
    """Una ADB sin las columnas de «ver como» (con sesiones ya creadas) las recibe al re-ejecutar el DDL, que es
    idempotente; las sesiones antiguas quedan con las tres columnas en NULL y el CHECK no las rechaza."""
    usuario = usuarios_e1[0]
    cursor = conn.cursor()
    cursor.execute("INSERT INTO sesion (sesion_id, usuario_id, expira_en) "
                   "VALUES ('vc_antigua', :u, SYSTIMESTAMP + INTERVAL '1' HOUR)", u=usuario)
    cursor.execute("ALTER TABLE sesion DROP CONSTRAINT ck_sesion_ver_como")
    for columna in ("ver_como_rol", "ver_como_cliente", "ver_como_desde"):
        cursor.execute(f"ALTER TABLE sesion DROP COLUMN {columna}")
    conn.commit()

    for _ in range(2):  # y de nuevo: idempotente
        for sentencia in _sentencias_de(_RAIZ / "db" / "schema_v2.sql"):
            cursor.execute(sentencia)
        conn.commit()

    cursor.execute("SELECT column_name FROM user_tab_columns WHERE table_name = 'SESION' "
                   "AND column_name IN ('VER_COMO_ROL','VER_COMO_CLIENTE','VER_COMO_DESDE')")
    assert len(cursor.fetchall()) == 3
    cursor.execute("SELECT COUNT(*) FROM user_constraints WHERE constraint_name = 'CK_SESION_VER_COMO'")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT ver_como_rol, ver_como_cliente, ver_como_desde FROM sesion WHERE sesion_id = 'vc_antigua'")
    assert cursor.fetchone() == (None, None, None)
    cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# Identidad — E10 «Ver como»: de punta a punta con la app FastAPI real
# ═══════════════════════════════════════════════════════════════════════════
# Misma técnica que los tests web de E3/E4: `db.connection` se reemplaza por la conexión de la
# fixture (`src.memoria.db` es un solo módulo, así que vale para acceso, sesión y portal), el cliente
# habla HTTPS (la cookie `__Host-` exige `secure`) y las sesiones se crean directo con
# `repo.crear_sesion(..., mfa_ok=True)`: el login ya lo prueban E3/E4.

_BASE_E10, _SECRETO_E10 = "https://dev.amalia.test", "secreto-de-prueba"


def _app_e10(conn, monkeypatch):
    """App real sobre el Oracle local + utilidades para hablarle con una sesión dada."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.identidad import repo as identidad_repo
    from src.identidad import sesiones as sesiones_mod
    from src.web import acceso, csrf, sesion
    from src.web.app import crear_app

    cfg = SimpleNamespace(
        enable_portal=True, identidad_secret=_SECRETO_E10, feedback_base_url=_BASE_E10, portal_cliente_id="cli",
        enable_feedback=False, daily_budget_usd=5.0, codigo_ttl_min=10, sesion_ttl_horas=12, sesion_max_dias=7,
        clients_dir="",  # la vista regional busca el YAML del cliente; sin él, el portal por defecto
    )

    @contextmanager
    def conexion(_cfg):
        yield conn
        conn.commit()

    monkeypatch.setattr(acceso.db, "connection", conexion)
    monkeypatch.setattr(sesion.db, "connection", conexion)
    c = TestClient(crear_app(cfg), base_url=_BASE_E10, follow_redirects=False)

    def nueva_sesion(usuario_id):
        token = sesiones_mod.nuevo_token()
        identidad_repo.crear_sesion(conn, sesiones_mod.hash_token(token), usuario_id, 12, mfa_ok=True)
        conn.commit()
        return token

    def get(token, ruta):
        return c.get(ruta, cookies={sesion.COOKIE: token})

    def post(token, ruta, datos, csrf_ok=True):
        datos = dict(datos, csrf=csrf.emitir(_SECRETO_E10, sesiones_mod.hash_token(token)) if csrf_ok else "malo")
        return c.post(ruta, data=datos, headers={"origin": _BASE_E10}, cookies={sesion.COOKIE: token})

    def ver_como(token, vista, csrf_ok=True):
        return post(token, "/portal/view-as", {"vista": vista}, csrf_ok=csrf_ok)

    def fila_sesion(token):
        cursor = conn.cursor()
        cursor.execute("SELECT ver_como_rol, ver_como_cliente, revocada FROM sesion WHERE sesion_id = :s",
                       s=sesiones_mod.hash_token(token))
        fila = cursor.fetchone()
        cursor.close()
        return fila

    return SimpleNamespace(nueva_sesion=nueva_sesion, get=get, post=post, ver_como=ver_como,
                           fila_sesion=fila_sesion)


def _auditoria_e10(conn, org):
    cursor = conn.cursor()
    cursor.execute("SELECT accion, usuario_id, recurso FROM auditoria WHERE org_id = :o ORDER BY auditoria_id", o=org)
    filas = cursor.fetchall()
    cursor.close()
    return filas


def _conteo_pipeline_e10(conn, cliente):
    cursor = conn.cursor()
    cursor.execute("SELECT (SELECT COUNT(*) FROM oportunidad_estado WHERE cliente_id = :c), "
                   "(SELECT COUNT(*) FROM oportunidad_evento WHERE cliente_id = :c) FROM dual", c=cliente)
    fila = cursor.fetchone()
    cursor.close()
    return fila


def test_ver_como_de_punta_a_punta_contra_oracle(
    conn, portal_y_org, dos_clientes_v2, pipeline_e5, limpiar_identidad, monkeypatch
):
    """admin_plataforma real → ve como gerente_pais de A (banner, tabla, controles deshabilitados, sin
    operativo) → el POST de estado no escribe → ceo (regional, todos los países) → operador (solo
    operativo) → vuelve a su vista, y todo queda auditado a su nombre. Además: el TTL vencido, el país
    desactivado a mitad y la sesión revocada apagan la simulación."""
    from urllib.parse import quote

    from src.identidad import repo as identidad_repo
    from src.identidad import service
    from src.identidad import sesiones as sesiones_mod

    org = portal_y_org["org_id"]
    cliente_a, cliente_b = dos_clientes_v2
    admin = service.alta_usuario(conn, org, "admin-e10@test.invalid", "admin_plataforma")
    conn.commit()
    app = _app_e10(conn, monkeypatch)
    token = app.nueva_sesion(admin)

    # Vista real del admin: solo operativo, con el selector y sin banner.
    r = app.get(token, "/portal")
    assert r.status_code == 303 and r.headers["location"] == "/portal/operations"
    r = app.get(token, "/portal/operations")
    assert r.status_code == 200
    assert 'action="/portal/view-as"' in r.text and "Viendo como" not in r.text
    assert f'value="gerente_pais|{cliente_a}"' in r.text

    # CSRF inválido: 403 y nada cambia.
    assert app.ver_como(token, f"gerente_pais|{cliente_a}", csrf_ok=False).status_code == 403
    assert app.fila_sesion(token)[0] is None

    # (a) gerente_pais de A.
    r = app.ver_como(token, f"gerente_pais|{cliente_a}")
    assert r.status_code == 303 and r.headers["location"] == "/portal?notice=ver_como_ok"
    assert app.fila_sesion(token)[:2] == ("gerente_pais", cliente_a)
    r = app.get(token, "/portal?notice=ver_como_ok")
    assert r.status_code == 200
    assert "Viendo como" in r.text and "solo lectura" in r.text and "expira en" in r.text
    assert "INSTITUCION E5" in r.text  # la tabla del país A
    assert '<fieldset class="solo-vista" disabled>' in r.text  # mismos controles, deshabilitados
    assert app.get(token, "/portal/operations").status_code == 404
    assert app.get(token, "/portal/regional").status_code == 404
    # `?client=` ajeno bajo simulación: el 404 que vería esa persona, auditado con la marca.
    assert app.get(token, f"/portal?client={cliente_b}").status_code == 404
    assert ("ACCESO_FUERA_DE_ALCANCE", admin, f"cliente={cliente_b} [ver_como=gerente_pais]") \
        in _auditoria_e10(conn, org)

    # (b) El POST de estado bajo simulación no escribe y solo hay auditoría de «ver como».
    antes_pipeline = _conteo_pipeline_e10(conn, cliente_a)
    antes_audit = _auditoria_e10(conn, org)
    r = app.post(token, f"/portal/opportunity/{quote(pipeline_e5[cliente_a], safe='')}/status",
                 {"cliente": cliente_a, "estado": "EVALUANDO", "actualizado_en": "", "dias": "90"})
    assert r.status_code == 303 and "notice=simulacion" in r.headers["location"]
    assert _conteo_pipeline_e10(conn, cliente_a) == antes_pipeline == (0, 0)
    assert _auditoria_e10(conn, org) == antes_audit
    acciones = [a for a, _, _ in _auditoria_e10(conn, org)]
    assert "ESTADO_CAMBIADO" not in acciones
    assert all(u == admin for a, u, _ in _auditoria_e10(conn, org) if a.startswith("VER_COMO"))

    # (c) ceo: /portal lleva a la regional, que muestra todos los países activos.
    assert app.ver_como(token, "ceo").status_code == 303
    r = app.get(token, "/portal")
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/regional")
    r = app.get(token, "/portal/regional")
    assert r.status_code == 200 and "Viendo como" in r.text
    assert f"client={cliente_a}" in r.text and f"client={cliente_b}" in r.text
    assert app.get(token, "/portal/operations").status_code == 404

    # (d) operador: solo operativo, ningún país.
    assert app.ver_como(token, "operador").status_code == 303
    r = app.get(token, "/portal")
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/operations")
    r = app.get(token, "/portal/operations")
    assert r.status_code == 200 and "Viendo como" in r.text
    assert app.get(token, f"/portal?client={cliente_a}").status_code == 404
    assert app.get(token, "/portal/regional").status_code == 404

    # Valores fuera de las opciones: rol de org con país, rol de país sin país, inventado, `|` de más.
    for mala in ("ceo|" + cliente_a, "gerente_pais", "admin_plataforma", "inventado",
                 f"gerente_pais|{cliente_a}|x", "gerente_pais|no_existe", "x" * 300):
        r = app.ver_como(token, mala)
        assert r.status_code == 303 and r.headers["location"] == "/portal?notice=ver_como_invalido", mala
    assert app.fila_sesion(token)[:2] == ("operador", None)  # la simulación anterior sigue intacta

    # (e) Volver a la vista real: operativo, columnas en NULL y VER_COMO_FIN a nombre del admin.
    r = app.ver_como(token, "")
    assert r.status_code == 303 and r.headers["location"] == "/portal?notice=ver_como_fin"
    assert app.fila_sesion(token)[:2] == (None, None)
    r = app.get(token, "/portal/operations")
    assert r.status_code == 200 and "Viendo como" not in r.text
    auditoria = _auditoria_e10(conn, org)
    assert ("VER_COMO_FIN", admin, None) in auditoria
    inicios = [rec for a, u, rec in auditoria if a == "VER_COMO_INICIO" and u == admin]
    assert inicios == [f"rol=gerente_pais;cliente={cliente_a}", "rol=ceo;cliente=-", "rol=operador;cliente=-"]

    # (g) TTL vencido (61 min): vuelve la vista real sin que nadie escriba nada.
    assert app.ver_como(token, f"gerente_pais|{cliente_a}").status_code == 303
    assert app.get(token, "/portal").status_code == 200  # simulada: la vista de país
    cursor = conn.cursor()
    cursor.execute("UPDATE sesion SET ver_como_desde = SYSTIMESTAMP - INTERVAL '61' MINUTE WHERE sesion_id = :s",
                   s=sesiones_mod.hash_token(token))
    conn.commit()
    r = app.get(token, "/portal")
    assert r.status_code == 303 and r.headers["location"] == "/portal/operations"

    # País desactivado a mitad de la simulación: se ignora (y vuelve si el país se reactiva).
    assert app.ver_como(token, f"gerente_pais|{cliente_a}").status_code == 303
    cursor.execute("UPDATE cliente SET activo = 0 WHERE cliente_id = :c", c=cliente_a)
    conn.commit()
    try:
        r = app.get(token, "/portal")
        assert r.status_code == 303 and r.headers["location"] == "/portal/operations"
    finally:
        cursor.execute("UPDATE cliente SET activo = 1 WHERE cliente_id = :c", c=cliente_a)
        conn.commit()

    # Deja de ser admin a mitad: la simulación (aún en la fila) se ignora.
    cursor.execute("DELETE FROM membresia_org WHERE usuario_id = :u AND rol = 'admin_plataforma'", u=admin)
    conn.commit()
    assert app.fila_sesion(token)[0] == "gerente_pais"
    assert app.get(token, "/portal").status_code == 404  # sin roles no ve nada: ni el país simulado
    cursor.execute("INSERT INTO membresia_org (usuario_id, org_id, rol) VALUES (:u, :o, 'admin_plataforma')",
                   u=admin, o=org)
    conn.commit()

    # Una sesión nueva del mismo admin nace sin simulación.
    otro = app.nueva_sesion(admin)
    assert app.get(otro, "/portal").headers["location"] == "/portal/operations"

    # (h) Sesión revocada (o cerrada con /salir): ni simulación ni acceso.
    assert app.get(token, "/portal").status_code == 200  # aún simulando gerente_pais de A
    assert identidad_repo.revocar_sesion(conn, sesiones_mod.hash_token(token)) is True
    conn.commit()
    r = app.get(token, "/portal")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert app.get(token, "/portal/operations").headers["location"].startswith("/login")
    assert app.ver_como(token, "ceo").headers["location"].startswith("/login")
    assert app.post(otro, "/logout", {}).status_code == 303
    assert app.fila_sesion(otro)[2] == 1
    cursor.close()


def test_ver_como_un_no_admin_recibe_404_y_queda_auditado(
    conn, portal_y_org, dos_clientes_v2, pipeline_e5, limpiar_identidad, monkeypatch
):
    """(f) Quien no es admin_plataforma no puede simular: 404 + ACCESO_FUERA_DE_ALCANCE y su sesión
    no cambia. Y unas columnas pobladas a mano en su sesión se ignoran: no le amplían el alcance."""
    from src.identidad import service
    from src.identidad import sesiones as sesiones_mod

    org = portal_y_org["org_id"]
    cliente_a, cliente_b = dos_clientes_v2
    gp = service.alta_usuario(conn, org, "gp-e10@test.invalid", "gerente_pais", cliente_a)
    conn.commit()
    app = _app_e10(conn, monkeypatch)
    token = app.nueva_sesion(gp)

    r = app.get(token, "/portal")
    assert r.status_code == 200 and 'action="/portal/view-as"' not in r.text  # sin selector

    for vista in ("ceo", f"gerente_pais|{cliente_b}", ""):
        assert app.ver_como(token, vista).status_code == 404
    assert app.fila_sesion(token) == (None, None, 0)
    fuera = [(u, rec) for a, u, rec in _auditoria_e10(conn, org) if a == "ACCESO_FUERA_DE_ALCANCE"]
    assert fuera == [(gp, "ver-como")] * 3
    assert not [a for a, _, _ in _auditoria_e10(conn, org) if a.startswith("VER_COMO")]

    # Una fila `sesion.ver_como_*` poblada a mano (sin pasar por el endpoint) no le da nada.
    cursor = conn.cursor()
    cursor.execute("UPDATE sesion SET ver_como_rol = 'ceo', ver_como_desde = SYSTIMESTAMP WHERE sesion_id = :s",
                   s=sesiones_mod.hash_token(token))
    conn.commit()
    cursor.close()
    assert app.get(token, "/portal/regional").status_code == 404
    assert app.get(token, f"/portal?client={cliente_b}").status_code == 404
    r = app.get(token, "/portal")
    assert r.status_code == 200 and "Viendo como" not in r.text and "INSTITUCION E5" in r.text


def test_ver_como_gerente_pais_es_lo_mismo_que_ve_un_gerente_real(
    conn, portal_y_org, dos_clientes_v2, pipeline_e5, limpiar_identidad, monkeypatch
):
    """(i) Equivalencia: /portal de un admin que ve como gerente_pais de A recibe EXACTAMENTE el mismo
    contexto de plantilla (KPIs, filas, navegación) que un gerente_pais real de A; solo cambian el CSRF,
    el selector/banner de «ver como», el aviso y la marca de solo lectura."""
    from src.identidad import service
    from src.web import portal as portal_mod

    org = portal_y_org["org_id"]
    cliente_a = dos_clientes_v2[0]
    admin = service.alta_usuario(conn, org, "admin-eq@test.invalid", "admin_plataforma")
    gp = service.alta_usuario(conn, org, "gp-eq@test.invalid", "gerente_pais", cliente_a)
    conn.commit()
    app = _app_e10(conn, monkeypatch)
    t_admin, t_gp = app.nueva_sesion(admin), app.nueva_sesion(gp)

    capturas = []
    original = portal_mod.pagina

    def capturar(request, nombre, status=200, **contexto):
        capturas.append((nombre, contexto))
        return original(request, nombre, status=status, **contexto)

    monkeypatch.setattr(portal_mod, "pagina", capturar)

    assert app.ver_como(t_admin, f"gerente_pais|{cliente_a}").status_code == 303
    assert app.get(t_gp, "/portal").status_code == 200
    assert app.get(t_admin, "/portal").status_code == 200
    (n_real, real), (n_sim, simulado) = capturas[-2:]
    assert n_real == n_sim == "portal_pais.html"

    distintos = {"csrf", "ver_como", "aviso", "solo_vista", "tablero"}

    def sin_editable(ctx):  # `tablero.editable` = puede_cambiar y NO solo_vista: difiere a propósito bajo «ver como»
        return {k: v for k, v in ctx["tablero"].items() if k != "editable"}

    assert {k: v for k, v in real.items() if k not in distintos} == \
        {k: v for k, v in simulado.items() if k not in distintos}
    assert sin_editable(real) == sin_editable(simulado)
    assert (real["tablero"]["editable"], simulado["tablero"]["editable"]) == (True, False)
    assert any(c["tarjetas"] for c in real["tablero"]["columnas"]), "el test debe comparar al menos una tarjeta"
    assert real["puede_cambiar"] is simulado["puede_cambiar"] is True
    assert (real["solo_vista"], simulado["solo_vista"]) == (False, True)
    assert real["ver_como"]["puede"] is False and simulado["ver_como"]["simulando"]


# ═══════════════════════════════════════════════════════════════════════════
# E11 — vista de país «qué hago hoy»: lecturas de `analytics.tablero`, quién es asignable, asignar
# responsable, recuperar un descartado y la página real de punta a punta
# ═══════════════════════════════════════════════════════════════════════════
# Mismo patrón que E5/E10: INSERTs inline para los datos, funciones REALES para todo lo demás (SQL de
# `src/`), y la app FastAPI real con `db.connection` apuntando a la conexión de la fixture. Ningún stub de
# `tablero`: la página se arma con el SQL y la `clasificar` de verdad, que es justo lo que los tests
# unitarios de la vista no ejercitan.

# «Ahora» en El Salvador, como lo calcula `src.analytics.operativo._SQL_AHORA` (que fecha `ejecucion`); se
# repite como texto para que las fechas de los datos de prueba no dependan del código bajo prueba.
_AHORA_SV_E111 = "CAST(SYSTIMESTAMP AT TIME ZONE 'America/El_Salvador' AS TIMESTAMP)"

_MATRIZ_E11 = (
    '{"items": [{"categoria": "legal", "descripcion": "Contratación directa con proveedor único",'
    ' "severidad": "alta", "clausula_ref": "Art. 1"}], "resumen": "Contratación directa"}'
)
_MARCAS_E11 = (
    '[{"marca": "Dell", "razon": "servidores", "role": "Recomendada"},'
    ' {"marca": "HPE", "razon": "alternativa", "role": "Alternativa"}]'
)
_TITULOS_E11 = {
    "go": "Servidores E11 para decidir",
    "desc": "Mobiliario E11 descartado por regex",
    "desc2": "Papeleria E11 descartada por triaje",
    "est": "Firewall E11 en evaluacion",
    "otro": "Switches E11 de otro pais",
}


@pytest.fixture
def tablero_e11(conn, portal_y_org, dos_clientes_v2):
    """Cliente A: uno relevante para decidir (cierra en 3 días, con marcas y monto estimado), dos
    descartados por AMALIA (regex y triaje), y uno relevante publicado hace 400 días pero en EVALUANDO
    hace 10 días (estancado, no competible por su matriz). Cliente B: uno relevante y OFERTADO sin monto.
    Votos con destinatarios «secreto-*» que nunca deben llegar a la página. Limpia todo al final."""
    cliente_a, cliente_b = dos_clientes_v2
    portal = portal_y_org["portal_id"]
    sufijo = _uuid.uuid4().hex[:8]
    gids = {k: f"{portal}:TEST-E11-{k.upper()}-{sufijo}" for k in _TITULOS_E11}
    cursor = conn.cursor()

    def proceso(clave, pub_sql, limite_sql="NULL", monto=None, modalidad=None):
        cursor.execute(
            "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, titulo, modalidad_raw, "
            f"fecha_pub_utc, fecha_limite_utc, tz_portal, monto_usd, url_portal) VALUES (:g, :p, :r, 'INSTITUCION E11', "
            f":t, :m, {pub_sql}, {limite_sql}, 'America/El_Salvador', :monto, :url)",
            g=gids[clave], p=portal, r=gids[clave].split(":", 1)[1], t=_TITULOS_E11[clave], m=modalidad,
            monto=monto, url=f"https://example.test/{clave}")

    def cliente_proceso(cliente, clave, estado, relevante, triaje=None, score=0.9, marcas=None):
        cursor.execute(
            "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, es_relevante, triaje, match_tecnico, "
            f"match_categoria, factibilidad, marcas) VALUES (:c, :g, :e, :rel, :tri, :s, :s, :s, "
            f"{'JSON(:marcas)' if marcas else 'NULL'})",
            dict(c=cliente, g=gids[clave], e=estado, rel=relevante, tri=triaje, s=score,
                 **({"marcas": marcas} if marcas else {})))

    def voto(cliente, clave, destinatario, valor, motivo=None, motivos=None):
        cursor.execute(
            "INSERT INTO feedback (cliente_id, proceso_gid, destinatario, voto, motivo, motivos, comentario) "
            f"VALUES (:c, :g, :d, :v, :m, {'JSON(:ms)' if motivos else 'NULL'}, '<script>alert(1)</script>')",
            dict(c=cliente, g=gids[clave], d=destinatario, v=valor, m=motivo, **({"ms": motivos} if motivos else {})))

    version = f"test-e11-{sufijo}"
    try:
        # E11.1: `tendencia_semanal` asigna cada proceso a su día de corrida (`fecha_pub_utc + 11 h`) y lee los
        # revisados de `ejecucion`. «go» se publica el miércoles de la semana ISO anterior a las 01:00 UTC (lejos
        # del borde del domingo 13:00 UTC), y hay corridas ese miércoles (con una repetición menor: manda el MAX)
        # y hoy, para que el cálculo semanal sea determinista a cualquier hora en que corra el CI.
        proceso("go", f"TRUNC({_AHORA_SV_E111}, 'IW') - 5 + NUMTODSINTERVAL(1, 'HOUR')",
                "SYSTIMESTAMP + NUMTODSINTERVAL(3, 'DAY')")
        for ts_sql, revisados in ((f"TRUNC({_AHORA_SV_E111}, 'IW') - 5 + NUMTODSINTERVAL(7, 'HOUR')", 20),
                                  (f"TRUNC({_AHORA_SV_E111}, 'IW') - 5 + NUMTODSINTERVAL(10, 'HOUR')", 5),
                                  (_AHORA_SV_E111, 40)):
            cursor.execute(f"INSERT INTO ejecucion (run_timestamp, success, procesos_revisados, version) "
                           f"VALUES ({ts_sql}, 1, :n, :v)", n=revisados, v=version)
        cliente_proceso(cliente_a, "go", "ANALIZADO", 1, triaje="GO", marcas=_MARCAS_E11)
        cursor.execute("INSERT INTO analisis (cliente_id, proceso_gid, monto_estimado) VALUES (:c, :g, 1000)",
                       c=cliente_a, g=gids["go"])

        proceso("desc", "SYSTIMESTAMP")
        cliente_proceso(cliente_a, "desc", "DESCARTADO_REGEX", 0, score=0.7)
        proceso("desc2", "SYSTIMESTAMP")
        cliente_proceso(cliente_a, "desc2", "DESCARTADO_TRIAJE", None, triaje="NO-GO", score=0.5)

        proceso("est", "SYSTIMESTAMP - NUMTODSINTERVAL(400, 'DAY')", monto=2500, modalidad="Contratación Directa")
        cliente_proceso(cliente_a, "est", "ANALIZADO", 1)
        cursor.execute("INSERT INTO analisis (cliente_id, proceso_gid, matriz_riesgos, severidad_max) "
                       "VALUES (:c, :g, JSON(:m), 'alta')", c=cliente_a, g=gids["est"], m=_MATRIZ_E11)
        cursor.execute("INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen, actualizado_en) "
                       "VALUES (:c, :g, 'EVALUANDO', 'MANUAL', SYSTIMESTAMP - NUMTODSINTERVAL(10, 'DAY'))",
                       c=cliente_a, g=gids["est"])

        proceso("otro", "SYSTIMESTAMP")
        cliente_proceso(cliente_b, "otro", "ANALIZADO", 1, triaje="GO", marcas=_MARCAS_E11)
        cursor.execute("INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen) "
                       "VALUES (:c, :g, 'OFERTADO', 'MANUAL')", c=cliente_b, g=gids["otro"])

        voto(cliente_a, "go", "secreto-1@cliente.invalid", "IRRELEVANTE", "fuera_de_rubro",
             '["fuera_de_rubro", "precio"]')
        voto(cliente_a, "go", "secreto-2@cliente.invalid", "RELEVANTE")
        voto(cliente_a, "desc", "secreto-3@cliente.invalid", "RELEVANTE")
        voto(cliente_a, "est", "secreto-4@cliente.invalid", "IRRELEVANTE", "otro")  # fila anterior a `motivos`
        voto(cliente_b, "otro", "secreto-5@cliente.invalid", "IRRELEVANTE", "precio", '["precio"]')
        conn.commit()
        yield {"gids": gids, "a": cliente_a, "b": cliente_b}
    finally:
        conn.rollback()
        for cliente in (cliente_a, cliente_b):
            cursor.execute("DELETE FROM feedback WHERE cliente_id = :c", c=cliente)
            cursor.execute("DELETE FROM oportunidad_evento WHERE cliente_id = :c", c=cliente)
            cursor.execute("DELETE FROM oportunidad_estado WHERE cliente_id = :c", c=cliente)
        for gid in gids.values():
            cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=gid)  # cascada: cliente_proceso, analisis
        cursor.execute("DELETE FROM auditoria WHERE org_id = :o", o=portal_y_org["org_id"])
        cursor.execute("DELETE FROM ejecucion WHERE version = :v", v=version)
        conn.commit()
        cursor.close()


@pytest.fixture
def otra_org_e11(conn, portal_y_org, dos_clientes_v2):
    """Una persona de OTRA organización con membresía de gerente_pais en el cliente A (la base no lo
    impide: la FK de `membresia` solo mira usuario y cliente). Nunca debe ser asignable en A."""
    cursor = conn.cursor()
    cursor.execute("INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
                   "VALUES ('test_org_e11', 'Otra org E11', 'SV', 'America/El_Salvador')")
    cursor.execute("INSERT INTO usuario (usuario_id, org_id, email, nombre) "
                   "VALUES ('test_e11_ajeno', 'test_org_e11', 'ajeno-e11@test.invalid', 'Ajeno E11')")
    cursor.execute("INSERT INTO membresia (usuario_id, cliente_id, rol) VALUES ('test_e11_ajeno', :c, 'gerente_pais')",
                   c=dos_clientes_v2[0])
    conn.commit()
    try:
        yield "test_e11_ajeno"
    finally:
        cursor.execute("DELETE FROM membresia WHERE usuario_id = 'test_e11_ajeno'")
        cursor.execute("DELETE FROM usuario WHERE usuario_id = 'test_e11_ajeno'")
        cursor.execute("DELETE FROM organizacion WHERE org_id = 'test_org_e11'")
        conn.commit()
        cursor.close()


def _personas_e11(conn, org, cliente_a, cliente_b):
    """Altas por el servicio real: quién puede y quién no ser responsable en A."""
    from src.identidad import service

    p = {
        "gp": service.alta_usuario(conn, org, "gp-e11@test.invalid", "gerente_pais", cliente_a, nombre="Zoe Gerente"),
        "ev": service.alta_usuario(conn, org, "ev-e11@test.invalid", "equipo_ventas", cliente_a),  # sin nombre
        "dc": service.alta_usuario(conn, org, "dc-e11@test.invalid", "director_comercial", nombre="Ana Directora"),
        "ceo": service.alta_usuario(conn, org, "ceo-e11@test.invalid", "ceo", nombre="Carla Ceo"),
        "admin": service.alta_usuario(conn, org, "admin-e11@test.invalid", "admin_plataforma", nombre="Admin E11"),
        "gp_b": service.alta_usuario(conn, org, "gpb-e11@test.invalid", "gerente_pais", cliente_b, nombre="Beto B"),
        "inactivo": service.alta_usuario(conn, org, "baja-e11@test.invalid", "gerente_pais", cliente_a,
                                         nombre="Baja E11"),
    }
    cursor = conn.cursor()
    cursor.execute("UPDATE usuario SET activo = 0 WHERE usuario_id = :u", u=p["inactivo"])
    cursor.close()
    conn.commit()
    return p


def _fila_op_e11(conn, cliente, gid):
    cursor = conn.cursor()
    cursor.execute("SELECT estado, origen, responsable_usuario_id FROM oportunidad_estado "
                   "WHERE cliente_id = :c AND proceso_gid = :g", c=cliente, g=gid)
    fila = cursor.fetchone()
    cursor.close()
    return fila


def _eventos_e11(conn, cliente, gid):
    cursor = conn.cursor()
    cursor.execute("SELECT estado_anterior, estado_nuevo, usuario_id, nota FROM oportunidad_evento "
                   "WHERE cliente_id = :c AND proceso_gid = :g ORDER BY evento_id", c=cliente, g=gid)
    filas = cursor.fetchall()
    cursor.close()
    return filas


def _auditoria_e11(conn, org, *acciones):
    cursor = conn.cursor()
    cursor.execute("SELECT accion, usuario_id, recurso, ip_hash FROM auditoria WHERE org_id = :o "
                   "ORDER BY auditoria_id", o=org)
    filas = [f for f in cursor.fetchall() if not acciones or f[0] in acciones]
    cursor.close()
    return filas


def test_e11_lecturas_del_tablero_contra_oracle(conn, tablero_e11):
    """Todo el SQL de `analytics.tablero` contra el motor real: ventana del rango (con la oportunidad
    activa vieja), tope y `truncado`, JSON nativo de marcas/matriz/motivos, `JSON_TABLE`, filtro por
    cliente en cada lectura, y ningún destinatario en lo que se devuelve."""
    from src.analytics import tablero

    g, a, b = tablero_e11["gids"], tablero_e11["a"], tablero_e11["b"]

    rango = tablero.procesos_del_rango(conn, a, 90)
    por_gid = {f["proceso_gid"]: f for f in rango["filas"]}
    # sin el de B; con la activa de hace 400 días; SIN los no relevantes (viven en `descartados_del_rango`)
    assert set(por_gid) == {g["go"], g["est"]}
    assert (rango["total"], rango["truncado"]) == (2, False)
    assert rango["filas"][0]["proceso_gid"] == g["go"]  # el único con cierre: primero
    go = por_gid[g["go"]]
    assert go["marcas"] == ["Dell", "HPE"] and go["match"] == 90 and go["es_relevante"] is True
    assert go["monto_estimado"] == 1000.0 and go["monto_usd"] is None and go["token"] == ""
    assert go["url"] == "https://example.test/go" and go["op_estado"] is None
    est = por_gid[g["est"]]
    assert est["op_estado"] == "EVALUANDO" and est["token"] and est["monto_usd"] == 2500.0
    assert isinstance(est["matriz_riesgos"], dict) and est["matriz_riesgos"]["items"]

    corto = tablero.procesos_del_rango(conn, a, 90, limite=1)
    assert (len(corto["filas"]), corto["total"], corto["truncado"]) == (1, 2, True)

    clasif = tablero.clasificar(rango["filas"], None, datetime.now(timezone.utc))
    grupo = {f["proceso_gid"]: f for f in clasif["filas"]}
    assert grupo[g["go"]]["grupo"] == "decidir" and grupo[g["go"]]["dias_restantes"] == 3
    assert grupo[g["go"]]["urgencia"] == "rojo" and grupo[g["go"]]["competible"] is None  # sin matriz: no bloquea
    assert (grupo[g["est"]]["grupo"], grupo[g["est"]]["competible"], grupo[g["est"]]["motivo_bloqueo"]) == \
        ("no_competible", False, "proveedor_unico")
    # los motivos de descarte se comprueban en `descartados_del_rango` (E11.1: ya no entran al rango del tablero)
    desc = {f["proceso_gid"]: f for f in tablero.descartados_del_rango(conn, a, 90)["filas"]}
    assert desc[g["desc"]]["motivo_descarte"] == "filtro de texto" and desc[g["desc2"]]["motivo_descarte"] == "triaje NO-GO"
    assert clasif["conteos"] == {"decidir": 1, "no_competible": 1, "baja": 0, "descartado": 0, "pendiente": 0}

    resumen = tablero.votos_resumen(conn, a, 90)
    assert resumen == {"relevante": 2, "irrelevante": 2,
                       "motivos": [("fuera_de_rubro", 1), ("otro", 1), ("precio", 1)]}
    por_proceso = tablero.votos_por_proceso(conn, a, [g["go"], g["desc"], g["est"], g["otro"]])
    assert por_proceso == {
        g["go"]: {"relevante": 1, "irrelevante": 1, "motivos": ["fuera_de_rubro", "precio"]},
        g["desc"]: {"relevante": 1, "irrelevante": 0, "motivos": []},
        g["est"]: {"relevante": 0, "irrelevante": 1, "motivos": ["otro"]},
    }  # el gid de B no aparece aunque se pida
    assert "secreto" not in repr(resumen) + repr(por_proceso) and "<script>" not in repr(por_proceso)

    # E11.1: revisados = MAX diario de `ejecucion` (la repetición de 5 del miércoles no suma); relevantes = solo
    # lo relevante (los 2 descartados ya no cuentan), en la semana de su día de corrida.
    tendencia = tablero.tendencia_semanal(conn, a, semanas=12)
    assert len(tendencia) == 12 and [t["semana"] for t in tendencia] == sorted(t["semana"] for t in tendencia)
    assert (tendencia[-1]["revisados"], tendencia[-1]["relevantes"]) == (40, 0)
    assert (tendencia[-2]["revisados"], tendencia[-2]["relevantes"]) == (20, 1)  # «go», el miércoles pasado
    assert sum(t["revisados"] for t in tendencia) == 60 and sum(t["relevantes"] for t in tendencia) == 1
    assert [t["revisados"] for t in tablero.tendencia_semanal(conn, b, semanas=12)][-2:] == [20, 40]  # global

    assert tablero.marcas_top(conn, a, 90) == [{"marca": "Dell", "conteo": 1}, {"marca": "HPE", "conteo": 1}]
    assert tablero.marcas_top(conn, a, 90, limite=1) == [{"marca": "Dell", "conteo": 1}]

    assert tablero.pipeline_en_juego(conn, a) == {"monto_usd": 2500.0, "activas": 1, "con_monto": 1}
    assert tablero.pipeline_en_juego(conn, b) == {"monto_usd": None, "activas": 1, "con_monto": 0}  # nunca un 0

    assert tablero.estancadas(conn, a) == 1
    assert tablero.estancadas(conn, a, dias_sin_mover=30) == 0
    assert tablero.estancadas(conn, b) == 0  # OFERTADO no cuenta


def test_e11_usuarios_asignables_contra_oracle(
    conn, portal_y_org, dos_clientes_v2, limpiar_identidad, otra_org_e11
):
    """Activos, de la misma org, con rol que cambia estado y acceso a ESE cliente; ordenados por nombre y
    con la parte local del correo si no hay nombre."""
    from src.identidad import repo as identidad_repo

    org = portal_y_org["org_id"]
    a, b = dos_clientes_v2
    p = _personas_e11(conn, org, a, b)

    assert identidad_repo.usuarios_asignables(conn, org, a) == [
        {"usuario_id": p["dc"], "nombre": "Ana Directora"},   # director_comercial: todos los países
        {"usuario_id": p["ev"], "nombre": "ev-e11"},          # sin nombre: la parte local del correo
        {"usuario_id": p["gp"], "nombre": "Zoe Gerente"},
    ]  # fuera: ceo y admin (no cambian estado), gp de B, el dado de baja y el de la otra org
    assert identidad_repo.usuarios_asignables(conn, org, b) == [
        {"usuario_id": p["dc"], "nombre": "Ana Directora"}, {"usuario_id": p["gp_b"], "nombre": "Beto B"}]
    assert identidad_repo.usuarios_asignables(conn, "test_org_e11", a) == []  # el cliente no es de esa org
    assert identidad_repo.usuarios_asignables(conn, org, "no_existe") == []

    cursor = conn.cursor()
    cursor.execute("UPDATE cliente SET activo = 0 WHERE cliente_id = :c", c=a)
    conn.commit()
    try:
        assert identidad_repo.usuarios_asignables(conn, org, a) == []  # país dado de baja: nadie
    finally:
        cursor.execute("UPDATE cliente SET activo = 1 WHERE cliente_id = :c", c=a)
        conn.commit()
        cursor.close()


def test_e11_asignar_responsable_contra_oracle(
    conn, portal_y_org, tablero_e11, limpiar_identidad, otra_org_e11
):
    from dataclasses import replace

    from src.identidad.alcance import resolver_alcance
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    org = portal_y_org["org_id"]
    g, a, b = tablero_e11["gids"], tablero_e11["a"], tablero_e11["b"]
    p = _personas_e11(conn, org, a, b)
    alcance = resolver_alcance(conn, p["gp"])

    # Sin fila: asignar crea la oportunidad NUEVO (MANUAL) con su evento y su auditoría.
    assert service.asignar_responsable(conn, alcance, a, g["go"], p["ev"], "", ip_hash="h-e11") == service.OK
    conn.commit()
    assert _fila_op_e11(conn, a, g["go"]) == ("NUEVO", "MANUAL", p["ev"])
    assert _eventos_e11(conn, a, g["go"]) == [("NUEVO", "NUEVO", p["gp"], "Asignada a ev-e11")]
    assert _auditoria_e11(conn, org, "RESPONSABLE_ASIGNADO") == [
        ("RESPONSABLE_ASIGNADO", p["gp"], f"cliente={a};proceso={g['go']};responsable={p['ev']}", "h-e11")]
    token = orepo.obtener(conn, a, g["go"])["token"]

    # Formulario viejo (sin token) → conflicto; mismo responsable → OK sin evento nuevo.
    assert service.asignar_responsable(conn, alcance, a, g["go"], p["gp"], "") == service.CONFLICTO
    assert service.asignar_responsable(conn, alcance, a, g["go"], p["ev"], token) == service.OK
    assert len(_eventos_e11(conn, a, g["go"])) == 1

    # Responsables inválidos: no escriben.
    for malo in (p["ceo"], p["admin"], p["gp_b"], p["inactivo"], otra_org_e11, "no-existe"):
        assert service.asignar_responsable(conn, alcance, a, g["go"], malo, token) == service.RESPONSABLE_INVALIDO, malo
    assert _fila_op_e11(conn, a, g["go"])[2] == p["ev"]

    # Reasignar renueva el token: el anterior queda vencido.
    assert service.asignar_responsable(conn, alcance, a, g["go"], p["dc"], token) == service.OK
    assert service.asignar_responsable(conn, alcance, a, g["go"], p["gp"], token) == service.CONFLICTO
    token = orepo.obtener(conn, a, g["go"])["token"]

    # Quitar (vacío o espacios) y quitar otra vez (nada cambia: sin evento).
    assert service.asignar_responsable(conn, alcance, a, g["go"], "  ", token) == service.OK
    conn.commit()
    assert _fila_op_e11(conn, a, g["go"]) == ("NUEVO", "MANUAL", None)
    assert _eventos_e11(conn, a, g["go"])[-1] == ("NUEVO", "NUEVO", p["gp"], "Responsable quitado")
    n_eventos = len(_eventos_e11(conn, a, g["go"]))
    assert service.asignar_responsable(conn, alcance, a, g["go"], None, orepo.obtener(conn, a, g["go"])["token"]) \
        == service.OK
    assert len(_eventos_e11(conn, a, g["go"])) == n_eventos

    # Una oportunidad ya en EVALUANDO conserva su estado.
    token_est = orepo.obtener(conn, a, g["est"])["token"]
    assert service.asignar_responsable(conn, alcance, a, g["est"], p["ev"], token_est) == service.OK
    conn.commit()
    assert _fila_op_e11(conn, a, g["est"]) == ("EVALUANDO", "MANUAL", p["ev"])
    assert _eventos_e11(conn, a, g["est"]) == [("EVALUANDO", "EVALUANDO", p["gp"], "Asignada a ev-e11")]

    # Ajeno, sin permiso o simulado: no escriben.
    antes = (_fila_op_e11(conn, b, g["otro"]), _eventos_e11(conn, b, g["otro"]))
    assert service.asignar_responsable(conn, alcance, a, g["otro"], p["ev"], "") == service.NO_ENCONTRADO
    assert service.asignar_responsable(conn, alcance, b, g["otro"], p["gp_b"], "") == service.NO_ENCONTRADO
    assert (_fila_op_e11(conn, b, g["otro"]), _eventos_e11(conn, b, g["otro"])) == antes
    ceo = resolver_alcance(conn, p["ceo"])
    assert ceo.puede_ver(a) and service.asignar_responsable(conn, ceo, a, g["go"], p["ev"], "") == service.NO_ENCONTRADO
    simulado = replace(alcance, simulado="gerente_pais")
    n_audit = len(_auditoria_e11(conn, org))
    assert service.asignar_responsable(conn, simulado, a, g["est"], p["gp"], orepo.obtener(conn, a, g["est"])["token"]) \
        == service.SIMULACION
    assert _fila_op_e11(conn, a, g["est"])[2] == p["ev"] and len(_auditoria_e11(conn, org)) == n_audit


def test_e11_asignar_y_recuperar_hacen_rollback_completo(
    conn, portal_y_org, tablero_e11, limpiar_identidad, monkeypatch
):
    """Estado + evento + auditoría entran juntos o no entra nada (el rollback es el de `db.connection`)."""
    from src.identidad.alcance import resolver_alcance
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    org = portal_y_org["org_id"]
    g, a, b = tablero_e11["gids"], tablero_e11["a"], tablero_e11["b"]
    p = _personas_e11(conn, org, a, b)
    alcance = resolver_alcance(conn, p["gp"])

    def boom(*_a, **_k):
        raise RuntimeError("falla a mitad de la transacción")

    with monkeypatch.context() as m:
        m.setattr(orepo, "insertar_evento", boom)
        with pytest.raises(RuntimeError):
            service.asignar_responsable(conn, alcance, a, g["go"], p["ev"], "")
        conn.rollback()
        with pytest.raises(RuntimeError):
            service.recuperar_descartado(conn, alcance, a, g["desc"], "")
        conn.rollback()
    with monkeypatch.context() as m:
        m.setattr(service.identidad, "registrar_auditoria", boom)
        with pytest.raises(RuntimeError):
            service.asignar_responsable(conn, alcance, a, g["est"], p["ev"], orepo.obtener(conn, a, g["est"])["token"])
        conn.rollback()
        with pytest.raises(RuntimeError):
            service.recuperar_descartado(conn, alcance, a, g["desc2"], "")
        conn.rollback()

    assert orepo.obtener(conn, a, g["go"]) is None and orepo.obtener(conn, a, g["desc"]) is None
    assert orepo.obtener(conn, a, g["desc2"]) is None
    assert _fila_op_e11(conn, a, g["est"]) == ("EVALUANDO", "MANUAL", None)
    assert _eventos_e11(conn, a, g["est"]) == []
    assert _auditoria_e11(conn, org, "RESPONSABLE_ASIGNADO", "DESCARTADO_RECUPERADO") == []


def test_e11_recuperar_descartado_contra_oracle(conn, portal_y_org, tablero_e11, limpiar_identidad):
    from dataclasses import replace

    from src.analytics import tablero
    from src.identidad.alcance import resolver_alcance
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    org = portal_y_org["org_id"]
    g, a, b = tablero_e11["gids"], tablero_e11["a"], tablero_e11["b"]
    p = _personas_e11(conn, org, a, b)
    alcance = resolver_alcance(conn, p["gp"])
    cursor = conn.cursor()
    sql_cp = ("SELECT estado, es_relevante, triaje, match_tecnico, razon, actualizado_en FROM cliente_proceso "
              "WHERE cliente_id = :c AND proceso_gid = :g")
    cursor.execute(sql_cp, c=a, g=g["desc"])
    clasificacion_antes = cursor.fetchone()

    assert service.recuperar_descartado(conn, alcance, a, g["desc"], "", ip_hash="h-e11") == service.OK
    conn.commit()
    assert _fila_op_e11(conn, a, g["desc"]) == ("NUEVO", "MANUAL", None)
    assert _eventos_e11(conn, a, g["desc"]) == [(None, "NUEVO", p["gp"], "Recuperada de descartados de AMALIA")]
    assert _auditoria_e11(conn, org, "DESCARTADO_RECUPERADO") == [
        ("DESCARTADO_RECUPERADO", p["gp"], f"cliente={a};proceso={g['desc']}", "h-e11")]
    cursor.execute(sql_cp, c=a, g=g["desc"])
    assert cursor.fetchone() == clasificacion_antes  # la clasificación de AMALIA no se toca

    # Ya recuperado, relevante, ya trabajado, token de una fila que no existe, ajeno y simulado.
    assert service.recuperar_descartado(conn, alcance, a, g["desc"], "") == service.YA_RECUPERADO
    assert service.recuperar_descartado(conn, alcance, a, g["go"], "") == service.NO_RECUPERABLE
    assert service.recuperar_descartado(conn, alcance, a, g["est"], "") == service.YA_RECUPERADO
    assert service.recuperar_descartado(conn, alcance, a, g["desc2"], "2026-01-01T00:00:00.000000") == \
        service.CONFLICTO
    assert service.recuperar_descartado(conn, alcance, a, g["otro"], "") == service.NO_ENCONTRADO
    assert service.recuperar_descartado(conn, alcance, b, g["otro"], "") == service.NO_ENCONTRADO
    assert service.recuperar_descartado(conn, replace(alcance, simulado="gerente_pais"), a, g["desc2"], "") == \
        service.SIMULACION
    assert orepo.obtener(conn, a, g["desc2"]) is None and orepo.obtener(conn, a, g["go"]) is None
    assert len(_auditoria_e11(conn, org, "DESCARTADO_RECUPERADO")) == 1

    # Entra a la lista de oportunidades y deja de ser «descartado» en el tablero, sin dejar de ser no relevante.
    assert g["desc"] in {f["proceso_gid"] for f in orepo.listar(conn, a)}
    assert g["desc2"] not in {f["proceso_gid"] for f in orepo.listar(conn, a)}
    filas = tablero.clasificar(tablero.procesos_del_rango(conn, a, 90)["filas"], None, datetime.now(timezone.utc))
    rec = next(f for f in filas["filas"] if f["proceso_gid"] == g["desc"])
    assert rec["op_estado"] == "NUEVO" and rec["grupo"] != "descartado" and rec["recuperado"] is True
    assert filas["conteos"]["descartado"] == 0  # el rango del tablero ya no trae descartados sin recuperar
    cursor.close()


# ── E11 de punta a punta con la app FastAPI real ──────────────────────────────

def _app_e11(conn, monkeypatch):
    """`_app_e10` (sesiones directas, get/post con CSRF y Origin reales) + login REAL por código y la
    captura del contexto que recibe `portal_pais.html`."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.web import acceso, csrf, sesion
    from src.web import portal as portal_mod
    from src.web.app import crear_app

    base = _app_e10(conn, monkeypatch)
    enviados, capturas = [], []
    monkeypatch.setattr(acceso.email_sender, "send_access_code",
                        lambda _c, email, codigo, _ttl: enviados.append((email, codigo)))
    original = portal_mod.pagina

    def capturar(request, nombre, status=200, **contexto):
        capturas.append((nombre, contexto))
        return original(request, nombre, status=status, **contexto)

    monkeypatch.setattr(portal_mod, "pagina", capturar)
    cfg = SimpleNamespace(
        enable_portal=True, identidad_secret=_SECRETO_E10, feedback_base_url=_BASE_E10, portal_cliente_id="cli",
        enable_feedback=False, daily_budget_usd=5.0, codigo_ttl_min=10, sesion_ttl_horas=12, sesion_max_dias=7,
        clients_dir="",
    )
    cliente_login = TestClient(crear_app(cfg), base_url=_BASE_E10, follow_redirects=False)
    origen = {"origin": _BASE_E10}

    def login(email):
        r = cliente_login.post("/login", data={"email": email, "csrf": csrf.emitir(_SECRETO_E10, "anon")},
                               headers=origen)
        assert r.status_code == 200
        codigo = [k for e, k in enviados if e == email][-1]
        ok = cliente_login.post("/login/code", data={"email": email, "codigo": codigo,
                                                        "csrf": csrf.emitir(_SECRETO_E10, "anon")}, headers=origen)
        assert ok.status_code == 303 and ok.headers["location"] == "/portal"
        token = ok.cookies.get(sesion.COOKIE) or ok.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        cliente_login.cookies.clear()
        return token

    base.login = login
    base.capturas = capturas
    return base


def _seccion_e11(html, desde, hasta):
    inicio = html.index(desde)
    return html[inicio:html.index(hasta, inicio)]


def _ruta_e11(gid, accion):
    from urllib.parse import quote
    accion = {"estado": "status", "responsable": "owner", "recuperar": "recover"}[accion]
    return f"/portal/opportunity/{quote(gid, safe='')}/{accion}"


def test_e11_vista_de_pais_de_punta_a_punta_contra_oracle(
    conn, portal_y_org, tablero_e11, limpiar_identidad, otra_org_e11, monkeypatch
):
    """gerente_pais real (login por código) → ve el tablero y los descartados → asigna responsable y
    recupera un descartado (CSRF + Origin reales) → todo en la base y auditado. «Ver como» gerente_pais:
    ve lo mismo y sus POST nuevos no escriben. Un gerente de OTRO país: 404 y auditado."""
    org = portal_y_org["org_id"]
    g, a, b = tablero_e11["gids"], tablero_e11["a"], tablero_e11["b"]
    p = _personas_e11(conn, org, a, b)
    app = _app_e11(conn, monkeypatch)
    token = app.login("gp-e11@test.invalid")

    r = app.get(token, f"/portal?client={a}")
    assert r.status_code == 200
    tablero_html = _seccion_e11(r.text, 'id="h-tablero"', 'id="h-clasif"')
    clasif_html = _seccion_e11(r.text, 'id="h-clasif"', 'id="h-votos"')
    assert _TITULOS_E11["go"] in tablero_html and _TITULOS_E11["est"] in tablero_html
    assert _TITULOS_E11["desc"] in clasif_html and _TITULOS_E11["desc2"] in clasif_html
    assert _TITULOS_E11["desc"] not in tablero_html and _TITULOS_E11["otro"] not in r.text
    assert "ev-e11" in tablero_html and "Zoe Gerente" in tablero_html  # asignables de la misma org y país
    for ajeno in ("Ajeno E11", "Beto B", "Carla Ceo", "Baja E11"):
        assert ajeno not in r.text, ajeno
    assert "secreto-" not in r.text and "<script>alert" not in r.text  # ni destinatarios ni comentarios
    assert "disabled" not in _seccion_e11(r.text, 'id="h-tablero"', 'id="h-votos"')  # sesión real: editable

    # CSRF malo: 403 y nada escrito.
    r = app.post(token, _ruta_e11(g["go"], "responsable"),
                 {"cliente": a, "responsable": p["ev"], "actualizado_en": "", "dias": "90"}, csrf_ok=False)
    assert r.status_code == 403 and _fila_op_e11(conn, a, g["go"]) is None

    # Asignar (el contrato de la ruta: campo `responsable`).
    r = app.post(token, _ruta_e11(g["go"], "responsable"),
                 {"cliente": a, "responsable": p["ev"], "actualizado_en": "", "dias": "90"})
    assert r.status_code == 303 and "notice=asignado" in r.headers["location"], r.headers.get("location")
    assert _fila_op_e11(conn, a, g["go"]) == ("NUEVO", "MANUAL", p["ev"])
    audit = _auditoria_e11(conn, org, "RESPONSABLE_ASIGNADO")
    assert [(u, rec) for _a, u, rec, _ip in audit] == [
        (p["gp"], f"cliente={a};proceso={g['go']};responsable={p['ev']}")]
    assert audit[0][3]  # con el hash de la IP

    # Recuperar.
    r = app.post(token, _ruta_e11(g["desc"], "recuperar"), {"cliente": a, "actualizado_en": "", "dias": "90"})
    assert r.status_code == 303 and "notice=recuperado" in r.headers["location"]
    assert _fila_op_e11(conn, a, g["desc"]) == ("NUEVO", "MANUAL", None)
    assert [(u, rec) for _a, u, rec, _ip in _auditoria_e11(conn, org, "DESCARTADO_RECUPERADO")] == [
        (p["gp"], f"cliente={a};proceso={g['desc']}")]
    r = app.post(token, _ruta_e11(g["desc"], "recuperar"), {"cliente": a, "actualizado_en": "", "dias": "90"})
    assert r.status_code == 303 and "notice=no_recuperable" in r.headers["location"]

    r = app.get(token, f"/portal?client={a}&notice=recuperado")
    assert r.status_code == 200
    assert _TITULOS_E11["desc"] in _seccion_e11(r.text, 'id="h-tablero"', 'id="h-clasif"')
    assert _TITULOS_E11["desc"] not in _seccion_e11(r.text, 'id="h-clasif"', 'id="h-votos"')

    # IDOR: el proceso de B por el cliente A → 404 auditado, sin escribir.
    r = app.post(token, _ruta_e11(g["otro"], "responsable"),
                 {"cliente": a, "responsable": p["ev"], "actualizado_en": "", "dias": "90"})
    assert r.status_code == 404
    assert ("ACCESO_FUERA_DE_ALCANCE", p["gp"], f"responsable:{g['otro']} cliente={a}") in \
        [f[:3] for f in _auditoria_e11(conn, org)]
    assert _fila_op_e11(conn, b, g["otro"]) == ("OFERTADO", "MANUAL", None)

    # «Ver como» gerente_pais de A: misma página, controles deshabilitados, POST nuevos sin escribir.
    t_admin = app.nueva_sesion(p["admin"])
    assert app.ver_como(t_admin, f"gerente_pais|{a}").status_code == 303
    r = app.get(t_admin, f"/portal?client={a}")
    assert r.status_code == 200 and "Viendo como" in r.text
    assert _TITULOS_E11["go"] in r.text and _TITULOS_E11["desc2"] in r.text
    assert '<fieldset class="solo-vista" disabled>' in r.text
    antes = (_conteo_pipeline_e10(conn, a), _fila_op_e11(conn, a, g["go"]), _fila_op_e11(conn, a, g["desc2"]))
    n_audit = len(_auditoria_e11(conn, org, "RESPONSABLE_ASIGNADO", "DESCARTADO_RECUPERADO"))
    token_go = next(t["token"] for c in app.capturas[-1][1]["tablero"]["columnas"] for t in c["tarjetas"]
                    if t["gid"] == g["go"])
    for ruta, datos in (
        (_ruta_e11(g["go"], "responsable"), {"responsable": "", "actualizado_en": token_go}),
        (_ruta_e11(g["desc2"], "recuperar"), {"actualizado_en": ""}),
    ):
        r = app.post(t_admin, ruta, {"cliente": a, "dias": "90", **datos})
        assert r.status_code == 303 and "notice=simulacion" in r.headers["location"], ruta
    assert (_conteo_pipeline_e10(conn, a), _fila_op_e11(conn, a, g["go"]), _fila_op_e11(conn, a, g["desc2"])) == antes
    assert len(_auditoria_e11(conn, org, "RESPONSABLE_ASIGNADO", "DESCARTADO_RECUPERADO")) == n_audit

    # gerente_pais de OTRO país: ni la página de A ni sus escrituras; ajeno = 404 auditado.
    t_b = app.nueva_sesion(p["gp_b"])
    assert app.get(t_b, f"/portal?client={a}").status_code == 404
    r = app.post(t_b, _ruta_e11(g["go"], "responsable"),
                 {"cliente": a, "responsable": p["gp_b"], "actualizado_en": "", "dias": "90"})
    assert r.status_code == 404
    r = app.post(t_b, _ruta_e11(g["desc2"], "recuperar"), {"cliente": b, "actualizado_en": "", "dias": "90"})
    assert r.status_code == 404  # su país, pero el proceso es de A
    fuera = [rec for acc, u, rec, _ip in _auditoria_e11(conn, org, "ACCESO_FUERA_DE_ALCANCE") if u == p["gp_b"]]
    assert fuera.count(f"cliente={a}") == 2 and f"recuperar:{g['desc2']} cliente={b}" in fuera
    assert _fila_op_e11(conn, a, g["go"]) == ("NUEVO", "MANUAL", p["ev"]) and _fila_op_e11(conn, a, g["desc2"]) is None


def test_e11_la_pagina_real_respeta_estado_cierre_y_formularios_contra_oracle(
    conn, portal_y_org, tablero_e11, limpiar_identidad, monkeypatch
):
    """El contrato entre `analytics.tablero` (lo que devuelve la base), `web.portal` (lo que lee) y las
    plantillas (lo que envían), sin stubs: una oportunidad en EVALUANDO va a su columna, el cierre de
    mañana-pasado entra al calendario, la tarjeta «Para decidir» no imprime un dict y el `<select>` del
    responsable envía el campo que la ruta lee."""
    org = portal_y_org["org_id"]
    g, a, b = tablero_e11["gids"], tablero_e11["a"], tablero_e11["b"]
    p = _personas_e11(conn, org, a, b)
    app = _app_e11(conn, monkeypatch)
    token = app.nueva_sesion(p["gp"])

    r = app.get(token, f"/portal?client={a}")
    assert r.status_code == 200
    nombre, ctx = app.capturas[-1]
    assert nombre == "portal_pais.html"
    columnas = {c["clave"]: {t["gid"] for t in c["tarjetas"]} for c in ctx["tablero"]["columnas"]}
    assert g["est"] in columnas["evaluando"], columnas
    assert g["go"] in columnas["nuevo"], columnas
    # E11.1: el contexto ya no lleva la lista de días; el calendario se lee del HTML que arma `barras_dias`.
    calendario = str(ctx["cierres"]["svg"])
    assert "Cierres de los próximos 14 días: 1 en total" in calendario, calendario  # «go» cierra en 3 días
    assert "1 cierra el" in calendario and set(ctx["cierres"]) == {"svg", "dia"}
    assert ctx["kpis"]["estancadas"]["n"] == 1
    assert ctx["kpis"]["pipeline"] == {"monto_usd": 2500.0, "activas": 1, "con_monto": 1}
    assert ctx["kpis"]["decidir"]["n"] == 1
    assert "&#39;gid&#39;" not in r.text and "{&#39;" not in r.text  # ningún dict de Python en el HTML

    formulario = re.search(r'action="/portal/opportunity/[^"]+/owner".*?<select name="([^"]+)"', r.text, re.S)
    assert formulario, "la tarjeta no trae el formulario de responsable"
    campo = formulario.group(1)
    r = app.post(token, _ruta_e11(g["go"], "responsable"),
                 {"cliente": a, campo: p["ev"], "actualizado_en": "", "dias": "90"})
    assert r.status_code == 303
    assert _fila_op_e11(conn, a, g["go"]) == ("NUEVO", "MANUAL", p["ev"]), f"el <select> envía `{campo}`"


# ═══════════════════════════════════════════════════════════════════════════
# E11.1 — el pipeline guarda también los NO relevantes, relevancia estricta (aptos / revisados) y
# descartados persistidos con «Recuperar»
# ═══════════════════════════════════════════════════════════════════════════
# Mismo patrón que E11: INSERTs inline para los datos, funciones REALES para el resto. `registrar_clasificados`
# corre con el pool REAL de `src.memoria.db` (su commit y su rollback son parte de lo que se prueba). Las fechas
# se arman en SQL contra el reloj de El Salvador para que el mapeo «día de corrida» (`fecha_pub_utc + 11 h`) sea
# determinista a cualquier hora en que corra el CI.

def _pub_e111(dias_atras: int, segundos: int = 3600) -> str:
    """`fecha_pub_utc` como texto SQL: medianoche (hora SV) de hace `dias_atras` días + `segundos`, leída como
    UTC. Con el valor por defecto (01:00 UTC) el proceso cae en la corrida de ese mismo día."""
    return f"TRUNC({_AHORA_SV_E111}) - {int(dias_atras)} + NUMTODSINTERVAL({int(segundos)}, 'SECOND')"


def _insertar_e111(cursor, portal, cliente, ref, pub_sql, *, estado="DESCUBIERTO", rel=1, score=0.9,
                   triaje=None, institucion="INSTITUCION E11.1", limite_sql="NULL", clasificado_sql="NULL"):
    """Un `proceso` + su `cliente_proceso`. Devuelve el `proceso_gid`."""
    gid = f"{portal}:{ref}"
    cursor.execute(
        "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, titulo, categoria_raw, "
        f"fecha_pub_utc, fecha_limite_utc, tz_portal, url_portal) VALUES (:g, :p, :r, :i, :t, 'infraestructura', "
        f"{pub_sql}, {limite_sql}, 'America/El_Salvador', 'https://example.test/e111')",
        g=gid, p=portal, r=ref, i=institucion, t=f"Titulo {ref}")
    cursor.execute(
        "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, es_relevante, triaje, match_tecnico, "
        f"match_categoria, factibilidad, clasificado_en) VALUES (:c, :g, :e, :rel, :tri, :s, :s, :s, {clasificado_sql})",
        dict(c=cliente, g=gid, e=estado, rel=rel, tri=triaje, s=score))
    return gid


def _ejecucion_e111(cursor, ts_sql, revisados, version, success=1):
    cursor.execute(f"INSERT INTO ejecucion (run_timestamp, success, procesos_revisados, version) "
                   f"VALUES ({ts_sql}, :ok, :n, :v)", ok=success, n=revisados, v=version)


def _clasificado_e111(ref, *, relevante=None, score=0.0, razon="", fecha_pub=None, fecha_limite=None,
                      titulo=None, tipo_actividad="Servicios de informatica; Equipo de computo"):
    """`ProcesoClasificado` como lo devuelve `classify_procesos`. `relevante=None` = «sin clasificar»: el
    `from_proceso` tal cual que deja el motor cuando el LLM falla (scores en 0, sin razón ni categoría)."""
    from src.scraper.models import Proceso, ProcesoClasificado

    proceso = Proceso(
        institucion="MINISTERIO E11.1", descripcion=titulo or f"Proceso {ref}",
        fecha_publicacion=fecha_pub or datetime.now(timezone.utc).replace(tzinfo=None),
        url_directa=f"https://example.test/{ref}", numero_referencia=ref,
        forma_contratacion="Licitación Pública", tipo_actividad=tipo_actividad, fecha_limite=fecha_limite,
    )
    c = ProcesoClasificado.from_proceso(proceso)
    if relevante is not None:
        c.es_relevante = relevante
        c.match_tecnico = c.match_categoria = c.factibilidad = score
        c.razon = razon or ("encaja con el portafolio" if relevante else "fuera del portafolio")
        c.categoria_detectada = "infraestructura" if relevante else "otro"
    return c


def _cp_e111(conn, cliente, gid):
    cursor = conn.cursor()
    cursor.execute("SELECT estado, es_relevante, clasificado_en, razon FROM cliente_proceso "
                   "WHERE cliente_id = :c AND proceso_gid = :g", c=cliente, g=gid)
    fila = cursor.fetchone()
    cursor.close()
    return fila


def _borrar_procesos_e111(conn, gids, version=None):
    conn.rollback()
    cursor = conn.cursor()
    for gid in gids:
        cursor.execute("DELETE FROM oportunidad_evento WHERE proceso_gid = :g", g=gid)
        cursor.execute("DELETE FROM oportunidad_estado WHERE proceso_gid = :g", g=gid)
        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=gid)  # cascada: cliente_proceso, analisis
    if version:
        cursor.execute("DELETE FROM ejecucion WHERE version = :v", v=version)
    conn.commit()
    cursor.close()


@pytest.fixture
def cfg_memoria_e111():
    """Memoria + esquema v2 encendidos y el pool REAL de `src.memoria.db` apuntando al Oracle local."""
    import types

    from src.memoria import db as memoria_db

    previo = memoria_db._pool
    memoria_db._pool = None
    yield types.SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, adb_dsn=_DSN, adb_user=_USUARIO,
                                adb_password=_CLAVE, clients_dir="")
    memoria_db.close_pool()
    memoria_db._pool = previo


def test_e111_registrar_clasificados_persiste_todo_sin_pisar_lo_avanzado(
    conn, dos_clientes_reales, cfg_memoria_e111
):
    """(1) Persistencia REAL: relevante y no relevante con `clasificado_en`; el «sin clasificar» se omite; una
    fila ya ANALIZADO conserva su estado y su `es_relevante = 1`; idempotente; `categoria_raw` = `tipo_actividad`;
    flag apagado no escribe."""
    import types

    from src.memoria import repo, service

    a, b = dos_clientes_reales
    sufijo = _uuid.uuid4().hex[:8]
    ref = {k: f"TEST-E111-{k.upper()}-{sufijo}" for k in ("rel", "norel", "sin", "anal")}
    gid = {k: f"{repo.PORTAL_ID_DEFAULT}:{v}" for k, v in ref.items()}
    try:
        # Lo que el drilldown ya llevó a ANALIZADO (relevante) en una corrida anterior.
        repo.upsert_proceso_v2(conn, _clasificado_e111(ref["anal"], relevante=True, score=0.9), a, estado="ANALIZADO")
        conn.commit()

        lote = [_clasificado_e111(ref["rel"], relevante=True, score=0.9),
                _clasificado_e111(ref["norel"], relevante=False, score=0.2),
                _clasificado_e111(ref["sin"]),  # fallo del LLM: no se guarda como «no relevante»
                _clasificado_e111(ref["anal"], relevante=False, score=0.1, razon="hoy no encaja")]
        esperado = {"guardados": 3, "omitidos": 1, "fallidos": 0, "error": False}
        assert service.registrar_clasificados(cfg_memoria_e111, a, lote) == esperado

        rel, norel, anal = (_cp_e111(conn, a, gid[k]) for k in ("rel", "norel", "anal"))
        assert rel[:2] == ("DESCUBIERTO", 1) and isinstance(rel[2], datetime)
        assert norel[:2] == ("DESCUBIERTO", 0) and isinstance(norel[2], datetime)
        assert norel[3] == "fuera del portafolio"
        assert anal[:2] == ("ANALIZADO", 1), "un ANALIZADO no retrocede ni pierde su relevancia"
        assert anal[3] == "hoy no encaja" and isinstance(anal[2], datetime)
        assert _cp_e111(conn, a, gid["sin"]) is None and _cp_e111(conn, b, gid["rel"]) is None
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM proceso WHERE proceso_gid = :g", g=gid["sin"])
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT categoria_raw, titulo FROM proceso WHERE proceso_gid = :g", g=gid["norel"])
        assert cursor.fetchone() == ("Servicios de informatica; Equipo de computo", f"Proceso {ref['norel']}")

        # Idempotente: la segunda corrida actualiza las mismas filas y `clasificado_en` nunca retrocede.
        assert service.registrar_clasificados(cfg_memoria_e111, a, lote) == esperado
        cursor.execute("SELECT COUNT(*) FROM cliente_proceso WHERE cliente_id = :c AND proceso_gid IN "
                       "(:g1, :g2, :g3, :g4)", c=a, g1=gid["rel"], g2=gid["norel"], g3=gid["sin"], g4=gid["anal"])
        assert cursor.fetchone()[0] == 3
        assert _cp_e111(conn, a, gid["rel"])[2] >= rel[2]
        assert _cp_e111(conn, a, gid["norel"])[:2] == ("DESCUBIERTO", 0)
        assert _cp_e111(conn, a, gid["anal"])[:2] == ("ANALIZADO", 1)
        cursor.close()

        # Un DESCUBIERTO que el LLM ahora juzga relevante sí sube (el 0 no se congela).
        subir = [_clasificado_e111(ref["norel"], relevante=True, score=0.85)]
        assert service.registrar_clasificados(cfg_memoria_e111, a, subir)["guardados"] == 1
        assert _cp_e111(conn, a, gid["norel"])[:2] == ("DESCUBIERTO", 1)

        # Flag apagado (o lista vacía): nada.
        apagado = types.SimpleNamespace(**{**vars(cfg_memoria_e111), "enable_esquema_v2": False})
        assert service.registrar_clasificados(apagado, b, lote[:1]) == {"guardados": 0, "omitidos": 0, "fallidos": 0, "error": False}
        assert service.registrar_clasificados(cfg_memoria_e111, b, []) == {"guardados": 0, "omitidos": 0, "fallidos": 0, "error": False}
        assert _cp_e111(conn, b, gid["rel"]) is None
    finally:
        _borrar_procesos_e111(conn, gid.values())


def test_e111_una_fila_invalida_no_tumba_el_lote(conn, dos_clientes_reales, cfg_memoria_e111):
    from src.memoria import repo, service

    a, _ = dos_clientes_reales
    sufijo = _uuid.uuid4().hex[:8]
    bueno = _clasificado_e111(f"TEST-E111-BUENO-{sufijo}", relevante=False, score=0.2)
    malo = _clasificado_e111(f"TEST-E111-MALO-{sufijo}", relevante=True, score=0.9)
    malo.match_tecnico = 85.0  # el LLM respondió en escala 0-100: ORA-01438 en NUMBER(3,2)
    gids = [f"{repo.PORTAL_ID_DEFAULT}:{p.numero_referencia}" for p in (bueno, malo)]
    try:
        resultado = service.registrar_clasificados(cfg_memoria_e111, a, [bueno, malo])
        assert _cp_e111(conn, a, gids[0]) is not None, resultado
    finally:
        _borrar_procesos_e111(conn, gids)


def test_e111_reclasificar_no_borra_la_fecha_limite_del_drilldown(conn, dos_clientes_reales, cfg_memoria_e111):
    from src.memoria import repo, service

    a, _ = dos_clientes_reales
    ref = f"TEST-E111-LIMITE-{_uuid.uuid4().hex[:8]}"
    gid = f"{repo.PORTAL_ID_DEFAULT}:{ref}"
    limite = datetime(2030, 1, 15, 21, 0)
    try:
        con_fecha = _clasificado_e111(ref, relevante=True, score=0.9, fecha_limite=limite)
        repo.upsert_proceso_v2(conn, con_fecha, a, estado="ANALIZADO")  # lo que deja el drilldown
        conn.commit()
        sin_fecha = [_clasificado_e111(ref, relevante=True, score=0.9)]  # el listado: sin fecha_limite
        assert service.registrar_clasificados(cfg_memoria_e111, a, sin_fecha)["guardados"] == 1
        cursor = conn.cursor()
        cursor.execute("SELECT fecha_limite_utc FROM proceso WHERE proceso_gid = :g", g=gid)
        assert cursor.fetchone()[0] == limite
        cursor.close()
    finally:
        _borrar_procesos_e111(conn, [gid])


def test_e111_relevancia_estricta_contra_oracle(conn, portal_y_org, dos_clientes_v2):
    """(2) `pais.relevancia_estricta` REAL: MAX diario con repetición, días sin corrida (o con corrida fallida o
    de 0 revisados) fuera, borde de las 13:00 UTC, apto = match ≥ 80 sin NO-GO, LEAST por día, por cliente."""
    from src.analytics import pais

    a, b = dos_clientes_v2
    portal = portal_y_org["portal_id"]
    sufijo = _uuid.uuid4().hex[:8]
    version = f"test-e111-{sufijo}"
    base = pais.relevancia_estricta(conn, a, 15)
    assert (base["relevantes"], base["aptos"]) == (0, 0)
    if base["revisados"] == 0:  # sin corridas: sin dato, nunca un 0 % falso ni una división por cero
        assert base["pct_aptos"] is None and base["pct_relevantes"] is None

    cursor = conn.cursor()
    gids = []

    def proc(clave, dias_atras, segundos=3600, cliente=a, **kw):
        gids.append(_insertar_e111(cursor, portal, cliente, f"TEST-E111-{clave}-{sufijo}",
                                   _pub_e111(dias_atras, segundos), **kw))

    def corrida(dias_atras, hora, revisados, success=1):
        _ejecucion_e111(cursor, f"TRUNC({_AHORA_SV_E111}) - {dias_atras} + NUMTODSINTERVAL({hora}, 'HOUR')",
                        revisados, version, success)

    try:
        corrida(11, 7, 10)
        corrida(11, 10, 3)               # repetición del mismo día con menos revisados: manda el MAX (10)
        corrida(13, 7, 50, success=0)    # corrida fallida: no cuenta
        corrida(13, 9, 0)                # 0 revisados: no cuenta
        corrida(14, 7, 2)
        # Corrida de hace 11 días: 6 relevantes, 3 aptos.
        proc("apto", 11)
        proc("apto80", 11, score=0.80)   # match 80: apto (el umbral es ≥)
        proc("rel79", 11, score=0.79)    # match 79: relevante, no apto
        proc("rel50", 11, score=0.5)
        proc("nogo", 11, triaje="NO-GO")  # relevante con match alto, pero NO-GO: no apto
        proc("norel", 11, rel=0)          # no relevante con match alto: no cuenta en nada
        proc("borde13", 12, segundos=13 * 3600)        # 13:00:00 UTC del día anterior → corrida de hace 11 días
        proc("borde1259", 12, segundos=13 * 3600 - 1)  # 12:59:59 UTC → corrida de hace 12 días, que no existe
        proc("fallida", 13)                            # su día solo tiene la fallida y la de 0
        for i in range(3):
            proc(f"tope{i}", 14)                       # 3 aptos un día de 2 revisados → LEAST: 2 y 2
        proc("otro", 11, cliente=b)                    # de B: no suma a A
        conn.commit()

        r = pais.relevancia_estricta(conn, a, 15)
        assert r["revisados"] - base["revisados"] == 12, r  # 10 + 2
        assert (r["relevantes"], r["aptos"]) == (8, 5), r
        assert r["aptos"] <= r["relevantes"] <= r["revisados"]
        if base["revisados"] == 0:
            assert (r["revisados"], r["pct_aptos"], r["pct_relevantes"]) == (12, 41.7, 66.7)
        rb = pais.relevancia_estricta(conn, b, 15)
        assert (rb["relevantes"], rb["aptos"], rb["revisados"]) == (1, 1, r["revisados"])
        # Un rango que no alcanza esas corridas no las ve.
        corto = pais.relevancia_estricta(conn, a, 5)
        assert (corto["relevantes"], corto["aptos"]) == (0, 0)
    finally:
        cursor.close()
        _borrar_procesos_e111(conn, gids, version)


def test_e111_descartados_del_rango_contra_oracle(conn, portal_y_org, dos_clientes_v2):
    """(3) `tablero.descartados_del_rango` REAL: vacío → `desde` None; con filas, por match descendente,
    sin los recuperados ni los relevantes ni los fuera de rango; `total` real con tope; `desde` = MIN histórico."""
    from src.analytics import tablero

    a, b = dos_clientes_v2
    portal = portal_y_org["portal_id"]
    sufijo = _uuid.uuid4().hex[:8]
    assert tablero.descartados_del_rango(conn, b, 30) == {"filas": [], "total": 0, "desde": None}
    cursor = conn.cursor()
    gids = {}

    def proc(clave, pub_sql=_pub_e111(1), **kw):
        gids[clave] = _insertar_e111(cursor, portal, a, f"TEST-E111-D-{clave}-{sufijo}", pub_sql, **kw)

    try:
        proc("norel70", rel=0, score=0.7, clasificado_sql=f"TRUNC({_AHORA_SV_E111}) - 2")
        proc("regex60", estado="DESCARTADO_REGEX", rel=None, score=0.6)
        proc("norel50", rel=0, score=0.5, clasificado_sql=f"TRUNC({_AHORA_SV_E111}) - 3")
        proc("recuperado", rel=0, score=0.95, clasificado_sql=f"TRUNC({_AHORA_SV_E111}) - 1")
        cursor.execute("INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen) "
                       "VALUES (:c, :g, 'NUEVO', 'MANUAL')", c=a, g=gids["recuperado"])
        proc("relevante", rel=1, score=0.3)
        proc("viejo", pub_sql="SYSTIMESTAMP - NUMTODSINTERVAL(400, 'DAY')", rel=0, score=0.99,
             clasificado_sql="TIMESTAMP '2026-01-02 03:04:05'")
        conn.commit()

        r = tablero.descartados_del_rango(conn, a, 30)
        assert [f["proceso_gid"] for f in r["filas"]] == [gids["norel70"], gids["regex60"], gids["norel50"]]
        assert [f["match"] for f in r["filas"]] == [70, 60, 50] and r["total"] == 3
        assert [f["motivo_descarte"] for f in r["filas"]] == [
            tablero.MOTIVO_DESCARTE_PORTAFOLIO, tablero.MOTIVO_DESCARTE_REGEX, tablero.MOTIVO_DESCARTE_PORTAFOLIO]
        assert all(set(f) == {"proceso_gid", "numero_referencia", "titulo", "institucion", "match",
                              "motivo_descarte", "token"} and f["token"] == "" for f in r["filas"])
        assert r["desde"] == datetime(2026, 1, 2, 3, 4, 5)  # MIN histórico de los `es_relevante = 0`

        corto = tablero.descartados_del_rango(conn, a, 30, limite=1)
        assert [f["proceso_gid"] for f in corto["filas"]] == [gids["norel70"]] and corto["total"] == 3
        assert tablero.descartados_del_rango(conn, b, 30)["filas"] == []
    finally:
        cursor.close()
        _borrar_procesos_e111(conn, gids.values())


def test_e111_lectores_no_cuentan_los_no_relevantes_contra_oracle(conn, portal_y_org, dos_clientes_v2):
    """(4) Con el pipeline guardando también los NO relevantes, ningún lector de oportunidades los cuenta:
    embudo, severidad, instituciones (v2 y KPI 3), tendencias del RAG, conversión, regional, `listar`; y el tope
    de `procesos_del_rango` pone lo relevante primero aunque cierre después."""
    from src.analytics import pais, regional, tablero
    from src.memoria import rag, repo
    from src.oportunidades import repo as orepo

    a, _ = dos_clientes_v2
    portal = portal_y_org["portal_id"]
    sufijo = _uuid.uuid4().hex[:8]
    inst_rel, inst_no = f"INST-E111-REL-{sufijo}", f"INST-E111-NOREL-{sufijo}"
    cursor = conn.cursor()
    gids = []
    try:
        grel = _insertar_e111(cursor, portal, a, f"TEST-E111-L-REL-{sufijo}", _pub_e111(1), estado="ANALIZADO",
                              triaje="GO", institucion=inst_rel,
                              limite_sql="SYSTIMESTAMP + NUMTODSINTERVAL(20, 'DAY')")
        gids.append(grel)
        cursor.execute("INSERT INTO analisis (cliente_id, proceso_gid, severidad_max) VALUES (:c, :g, 'alta')",
                       c=a, g=grel)
        for i in range(4):
            gids.append(_insertar_e111(cursor, portal, a, f"TEST-E111-L-NO{i}-{sufijo}", _pub_e111(1), rel=0,
                                       score=0.95, institucion=inst_no,
                                       limite_sql="SYSTIMESTAMP + NUMTODSINTERVAL(2, 'DAY')"))
        # Defensivo: aunque un no relevante tuviera análisis, la severidad no lo cuenta.
        cursor.execute("INSERT INTO analisis (cliente_id, proceso_gid, severidad_max) VALUES (:c, :g, 'media')",
                       c=a, g=gids[1])
        conn.commit()

        camino = {e["estado"]: e["conteo"] for e in pais.embudo_pipeline(conn, a, 90)["camino"]}
        assert (camino["DESCUBIERTO"], camino["ANALIZADO"]) == (0, 1)
        assert pais.severidad_riesgos(conn, a, 90) == [{"severidad": "alta", "conteo": 1}]
        assert [f["institucion"] for f in repo.stats_por_institucion_v2(conn, a, 90)] == [inst_rel]
        assert [i["institucion"] for i in pais.instituciones_top(conn, a)] == [inst_rel]
        assert {f["institucion"] for f in rag.tendencias_v2(conn, a)} == {inst_rel}
        assert pais.conversion_comercial(conn, a)["detectadas"] == 1
        assert regional.oportunidades_por_pais(conn, [a]) == [{"cliente_id": a, "conteo": 1}]
        assert [f["proceso_gid"] for f in orepo.listar(conn, a)] == [grel]
        assert orepo.es_descartado_por_amalia(conn, a, gids[1]) is True
        assert orepo.es_descartado_por_amalia(conn, a, grel) is False
        # La función vieja sigue tomando lo persistido como denominador (por eso la vista de país ya no la usa).
        assert {k: v for k, v in pais.pct_relevancia_clasificador(conn, a).items() if k != "valor"} == \
            {"total": 5, "relevantes": 1}

        rango = tablero.procesos_del_rango(conn, a, 90, limite=1)
        assert [f["proceso_gid"] for f in rango["filas"]] == [grel]
        # Los no relevantes sin recuperar ya no entran al rango del tablero (viven en `descartados_del_rango`):
        # el tope de filas no se agota con ellos y «truncado» deja de estar permanentemente en verdadero.
        assert (rango["total"], rango["truncado"]) == (1, False)
    finally:
        cursor.close()
        _borrar_procesos_e111(conn, gids)


def test_e111_vista_de_pais_de_punta_a_punta_contra_oracle(
    conn, portal_y_org, dos_clientes_v2, portal_comprasal_real, limpiar_identidad, monkeypatch
):
    """(5) Del pipeline a la página: `registrar_clasificados` REAL guarda un apto, un vencido y un no relevante;
    login real de un gerente_pais → `GET /portal` 200 con la relevancia estricta en la página, el vencido fuera de
    «para decidir» (y contado aparte), el descartado en la clasificación con «Recuperar» → `POST` 303, fila NUEVO
    y auditoría → el recuperado pasa al tablero."""
    import types

    from src.memoria import repo, service

    org = portal_y_org["org_id"]
    a, b = dos_clientes_v2
    p = _personas_e11(conn, org, a, b)
    app = _app_e11(conn, monkeypatch)  # deja `db.connection` apuntando a `conn` (también para el servicio)
    sufijo = _uuid.uuid4().hex[:8]
    version = f"test-e111-{sufijo}"
    titulos = {"apto": f"Servidores E111 aptos {sufijo}", "vencido": f"Licencias E111 vencidas {sufijo}",
               "desc": f"Mobiliario E111 descartado {sufijo}"}
    refs = {k: f"TEST-E111-W-{k.upper()}-{sufijo}" for k in titulos}
    gids = {k: f"{repo.PORTAL_ID_DEFAULT}:{v}" for k, v in refs.items()}
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT TRUNC({_AHORA_SV_E111}) FROM dual")
        hoy_sv = cursor.fetchone()[0]
        pub = hoy_sv + timedelta(hours=1)  # leído como UTC → cae en la corrida de hoy
        ahora_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        lote = [
            _clasificado_e111(refs["apto"], relevante=True, score=0.9, fecha_pub=pub, titulo=titulos["apto"],
                              fecha_limite=ahora_utc + timedelta(days=5)),
            _clasificado_e111(refs["vencido"], relevante=True, score=0.9, fecha_pub=pub, titulo=titulos["vencido"],
                              fecha_limite=ahora_utc - timedelta(days=2)),
            _clasificado_e111(refs["desc"], relevante=False, score=0.6, fecha_pub=pub, titulo=titulos["desc"]),
        ]
        cfg = types.SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, clients_dir="")
        assert service.registrar_clasificados(cfg, a, lote) == {"guardados": 3, "omitidos": 0, "fallidos": 0, "error": False}
        _ejecucion_e111(cursor, _AHORA_SV_E111, 20, version)
        conn.commit()

        token = app.login("gp-e11@test.invalid")
        r = app.get(token, f"/portal?client={a}&days=30")
        assert r.status_code == 200
        nombre, ctx = app.capturas[-1]
        assert nombre == "portal_pais.html"
        rel = ctx["kpis"]["relevancia"]
        assert (rel["relevantes"], rel["aptos"]) == (2, 2) and rel["revisados"] >= 20, rel  # el vencido es apto
        pct = ("%.1f" % rel["pct_aptos"]).replace(".", ",").replace(",0", "")
        assert f"{pct}%" in r.text and f"{rel['aptos']} aptos de {rel['revisados']} revisados" in r.text
        assert f"{rel['revisados']} revisados → {rel['relevantes']} relevantes" in r.text
        assert ctx["kpis"]["decidir"]["n"] == 1 and ctx["tablero"]["vencidas"] == {"n": 1}
        assert ctx["clasificacion"]["grupos"]["decidir"] == 1
        assert ctx["clasificacion"]["no_relevantes"] == rel["revisados"] - rel["relevantes"]
        tablero_html = _seccion_e11(r.text, 'id="h-tablero"', 'id="h-clasif"')
        clasif_html = _seccion_e11(r.text, 'id="h-clasif"', 'id="h-votos"')
        assert titulos["apto"] in tablero_html and titulos["vencido"] not in tablero_html
        assert "1 vencida" in tablero_html
        assert titulos["desc"] in clasif_html and titulos["desc"] not in tablero_html
        assert _ruta_e11(gids["desc"], "recuperar") in clasif_html
        assert ctx["clasificacion"]["desde"] is not None and ctx["clasificacion"]["total_descartados"] == 1

        r = app.post(token, _ruta_e11(gids["desc"], "recuperar"), {"cliente": a, "actualizado_en": "", "dias": "30"})
        assert r.status_code == 303 and "notice=recuperado" in r.headers["location"], r.headers.get("location")
        assert _fila_op_e11(conn, a, gids["desc"]) == ("NUEVO", "MANUAL", None)
        assert [(u, rec) for _acc, u, rec, _ip in _auditoria_e11(conn, org, "DESCARTADO_RECUPERADO")] == [
            (p["gp"], f"cliente={a};proceso={gids['desc']}")]
        assert _cp_e111(conn, a, gids["desc"])[:2] == ("DESCUBIERTO", 0)  # la clasificación no se toca

        r = app.get(token, f"/portal?client={a}&days=30&notice=recuperado")
        assert r.status_code == 200
        assert titulos["desc"] in _seccion_e11(r.text, 'id="h-tablero"', 'id="h-clasif"')
        assert titulos["desc"] not in _seccion_e11(r.text, 'id="h-clasif"', 'id="h-votos"')
    finally:
        cursor.close()
        _borrar_procesos_e111(conn, gids.values(), version)


# ═══════════════════════════════════════════════════════════════════════════
# E14 — vistas «solo Oracle»: `solo_marcas` contra el motor real
# ═══════════════════════════════════════════════════════════════════════════
# Los tests unitarios de E14 (`tests/test_solo_marca_*.py`) comprueban que cada SQL LLEVA la cláusula; estos
# comprueban que la cláusula FILTRA bien en Oracle 23ai: el `JSON_TABLE` sobre `cliente_proceso.marcas`, el
# `REGEXP_LIKE` de palabra completa sobre el título y los `EXISTS` correlacionados de `sql_existe_marca`.
#
# Fixture MIXTO en el cliente A (mismo patrón que E11/E11.1: INSERTs inline, funciones REALES para el resto):
#   - 7 que involucran Oracle: marca «Oracle» Recomendada, «Oracle» SOLO como Alternativa, «Oracle NetSuite»,
#     co-marca Dell+Oracle (cuenta como Oracle, plan §1 «casos ambiguos»; su tarjeta no debe nombrar a Dell), una con
#     conflicto de mercado, y dos sin marca con «Oracle»/«ORACLE» como palabra del título (una no relevante y otra
#     descartada por regex: el respaldo por título cubre lo que el clasificador deja con `marcas = []`).
#   - 6 que no: Dell+HPE, «Oraclexyz» en el título (NO es la palabra), HPE estancada, Cisco con conflicto, un no
#     relevante y un descartado por regex.
#   - cliente B: uno relevante, no Oracle (la vista regional lo muestra en 0, no lo oculta).
# Todo se publica «ayer» (`_pub_e111(1)`) con una corrida de `ejecucion` ese día, para que `relevancia_estricta` y
# `tendencia_semanal` sean deterministas a cualquier hora. Los eventos caen en días enteros (`TRUNC(SYSDATE) - n`)
# para que las duraciones del KPI 16 sean exactas.

_ORACLE_E14 = frozenset({"oracle"})
_SIN_RESTRICCION_E14 = frozenset()

# clave: (cliente, título, institución, marcas [(marca, rol)], estado_cp, es_relevante, triaje, score, clasificado_en)
_DATOS_E14 = {
    "ora_rec": ("a", "Licenciamiento de base de datos institucional", "INST-E14-ORA-1",
                [("Oracle", "Recomendada")], "ANALIZADO", 1, "GO", 0.9, None),
    "ora_alt": ("a", "Plataforma de respaldo institucional", "INST-E14-ORA-2",
                [("Oracle", "Alternativa")], "NOTIFICADO", 1, "GO", 0.85, None),
    "ora_ns": ("a", "ERP financiero en la nube", "INST-E14-ORA-1",
               [("Oracle NetSuite", "Recomendada")], "ANALIZADO", 1, None, 0.7, None),
    "ora_co": ("a", "Servidores y base de datos para hospitales", "INST-E14-ORA-3",
               [("Dell", "Recomendada"), ("Oracle", "Recomendada")], "ANALIZADO", 1, "GO", 0.95, None),
    "ora_conf": ("a", "Exadata para el centro de datos", "INST-E14-ORA-2",
                 [("Oracle", "Recomendada")], "ANALIZADO", 1, "GO", 0.9, None),
    "ora_tit": ("a", "Renovacion de soporte Oracle, ERP institucional", "INST-E14-ORA-4",
                None, "DESCUBIERTO", 0, None, 0.6, "TIMESTAMP '2026-02-03 04:05:06'"),
    "ora_desc": ("a", "Migracion a ORACLE Cloud", "INST-E14-ORA-4",
                 None, "DESCARTADO_REGEX", None, None, 0.5, None),
    "otro_dell": ("a", "Servidores para la red de clinicas", "INST-E14-OTRA-1",
                  [("Dell", "Recomendada"), ("HPE", "Alternativa")], "ANALIZADO", 1, "GO", 0.9, None),
    "otro_xyz": ("a", "Equipos Oraclexyz para laboratorio", "INST-E14-OTRA-2",
                 [("Lenovo", "Recomendada")], "NOTIFICADO", 1, "GO", 0.6, None),
    "otro_hpe": ("a", "Almacenamiento para expedientes", "INST-E14-OTRA-1",
                 [("HPE", "Recomendada")], "ANALIZADO", 1, "GO", 0.85, None),
    "otro_conf": ("a", "Switches de nucleo", "INST-E14-OTRA-3",
                  [("Cisco", "Recomendada")], "ANALIZADO", 1, "GO", 0.9, None),
    "otro_norel": ("a", "Mobiliario de oficina", "INST-E14-OTRA-2",
                   None, "DESCUBIERTO", 0, None, 0.65, "TIMESTAMP '2026-01-02 03:04:05'"),
    "otro_regex": ("a", "Papeleria escolar", "INST-E14-OTRA-3",
                   None, "DESCARTADO_REGEX", None, None, 0.4, None),
    "b_otro": ("b", "Impresoras de otro pais", "INST-E14-OTRA-B",
               [("Epson", "Recomendada")], "ANALIZADO", 1, "GO", 0.9, None),
}
_ORACLE_RELEVANTES_E14 = ("ora_rec", "ora_alt", "ora_ns", "ora_co", "ora_conf")
_OTROS_RELEVANTES_E14 = ("otro_dell", "otro_xyz", "otro_hpe", "otro_conf")
# Además de gids y títulos no-Oracle: lo que NUNCA puede aparecer en una lectura con `solo_marcas={"oracle"}`.
_MARCAS_AJENAS_E14 = ("Dell", "HPE", "Cisco", "Lenovo", "Epson", "INST-E14-OTRA")


def _marcas_json_e14(pares):
    import json

    return json.dumps([{"marca": m, "razon": "prueba E14", "role": r} for m, r in pares]) if pares else None


@pytest.fixture
def mixto_e14(conn, portal_y_org, dos_clientes_v2):
    """Los datos de arriba + análisis, estados, eventos, votos, participación y una corrida. Limpia todo al final."""
    a, b = dos_clientes_v2
    clientes = {"a": a, "b": b}
    portal = portal_y_org["portal_id"]
    sufijo = _uuid.uuid4().hex[:8]
    version = f"test-e14-{sufijo}"
    refs = {k: f"TEST-E14-{k.upper()}-{sufijo}" for k in _DATOS_E14}
    gids = {k: f"{portal}:{ref}" for k, ref in refs.items()}
    pub = _pub_e111(1)
    cursor = conn.cursor()

    def estado(clave, valor, hace_dias=None):
        actualizado = f"SYSTIMESTAMP - NUMTODSINTERVAL({hace_dias}, 'DAY')" if hace_dias else "SYSTIMESTAMP"
        cursor.execute("INSERT INTO oportunidad_estado (cliente_id, proceso_gid, estado, origen, actualizado_en) "
                       f"VALUES (:c, :g, :e, 'MANUAL', {actualizado})", c=a, g=gids[clave], e=valor)

    def evento(clave, anterior, nuevo, hace_dias):
        cursor.execute("INSERT INTO oportunidad_evento (cliente_id, proceso_gid, estado_anterior, estado_nuevo, ts) "
                       "VALUES (:c, :g, :ant, :nue, TRUNC(SYSDATE) - :d + 0.5)",
                       c=a, g=gids[clave], ant=anterior, nue=nuevo, d=hace_dias)

    def voto(clave, destinatario, valor, motivo=None, motivos=None):
        cursor.execute(
            "INSERT INTO feedback (cliente_id, proceso_gid, destinatario, voto, motivo, motivos) "
            f"VALUES (:c, :g, :d, :v, :m, {'JSON(:ms)' if motivos else 'NULL'})",
            dict(c=a, g=gids[clave], d=destinatario, v=valor, m=motivo, **({"ms": motivos} if motivos else {})))

    try:
        for clave, (cli, titulo, inst, marcas, est, rel, triaje, score, clasificado) in _DATOS_E14.items():
            cursor.execute(
                "INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, institucion, titulo, categoria_raw, "
                f"fecha_pub_utc, tz_portal, monto_usd, url_portal) VALUES (:g, :p, :r, :i, :t, 'infraestructura', "
                f"{pub}, 'America/El_Salvador', :monto, 'https://example.test/e14')",
                g=gids[clave], p=portal, r=refs[clave], i=inst, t=titulo,
                monto=3000 if clave == "ora_alt" else None)
            marcas_json = _marcas_json_e14(marcas)
            cursor.execute(
                "INSERT INTO cliente_proceso (cliente_id, proceso_gid, estado, es_relevante, triaje, match_tecnico, "
                f"match_categoria, factibilidad, marcas, clasificado_en) VALUES (:c, :g, :e, :rel, :tri, :s, :s, :s, "
                f"{'JSON(:marcas)' if marcas_json else 'NULL'}, {clasificado or 'NULL'})",
                dict(c=clientes[cli], g=gids[clave], e=est, rel=rel, tri=triaje, s=score,
                     **({"marcas": marcas_json} if marcas_json else {})))

        # Análisis: severidad (KPI 6) y monto (pipeline). El de «ora_tit» es de un NO relevante: no cuenta.
        for clave, monto, sev in (("ora_rec", 1000, "alta"), ("ora_co", None, "media"),
                                  ("otro_dell", 5000, "baja"), ("ora_tit", None, "alta")):
            cursor.execute("INSERT INTO analisis (cliente_id, proceso_gid, monto_estimado, severidad_max) "
                           "VALUES (:c, :g, :m, :s)", c=a, g=gids[clave], m=monto, s=sev)

        # Pipeline comercial: dos estancadas (EVALUANDO hace 10 días), dos activas en OFERTADO y dos finales que
        # contradicen al mercado (conflictos).
        estado("ora_rec", "EVALUANDO", hace_dias=10)
        estado("ora_alt", "OFERTADO")
        estado("ora_conf", "GANADO")
        estado("otro_dell", "OFERTADO")
        estado("otro_hpe", "EVALUANDO", hace_dias=10)
        estado("otro_conf", "PERDIDO")
        for clave, anterior, nuevo, hace in (
            ("ora_rec", "NUEVO", "EVALUANDO", 10), ("ora_rec", "EVALUANDO", "EVALUANDO", 9),  # asignación: no cuenta
            ("ora_alt", "NUEVO", "EVALUANDO", 8), ("ora_alt", "EVALUANDO", "OFERTADO", 6),
            ("ora_conf", "NUEVO", "EVALUANDO", 9), ("ora_conf", "EVALUANDO", "OFERTADO", 5),
            ("ora_conf", "OFERTADO", "GANADO", 2),
            ("otro_dell", "NUEVO", "EVALUANDO", 20), ("otro_dell", "EVALUANDO", "OFERTADO", 10),
            ("otro_hpe", "NUEVO", "EVALUANDO", 10),
            ("otro_conf", "NUEVO", "EVALUANDO", 30), ("otro_conf", "EVALUANDO", "OFERTADO", 20),
            ("otro_conf", "OFERTADO", "PERDIDO", 1),
        ):
            evento(clave, anterior, nuevo, hace)
        for clave, resultado in (("ora_conf", "PERDIDO"), ("otro_conf", "GANADO")):
            cursor.execute("INSERT INTO proceso_participado (cliente_id, codigo_proceso, resultado) "
                           "VALUES (:c, :cod, :r)", c=a, cod=refs[clave], r=resultado)

        voto("ora_ns", "secreto-e14-1@cliente.invalid", "IRRELEVANTE", "precio", '["precio"]')
        voto("ora_ns", "secreto-e14-2@cliente.invalid", "RELEVANTE")
        voto("ora_co", "secreto-e14-3@cliente.invalid", "RELEVANTE")
        voto("ora_tit", "secreto-e14-4@cliente.invalid", "IRRELEVANTE", "otro")  # fila anterior a `motivos`
        voto("otro_dell", "secreto-e14-5@cliente.invalid", "IRRELEVANTE", "fuera_de_rubro",
             '["fuera_de_rubro", "precio"]')
        voto("otro_xyz", "secreto-e14-6@cliente.invalid", "RELEVANTE")
        voto("otro_norel", "secreto-e14-7@cliente.invalid", "IRRELEVANTE", "fuera_de_rubro")

        _ejecucion_e111(cursor, f"TRUNC({_AHORA_SV_E111}) - 1 + NUMTODSINTERVAL(7, 'HOUR')", 100, version)
        conn.commit()

        ajenas = [k for k in _DATOS_E14 if not k.startswith("ora_")]
        prohibido = [gids[k] for k in ajenas] + [_DATOS_E14[k][1] for k in ajenas] + list(_MARCAS_AJENAS_E14)
        yield {"gids": gids, "refs": refs, "a": a, "b": b, "org": portal_y_org["org_id"],
               "portal": portal, "prohibido": prohibido}
    finally:
        conn.rollback()
        for cliente in (a, b):
            cursor.execute("DELETE FROM feedback WHERE cliente_id = :c", c=cliente)
            cursor.execute("DELETE FROM oportunidad_evento WHERE cliente_id = :c", c=cliente)
            cursor.execute("DELETE FROM oportunidad_estado WHERE cliente_id = :c", c=cliente)
            cursor.execute("DELETE FROM proceso_participado WHERE cliente_id = :c", c=cliente)
        for gid in gids.values():
            cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=gid)  # cascada: cliente_proceso, analisis
        cursor.execute("DELETE FROM auditoria WHERE org_id = :o", o=portal_y_org["org_id"])
        cursor.execute("DELETE FROM ejecucion WHERE version = :v", v=version)
        conn.commit()
        cursor.close()


def _sin_fuga_e14(m, nombre, valor):
    """Nada de lo no-Oracle (gid, título, institución, marca ajena) en el resultado, ni un destinatario de voto."""
    texto = repr(valor)
    fugas = [s for s in m["prohibido"] if s in texto]
    assert not fugas, f"{nombre} con solo_marcas={{'oracle'}} filtró {fugas}"
    assert "secreto-e14" not in texto


def _en_mes_actual_e14(conn) -> bool:
    """Si «ayer» (la publicación del fixture) cae en el mes en curso, como lo mira el KPI 1 (`TRUNC(SYSDATE, 'MM')`)."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT CASE WHEN {_pub_e111(1)} >= TRUNC(SYSDATE, 'MM') THEN 1 ELSE 0 END FROM dual")
    en_mes = bool(cursor.fetchone()[0])
    cursor.close()
    return en_mes


def test_e14_kpis_de_pais_y_regional_solo_oracle_contra_oracle(conn, mixto_e14):
    """`analytics.pais.*` y `regional.oportunidades_por_pais` con `{"oracle"}`: solo filas Oracle (marca
    Recomendada o Alternativa, «Oracle NetSuite», co-marca, palabra del título) y conteos exactos; con
    `frozenset()`, todo (contraste: el filtro no pasa por estar vacío)."""
    from src.analytics import pais, regional
    from src.memoria import repo as mrepo

    m = mixto_e14
    a, b = m["a"], m["b"]
    ora, sin = _ORACLE_E14, _SIN_RESTRICCION_E14

    # KPI 1 · oportunidades del mes: las 5 relevantes Oracle (las dos de título no son relevantes).
    en_mes = _en_mes_actual_e14(conn)
    del_mes = pais.oportunidades_del_mes(conn, a, solo_marcas=ora)
    assert del_mes["valor"] == (5 if en_mes else 0) and sum(del_mes["serie"]) == del_mes["valor"]
    assert pais.oportunidades_del_mes(conn, a, solo_marcas=sin)["valor"] == (9 if en_mes else 0)

    # KPI 2 (viejo) · denominador = lo persistido de la marca: 7 Oracle (incl. las dos por título), 5 relevantes.
    assert pais.pct_relevancia_clasificador(conn, a, 30, solo_marcas=ora) == {
        "valor": 71.4, "total": 7, "relevantes": 5}
    assert pais.pct_relevancia_clasificador(conn, a, 30, solo_marcas=sin) == {
        "valor": 69.2, "total": 13, "relevantes": 9}

    # KPI 2 (E11.1) · con restricción, nada global: ni revisados ni %; aptos = match ≥ 80 («ora_ns» tiene 70).
    assert pais.relevancia_estricta(conn, a, 30, solo_marcas=ora) == {
        "revisados": None, "relevantes": 5, "aptos": 4, "pct_aptos": None, "pct_relevantes": None,
        "filtrado_por_marca": True}
    todo = pais.relevancia_estricta(conn, a, 30, solo_marcas=sin)
    assert (todo["relevantes"], todo["aptos"], todo["filtrado_por_marca"]) == (9, 7, False)
    assert todo["revisados"] >= 100 and todo["pct_aptos"] is not None

    # KPI 3 · instituciones: «INST-E14-ORA-4» solo tiene no relevantes y no aparece.
    top = pais.instituciones_top(conn, a, solo_marcas=ora)
    assert {f["institucion"]: f["conteo"] for f in top} == {
        "INST-E14-ORA-1": 2, "INST-E14-ORA-2": 2, "INST-E14-ORA-3": 1}
    assert {f["institucion"]: f["conteo"] for f in pais.instituciones_top(conn, a, solo_marcas=sin)} == {
        "INST-E14-ORA-1": 2, "INST-E14-ORA-2": 2, "INST-E14-ORA-3": 1,
        "INST-E14-OTRA-1": 2, "INST-E14-OTRA-2": 1, "INST-E14-OTRA-3": 1}
    stats = mrepo.stats_por_institucion_v2(conn, a, 90, solo_marcas=ora)
    assert sum(f["conteo"] for f in stats) == 5

    # KPI 4 · embudo
    camino = {e["estado"]: e["conteo"] for e in pais.embudo_pipeline(conn, a, 90, solo_marcas=ora)["camino"]}
    assert (camino["ANALIZADO"], camino["NOTIFICADO"], sum(camino.values())) == (4, 1, 5)
    camino = {e["estado"]: e["conteo"] for e in pais.embudo_pipeline(conn, a, 90, solo_marcas=sin)["camino"]}
    assert (camino["ANALIZADO"], camino["NOTIFICADO"], sum(camino.values())) == (7, 2, 9)

    # KPI 6 · severidad: la «baja» es de Dell; la «alta» de «ora_tit» no cuenta (no relevante).
    assert pais.severidad_riesgos(conn, a, 90, solo_marcas=ora) == [
        {"severidad": "alta", "conteo": 1}, {"severidad": "media", "conteo": 1}]
    assert pais.severidad_riesgos(conn, a, 90, solo_marcas=sin) == [
        {"severidad": "alta", "conteo": 1}, {"severidad": "media", "conteo": 1}, {"severidad": "baja", "conteo": 1}]

    # KPI 15 · conversión: detectadas y eventos solo de procesos Oracle (la asignación no cuenta).
    assert pais.conversion_comercial(conn, a, solo_marcas=ora) == {
        "detectadas": 5,
        "etapas": [{"estado": "EVALUANDO", "conteo": 3}, {"estado": "OFERTADO", "conteo": 2},
                   {"estado": "GANADO", "conteo": 1}],
        "perdido": 0, "descartado": 0, "conv_ofertado_pct": 40.0, "conv_ganado_pct": 50.0}
    assert pais.conversion_comercial(conn, a, solo_marcas=sin) == {
        "detectadas": 9,
        "etapas": [{"estado": "EVALUANDO", "conteo": 6}, {"estado": "OFERTADO", "conteo": 4},
                   {"estado": "GANADO", "conteo": 1}],
        "perdido": 1, "descartado": 0, "conv_ofertado_pct": 44.4, "conv_ganado_pct": 25.0}

    # KPI 16 · el filtro va ANTES del LEAD: las duraciones salen solo de eventos Oracle.
    assert pais.tiempo_por_etapa(conn, a, solo_marcas=ora) == [
        {"etapa": "EVALUANDO", "dias_promedio": 3.0, "muestra": 2},
        {"etapa": "OFERTADO", "dias_promedio": 3.0, "muestra": 1}]
    assert pais.tiempo_por_etapa(conn, a, solo_marcas=sin) == [
        {"etapa": "EVALUANDO", "dias_promedio": 6.5, "muestra": 4},
        {"etapa": "OFERTADO", "dias_promedio": 11.0, "muestra": 2}]

    # R1 · B no tiene nada Oracle: sale en 0, no desaparece.
    assert regional.oportunidades_por_pais(conn, [a, b], solo_marcas=ora) == [
        {"cliente_id": a, "conteo": 5}, {"cliente_id": b, "conteo": 0}]
    assert regional.oportunidades_por_pais(conn, [a, b], solo_marcas=sin) == [
        {"cliente_id": a, "conteo": 9}, {"cliente_id": b, "conteo": 1}]

    for nombre, valor in (("instituciones_top", top), ("stats_por_institucion_v2", stats)):
        _sin_fuga_e14(m, nombre, valor)


def test_e14_lecturas_del_tablero_solo_oracle_contra_oracle(conn, mixto_e14):
    """`analytics.tablero.*` con `{"oracle"}`: filas, `total`, `truncado`, `desde`, votos, tendencia, marcas,
    pipeline y estancadas son de Oracle; las tarjetas no nombran co-marcas ajenas; con `frozenset()`, todo."""
    from src.analytics import tablero

    m = mixto_e14
    g, a = m["gids"], m["a"]
    ora, sin = _ORACLE_E14, _SIN_RESTRICCION_E14

    rango = tablero.procesos_del_rango(conn, a, 90, solo_marcas=ora)
    por_gid = {f["proceso_gid"]: f for f in rango["filas"]}
    assert set(por_gid) == {g[k] for k in _ORACLE_RELEVANTES_E14}
    assert (rango["total"], rango["truncado"]) == (5, False)
    assert por_gid[g["ora_co"]]["marcas"] == ["Oracle"]               # sin «Dell»
    assert por_gid[g["ora_ns"]]["marcas"] == ["Oracle NetSuite"]
    assert por_gid[g["ora_alt"]]["marcas"] == ["Oracle"]              # Oracle solo como Alternativa: entra
    assert all(x.lower().startswith("oracle") for f in rango["filas"] for x in f["marcas"])
    corto = tablero.procesos_del_rango(conn, a, 90, limite=2, solo_marcas=ora)
    assert (len(corto["filas"]), corto["total"], corto["truncado"]) == (2, 5, True)  # el total es de la marca
    todo = tablero.procesos_del_rango(conn, a, 90, solo_marcas=sin)
    assert {f["proceso_gid"] for f in todo["filas"]} == {
        g[k] for k in _ORACLE_RELEVANTES_E14 + _OTROS_RELEVANTES_E14}
    assert todo["total"] == 9
    assert next(f for f in todo["filas"] if f["proceso_gid"] == g["ora_co"])["marcas"] == ["Dell", "Oracle"]

    # Descartados: las dos Oracle por título (minúsculas y MAYÚSCULAS); `desde` es el MIN de la marca, no del cliente.
    desc = tablero.descartados_del_rango(conn, a, 90, solo_marcas=ora)
    assert [f["proceso_gid"] for f in desc["filas"]] == [g["ora_tit"], g["ora_desc"]]
    assert (desc["total"], desc["desde"]) == (2, datetime(2026, 2, 3, 4, 5, 6))
    corto = tablero.descartados_del_rango(conn, a, 90, limite=1, solo_marcas=ora)
    assert ([f["proceso_gid"] for f in corto["filas"]], corto["total"]) == ([g["ora_tit"]], 2)
    desc_todo = tablero.descartados_del_rango(conn, a, 90, solo_marcas=sin)
    assert [f["proceso_gid"] for f in desc_todo["filas"]] == [
        g["otro_norel"], g["ora_tit"], g["ora_desc"], g["otro_regex"]]
    assert (desc_todo["total"], desc_todo["desde"]) == (4, datetime(2026, 1, 2, 3, 4, 5))

    # Votos: conteos y motivos solo de procesos Oracle; sin destinatarios.
    resumen = tablero.votos_resumen(conn, a, 90, solo_marcas=ora)
    assert resumen == {"relevante": 2, "irrelevante": 2, "motivos": [("otro", 1), ("precio", 1)]}
    assert tablero.votos_resumen(conn, a, 90, solo_marcas=sin) == {
        "relevante": 3, "irrelevante": 4, "motivos": [("fuera_de_rubro", 2), ("precio", 2), ("otro", 1)]}
    pedidos = list(g.values())
    por_proceso = tablero.votos_por_proceso(conn, a, pedidos, solo_marcas=ora)
    assert por_proceso == {
        g["ora_ns"]: {"relevante": 1, "irrelevante": 1, "motivos": ["precio"]},
        g["ora_co"]: {"relevante": 1, "irrelevante": 0, "motivos": []},
        g["ora_tit"]: {"relevante": 0, "irrelevante": 1, "motivos": ["otro"]},
    }  # los gids ajenos no aparecen aunque se pidan
    assert set(tablero.votos_por_proceso(conn, a, pedidos, solo_marcas=sin)) == {
        g[k] for k in ("ora_ns", "ora_co", "ora_tit", "otro_dell", "otro_xyz", "otro_norel")}

    # Tendencia: revisados (global) oculto; relevantes solo de la marca.
    tendencia = tablero.tendencia_semanal(conn, a, semanas=12, solo_marcas=ora)
    assert len(tendencia) == 12 and all(t["revisados"] is None for t in tendencia)
    assert sum(t["relevantes"] for t in tendencia) == 5
    tendencia_todo = tablero.tendencia_semanal(conn, a, semanas=12, solo_marcas=sin)
    assert sum(t["relevantes"] for t in tendencia_todo) == 9
    assert all(isinstance(t["revisados"], int) for t in tendencia_todo)

    # Marcas: solo las de Oracle, contadas sobre procesos Oracle; la co-marca Dell no se nombra.
    marcas = tablero.marcas_top(conn, a, 90, solo_marcas=ora)
    assert marcas == [{"marca": "Oracle", "conteo": 4}, {"marca": "Oracle NetSuite", "conteo": 1}]
    assert tablero.marcas_top(conn, a, 90, limite=10, solo_marcas=sin) == [
        {"marca": "Oracle", "conteo": 4}, {"marca": "Dell", "conteo": 2}, {"marca": "HPE", "conteo": 2},
        {"marca": "Cisco", "conteo": 1}, {"marca": "Lenovo", "conteo": 1}, {"marca": "Oracle NetSuite", "conteo": 1}]

    # Pipeline en juego y estancadas: el monto y los conteos son de lo Oracle activo.
    assert tablero.pipeline_en_juego(conn, a, solo_marcas=ora) == {"monto_usd": 4000.0, "activas": 2, "con_monto": 2}
    assert tablero.pipeline_en_juego(conn, a, solo_marcas=sin) == {"monto_usd": 9000.0, "activas": 4, "con_monto": 3}
    assert tablero.estancadas(conn, a, solo_marcas=ora) == 1
    assert tablero.estancadas(conn, a, solo_marcas=sin) == 2

    for nombre, valor in (("procesos_del_rango", rango), ("descartados_del_rango", desc),
                          ("votos_resumen", resumen), ("votos_por_proceso", por_proceso),
                          ("tendencia_semanal", tendencia), ("marcas_top", marcas)):
        _sin_fuga_e14(m, nombre, valor)


def test_e14_oportunidades_y_conflictos_solo_oracle_contra_oracle(conn, mixto_e14):
    """`oportunidades.repo` (lista y lecturas por gid: la barrera IDOR) y `mercado.listar_conflictos_oportunidad_v2`:
    un gid de otra marca del MISMO cliente es igual de invisible que uno inexistente."""
    from src.mercado import repo as mercado
    from src.oportunidades import repo as orepo

    m = mixto_e14
    g, a = m["gids"], m["a"]
    ora, sin = _ORACLE_E14, _SIN_RESTRICCION_E14
    inexistente = f"{m['portal']}:TEST-E14-NO-EXISTE"

    lista = orepo.listar(conn, a, solo_marcas=ora)
    assert {f["proceso_gid"] for f in lista} == {g[k] for k in _ORACLE_RELEVANTES_E14}
    assert {f["proceso_gid"] for f in orepo.listar(conn, a, solo_marcas=sin)} == {
        g[k] for k in _ORACLE_RELEVANTES_E14 + _OTROS_RELEVANTES_E14}

    for clave in ("ora_rec", "ora_alt", "ora_ns", "ora_co", "ora_tit", "ora_desc"):
        assert orepo.proceso_visible(conn, a, g[clave], solo_marcas=ora), clave
    for clave in ("otro_dell", "otro_xyz", "otro_norel", "otro_regex"):
        assert not orepo.proceso_visible(conn, a, g[clave], solo_marcas=ora), clave  # «Oraclexyz» no es Oracle
        assert orepo.proceso_visible(conn, a, g[clave], solo_marcas=sin), clave
    assert not orepo.proceso_visible(conn, a, inexistente, solo_marcas=ora)

    assert orepo.obtener(conn, a, g["otro_dell"], solo_marcas=ora) is None
    assert orepo.obtener(conn, a, g["otro_dell"], solo_marcas=sin)["estado"] == "OFERTADO"
    assert orepo.obtener(conn, a, g["ora_rec"], solo_marcas=ora)["estado"] == "EVALUANDO"

    assert orepo.es_descartado_por_amalia(conn, a, g["otro_norel"], solo_marcas=ora) is None  # = inexistente
    assert orepo.es_descartado_por_amalia(conn, a, inexistente, solo_marcas=ora) is None
    assert orepo.es_descartado_por_amalia(conn, a, g["otro_norel"], solo_marcas=sin) is True
    assert orepo.es_descartado_por_amalia(conn, a, g["ora_tit"], solo_marcas=ora) is True
    assert orepo.es_descartado_por_amalia(conn, a, g["ora_rec"], solo_marcas=ora) is False

    conflictos = mercado.listar_conflictos_oportunidad_v2(conn, a, m["portal"], solo_marcas=ora)
    assert [(c["proceso_gid"], c["estado_manual"], c["resultado_mercado"]) for c in conflictos] == [
        (g["ora_conf"], "GANADO", "PERDIDO")]
    assert {c["proceso_gid"] for c in mercado.listar_conflictos_oportunidad_v2(
        conn, a, m["portal"], solo_marcas=sin)} == {g["ora_conf"], g["otro_conf"]}

    for nombre, valor in (("listar", lista), ("listar_conflictos_oportunidad_v2", conflictos)):
        _sin_fuga_e14(m, nombre, valor)


def _foto_e14(conn, cliente, org):
    """Todo lo que una escritura podría tocar: estados, eventos y auditoría."""
    cursor = conn.cursor()
    cursor.execute("SELECT proceso_gid, estado, responsable_usuario_id, actualizado_en FROM oportunidad_estado "
                   "WHERE cliente_id = :c ORDER BY proceso_gid", c=cliente)
    estados = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) FROM oportunidad_evento WHERE cliente_id = :c", c=cliente)
    eventos = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM auditoria WHERE org_id = :o", o=org)
    auditoria = cursor.fetchone()[0]
    cursor.close()
    return estados, eventos, auditoria


def test_e14_escrituras_por_gid_ajeno_son_no_encontrado_sin_escribir_contra_oracle(conn, mixto_e14):
    """Con un alcance restringido a Oracle que SÍ podría escribir en A (hipotético: los roles Oracle reales son de
    solo lectura, ver el test siguiente), cambiar estado, asignar o recuperar un gid no-Oracle responde
    NO_ENCONTRADO —igual que uno inexistente— y no toca la base. El mismo alcance sobre un gid Oracle sí escribe
    (control positivo: la barrera no es «nada pasa»)."""
    from src.identidad.alcance import Alcance
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    m = mixto_e14
    g, a, org = m["gids"], m["a"], m["org"]
    alcance = Alcance(usuario_id="u-e14", org_id=org, roles=frozenset({"director_servicios_oracle"}),
                      clientes=frozenset({a}), cambia_estado=frozenset({a}), ve_operativo=False,
                      solo_marcas=_ORACLE_E14)
    inexistente = f"{m['portal']}:TEST-E14-NO-EXISTE"
    token_hpe = orepo.obtener(conn, a, g["otro_hpe"])["token"]
    token_dell = orepo.obtener(conn, a, g["otro_dell"])["token"]

    antes = _foto_e14(conn, a, org)
    assert service.cambiar_estado(conn, alcance, a, g["otro_hpe"], "OFERTADO", token_hpe) == service.NO_ENCONTRADO
    assert service.cambiar_estado(conn, alcance, a, g["otro_xyz"], "EVALUANDO", "") == service.NO_ENCONTRADO
    assert service.cambiar_estado(conn, alcance, a, inexistente, "EVALUANDO", "") == service.NO_ENCONTRADO
    assert service.asignar_responsable(conn, alcance, a, g["otro_dell"], None, token_dell) == service.NO_ENCONTRADO
    assert service.recuperar_descartado(conn, alcance, a, g["otro_norel"]) == service.NO_ENCONTRADO
    assert service.recuperar_descartado(conn, alcance, a, g["otro_regex"]) == service.NO_ENCONTRADO
    conn.commit()
    assert _foto_e14(conn, a, org) == antes

    # Control positivo con el MISMO alcance: lo Oracle sí se escribe (también lo Oracle solo por título).
    token_rec = orepo.obtener(conn, a, g["ora_rec"], solo_marcas=_ORACLE_E14)["token"]
    assert service.cambiar_estado(conn, alcance, a, g["ora_rec"], "OFERTADO", token_rec) == service.OK
    assert service.recuperar_descartado(conn, alcance, a, g["ora_tit"]) == service.OK
    conn.commit()
    assert orepo.obtener(conn, a, g["ora_rec"], solo_marcas=_ORACLE_E14)["estado"] == "OFERTADO"
    assert orepo.obtener(conn, a, g["ora_tit"], solo_marcas=_ORACLE_E14)["estado"] == "NUEVO"
    estados, eventos, auditoria = _foto_e14(conn, a, org)
    assert (eventos - antes[1], auditoria - antes[2]) == (2, 2)
    # Lo ajeno sigue intacto después de las escrituras legítimas.
    ajenos = {fila[0]: fila for fila in antes[0] if fila[0] not in (g["ora_rec"], g["ora_tit"])}
    assert {fila[0]: fila for fila in estados if fila[0] in ajenos} == ajenos


def test_e14_resolver_alcance_de_un_rol_oracle_real_contra_oracle(conn, mixto_e14, limpiar_identidad):
    """De punta a punta por `resolver_alcance`: personas reales con cada rol Oracle obtienen `solo_marcas={"oracle"}`
    y todos los países activos, de solo lectura, y lo que leen con SU alcance es solo Oracle. Una membresía de país
    levanta la restricción (gana el permisivo); «ver como» un rol Oracle equivale al real."""
    from dataclasses import replace

    from src.analytics import regional, tablero
    from src.identidad import alcance as alc
    from src.identidad import service as identidad
    from src.oportunidades import repo as orepo
    from src.oportunidades import service

    m = mixto_e14
    g, a, b, org = m["gids"], m["a"], m["b"], m["org"]
    roles_oracle = ("director_servicios_oracle", "gerente_marca_oracle", "gerente_regional_comercial_oracle")
    personas = {rol: identidad.alta_usuario(conn, org, f"{rol}@test.invalid", rol) for rol in roles_oracle}
    mixto = identidad.alta_usuario(conn, org, "oracle-y-pais@test.invalid", "gerente_marca_oracle")
    identidad.alta_usuario(conn, org, "oracle-y-pais@test.invalid", "gerente_pais", b)
    admin = identidad.alta_usuario(conn, org, "admin-e14@test.invalid", "admin_plataforma")
    conn.commit()

    for rol, usuario in personas.items():
        real = alc.resolver_alcance(conn, usuario)
        assert real.solo_marcas == _ORACLE_E14, rol
        assert real.clientes == {a, b} and not real.cambia_estado and not real.ve_operativo, rol

        # Lo que esa persona lee con SU alcance.
        assert regional.oportunidades_por_pais(conn, sorted(real.clientes), solo_marcas=real.solo_marcas) == [
            {"cliente_id": a, "conteo": 5}, {"cliente_id": b, "conteo": 0}]
        rango = tablero.procesos_del_rango(conn, a, 90, solo_marcas=real.solo_marcas)
        assert {f["proceso_gid"] for f in rango["filas"]} == {g[k] for k in _ORACLE_RELEVANTES_E14}
        _sin_fuga_e14(m, f"procesos_del_rango ({rol})", rango)
        assert tablero.procesos_del_rango(conn, b, 90, solo_marcas=real.solo_marcas) == {
            "filas": [], "total": 0, "truncado": False}

        # Solo lectura: ni siquiera lo Oracle se escribe.
        token = orepo.obtener(conn, a, g["ora_rec"], solo_marcas=real.solo_marcas)["token"]
        assert service.cambiar_estado(conn, real, a, g["ora_rec"], "OFERTADO", token) == service.NO_ENCONTRADO

        # «Ver como» ese rol (admin_plataforma) == el alcance real, salvo quién es y la marca de simulación.
        simulado = alc.alcance_simulado(conn, alc.resolver_alcance(conn, admin), rol)
        assert simulado is not None and simulado.simulado == rol
        assert replace(simulado, usuario_id=real.usuario_id, simulado=None) == real

    # Rol Oracle + gerente_pais en B: la membresía de país NO levanta la restricción (valdría para todos los países).
    con_pais = alc.resolver_alcance(conn, mixto)
    assert con_pais.solo_marcas == {"oracle"} and con_pais.clientes == {a, b}
    assert regional.oportunidades_por_pais(conn, sorted(con_pais.clientes), solo_marcas=con_pais.solo_marcas) == [
        {"cliente_id": a, "conteo": 5}, {"cliente_id": b, "conteo": 0}]


# ── src/clientes: crear → importar → revertir con el SQL real ─────────────────

def test_clientes_cli_crear_importar_revertir_real(conn):
    """El SQL de `src.clientes.repo` y de `registrar_version` contra Oracle de verdad (LOBs, JSON, FK)."""
    from src.clientes import repo as clientes_repo

    org, cid = "test_org_clientes_cli", f"test_cli_{_uuid.uuid4().hex[:8]}"
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
        "VALUES (:o, 'Org de prueba CLI', 'SV', 'America/El_Salvador')", o=org,
    )
    conn.commit()
    try:
        assert clientes_repo.crear(conn, cid, "Cliente CLI", org_id=org, creado_por="test") == 1
        _, datos = clientes_repo.requerir_activo(conn, cid)
        assert datos["client"]["name"] == "Cliente CLI"

        datos["rules"]["context_note"] = "ñandú — nota con tildes"
        assert clientes_repo.importar(conn, cid, datos, "test") == 2
        assert clientes_repo.importar(conn, cid, datos, "test") is None  # mismo hash: sin versión

        assert clientes_repo.fijar_activo(conn, cid, False, "test") == 3
        assert {f["cliente_id"]: f["activo"] for f in clientes_repo.listar(conn)}[cid] is False

        assert clientes_repo.revertir(conn, cid, 1, "test") == 4
        assert [v["version"] for v in clientes_repo.versiones(conn, cid) if v["activa"]] == [4]
        assert clientes_repo.activo(conn, cid)[1]["client"]["active"] is True
        assert len(clientes_repo.versiones(conn, cid)) == 4  # nunca se borra
        conn.commit()
    finally:
        conn.rollback()
        cursor.execute("DELETE FROM cliente_config WHERE cliente_id = :c", c=cid)
        cursor.execute("DELETE FROM cliente WHERE cliente_id = :c", c=cid)
        cursor.execute("DELETE FROM organizacion WHERE org_id = :o", o=org)
        conn.commit()
        cursor.close()


def test_destinatarios_reporte_y_admins_contra_oracle(conn, portal_y_org, dos_clientes_v2, limpiar_identidad):
    """El SQL de `destinatarios_reporte`, `org_de_cliente` y `usuarios_con_rol` contra el motor real, con altas por
    el servicio: reciben los roles de negocio del país (el rol Oracle, restringido a Oracle), no los técnicos ni
    una baja ni quien tiene otro país; los admin_plataforma salen por su rol."""
    from src.identidad import repo as identidad_repo
    from src.identidad import service

    org = portal_y_org["org_id"]
    a, b = dos_clientes_v2
    service.alta_usuario(conn, org, "ceo-rep@test.invalid", "ceo")
    service.alta_usuario(conn, org, "ventas-a-rep@test.invalid", "equipo_ventas", a)
    service.alta_usuario(conn, org, "ventas-b-rep@test.invalid", "equipo_ventas", b)
    service.alta_usuario(conn, org, "oracle-rep@test.invalid", "gerente_marca_oracle")
    service.alta_usuario(conn, org, "plataforma-rep@test.invalid", "admin_plataforma")
    service.alta_usuario(conn, org, "operador-rep@test.invalid", "operador")
    baja = service.alta_usuario(conn, org, "baja-rep@test.invalid", "ceo")
    cursor = conn.cursor()
    cursor.execute("UPDATE usuario SET activo = 0 WHERE usuario_id = :u", u=baja)
    cursor.close()
    conn.commit()

    assert identidad_repo.org_de_cliente(conn, a) == org
    assert identidad_repo.org_de_cliente(conn, "no_existe") is None
    assert dict(identidad_repo.destinatarios_reporte(conn, org, a)) == {
        "ceo-rep@test.invalid": frozenset(),
        "ventas-a-rep@test.invalid": frozenset(),
        "oracle-rep@test.invalid": frozenset({"oracle"}),
    }
    assert "ventas-b-rep@test.invalid" in dict(identidad_repo.destinatarios_reporte(conn, org, b))
    assert identidad_repo.usuarios_con_rol(conn, org, "admin_plataforma") == ["plataforma-rep@test.invalid"]
    assert "plataforma-rep@test.invalid" in identidad_repo.usuarios_con_rol(conn, None, "admin_plataforma")


def test_administracion_de_clientes_contra_oracle(conn, portal_y_org, monkeypatch):
    """G2: el SQL del portal de clientes (`repo.obtener/listar/bloquear/guardar_perfil/editar_general/fijar_activo/
    revertir`) y su servicio contra el motor real: alta con versión 1, edición de una sección con versión base,
    CONFLICTO si la activa cambió, activar/desactivar con `cliente.activo` y `client.active` juntos, reversión como
    versión nueva y alcance por organización. `FOR UPDATE` y `JSON_SERIALIZE … RETURNING CLOB` solo se validan acá."""
    from src.clientes import repo, servicio
    from src.identidad.alcance import Alcance

    org = portal_y_org["org_id"]
    cid = "test_cli_g2"
    monkeypatch.setattr(servicio, "creado_por", lambda c, a: "admin@example.com")

    def alc(org_id, rol="admin_plataforma"):
        return Alcance(usuario_id="u-g2", org_id=org_id, roles=frozenset({rol}), clientes=frozenset(),
                       cambia_estado=frozenset(), ve_operativo=False)

    plataforma, de_la_org, otra_org = alc(org), alc(org, "admin_organizacion"), alc("otra", "admin_organizacion")
    cursor = conn.cursor()
    try:
        assert servicio.crear_cliente(conn, plataforma, cid, "Cliente G2", "comprasal_sv", org) == servicio.OK
        assert servicio.crear_cliente(conn, plataforma, cid, "Otra vez", "comprasal_sv", org) == servicio.INVALIDO
        fila = repo.obtener(conn, cid)
        assert fila["org_id"] == org and fila["activo"] is True and fila["locale"] == "es-SV"
        assert [c["cliente_id"] for c in servicio.listar(conn, de_la_org)] == [cid]
        assert servicio.listar(conn, otra_org) == [] and servicio.visible(conn, otra_org, cid) is None
        assert any(o["org_id"] == org for o in repo.organizaciones(conn))
        assert repo.portales_de(conn, cid) == []
        assert repo.activo(conn, cid)[0] == 1

        # Una sección, con la versión base correcta: versión 2; con la vieja: CONFLICTO y nada cambia.
        campos = {"relevance_threshold": "0.55", "confidence_threshold": "0.8"}
        assert servicio.editar_seccion(conn, de_la_org, cid, "thresholds", campos, "1") == servicio.OK
        version, datos = repo.activo(conn, cid)
        assert version == 2 and datos["classification"] == {"relevance_threshold": 0.55, "confidence_threshold": 0.8}
        assert servicio.editar_seccion(conn, de_la_org, cid, "thresholds", campos, "2") == servicio.SIN_CAMBIOS
        assert servicio.editar_seccion(conn, de_la_org, cid, "rules",
                                       {"context_note": "ñandú", "categories": "otro"}, "1") == servicio.CONFLICTO
        assert servicio.editar_seccion(conn, otra_org, cid, "rules",
                                       {"context_note": "x", "categories": "otro"}, "2") == servicio.NO_ENCONTRADO
        assert repo.activo(conn, cid)[0] == 2
        assert servicio.editar_seccion(conn, de_la_org, cid, "rules",
                                       {"context_note": "ñandú", "categories": "otro"}, "2") == servicio.OK
        assert repo.activo(conn, cid)[1]["rules"]["context_note"] == "ñandú"      # tildes: la columna JSON no las pierde

        # Datos generales: columna y JSON a la vez.
        assert servicio.editar_general(conn, de_la_org, cid, "Cliente G2 CR", "es-CR", "America/Costa_Rica",
                                       "3") == servicio.OK
        fila = repo.obtener(conn, cid)
        assert (fila["nombre"], fila["locale"], fila["zona_horaria"]) == ("Cliente G2 CR", "es-CR", "America/Costa_Rica")
        assert repo.activo(conn, cid)[1]["client"]["name"] == "Cliente G2 CR"

        # Activar/desactivar: un solo punto escribe la columna y el JSON.
        assert servicio.fijar_activo(conn, plataforma, cid, False, "4") == servicio.OK
        assert repo.obtener(conn, cid)["activo"] is False and repo.activo(conn, cid)[1]["client"]["active"] is False
        assert servicio.fijar_activo(conn, plataforma, cid, True, "4") == servicio.CONFLICTO     # ya es la 5
        assert servicio.fijar_activo(conn, plataforma, cid, True, "5") == servicio.OK

        # Revertir a la 1: versión nueva con el contenido de la 1.
        assert servicio.revertir(conn, plataforma, cid, "99", "6") == servicio.NO_ENCONTRADO
        assert servicio.revertir(conn, plataforma, cid, "1", "5") == servicio.CONFLICTO
        assert servicio.revertir(conn, plataforma, cid, "1", "6") == servicio.OK
        assert repo.activo(conn, cid)[0] == 7 and repo.activo(conn, cid)[1]["rules"]["context_note"] == ""
        assert [v["version"] for v in repo.versiones(conn, cid)] == [7, 6, 5, 4, 3, 2, 1]
        assert sum(1 for v in repo.versiones(conn, cid) if v["activa"]) == 1
        conn.commit()
    finally:
        conn.rollback()
        cursor.execute("DELETE FROM cliente_config WHERE cliente_id = :c", c=cid)
        cursor.execute("DELETE FROM cliente WHERE cliente_id = :c", c=cid)


def test_admin_usuarios_contra_oracle(conn, portal_y_org, dos_clientes_v2, limpiar_identidad):
    """G1: el SQL de la administración de usuarios contra el motor real — alta, cambio de rol, membresías, baja y
    reactivación, sesiones por prefijo, invitaciones (crear, canjear, revocar) y reinicio de MFA."""
    from src.identidad import alcance as identidad_alcance
    from src.identidad import repo as identidad_repo
    from src.identidad import service, sesiones

    org = portal_y_org["org_id"]
    a, b = dos_clientes_v2
    adm = identidad_alcance.resolver_alcance(conn, service.alta_usuario(conn, org, "adm-g1@test.invalid", "admin_organizacion"))
    plat = identidad_alcance.resolver_alcance(conn, service.alta_usuario(conn, org, "plat-g1@test.invalid", "admin_plataforma"))
    conn.commit()
    try:
        # Alta, lista y roles
        assert service.admin_crear_usuario(conn, adm, "Ana-G1@test.invalid", "Ana", "gerente_pais", a) == service.OK
        assert service.admin_crear_usuario(conn, adm, "ana-g1@test.invalid", "Ana", "ceo") == service.CONFLICTO
        ana = identidad_repo.obtener_usuario_por_email(conn, org, "ana-g1@test.invalid")["usuario_id"]
        assert {u["email"] for u in identidad_repo.listar_usuarios(conn, org)} >= {"ana-g1@test.invalid"}
        assert identidad_repo.roles_de_usuarios(conn, org)[ana] == {"org": [], "pais": [(a, "gerente_pais")]}

        # Cambiar rol, agregar y quitar país y rol de organización
        assert service.admin_cambiar_rol(conn, adm, ana, a, "equipo_ventas") == service.OK
        assert service.admin_agregar_pais(conn, adm, ana, b, "gerente_pais") == service.OK
        assert service.admin_agregar_pais(conn, adm, ana, b, "gerente_pais") == service.CONFLICTO
        assert service.admin_quitar_pais(conn, adm, ana, b) == service.OK
        assert service.admin_agregar_rol_org(conn, adm, ana, "gerente_marca_oracle") == service.OK
        assert service.admin_cambiar_rol(conn, adm, ana, "", "ceo") == service.OK
        assert identidad_repo.roles_de_usuarios(conn, org)[ana] == {"org": ["ceo"], "pais": [(a, "equipo_ventas")]}
        assert service.admin_quitar_rol_org(conn, adm, ana) == service.OK
        assert service.admin_editar_nombre(conn, adm, ana, "Ana G1") == service.OK
        assert identidad_repo.obtener_usuario_admin(conn, ana)["nombre"] == "Ana G1"

        # Sesiones: se listan por prefijo y se revocan una a una o todas
        for token in ("tok-g1-1", "tok-g1-2"):
            identidad_repo.crear_sesion(conn, sesiones.hash_token(token), ana, 12)
        vivas = identidad_repo.listar_sesiones_vigentes(conn, ana)
        assert len(vivas) == 2 and all(len(s["id"]) == 16 for s in vivas)
        assert service.admin_revocar_sesion(conn, adm, ana, vivas[0]["id"]) == service.OK
        assert service.admin_revocar_sesion(conn, adm, ana, vivas[0]["id"]) == service.CONFLICTO
        assert len(identidad_repo.listar_sesiones_vigentes(conn, ana)) == 1
        assert service.admin_cerrar_sesiones(conn, adm, ana) == service.OK
        assert identidad_repo.listar_sesiones_vigentes(conn, ana) == []

        # Baja y reactivación
        assert service.admin_baja_usuario(conn, adm, ana) == service.OK
        assert identidad_repo.obtener_usuario_admin(conn, ana)["activo"] is False
        assert service.admin_reactivar_usuario(conn, adm, ana) == service.OK
        assert identidad_repo.obtener_usuario_admin(conn, ana)["activo"] is True

        # Invitaciones: crear, revocar, canjear
        assert service.admin_invitar(conn, adm, "inv-g1@test.invalid", "equipo_ventas", a) == service.OK
        assert service.admin_invitar(conn, adm, "inv-g1@test.invalid", "equipo_ventas", a) == service.CONFLICTO
        assert service.admin_invitar(conn, adm, "rev-g1@test.invalid", "ceo") == service.OK
        listadas = {i["email"]: i for i in identidad_repo.listar_invitaciones(conn, org)}
        assert listadas["inv-g1@test.invalid"]["estado"] == "PENDIENTE"
        assert listadas["inv-g1@test.invalid"]["rol_propuesto"] == f"equipo_ventas:{a}"
        assert service.admin_revocar_invitacion(conn, adm, listadas["rev-g1@test.invalid"]["invitacion_id"]) == service.OK
        nuevo = service.canjear_invitacion(conn, "INV-G1@test.invalid")
        assert nuevo and identidad_repo.membresias_pais(conn, nuevo["usuario_id"]) == [(a, "equipo_ventas")]
        estados = {i["email"]: i["estado"] for i in identidad_repo.listar_invitaciones(conn, org)}
        assert estados == {"inv-g1@test.invalid": "ACEPTADA", "rev-g1@test.invalid": "REVOCADA"}
        assert service.canjear_invitacion(conn, "rev-g1@test.invalid") is None

        # MFA: un admin_plataforma reinicia el TOTP de otro admin, nunca el propio
        adm_id = adm.usuario_id
        identidad_repo.guardar_secreto_mfa(conn, adm_id, "secreto-cifrado")
        assert identidad_repo.confirmar_mfa(conn, adm_id, 123) is True
        identidad_repo.reemplazar_recuperacion(conn, adm_id, ["h1", "h2"])
        assert service.admin_reiniciar_mfa(conn, plat, plat.usuario_id) == service.INVALIDO
        assert service.admin_reiniciar_mfa(conn, adm, adm_id) == service.NO_ENCONTRADO
        assert service.admin_reiniciar_mfa(conn, plat, adm_id) == service.OK
        assert identidad_repo.obtener_mfa(conn, adm_id) == {"secreto_cifrado": None, "confirmado": False,
                                                           "ultimo_paso": None}
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM recuperacion_mfa WHERE usuario_id = :u", u=adm_id)
        assert cursor.fetchone()[0] == 0
        cursor.close()
        conn.commit()
    finally:
        conn.rollback()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM invitacion WHERE org_id = :o", o=org)
        conn.commit()
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════
# Memoria de aprendizaje por marca (db/schema_v2.sql §7, src/aprendizaje/repo.py)
# ═══════════════════════════════════════════════════════════════════════════
# Todo corre en UNA transacción que se deshace al final: no deja filas ni
# choca con las fixtures de las otras secciones. El contenedor no tiene el
# modelo ONNX: `agregar_ejemplo` debe caer solo a embedding NULL (si algún
# día el modelo está, el test acepta el vector calculado).

def test_aprendizaje_memoria_contra_el_motor(conn, monkeypatch):
    from src.aprendizaje import repo as apr
    from src.identidad import repo as identidad_repo

    # Hasta integrar la SOLICITUD sobre src/identidad/politica.py.
    monkeypatch.setattr(identidad_repo, "ACCIONES_AUDITADAS", identidad_repo.ACCIONES_AUDITADAS | apr.ACCIONES)

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM user_tables WHERE table_name IN ("
        "'EJEMPLO_MARCA','MARCA_TERMINO','MARCA_CALIBRACION','DECISION_CAPA','PAGINA_PLIEGO','SENAL_MERCADO')"
    )
    assert cursor.fetchone()[0] == 6

    cli, gid = "test_cli_apr", "test_portal_apr:APR-1"
    try:
        cursor.execute(
            "INSERT INTO portal (portal_id, nombre, pais, zona_horaria, moneda_default, base_url) VALUES "
            "('test_portal_apr', 'Portal', 'SV', 'America/El_Salvador', 'USD', 'https://example.test')"
        )
        cursor.execute("INSERT INTO organizacion (org_id, nombre, pais_sede, zona_horaria) "
                       "VALUES ('test_org_apr', 'Org', 'SV', 'America/El_Salvador')")
        cursor.execute("INSERT INTO cliente (cliente_id, org_id, nombre) VALUES (:c, 'test_org_apr', 'Cliente')", c=cli)
        cursor.execute("INSERT INTO proceso (proceso_gid, portal_id, numero_referencia, titulo) "
                       "VALUES (:g, 'test_portal_apr', 'APR-1', 'Licencias Exadata')", g=gid)

        # ejemplo_marca: embedding in-DB o NULL, idempotente por proceso, SET NULL al borrar el proceso
        r = apr.agregar_ejemplo(conn, cli, "oracle", "positivo", "humana", "Licencias Exadata", proceso_gid=gid,
                                evidencia="Exadata", split="critico", creado_por="test")
        assert r["nuevo"] is True and r["embedding"] in (True, False)
        assert apr.agregar_ejemplo(conn, cli, "oracle", "positivo", "humana", "Licencias Exadata",
                                   proceso_gid=gid)["ejemplo_id"] == r["ejemplo_id"]
        otro = apr.agregar_ejemplo(conn, cli, "oracle", "ajena", "semilla", "Cognos Analytics",
                                   calcular_embedding=False)
        (ej,) = apr.listar_ejemplos(conn, cli, "oracle", split="critico")
        assert ej["ejemplo_id"] == r["ejemplo_id"] and ej["tiene_embedding"] is r["embedding"]
        # El prefijo E5 solo pertenece al vector, no al título persistido.
        assert ej["titulo_texto"] == "Licencias Exadata"
        assert apr.desactivar_ejemplo(conn, r["ejemplo_id"], proteger_humanos=True) is False
        assert apr.desactivar_ejemplo(conn, otro["ejemplo_id"]) is True
        with pytest.raises(oracledb.IntegrityError):
            cursor.execute("INSERT INTO ejemplo_marca (cliente_id, marca_id, etiqueta, fuente, titulo_texto) "
                           "VALUES (:c, 'oracle', 'quizas', 'humana', 't')", c=cli)

        # marca_termino: semilla idempotente, propuesto → activo, semilla → rechazado
        semilla = [("oracle", "Exadata", "fuerte"), ("ibm", "Cognos", "ajena")]
        assert apr.sembrar_terminos(conn, cli, semilla, creado_por="test") == 2
        assert apr.sembrar_terminos(conn, cli, semilla, creado_por="test") == 0
        tid = apr.proponer_termino(conn, cli, "oracle", "Autonomous Database", "fuerte", soporte=5,
                                   precision_est=0.95)
        assert tid is not None
        assert apr.proponer_termino(conn, cli, "oracle", "Autonomous Database", "fuerte", soporte=6) == tid
        assert {t["termino"] for t in apr.listar_terminos_activos(conn, cli, "oracle")} == {"Exadata"}
        assert apr.activar_termino(conn, tid, actor="test") is True
        assert apr.proponer_termino(conn, cli, "oracle", "Autonomous Database", "fuerte") is None  # no lo pisa
        cognos = next(t for t in apr.listar_terminos_activos(conn, cli) if t["termino"] == "Cognos")
        assert apr.rechazar_termino(conn, cognos["termino_id"]) is True
        assert {t["termino"] for t in apr.listar_terminos_activos(conn, cli)} == {"Exadata", "Autonomous Database"}
        assert apr.sembrar_terminos(conn, cli, semilla) == 0  # el rechazo se respeta

        # marca_calibracion: promover archiva la anterior; nunca dos activas
        v1 = apr.registrar_candidata(conn, cli, "oracle", umbral_confirmada=0.9, umbral_posible=0.5, k=5,
                                     pesos_fuentes={"humana": 3.0}, metricas={"precision": 0.9})
        v2 = apr.registrar_candidata(conn, cli, "oracle", umbral_confirmada=0.85, parametros={"w": [0.1, 0.2]})
        vg = apr.registrar_candidata(conn, cli, None, umbral_confirmada=0.95)
        assert apr.calibracion_activa(conn, cli, "oracle") is None
        assert apr.promover(conn, vg) is None
        assert apr.calibracion_activa(conn, cli, "oracle")["version"] == vg  # cae a la global
        assert apr.promover(conn, v1) is None
        assert apr.promover(conn, v2) == v1
        activa = apr.calibracion_activa(conn, cli, "oracle")
        assert activa["version"] == v2 and activa["parametros"] == {"w": [0.1, 0.2]}
        archivada = apr.obtener_calibracion(conn, v1)
        assert archivada["estado"] == "archivada" and archivada["pesos_fuentes"] == {"humana": 3.0}
        with pytest.raises(oracledb.IntegrityError):
            cursor.execute("UPDATE marca_calibracion SET estado = 'activa' WHERE version = :v", v=v1)
        assert apr.promover(conn, v1) == v2  # rollback = reactivar la anterior
        assert apr.archivar(conn, v1) is True and apr.archivar(conn, v1) is False

        # decision_capa: guardar y leer por hash
        did = apr.guardar_decision(conn, cli, gid, "triaje", version="v1", salida={"veredicto": "GO"},
                                   score=0.8123, propension=0.1, costo_usd=0.00042, latencia_ms=950,
                                   entrada_hash="a" * 64)
        d = apr.buscar_decision_por_hash(conn, "a" * 64, "triaje", version="v1", cliente_id=cli)
        assert d["id"] == did and d["salida"] == {"veredicto": "GO"} and d["latencia_ms"] == 950
        assert apr.buscar_decision_por_hash(conn, "a" * 64, "triaje", variante="sombra") is None

        # pagina_pliego / senal_mercado: CHECK y FK del DDL
        cursor.execute("INSERT INTO pagina_pliego (proceso_gid, pagina, texto, fuente) "
                       "VALUES (:g, 1, 'texto', 'capa_texto')", g=gid)
        with pytest.raises(oracledb.IntegrityError):
            cursor.execute("INSERT INTO pagina_pliego (proceso_gid, pagina, fuente) VALUES (:g, 2, 'adivino')",
                           g=gid)
        cursor.execute("INSERT INTO senal_mercado (cliente_id, tipo, institucion_norm, marca_id, n_adj, share_top) "
                       "VALUES (:c, 'institucion_marca', 'MINISTERIO', 'oracle', 3, 0.667)", c=cli)

        conteos = apr.contar_por_tabla(conn, cli)
        assert conteos["ejemplo_marca"] == 2 and conteos["marca_termino"] == 3 and conteos["decision_capa"] == 1
        cursor.execute("SELECT COUNT(*) FROM auditoria WHERE org_id = 'test_org_apr'")
        assert cursor.fetchone()[0] >= 8

        cursor.execute("DELETE FROM proceso WHERE proceso_gid = :g", g=gid)
        cursor.execute("SELECT proceso_gid FROM ejemplo_marca WHERE ejemplo_id = :e", e=r["ejemplo_id"])
        assert cursor.fetchone()[0] is None
        cursor.execute("SELECT COUNT(*) FROM pagina_pliego WHERE proceso_gid = :g", g=gid)
        assert cursor.fetchone()[0] == 0
    finally:
        conn.rollback()
        cursor.close()


# ═══ P3 · contrato de marcas: «sin clasificación» (None) ≠ «clasificado sin marcas» ([]) ══════════════════════

def _marcas_cp_p3(conn, cliente, gid):
    """`cliente_proceso.marcas` tal como lo devuelve Oracle (CLOB → lista)."""
    import json as _json

    cursor = conn.cursor()
    cursor.execute("SELECT JSON_SERIALIZE(marcas RETURNING CLOB) FROM cliente_proceso "
                   "WHERE cliente_id = :c AND proceso_gid = :g", c=cliente, g=gid)
    fila = cursor.fetchone()
    cursor.close()
    if fila is None:
        return None
    valor = fila[0].read() if hasattr(fila[0], "read") else fila[0]
    return _json.loads(valor) if valor else None


def test_p3_marcas_vacias_sobrescriben_y_none_conserva_contra_oracle(conn, dos_clientes_reales, cfg_memoria_e111,
                                                                      monkeypatch):
    """`[]` de un proceso clasificado sobrescribe; sin clasificación se conserva; `actualizar_marcas_v2` reemplaza
    solo las marcas (también un JSON de más de 4000 bytes, por CLOB); una fila vieja `{"marca","role"}` se sigue
    leyendo con los campos nuevos en sus defaults; con la ADB caída (DSN inválido) la fachada devuelve False."""
    import dataclasses
    import types

    from src import history
    from src.memoria import db as memoria_db
    from src.memoria import repo, service
    from src.scraper.models import MarcaRecomendada, Proceso

    a, _ = dos_clientes_reales
    ref = f"TEST-P3-MARCAS-{_uuid.uuid4().hex[:8]}"
    gid = f"{repo.PORTAL_ID_DEFAULT}:{ref}"
    oracle = MarcaRecomendada(marca="Oracle", razon="base de datos", role="Recomendada", score=0.9,
                              evidencia="licencias Oracle Database", evidencia_valida=True, origen="diccionario",
                              veredicto="confirmada", por_que="término fuerte en el título")
    try:
        # 1. Alta con una marca completa: el JSON guarda TODOS los campos.
        con_marca = _clasificado_e111(ref, relevante=True, score=0.9, titulo="Licencias Oracle Database")
        con_marca.marcas_recomendadas = [oracle]
        repo.upsert_proceso_v2(conn, con_marca, a)
        conn.commit()
        assert _marcas_cp_p3(conn, a, gid) == [dataclasses.asdict(oracle)]

        # 2. Reclasificado sin marcas ([]): SÍ sobrescribe a [].
        repo.upsert_proceso_v2(conn, _clasificado_e111(ref, relevante=False, score=0.1), a)
        conn.commit()
        assert _marcas_cp_p3(conn, a, gid) == []

        # 3. actualizar_marcas_v2 con una fila con la forma VIEJA (sin evidencia ni veredicto).
        assert repo.actualizar_marcas_v2(conn, a, gid, '[{"marca": "Oracle", "role": "Alternativa"}]') == 1
        conn.commit()
        assert _marcas_cp_p3(conn, a, gid) == [{"marca": "Oracle", "role": "Alternativa"}]

        # 4. Sin clasificación (Proceso crudo y «sin clasificar» de E11.1): se conserva lo guardado.
        crudo = Proceso(institucion="MINISTERIO E11.1", descripcion=f"Proceso {ref}",
                        fecha_publicacion=datetime.now(timezone.utc).replace(tzinfo=None),
                        url_directa=f"https://example.test/{ref}", numero_referencia=ref)
        repo.upsert_proceso_v2(conn, crudo, a)
        repo.upsert_proceso_v2(conn, _clasificado_e111(ref), a)
        # Forzado (lo que hace el backfill de clasificación con un JSONL viejo): tampoco borra.
        repo.upsert_proceso_v2(conn, _clasificado_e111(ref, relevante=False, score=0.1), a, conservar_marcas=True)
        conn.commit()
        assert _marcas_cp_p3(conn, a, gid) == [{"marca": "Oracle", "role": "Alternativa"}]

        # 5. La fila vieja se sigue leyendo (SQL real de history) con los campos nuevos en sus defaults.
        monkeypatch.setattr(history, "_cfg", lambda: cfg_memoria_e111)
        run = history._leer_run_adb(a)
        assert run is not None
        (p,) = [x for x in run["procesos"] if x.numero_referencia == ref]
        assert p.marcas_recomendadas == [MarcaRecomendada(marca="Oracle", role="Alternativa")]
        assert p.marcas_recomendadas[0].veredicto == "" and p.marcas_recomendadas[0].score is None

        # 6. JSON de más de 4000 bytes (evidencia larga, con tildes): por actualizar_marcas_v2 y por el upsert.
        largas = [dataclasses.replace(oracle, marca=f"Oracle {i}", evidencia="evidencia ñandú " * 200)
                  for i in range(3)]
        assert repo.actualizar_marcas_v2(conn, a, gid, largas) == 1
        conn.commit()
        assert [m["marca"] for m in _marcas_cp_p3(conn, a, gid)] == ["Oracle 0", "Oracle 1", "Oracle 2"]
        con_marca.marcas_recomendadas = largas[:2]
        repo.upsert_proceso_v2(conn, con_marca, a)
        conn.commit()
        assert _marcas_cp_p3(conn, a, gid)[1]["evidencia"] == "evidencia ñandú " * 200

        # 7. Fila inexistente: 0, sin crear nada.
        assert repo.actualizar_marcas_v2(conn, a, f"{gid}-NO-EXISTE", []) == 0
        conn.commit()

        # 8. Fachada con la ADB real: True; con la ADB caída (DSN inválido, NUNCA contraseña mala): False.
        assert service.actualizar_marcas(cfg_memoria_e111, a, ref, []) is True
        assert _marcas_cp_p3(conn, a, gid) == []
        previo = memoria_db._pool
        memoria_db._pool = None
        caida = types.SimpleNamespace(**{**vars(cfg_memoria_e111), "adb_dsn": "127.0.0.1:1/NOEXISTE"})
        try:
            assert service.actualizar_marcas(caida, a, ref, [oracle]) is False
        finally:
            memoria_db.close_pool()
            memoria_db._pool = previo
        assert _marcas_cp_p3(conn, a, gid) == []
    finally:
        _borrar_procesos_e111(conn, [gid])


def test_p8_exploracion_y_paginacion_aprendizaje(conn, dos_clientes_reales):
    """Ejecuta el SQL de la pantalla real: JSON_TABLE, CLOB grande y aislamiento por cliente."""
    import dataclasses

    from src.memoria import repo
    from src.scraper.models import MarcaRecomendada
    from src.web.admin_aprendizaje import _listar

    a, b = dos_clientes_reales
    ref = f"TEST-P8-{_uuid.uuid4().hex[:8]}"
    gid = f"{repo.PORTAL_ID_DEFAULT}:{ref}"
    proceso = _clasificado_e111(ref, relevante=True, score=0.8, titulo="Licencias de ejemplo")
    marca = MarcaRecomendada(marca="Oracle", veredicto="por_confirmar", evidencia="texto público " * 500)
    proceso.marcas_recomendadas = [marca]
    try:
        repo.upsert_proceso_v2(conn, proceso, a)
        filas, _ = _listar(conn, a, 'explore', {'marca_id': 'oracle'}, 1)
        fila = next(f for f in filas if f['proceso_gid'] == gid)
        assert fila['marca_id'] == 'oracle'
        assert fila['marcas'] == [dataclasses.asdict(marca)]
        ajenas, _ = _listar(conn, b, 'explore', {}, 1)
        assert all(f['proceso_gid'] != gid for f in ajenas)
        for seccion in ('labels', 'terms', 'calibration'):
            _listar(conn, a, seccion, {}, 1)
    finally:
        _borrar_procesos_e111(conn, [gid])


def test_c2_paginas_y_busqueda_dirigida_contra_el_motor(conn, portal_comprasal_real):
    """SQL C2 real; solo se sustituye el embedding ONNX ausente del contenedor."""
    from src.aprendizaje import paginas
    from src.memoria import rag

    ref = f"TEST-C2-{_uuid.uuid4().hex[:8]}"
    gid = f"{portal_comprasal_real}:{ref}"
    cursor = conn.cursor()

    class CursorSinOnnx:
        def __init__(self):
            self.real = conn.cursor()

        def execute(self, sql, binds):
            if "VECTOR_EMBEDDING" in sql:
                assert binds["q"].startswith("query: ")
                sql = _sin_modelo_onnx(sql)
                binds = _binds_presentes(sql, binds)
            self.real.execute(sql, binds)

        def fetchall(self):
            return self.real.fetchall()

        def close(self):
            self.real.close()

    class ConexionSinOnnx:
        def cursor(self):
            return CursorSinOnnx()

    try:
        cursor.execute("INSERT INTO proceso (proceso_gid, portal_id, numero_referencia) "
                       "VALUES (:g, :p, :n)", g=gid, p=portal_comprasal_real, n=ref)
        texto = "Carta de autorización del fabricante. Proveedor único."
        lote = [{"pagina": 23, "texto": texto, "fuente": "vision_llm"}]
        assert paginas.guardar_paginas(conn, gid, lote, hash_pdf="a" * 64) == 1
        assert paginas.guardar_paginas(conn, gid, lote, hash_pdf="a" * 64) == 1
        assert len(paginas.leer_paginas(conn, gid)) == 1
        assert paginas.existe(conn, "a" * 64)
        cursor.execute("INSERT INTO chunk (proceso_gid, seq, pagina, texto, embedding) "
                       f"VALUES (:g, 0, 23, :t, {_VECTOR_LITERAL})", g=gid, t=texto)
        resultados = rag.buscar_en_pliego(ConexionSinOnnx(), gid, ["carta del fabricante"], 3)
        assert len(resultados) == 1
        assert resultados[0]["pagina"] == 23
        assert resultados[0]["texto"] == texto
        assert rag.buscar_en_pliego(ConexionSinOnnx(), gid + "-AJENO", ["fabricante"], 3) == []
    finally:
        conn.rollback()


def test_i9_senales_mercado_lexico_y_reemplazo_idempotente(conn, dos_clientes_reales):
    """SQL de C7 contra Oracle local: palabra completa, aislamiento y reemplazo."""
    from src.mercado import repo

    cliente, otro = dos_clientes_reales
    sufijo = _uuid.uuid4().hex[:10]
    institucion = f"Institucion sintetica I9 {sufijo}"
    base_id = -int(sufijo, 16)
    codigos = [f"TEST-I9-{sufijo}-{i}" for i in range(4)]
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO marca_termino (cliente_id, marca_id, termino, tipo, estado, activo) "
            "VALUES (:cliente_id, 'oracle', 'oracle', 'fuerte', 'activo', 1)",
            {"cliente_id": cliente},
        )
        for i, codigo in enumerate(codigos):
            repo.upsert_adjudicacion_item(conn, {
                "id": base_id - i,
                "proceso_compra": {
                    "codigo_proceso": codigo,
                    "nombre_proceso": "Licencias Oracle" if i < 3 else "Servicio oraculo",
                    "fecha_adjudicacion": "2026-01-15",
                },
                "institucion": {"nombre": institucion},
                "proveedor": {"id": base_id, "nombre": "Proveedor sintetico I9"},
            }, False)
            repo.upsert_proceso_participado(conn, cliente, {
                "codigo": codigo, "institucion": institucion,
                "metodo": "Contratacion directa", "fecha": "2026-01-01",
            })
        conn.commit()
        senales = repo.calcular_senales(conn, cliente)
        propias = [s for s in senales if sufijo in s["institucion_norm"]]
        general = next(s for s in propias if s["tipo"] == "institucion")
        afinidad = next(s for s in propias if s["tipo"] == "institucion_marca")
        assert general["n_adj"] == 4
        assert general["n_proveedores"] == 1
        assert general["share_top"] == 1
        assert general["n_directas"] == 4
        assert afinidad["marca_id"] == "oracle"
        assert afinidad["n_adj"] == 3
        for _ in range(2):
            repo.guardar_senales(conn, cliente, propias, version=917)
        cursor.execute(
            "SELECT tipo, COUNT(*) FROM senal_mercado "
            "WHERE cliente_id = :cliente_id AND version = 917 GROUP BY tipo",
            {"cliente_id": cliente},
        )
        assert dict(cursor.fetchall()) == {"institucion": 1, "institucion_marca": 1}
        cursor.execute(
            "SELECT COUNT(*) FROM senal_mercado WHERE cliente_id = :cliente_id AND version = 917",
            {"cliente_id": otro},
        )
        assert cursor.fetchone()[0] == 0
        repo.guardar_senales(conn, cliente, [], version=917)
        cursor.execute(
            "SELECT COUNT(*) FROM senal_mercado WHERE cliente_id = :cliente_id AND version = 917",
            {"cliente_id": cliente},
        )
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.execute("DELETE FROM senal_mercado WHERE cliente_id = :c", {"c": cliente})
        cursor.execute("DELETE FROM marca_termino WHERE cliente_id = :c", {"c": cliente})
        for i, codigo in enumerate(codigos):
            cursor.execute("DELETE FROM adjudicacion_item WHERE id = :id", {"id": base_id - i})
            cursor.execute(
                "DELETE FROM proceso_participado WHERE cliente_id = :c AND codigo_proceso = :codigo",
                {"c": cliente, "codigo": codigo},
            )
        conn.commit()
        cursor.close()


# P10 · paridad del veredicto por marca: consulta sintética, sin DML ni datos reales.
@pytest.mark.parametrize("nombre,nivel", [
    ("Oracle", "confirmada"), ("Oracle NetSuite", "por_confirmar"),
    ("MySQL", "no_competible"), ("Java", None), ("PeopleSoft", "fuera"),
    ("Oracle", "fuera"), ("OracleXYZ", "confirmada"), ("IBM", "confirmada"),
    ("Sun", "confirmada"), ("JD Edwards", "por_confirmar"),
])
def test_p10_paridad_python_sql_veredicto(conn, monkeypatch, nombre, nivel):
    import json
    from types import SimpleNamespace

    from src.memoria import relevancia
    from src.notifier.por_marca import es_de_marca_proceso

    monkeypatch.setattr(relevancia, "get_config", lambda: SimpleNamespace(marca_veredicto="activo"))
    marca = {"marca": nombre}
    if nivel is not None:
        marca["veredicto"] = nivel
    proceso = SimpleNamespace(descripcion="Oracle en título no decide",
                              marcas_recomendadas=[SimpleNamespace(**marca)])
    # La firma del filtro permite seleccionar la capa sin construir una configuración real.
    python_visible = es_de_marca_proceso(proceso, {"oracle"}, modo="activo")
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM (SELECT JSON(:marcas) marcas FROM dual) cp WHERE "
            + relevancia.sql_marca({"oracle"}),
            {"marcas": json.dumps([marca])},
        )
        assert bool(cursor.fetchone()[0]) is python_visible
    finally:
        cursor.close()
