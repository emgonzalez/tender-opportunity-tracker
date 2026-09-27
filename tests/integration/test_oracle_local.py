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
