"""src.memoria.repo — Fase 2 Track B. Ver docs/planes/fase2-track-b.md.

Capa de acceso a datos (repositorio) sobre las tablas de
``licitaciones_metadata`` y ``licitaciones_analisis`` definidas en
``docs/arquitectura/modelo-de-datos.md`` §2 ("Esquema
físico cerrado (DDL Oracle 23ai)").

Responsabilidades de este módulo:
    - Mantener los metadatos de cada proceso (``licitaciones_metadata``):
      alta/actualización (:func:`upsert_proceso`), lectura y escritura de su
      estado en la máquina de estados del pipeline (:func:`get_estado`,
      :func:`set_estado`).
    - Implementar la cola durable (DB-as-queue) que permite reanudar el
      drill-down tras un corte, sin bloqueos entre reintentos
      (:func:`claim_siguiente`, con ``FOR UPDATE SKIP LOCKED``).
    - Persistir el resultado del análisis profundo de cada pliego
      (:func:`save_analisis_db`), en ``licitaciones_analisis``.
    - Ofrecer una analítica agregada simple (sin vectores) sobre el
      histórico (:func:`stats_por_institucion`).

Todas las funciones reciben una conexión ``conn`` ya abierta (oracledb) como
primer parámetro y NO gestionan su ciclo de vida (apertura, commit,
rollback, cierre): eso es responsabilidad exclusiva de
``src.memoria.db.connection``.

**Regla de seguridad innegociable:** todo valor viaja como *bind variable*
(``:nombre``); nunca se concatena o interpola un valor dentro de un string
SQL. La única excepción reconocida en todo Track B es el nombre del modelo
de embeddings in-DB (usado en ``src.memoria.rag``, no en este módulo), que
Oracle no permite pasar como bind por ser un identificador SQL.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import oracledb

from src.memoria.relevancia import SQL_ES_RELEVANTE
from src.portales import PORTAL_ID_DEFAULT

logger = logging.getLogger(__name__)


def upsert_proceso(
    conn: Any,
    proceso: Any,
    cliente_id: str,
    estado: str = "DESCUBIERTO",
) -> None:
    """Da de alta o actualiza los metadatos de un proceso (MERGE idempotente).

    La clave de negocio es ``proceso_id = proceso.numero_referencia`` (el
    código COMPRASAL, ej. "96-2026-P0011"). Si el proceso ya existe
    (``WHEN MATCHED``) se refrescan los campos "volátiles" del proceso
    (institución, título, modalidad, categoría, monto, fechas, URL) y
    ``actualizado_en``; si no existe (``WHEN NOT MATCHED``) se inserta una
    fila nueva con el ``estado`` indicado.

    Mapeo de columnas (``licitaciones_metadata`` ← ``Proceso``):
        - ``institucion``    ← ``proceso.institucion``
        - ``titulo``         ← ``proceso.descripcion`` — **OJO**: en este
          proyecto el dataclass ``Proceso`` (``src/scraper/models.py``) NO
          tiene un campo ``titulo`` separado; el "título oficial" del
          proceso tal como lo publica COMPRASAL vive en ``descripcion``.
          Por eso ``titulo`` (columna) se llena con ``proceso.descripcion``
          (atributo).
        - ``modalidad``      ← ``proceso.forma_contratacion``
        - ``categoria``      ← ``proceso.tipo_actividad``
        - ``monto_estimado`` ← ``proceso.monto``
        - ``fecha_pub``      ← ``proceso.fecha_publicacion``
        - ``fecha_cierre``   ← ``proceso.fecha_limite``
        - ``url_comprasal``  ← ``proceso.url_directa``
        - ``cliente_id``     ← ``cliente_id`` (parámetro)

    **IMPORTANTE:** en la rama ``WHEN MATCHED`` NUNCA se toca la columna
    ``estado``: el estado del pipeline lo gestiona exclusivamente
    :func:`set_estado` (y, en el alta inicial, el parámetro ``estado`` de
    esta misma función). Así, reprocesar un proceso ya visto (por ejemplo,
    porque volvió a aparecer en un scrape posterior) actualiza sus datos
    descriptivos sin "resetear" el progreso que ya llevaba en la cola.

    Nota sobre fechas: ``fecha_pub`` y ``fecha_cierre`` son columnas
    ``DATE`` (sin zona horaria) en Oracle, mientras que
    ``proceso.fecha_publicacion``/``proceso.fecha_limite`` son objetos
    ``datetime`` de Python (potencialmente con ``tzinfo``). Se pasan tal
    cual como bind variable normal: ``oracledb`` los serializa
    automáticamente; Oracle, al no tener columna con zona horaria, conserva
    solo la parte de fecha/hora "de pared" (cualquier ``tzinfo`` se
    descarta implícitamente en la conversión, no se re-calcula a UTC).

    :param conn: conexión oracledb ya abierta.
    :param proceso: instancia de ``src.scraper.models.Proceso`` (o
        compatible: cualquier objeto con los mismos atributos).
    :param cliente_id: identificador del cliente (``ClientProfile.id``)
        dueño de este proceso.
    :param estado: estado inicial a usar SOLO si el proceso es nuevo
        (rama ``NOT MATCHED``). Por defecto ``"DESCUBIERTO"``.
    """
    proceso_id = proceso.numero_referencia

    sql = """
        MERGE INTO licitaciones_metadata tgt
        USING (SELECT :proceso_id AS proceso_id FROM dual) src
        ON (tgt.proceso_id = src.proceso_id)
        WHEN MATCHED THEN UPDATE SET
            tgt.institucion    = :institucion,
            tgt.titulo         = :titulo,
            tgt.modalidad      = :modalidad,
            tgt.categoria      = :categoria,
            tgt.monto_estimado = :monto_estimado,
            tgt.fecha_pub      = :fecha_pub,
            tgt.fecha_cierre   = :fecha_cierre,
            tgt.url_comprasal  = :url_comprasal,
            tgt.cliente_id     = :cliente_id,
            tgt.actualizado_en = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            proceso_id, cliente_id, institucion, titulo, categoria, modalidad,
            monto_estimado, fecha_pub, fecha_cierre, url_comprasal, estado,
            creado_en, actualizado_en
        ) VALUES (
            :proceso_id, :cliente_id, :institucion, :titulo, :categoria, :modalidad,
            :monto_estimado, :fecha_pub, :fecha_cierre, :url_comprasal, :estado,
            SYSTIMESTAMP, SYSTIMESTAMP
        )
    """

    binds = {
        "proceso_id": proceso_id,
        "cliente_id": cliente_id,
        "institucion": proceso.institucion,
        "titulo": proceso.descripcion,
        "categoria": proceso.tipo_actividad,
        "modalidad": proceso.forma_contratacion,
        "monto_estimado": proceso.monto,
        "fecha_pub": proceso.fecha_publicacion,
        "fecha_cierre": proceso.fecha_limite,
        "url_comprasal": proceso.url_directa,
        "estado": estado,
    }

    logger.info(
        "upsert_proceso: proceso_id=%s cliente_id=%s estado(alta)=%s",
        proceso_id, cliente_id, estado,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def set_estado(
    conn: Any,
    proceso_id: str,
    estado: str,
    error: str | None = None,
    worker_id: str | None = None,
    triaje: str | None = None,
    triaje_conf: float | None = None,
    formato: str | None = None,
) -> None:
    """Actualiza el estado (y opcionalmente el error/worker/triaje) de un proceso.

    Escribe ``estado``, ``ultimo_error`` y ``actualizado_en = SYSTIMESTAMP``
    sobre la fila identificada por ``proceso_id``. Si se provee
    ``worker_id`` también se actualiza esa columna (útil para registrar qué
    worker completó o abandonó el procesamiento).

    ``triaje``, ``triaje_conf`` y ``formato`` mapean a las columnas del mismo
    nombre (``triaje``/``triaje_conf``/``formato``) de
    ``licitaciones_metadata``, que existen en el esquema desde el principio
    pero hasta ahora **nunca se escribían**: `ResultadoAnalisis` los calcula
    en memoria (``pipeline.py``) y solo quedaban en el JSONL de la VM, nunca
    en la ADB. Se agregan aquí, y no en una función aparte, porque viajan
    junto con el mismo cambio de estado que los produce (el triaje/formato de
    un pliego no cambia sin que también cambie su estado).

    Todos los valores (incluido ``proceso_id``) viajan como bind variable;
    la única variación por parámetros opcionales es qué *columnas* aparecen
    en el ``SET`` (una decisión estructural, no un valor interpolado).

    :param conn: conexión oracledb ya abierta.
    :param proceso_id: clave de negocio del proceso (``proceso_id``).
    :param estado: nuevo estado (debe ser uno de los valores del
        ``CHECK ck_estado`` de la tabla).
    :param error: mensaje de error a registrar en ``ultimo_error``, o
        ``None`` para limpiarlo (por ejemplo, tras un reintento exitoso).
    :param worker_id: identificador del worker que hace la actualización;
        si se omite, la columna ``worker_id`` no se toca.
    :param triaje: ``"GO"`` | ``"NO-GO"``, o ``None`` para no tocar la columna.
    :param triaje_conf: confianza del triaje (0.0–1.0), o ``None`` para no
        tocar la columna.
    :param formato: ``"NATIVO"`` | ``"ESCANEADO"``, o ``None`` para no tocar
        la columna.
    """
    binds: dict[str, Any] = {"proceso_id": proceso_id, "estado": estado, "error": error}

    columnas_opcionales: dict[str, Any] = {
        "worker_id": worker_id,
        "triaje": triaje,
        "triaje_conf": triaje_conf,
        "formato": formato,
    }
    set_extra = ""
    for columna, valor in columnas_opcionales.items():
        if valor is not None:
            set_extra += f", {columna} = :{columna}"
            binds[columna] = valor

    sql = f"""
        UPDATE licitaciones_metadata
           SET estado = :estado,
               ultimo_error = :error,
               actualizado_en = SYSTIMESTAMP{set_extra}
         WHERE proceso_id = :proceso_id
    """

    logger.info(
        "set_estado: proceso_id=%s estado=%s worker_id=%s error=%s triaje=%s formato=%s",
        proceso_id, estado, worker_id, bool(error), triaje, formato,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def get_estado(conn: Any, proceso_id: str) -> str | None:
    """Consulta el estado actual de un proceso en la cola.

    :param conn: conexión oracledb ya abierta.
    :param proceso_id: clave de negocio del proceso a consultar.
    :return: el valor de la columna ``estado``, o ``None`` si no existe una
        fila con ese ``proceso_id`` en ``licitaciones_metadata``.
    """
    sql = "SELECT estado FROM licitaciones_metadata WHERE proceso_id = :proceso_id"

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"proceso_id": proceso_id})
        fila = cursor.fetchone()
    finally:
        cursor.close()

    estado = fila[0] if fila is not None else None
    logger.info("get_estado: proceso_id=%s -> %s", proceso_id, estado)
    return estado


def claim_siguiente(conn: Any, worker_id: str) -> str | None:
    """Reclama el siguiente proceso pendiente de la cola durable (DB-as-queue).

    Implementa la query de ``docs/arquitectura/modelo-de-datos.md`` §2
    ("Cola durable (DB-as-queue)") como bloque PL/SQL: el ``SELECT ... FOR
    UPDATE SKIP LOCKED`` y el ``UPDATE`` se separan porque Oracle no permite
    ``FOR UPDATE`` dentro de un subquery usado como filtro de ``UPDATE``.

    ``FOR UPDATE SKIP LOCKED`` es lo que hace la cola "durable" y reanudable
    sin necesidad de un broker externo (SQS, RabbitMQ, etc.): si otra
    transacción ya está procesando (bloqueó) la fila candidata, esta
    consulta simplemente la salta y toma la siguiente disponible, en vez de
    esperar el lock. Esto permite que, tras un corte del proceso a mitad de
    camino, una nueva ejecución pueda re-lanzarse sin quedar nunca
    bloqueada esperando una fila "huérfana" (que de todas formas ya no
    tiene ninguna transacción viva sosteniendo el lock).

    En este sistema, sin embargo, la concurrencia real es **1**: solo corre
    un worker a la vez (una VM OCI Free Tier, un timer systemd diario). El
    valor de ``SKIP LOCKED`` aquí no es evitar contención entre workers
    concurrentes (no los hay), sino garantizar que una re-ejecución tras un
    fallo/corte pueda avanzar de inmediato sin depender de que un lock
    "colgado" de la corrida anterior haya expirado.

    Manejo del bind de salida con python-oracledb: se usa
    ``cursor.var(oracledb.STRING)`` para ``:pid``. En un bloque PL/SQL con
    asignación directa (``:pid := v_pid``), ``var.getvalue()`` devuelve el
    valor escalar (``str`` o ``None``).

    :param conn: conexión oracledb ya abierta.
    :param worker_id: identificador del worker que reclama el proceso.
    :return: el ``proceso_id`` reclamado, o ``None`` si no hay ningún
        proceso pendiente (``estado='DESCARGADO'`` con ``intentos < 5``).
    """
    # Oracle no permite FOR UPDATE con FETCH FIRST (usa window functions
    # internamente) ni en subqueries de UPDATE. Se resuelve con un cursor
    # PL/SQL explícito + WHERE CURRENT OF para actualizar la fila bloqueada.
    sql = """
    DECLARE
      CURSOR c IS
        SELECT proceso_id
          FROM licitaciones_metadata
         WHERE estado = 'DESCARGADO' AND intentos < 5
         ORDER BY fecha_cierre NULLS LAST
           FOR UPDATE SKIP LOCKED;
      v_pid VARCHAR2(64);
    BEGIN
      OPEN c;
      FETCH c INTO v_pid;
      IF c%NOTFOUND THEN
        v_pid := NULL;
      ELSE
        UPDATE licitaciones_metadata
           SET estado = 'FORMATO_NATIVO', worker_id = :wid,
               intentos = intentos + 1, actualizado_en = SYSTIMESTAMP
         WHERE CURRENT OF c;
      END IF;
      CLOSE c;
      :pid := v_pid;
    END;
    """

    cursor = conn.cursor()
    try:
        pid_var = cursor.var(oracledb.STRING)
        cursor.execute(sql, {"wid": worker_id, "pid": pid_var})
        valor = pid_var.getvalue()
    finally:
        cursor.close()

    # En PL/SQL assignment (:pid := v_pid), getvalue() devuelve el escalar
    # directamente (str o None), no una lista como con RETURNING INTO.
    if not valor:
        logger.info("claim_siguiente: worker_id=%s -> ningún proceso pendiente", worker_id)
        return None

    logger.info("claim_siguiente: worker_id=%s -> proceso_id=%s", worker_id, valor)
    return valor


def save_analisis_db(conn: Any, resultado: Any) -> None:
    """Guarda (o actualiza) el resultado del análisis profundo de un pliego.

    MERGE idempotente sobre ``licitaciones_analisis``, con clave
    ``proceso_id = resultado.proceso_uuid``.

    Mapeo de columnas (``licitaciones_analisis`` ← ``ResultadoAnalisis``):
        - ``matriz_riesgos`` ← ``resultado.matriz`` (``MatrizRiesgos | None``)
        - ``anexos``         ← ``resultado.anexos`` (``dict``)
        - ``modelo_llm``     ← ``resultado.modelo_analisis``
        - ``generado_en``    ← ``SYSTIMESTAMP`` (no se usa el
          ``resultado.generado_en`` de Python; se prefiere el reloj de la
          base de datos como fuente única de verdad para la marca temporal
          de persistencia).

    Serialización de las columnas JSON nativas de 23ai: ``matriz_riesgos``
    y ``anexos`` se serializan a texto con ``json.dumps`` ANTES de pasarlas
    como bind:
        - ``matriz_riesgos``: si ``resultado.matriz`` es ``None`` se pasa
          ``None`` (columna JSON queda ``NULL``); si no,
          ``json.dumps(dataclasses.asdict(resultado.matriz))``.
        - ``anexos``: ``json.dumps(resultado.anexos)`` (siempre un dict,
          aunque sea ``{}``).

    Elección para insertar en la columna ``JSON``: se envuelve el bind con
    la función SQL ``JSON(:bind)`` en vez de confiar en la conversión
    implícita VARCHAR2→JSON. Motivo: ``JSON(...)`` fuerza una validación y
    conversión explícitas del texto a un valor JSON nativo en el momento de
    la escritura (fallando de forma clara con un texto mal formado), en vez
    de depender de que la conversión implícita esté habilitada/soportada en
    la versión/configuración exacta de la ADB de destino. El valor en sí
    sigue viajando 100% como bind variable (``JSON(:matriz_riesgos)``); lo
    único que cambia es que se envuelve con una función SQL, no que se
    interpole el contenido.

    :param conn: conexión oracledb ya abierta.
    :param resultado: instancia de
        ``src.scraper.models.ResultadoAnalisis`` (o compatible).
    """
    matriz = resultado.matriz
    matriz_json = json.dumps(dataclasses.asdict(matriz)) if matriz is not None else None
    anexos_json = json.dumps(resultado.anexos if resultado.anexos is not None else {})

    sql = """
        MERGE INTO licitaciones_analisis tgt
        USING (SELECT :proceso_id AS proceso_id FROM dual) src
        ON (tgt.proceso_id = src.proceso_id)
        WHEN MATCHED THEN UPDATE SET
            tgt.matriz_riesgos = JSON(:matriz_riesgos),
            tgt.anexos         = JSON(:anexos),
            tgt.modelo_llm     = :modelo_llm,
            tgt.generado_en    = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            proceso_id, matriz_riesgos, anexos, modelo_llm, generado_en
        ) VALUES (
            :proceso_id, JSON(:matriz_riesgos), JSON(:anexos), :modelo_llm, SYSTIMESTAMP
        )
    """

    binds = {
        "proceso_id": resultado.proceso_uuid,
        "matriz_riesgos": matriz_json,
        "anexos": anexos_json,
        "modelo_llm": resultado.modelo_analisis,
    }

    logger.info(
        "save_analisis_db: proceso_id=%s modelo_llm=%s matriz_presente=%s",
        resultado.proceso_uuid, resultado.modelo_analisis, matriz is not None,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def stats_por_institucion(conn: Any, dias: int = 90) -> list[dict]:
    """Analítica SQL pura (sin vectores) de procesos por institución/categoría.

    Cuenta los procesos publicados en los últimos ``dias`` días
    (``fecha_pub >= SYSDATE - :dias``), agrupados por institución y
    categoría, junto con el porcentaje de ellos que obtuvo ``triaje='GO'``.
    No usa ``VECTOR_DISTANCE`` ni ``VECTOR_EMBEDDING``: es una consulta
    analítica convencional sobre ``licitaciones_metadata``.

    :param conn: conexión oracledb ya abierta.
    :param dias: ventana de tiempo (en días) hacia atrás a considerar,
        según ``fecha_pub``. Por defecto 90.
    :return: lista de dicts, uno por combinación (institución, categoría),
        ordenada por ``conteo`` descendente. Cada dict tiene las claves
        ``institucion``, ``categoria``, ``conteo`` (total de procesos) y
        ``pct_go`` (porcentaje, 0.0–100.0, redondeado a 2 decimales, de
        procesos con ``triaje='GO'``; 0.0 si ``conteo`` es 0).
    """
    sql = """
        SELECT m.institucion,
               m.categoria,
               COUNT(*) AS conteo,
               ROUND(
                   100 * SUM(CASE WHEN m.triaje = 'GO' THEN 1 ELSE 0 END) / COUNT(*),
                   2
               ) AS pct_go
        FROM   licitaciones_metadata m
        WHERE  m.fecha_pub >= SYSDATE - :dias
        GROUP  BY m.institucion, m.categoria
        ORDER  BY conteo DESC
    """

    logger.info("stats_por_institucion: dias=%s", dias)

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"dias": dias})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    resultado = [
        {
            "institucion": institucion,
            "categoria": categoria,
            "conteo": conteo,
            "pct_go": float(pct_go) if pct_go is not None else 0.0,
        }
        for institucion, categoria, conteo, pct_go in filas
    ]

    logger.info("stats_por_institucion: %d combinaciones institución/categoría", len(resultado))
    return resultado


def stats_por_institucion_v2(conn: Any, cliente_id: str, dias: int = 90) -> list[dict]:
    """Espejo v2 de :func:`stats_por_institucion` — Fase "switch" (plan del
    portal §3.7 punto 4), con el mismo cambio de firma deliberado que
    :func:`src.memoria.rag.tendencias_v2` y por el mismo motivo: ``triaje``
    es una decisión por cliente, vive en ``cliente_proceso`` (PK con
    ``cliente_id``), no en un hecho global como ``licitaciones_metadata``.
    Ver el docstring de ``tendencias_v2`` para el razonamiento completo —
    esta función y esa son, a propósito, la misma agregación con el mismo
    problema de fondo, replicada en dos módulos distintos desde v1.

    Mismas claves de retorno que v1 (``institucion``, ``categoria``,
    ``conteo``, ``pct_go``), aunque la columna subyacente de categoría sea
    ``categoria_raw``.

    Desde E11.1 ``cliente_proceso`` guarda también los procesos clasificados
    como NO relevantes (ver :func:`src.memoria.service.registrar_clasificados`):
    sin el filtro :data:`~src.memoria.relevancia.SQL_ES_RELEVANTE` esos
    inflarían ``conteo`` y diluirían ``pct_go``, que siempre se calcularon
    sobre lo relevante.
    """
    sql = f"""
        SELECT p.institucion,
               p.categoria_raw,
               COUNT(*) AS conteo,
               ROUND(
                   100 * SUM(CASE WHEN cp.triaje = 'GO' THEN 1 ELSE 0 END) / COUNT(*),
                   2
               ) AS pct_go
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
          AND  {SQL_ES_RELEVANTE}
        GROUP  BY p.institucion, p.categoria_raw
        ORDER  BY conteo DESC
    """

    logger.info("stats_por_institucion_v2: cliente_id=%s dias=%s", cliente_id, dias)

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    resultado = [
        {
            "institucion": institucion,
            "categoria": categoria_raw,
            "conteo": conteo,
            "pct_go": float(pct_go) if pct_go is not None else 0.0,
        }
        for institucion, categoria_raw, conteo, pct_go in filas
    ]

    logger.info(
        "stats_por_institucion_v2: %d combinaciones institución/categoría para cliente_id=%s",
        len(resultado), cliente_id,
    )
    return resultado


# ── Historial de corridas (reemplaza el last_run.json de un solo proceso) ────
#
# Por qué existe: `_consecutive_errors` (main.py) es una variable de proceso, y
# cada corrida diaria es un `docker run --rm` — un proceso NUEVO. El circuit
# breaker que ese contador alimenta nunca se activa en producción:
# `_consecutive_errors` siempre arranca en 0. Esta tabla es lo que permite
# contar fallos consecutivos de verdad, mirando corridas pasadas.

def insertar_ejecucion(
    conn: Any,
    run_timestamp,
    success: bool,
    procesos_revisados: int = 0,
    clientes_procesados: int = 0,
    version: str = "",
    error: str | None = None,
) -> None:
    """Registra una corrida. Nunca actualiza una fila existente: cada corrida
    es un INSERT nuevo, es historial append-only, no un "último estado"."""
    sql = """
        INSERT INTO ejecucion (
            run_timestamp, success, procesos_revisados, clientes_procesados,
            version, error_msg
        ) VALUES (
            :run_timestamp, :success, :procesos_revisados, :clientes_procesados,
            :version, :error_msg
        )
    """
    binds = {
        "run_timestamp": run_timestamp,
        "success": 1 if success else 0,
        "procesos_revisados": procesos_revisados,
        "clientes_procesados": clientes_procesados,
        "version": (version or "")[:32],
        "error_msg": (error or None) if error is None else str(error)[:1000],
    }
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


# ── Espejo del esquema v2 multi-tenant/multi-país (db/schema_v2.sql) ────────
#
# Fase "Doble escritura" de la migración (plan del portal AMALIA §3.3). Estas
# funciones NO reemplazan a upsert_proceso/set_estado/save_analisis_db: se
# llaman ADEMÁS, desde src.memoria.service, cada una envuelta en su PROPIO
# try/except (ver ``service.py``) — un fallo acá nunca debe poder revertir ni
# bloquear la escritura del esquema actual, que ya está en producción.
#
# `portal_id` es fijo por ahora (hoy AMALIA vigila un solo portal, COMPRASAL):
# se vuelve un parámetro real cuando exista un segundo adaptador de portal
# (plan §4). Vive como constante de módulo, no en config, porque es un hecho
# del dominio (qué portal es "el de siempre"), no un ajuste de despliegue.


_ORDEN_SEVERIDAD = {"alta": 3, "media": 2, "baja": 1}


def _proceso_gid(numero_referencia: str, portal_id: str = PORTAL_ID_DEFAULT) -> str:
    """``proceso_gid`` = ``portal_id:numero_referencia`` (ver db/schema_v2.sql §3)."""
    return f"{portal_id}:{numero_referencia}"


def _bool_a_ora(valor: bool | None) -> int | None:
    """``True``/``False``/``None`` → ``1``/``0``/``None`` (columna ``NUMBER(1)``)."""
    return None if valor is None else (1 if valor else 0)


def _severidad_max(matriz: Any) -> str | None:
    """La severidad más alta entre los items de una matriz de riesgos, o None."""
    if matriz is None or not getattr(matriz, "items", None):
        return None
    mejor = max(matriz.items, key=lambda item: _ORDEN_SEVERIDAD.get(item.severidad, 0))
    return mejor.severidad


def upsert_proceso_v2(
    conn: Any,
    proceso: Any,
    cliente_id: str,
    estado: str = "DESCUBIERTO",
    portal_id: str = PORTAL_ID_DEFAULT,
) -> None:
    """Espejo v2 de :func:`upsert_proceso`: separa el HECHO del portal
    (tabla ``proceso``, sin ``cliente_id``) de la vista de este cliente sobre
    él (tabla ``cliente_proceso``, con ``estado``). Dos MERGE, no uno: el
    primer cliente que ve un proceso lo crea; los siguientes solo lo leen y
    escriben su propia fila en ``cliente_proceso`` — es justamente esta
    separación la que hace estructuralmente imposible el bug de
    ``licitaciones_metadata`` (el MERGE del segundo cliente le pisa el
    ``cliente_id`` al primero).

    Igual que en :func:`upsert_proceso`, ``WHEN MATCHED`` de
    ``cliente_proceso`` NUNCA toca ``estado``: eso es responsabilidad
    exclusiva de :func:`set_estado_v2`. Tampoco pisa con vacío
    ``tags``/``marcas``/``categoria_detectada``: si el valor nuevo viene
    vacío se conserva el que ya hay.

    Desde E11.1 (lo llama también :func:`src.memoria.service.registrar_clasificados`
    con TODO lo clasificado): escribe ``clasificado_en`` cuando ``proceso``
    trae clasificación, y no baja ``es_relevante`` de 1 a 0 en una fila ya
    ``ANALIZADO``/``NOTIFICADO`` (ver el comentario sobre el SQL).

    :param conn: conexión oracledb ya abierta.
    :param proceso: ``Proceso`` o ``ProcesoClasificado`` (los campos de
        clasificación — ``es_relevante``, ``match_tecnico``, etc. — se leen
        con ``getattr`` y quedan en ``NULL`` si no existen).
    :param cliente_id: dueño de esta vista sobre el proceso.
    :param estado: estado inicial, solo si la fila de ``cliente_proceso`` es
        nueva (misma semántica que ``upsert_proceso``).
    :param portal_id: portal de origen. Por ahora siempre
        :data:`PORTAL_ID_DEFAULT`.
    """
    proceso_gid = _proceso_gid(proceso.numero_referencia, portal_id)

    sql_proceso = """
        MERGE INTO proceso tgt
        USING (SELECT :proceso_gid AS proceso_gid FROM dual) src
        ON (tgt.proceso_gid = src.proceso_gid)
        WHEN MATCHED THEN UPDATE SET
            tgt.institucion      = :institucion,
            tgt.titulo           = :titulo,
            tgt.categoria_raw    = :categoria_raw,
            tgt.modalidad_raw    = :modalidad_raw,
            -- El listado de COMPRASAL no trae el cierre ni el monto (los obtiene el drilldown): una
            -- reclasificación posterior no debe borrar lo que el drilldown ya consiguió.
            tgt.monto_original   = COALESCE(:monto_original, tgt.monto_original),
            tgt.fecha_pub_utc    = :fecha_pub_utc,
            tgt.fecha_limite_utc = COALESCE(:fecha_limite_utc, tgt.fecha_limite_utc),
            tgt.url_portal       = :url_portal,
            tgt.actualizado_en   = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            proceso_gid, portal_id, numero_referencia, institucion, titulo,
            categoria_raw, modalidad_raw, monto_original, fecha_pub_utc,
            fecha_limite_utc, url_portal
        ) VALUES (
            :proceso_gid, :portal_id, :numero_referencia, :institucion, :titulo,
            :categoria_raw, :modalidad_raw, :monto_original, :fecha_pub_utc,
            :fecha_limite_utc, :url_portal
        )
    """
    binds_proceso = {
        "proceso_gid": proceso_gid,
        "portal_id": portal_id,
        "numero_referencia": proceso.numero_referencia,
        "institucion": proceso.institucion,
        "titulo": proceso.descripcion,
        "categoria_raw": proceso.tipo_actividad,
        "modalidad_raw": proceso.forma_contratacion,
        "monto_original": proceso.monto,
        "fecha_pub_utc": proceso.fecha_publicacion,
        "fecha_limite_utc": proceso.fecha_limite,
        "url_portal": proceso.url_directa,
    }

    tags = json.dumps(list(getattr(proceso, "tags", None) or []))
    marcas_recomendadas = getattr(proceso, "marcas_recomendadas", None) or []
    marcas = json.dumps([dataclasses.asdict(m) for m in marcas_recomendadas])

    # `es_relevante` en WHEN MATCHED: una fila que ya llegó a ANALIZADO/NOTIFICADO
    # pasó el filtro de relevancia y tiene pliego analizado (y quizá correo enviado).
    # Desde E11.1 cada corrida reescribe la clasificación de todo lo que ve; si el
    # LLM, al volver a ver el mismo proceso otro día, lo marca no relevante (o quien
    # llama no trae clasificación y el bind es NULL), bajar el 1 haría desaparecer
    # del portal un análisis ya hecho y ya notificado. Por eso se conserva el 1 en
    # esos dos estados; en cualquier otro estado manda la clasificación nueva.
    #
    # `estado` sigue sin aparecer en WHEN MATCHED (solo en el INSERT): una fila ya
    # avanzada (DESCARGADO, ANALIZADO, NOTIFICADO…) nunca retrocede a DESCUBIERTO.
    #
    # `clasificado_en` solo se escribe si quien llama trae una clasificación real
    # (`es_relevante` no es None): el backfill sin clasificación no debe estampar
    # una fecha de clasificación que nunca ocurrió.
    #
    # Los CAST son por tipos de Oracle: las ramas de un CASE deben ser del mismo
    # tipo, y python-oracledb liga `None` como VARCHAR2 (THEN 1 ELSE <varchar> →
    # ORA-00932), y SYSTIMESTAMP es WITH TIME ZONE mientras la columna es TIMESTAMP.
    sql_cliente_proceso = """
        MERGE INTO cliente_proceso tgt
        USING (SELECT :cliente_id AS cliente_id, :proceso_gid AS proceso_gid FROM dual) src
        ON (tgt.cliente_id = src.cliente_id AND tgt.proceso_gid = src.proceso_gid)
        WHEN MATCHED THEN UPDATE SET
            tgt.es_relevante        = CASE WHEN tgt.estado IN ('ANALIZADO', 'NOTIFICADO')
                                            AND tgt.es_relevante = 1
                                           THEN 1 ELSE CAST(:es_relevante AS NUMBER(1)) END,
            tgt.match_tecnico       = :match_tecnico,
            tgt.match_categoria     = :match_categoria,
            tgt.factibilidad        = :factibilidad,
            tgt.categoria_detectada = COALESCE(:categoria_detectada, tgt.categoria_detectada),
            tgt.tags                = CASE WHEN :tags_vacio = 1
                                           THEN tgt.tags ELSE JSON(:tags) END,
            tgt.marcas              = CASE WHEN :marcas_vacio = 1
                                           THEN tgt.marcas ELSE JSON(:marcas) END,
            tgt.razon               = :razon,
            tgt.clasificado_en      = CASE WHEN :clasificado = 1
                                           THEN CAST(SYSTIMESTAMP AS TIMESTAMP)
                                           ELSE tgt.clasificado_en END,
            tgt.actualizado_en      = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            cliente_id, proceso_gid, estado, es_relevante, match_tecnico,
            match_categoria, factibilidad, categoria_detectada, tags, marcas, razon,
            clasificado_en
        ) VALUES (
            :cliente_id, :proceso_gid, :estado, :es_relevante, :match_tecnico,
            :match_categoria, :factibilidad, :categoria_detectada, JSON(:tags), JSON(:marcas), :razon,
            CASE WHEN :clasificado = 1 THEN CAST(SYSTIMESTAMP AS TIMESTAMP) END
        )
    """
    binds_cliente_proceso = {
        "cliente_id": cliente_id,
        "proceso_gid": proceso_gid,
        "estado": estado,
        "es_relevante": _bool_a_ora(getattr(proceso, "es_relevante", None)),
        "match_tecnico": getattr(proceso, "match_tecnico", None),
        "match_categoria": getattr(proceso, "match_categoria", None),
        "factibilidad": getattr(proceso, "factibilidad", None),
        "categoria_detectada": getattr(proceso, "categoria_detectada", None) or None,
        "tags": tags,
        "marcas": marcas,
        # Un valor vacío NO pisa el existente (ver el WHEN MATCHED): quien
        # llama sin clasificación —p. ej. el backfill desde
        # licitaciones_metadata— no debe borrar tags/marcas/categoría ya buenos.
        "tags_vacio": 0 if getattr(proceso, "tags", None) else 1,
        "marcas_vacio": 0 if marcas_recomendadas else 1,
        "razon": getattr(proceso, "razon", None) or None,
        "clasificado": 0 if getattr(proceso, "es_relevante", None) is None else 1,
    }

    logger.info(
        "upsert_proceso_v2: proceso_gid=%s cliente_id=%s estado(alta)=%s",
        proceso_gid, cliente_id, estado,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql_proceso, binds_proceso)
        cursor.execute(sql_cliente_proceso, binds_cliente_proceso)
    finally:
        cursor.close()


def get_estado_v2(
    conn: Any,
    cliente_id: str,
    proceso_id: str,
    portal_id: str = PORTAL_ID_DEFAULT,
) -> str | None:
    """Espejo v2 de :func:`get_estado`: lee de ``cliente_proceso``, no de
    ``licitaciones_metadata``. A diferencia del v1, requiere ``cliente_id``
    (parte de la clave) — es justamente esta partición la que evita que un
    cliente lea el ``estado`` que en realidad corresponde a la última
    ejecución de OTRO cliente sobre el mismo proceso (ver el docstring de
    :func:`upsert_proceso_v2`).

    :param conn: conexión oracledb ya abierta.
    :param cliente_id: dueño de la vista sobre el proceso.
    :param proceso_id: clave de negocio del proceso a consultar
        (``numero_referencia``, no ``proceso_gid``).
    :param portal_id: portal de origen. Por ahora siempre
        :data:`PORTAL_ID_DEFAULT`.
    :return: el valor de la columna ``estado``, o ``None`` si no existe una
        fila ``(cliente_id, proceso_gid)`` en ``cliente_proceso``.
    """
    proceso_gid = _proceso_gid(proceso_id, portal_id)
    sql = (
        "SELECT estado FROM cliente_proceso "
        "WHERE cliente_id = :cliente_id AND proceso_gid = :proceso_gid"
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "proceso_gid": proceso_gid})
        fila = cursor.fetchone()
    finally:
        cursor.close()

    estado = fila[0] if fila is not None else None
    logger.info("get_estado_v2: cliente_id=%s proceso_gid=%s -> %s", cliente_id, proceso_gid, estado)
    return estado


def set_estado_v2(
    conn: Any,
    proceso_id: str,
    cliente_id: str,
    estado: str,
    error: str | None = None,
    triaje: str | None = None,
    triaje_conf: float | None = None,
    formato: str | None = None,
    portal_id: str = PORTAL_ID_DEFAULT,
) -> None:
    """Espejo v2 de :func:`set_estado`: actualiza ``cliente_proceso``, no
    ``licitaciones_metadata``. A diferencia del v1, requiere ``cliente_id``
    (parte de la clave) y no toca ``worker_id`` (esa columna no existe en
    ``cliente_proceso``: la cola durable sigue viviendo solo en el esquema
    actual mientras dure la migración).
    """
    proceso_gid = _proceso_gid(proceso_id, portal_id)
    binds: dict[str, Any] = {
        "cliente_id": cliente_id, "proceso_gid": proceso_gid,
        "estado": estado, "error": error,
    }

    columnas_opcionales: dict[str, Any] = {
        "triaje": triaje, "triaje_conf": triaje_conf, "formato": formato,
    }
    set_extra = ""
    for columna, valor in columnas_opcionales.items():
        if valor is not None:
            set_extra += f", {columna} = :{columna}"
            binds[columna] = valor

    sql = f"""
        UPDATE cliente_proceso
           SET estado = :estado,
               ultimo_error = :error,
               actualizado_en = SYSTIMESTAMP{set_extra}
         WHERE cliente_id = :cliente_id AND proceso_gid = :proceso_gid
    """

    logger.info(
        "set_estado_v2: proceso_gid=%s cliente_id=%s estado=%s triaje=%s formato=%s",
        proceso_gid, cliente_id, estado, triaje, formato,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def claim_siguiente_v2(
    conn: Any,
    cliente_id: str,
    worker_id: str,
    portal_id: str = PORTAL_ID_DEFAULT,
) -> str | None:
    """Espejo v2 de :func:`claim_siguiente` — Fase "switch" (plan del portal
    §3.7 punto 4), sin llamador real hoy (igual que la v1).

    Cambio de fondo respecto a v1, impuesto por el propio esquema: la cola
    v1 es global (un solo ``estado='DESCARGADO'`` por ``proceso_id`` en toda
    la app); en v2, "DESCARGADO" es un estado *por cliente* (vive en
    ``cliente_proceso``, junto con ``triaje``/``intentos``/etc. — ver
    :func:`upsert_proceso_v2`), así que la cola necesariamente se vuelve
    por-cliente: dos clientes pueden estar en estados distintos para el
    mismo ``proceso_gid`` (lo prueba
    ``test_cliente_proceso_no_pisa_entre_clientes`` en
    ``tests/integration/test_oracle_local.py``), y "el siguiente pendiente"
    solo tiene sentido acotado a UN cliente.

    ``worker_id`` se conserva en la firma solo para logging — **no se
    persiste** (``cliente_proceso`` no tiene columna ``worker_id``, mismo
    hueco ya documentado en :func:`set_estado_v2`: la cola durable con
    identidad de worker sigue viviendo solo en el esquema v1 mientras dure
    la migración).

    El ``FOR UPDATE OF cp.estado`` (con JOIN a ``proceso`` para poder
    ordenar por ``fecha_limite_utc``, que no vive en ``cliente_proceso``) es
    más frágil que el ``FOR UPDATE`` sin JOIN de v1 — Oracle lo soporta,
    pero se valida contra el motor real en
    ``tests/integration/test_oracle_local.py``, no solo contra el mock de
    ``tests/test_repo_sql.py``.

    :param conn: conexión oracledb ya abierta.
    :param cliente_id: dueño de la cola sobre la que se reclama.
    :param worker_id: identificador del worker que reclama el proceso (solo
        logging, ver arriba).
    :param portal_id: portal de origen. Por ahora siempre
        :data:`PORTAL_ID_DEFAULT`.
    :return: el ``numero_referencia`` reclamado, o ``None`` si no hay ningún
        proceso pendiente para este cliente.
    """
    sql = """
    DECLARE
      CURSOR c IS
        SELECT cp.proceso_gid, p.numero_referencia
          FROM cliente_proceso cp
          JOIN proceso p ON p.proceso_gid = cp.proceso_gid
         WHERE cp.cliente_id = :cid
           AND cp.estado = 'DESCARGADO' AND cp.intentos < 5
         ORDER BY p.fecha_limite_utc NULLS LAST
           FOR UPDATE OF cp.estado SKIP LOCKED;
      v_gid VARCHAR2(96);
      v_ref VARCHAR2(64);
    BEGIN
      OPEN c;
      FETCH c INTO v_gid, v_ref;
      IF c%NOTFOUND THEN
        v_ref := NULL;
      ELSE
        UPDATE cliente_proceso
           SET estado = 'FORMATO_NATIVO',
               intentos = intentos + 1, actualizado_en = SYSTIMESTAMP
         WHERE CURRENT OF c;
      END IF;
      CLOSE c;
      :ref := v_ref;
    END;
    """

    cursor = conn.cursor()
    try:
        ref_var = cursor.var(oracledb.STRING)
        cursor.execute(sql, {"cid": cliente_id, "ref": ref_var})
        valor = ref_var.getvalue()
    finally:
        cursor.close()

    if not valor:
        logger.info(
            "claim_siguiente_v2: cliente_id=%s worker_id=%s -> ningún proceso pendiente",
            cliente_id, worker_id,
        )
        return None

    logger.info(
        "claim_siguiente_v2: cliente_id=%s worker_id=%s -> numero_referencia=%s",
        cliente_id, worker_id, valor,
    )
    return valor


def save_analisis_v2(
    conn: Any,
    resultado: Any,
    cliente_id: str,
    portal_id: str = PORTAL_ID_DEFAULT,
) -> None:
    """Espejo v2 de :func:`save_analisis_db`: MERGE en ``analisis`` con clave
    ``(cliente_id, proceso_gid)`` en vez de la clave global ``proceso_id`` de
    ``licitaciones_analisis``. Esto es lo que permite que dos clientes
    analicen el mismo proceso sin pisarse — la FK exige que la fila de
    ``cliente_proceso`` correspondiente ya exista (la crea
    :func:`upsert_proceso_v2`, llamado siempre antes en ``service.py``).

    ``monto_estimado`` viaja tal cual desde ``resultado`` (ya existe en el
    esquema actual desde PR #33: COMPRASAL no publica montos, solo el pliego
    los declara). ``severidad_max`` se calcula acá, no en el pipeline: es un
    derivado de ``matriz.items`` que no vale la pena mantener en Python.
    """
    proceso_gid = _proceso_gid(resultado.proceso_uuid, portal_id)
    matriz = resultado.matriz
    matriz_json = json.dumps(dataclasses.asdict(matriz)) if matriz is not None else None

    sql = """
        MERGE INTO analisis tgt
        USING (SELECT :cliente_id AS cliente_id, :proceso_gid AS proceso_gid FROM dual) src
        ON (tgt.cliente_id = src.cliente_id AND tgt.proceso_gid = src.proceso_gid)
        WHEN MATCHED THEN UPDATE SET
            tgt.matriz_riesgos = JSON(:matriz_riesgos),
            tgt.severidad_max  = :severidad_max,
            tgt.resumen        = :resumen,
            tgt.modelo_llm     = :modelo_llm,
            tgt.monto_estimado = :monto_estimado,
            tgt.generado_en    = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            cliente_id, proceso_gid, matriz_riesgos, severidad_max, resumen,
            modelo_llm, monto_estimado, generado_en
        ) VALUES (
            :cliente_id, :proceso_gid, JSON(:matriz_riesgos), :severidad_max, :resumen,
            :modelo_llm, :monto_estimado, SYSTIMESTAMP
        )
    """
    binds = {
        "cliente_id": cliente_id,
        "proceso_gid": proceso_gid,
        "matriz_riesgos": matriz_json,
        "severidad_max": _severidad_max(matriz),
        "resumen": (matriz.resumen if matriz is not None else None) or None,
        "modelo_llm": resultado.modelo_analisis,
        "monto_estimado": resultado.monto_estimado,
    }

    logger.info(
        "save_analisis_v2: proceso_gid=%s cliente_id=%s modelo_llm=%s",
        proceso_gid, cliente_id, resultado.modelo_analisis,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
    finally:
        cursor.close()


def contar_fallos_consecutivos(conn: Any) -> int:
    """Fallos consecutivos mirando el historial real, no una variable de proceso.

    Cuenta las corridas más recientes con `success = 0`, en orden descendente
    por `run_timestamp`, hasta la primera `success = 1` (o hasta agotar la
    tabla). Sin ninguna corrida registrada todavía, devuelve 0 — no hay
    historial que diga lo contrario.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT success FROM ejecucion ORDER BY run_timestamp DESC, id DESC "
            "FETCH FIRST 50 ROWS ONLY"
        )
        filas = cursor.fetchall()
    finally:
        cursor.close()

    fallos = 0
    for (success,) in filas:
        if success == 1:
            break
        fallos += 1
    return fallos
