"""src.memoria.vectorizer — Vectorización in-database (Fase 2 Track B, tarea B2.3).

Los embeddings NO se calculan con una API externa: se generan **dentro de
Oracle 23ai** mediante la función SQL `VECTOR_EMBEDDING(<modelo> USING :texto
AS data)`, que invoca un modelo ONNX (p.ej. `MINILM_L6`, 384 dims) cargado en
la base de datos. Este módulo solo orquesta el chunking (delegado a
`src.memoria.chunker.chunk_text`) y la inserción por lotes en
`licitaciones_chunks`, usando una conexión (`conn`) ya abierta por
`src.memoria.db` — este módulo nunca abre ni cierra conexiones, ni hace commit
(eso es responsabilidad del context manager que entrega la conexión).

Nota de seguridad: el nombre del modelo ONNX es un **identificador SQL**, no
un valor de datos, por lo que la librería `python-oracledb` no permite pasarlo
como bind variable — debe interpolarse en el texto del SQL. Como toda cadena
interpolada en SQL, es una superficie de inyección si no se valida. Por eso
`_validar_nombre_modelo` aplica un allowlist estricto de identificador Oracle
antes de que el valor toque cualquier sentencia SQL.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.memoria.chunker import chunk_text

logger = logging.getLogger(__name__)

# Identificador Oracle válido: empieza con letra, sigue con letras/dígitos/'_',
# máximo 30 caracteres (límite clásico de identificador Oracle sin comillas).
_MODELO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")

# VARCHAR2(80) es el límite físico de la columna chunk_id (ver DDL en
# docs/arquitectura/modelo-de-datos.md §2).
_CHUNK_ID_MAX_LEN = 80


def _validar_nombre_modelo(nombre: str) -> str:
    """Valida el nombre del modelo de embeddings antes de interpolarlo en SQL.

    ``VECTOR_EMBEDDING(<modelo> USING :texto AS data)`` exige el nombre del
    modelo como identificador SQL (no acepta bind variable ahí), así que el
    valor de ``cfg.embedding_model_name`` se concatena directamente en el
    texto de la sentencia. Esa interpolación es, por definición, una
    superficie de inyección SQL si el valor viene de configuración externa
    (variable de entorno) sin control de forma. La defensa es un allowlist
    estricto: solo se acepta lo que sintácticamente es un identificador
    Oracle simple (letra inicial, luego letras/dígitos/guion bajo, máx. 30
    caracteres). Cualquier otro carácter (espacios, punto y coma, comillas,
    paréntesis, etc.) se rechaza con ``ValueError`` — de modo que un payload
    como ``"MINILM; DROP TABLE licitaciones_chunks--"`` nunca llega a
    ejecutarse.

    Args:
        nombre: Nombre del modelo tal como viene de configuración
            (``cfg.embedding_model_name``).

    Returns:
        El nombre normalizado en mayúsculas (los identificadores Oracle sin
        comillas son case-insensitive y se almacenan en mayúsculas).

    Raises:
        ValueError: Si `nombre` no cumple el patrón de identificador Oracle
            simple `^[A-Za-z][A-Za-z0-9_]{0,29}$`.
    """
    if not isinstance(nombre, str) or not _MODELO_RE.match(nombre):
        raise ValueError(
            "Nombre de modelo de embeddings inválido: "
            f"{nombre!r}. Debe cumplir ^[A-Za-z][A-Za-z0-9_]{{0,29}}$ "
            "(identificador Oracle simple, sin espacios ni símbolos) porque "
            "este valor se interpola directamente en el SQL de "
            "VECTOR_EMBEDDING(...) y no admite bind variable."
        )
    return nombre.upper()


def _construir_chunk_id(proceso_id: str, seq: int) -> str:
    """Construye `chunk_id` como `f"{proceso_id}#{seq}"` respetando VARCHAR2(80).

    Estrategia de truncado: `chunk_id` debe caber en VARCHAR2(80) y respetar
    el UNIQUE(proceso_id, seq) del DDL. `proceso_id` en la tabla es
    VARCHAR2(64), así que en el caso normal `chunk_id` (proceso_id + '#' +
    seq) siempre cabe en 80 caracteres. Como defensa adicional ante un
    `proceso_id` anómalamente largo (mayor a los 64 esperados), se trunca
    el `proceso_id` por la izquierda (se preserva el sufijo, que suele
    contener la parte más específica/discriminante de un UUID o código) para
    dejar espacio garantizado al separador '#' y al número de secuencia.

    Args:
        proceso_id: Identificador del proceso (FK a licitaciones_metadata).
        seq: Número de secuencia del chunk dentro del proceso (empieza en 0).

    Returns:
        chunk_id de longitud <= 80 caracteres.
    """
    sufijo = f"#{seq}"
    max_len_proceso = _CHUNK_ID_MAX_LEN - len(sufijo)
    proceso_id_seguro = proceso_id[-max_len_proceso:] if len(proceso_id) > max_len_proceso else proceso_id
    chunk_id = f"{proceso_id_seguro}{sufijo}"
    if len(proceso_id) > max_len_proceso:
        logger.warning(
            "proceso_id %r excede %d chars; truncado a %r para construir chunk_id (%r)",
            proceso_id,
            max_len_proceso,
            proceso_id_seguro,
            chunk_id,
        )
    return chunk_id


def vectorizar_proceso(
    conn: Any,
    proceso_id: str,
    texto_completo: str,
    cfg: Any,
    batch_size: int = 50,
) -> int:
    """Vectoriza el texto completo de un proceso y lo persiste en `licitaciones_chunks`.

    El cálculo del embedding ocurre **dentro de la base de datos**: cada
    INSERT incluye `VECTOR_EMBEDDING(<MODELO> USING :txt AS data)`, donde
    `<MODELO>` es el nombre del modelo ONNX ya validado/interpolado y `:txt`
    es un bind normal con el texto del chunk. No se llama a ninguna API
    externa de embeddings.

    Idempotencia: antes de insertar, borra cualquier chunk previo del mismo
    `proceso_id` (un re-análisis/re-vectorización reemplaza limpiamente en
    vez de acumular duplicados o violar el UNIQUE(proceso_id, seq)).

    Los chunks se insertan por lotes (`executemany`) de tamaño `batch_size`
    para acotar la memoria: nunca se materializa en Python la lista completa
    de binds de golpe si el texto genera muchos chunks — se procesa y se
    libera lote a lote (crítico en VMs con 1 GB de RAM).

    Esta función NO hace commit: la conexión (`conn`) es gestionada por el
    context manager de `src.memoria.db`, que decide cuándo confirmar la
    transacción.

    Args:
        conn: Conexión oracledb ya abierta (no se abre ni se cierra aquí).
        proceso_id: Identificador del proceso (FK a licitaciones_metadata).
        texto_completo: Texto íntegro extraído del pliego a vectorizar.
        cfg: Objeto de configuración con `embedding_model_name`,
            `chunk_tokens` y `chunk_overlap`.
        batch_size: Tamaño de lote para `executemany` (default: 50).

    Returns:
        Número total de chunks insertados (0 si el texto no produjo chunks).

    Raises:
        ValueError: Si `cfg.embedding_model_name` no es un identificador
            Oracle válido (ver `_validar_nombre_modelo`). En ese caso no se
            ejecuta ningún SQL (ni el DELETE de idempotencia).
    """
    modelo = _validar_nombre_modelo(cfg.embedding_model_name)

    cursor = conn.cursor()

    # Idempotencia: limpia chunks previos de este proceso ANTES de insertar.
    cursor.execute(
        "DELETE FROM licitaciones_chunks WHERE proceso_id = :pid",
        {"pid": proceso_id},
    )
    borrados = cursor.rowcount
    logger.info(
        "vectorizar_proceso: %d chunk(s) previo(s) borrado(s) para proceso_id=%s",
        borrados,
        proceso_id,
    )

    chunks = chunk_text(texto_completo, cfg.chunk_tokens, cfg.chunk_overlap)
    if not chunks:
        logger.warning(
            "vectorizar_proceso: chunk_text no produjo chunks para proceso_id=%s "
            "(texto vacío o demasiado corto); no se inserta nada",
            proceso_id,
        )
        return 0

    sql = (
        "INSERT INTO licitaciones_chunks "
        "(chunk_id, proceso_id, seq, chunk_text, embedding) "
        "VALUES (:cid, :pid, :seq, :txt, "
        f"VECTOR_EMBEDDING({modelo} USING :txt AS data))"
    )

    total_insertados = 0
    lote: list[dict[str, Any]] = []

    for seq, texto_chunk in chunks:
        lote.append(
            {
                "cid": _construir_chunk_id(proceso_id, seq),
                "pid": proceso_id,
                "seq": seq,
                "txt": texto_chunk,
            }
        )
        if len(lote) >= batch_size:
            cursor.executemany(sql, lote)
            total_insertados += len(lote)
            lote = []

    if lote:
        cursor.executemany(sql, lote)
        total_insertados += len(lote)

    logger.info(
        "vectorizar_proceso: %d chunk(s) insertado(s) para proceso_id=%s (modelo=%s)",
        total_insertados,
        proceso_id,
        modelo,
    )
    return total_insertados


def contar_chunks(conn: Any, proceso_id: str | None = None) -> int:
    """Cuenta filas en `licitaciones_chunks`, opcionalmente filtrando por proceso.

    Útil para decidir cuándo crear el índice vectorial IVF (el DDL indica
    que, por debajo de ~50k chunks, la búsqueda exacta sin índice ya es
    válida y precisa).

    Args:
        conn: Conexión oracledb ya abierta.
        proceso_id: Si se pasa, cuenta solo los chunks de ese proceso.
            Si es `None`, cuenta el total de la tabla.

    Returns:
        Número de filas que cumplen el filtro.
    """
    cursor = conn.cursor()
    if proceso_id is None:
        cursor.execute("SELECT COUNT(*) FROM licitaciones_chunks")
    else:
        cursor.execute(
            "SELECT COUNT(*) FROM licitaciones_chunks WHERE proceso_id = :pid",
            {"pid": proceso_id},
        )
    (total,) = cursor.fetchone()
    return int(total)
