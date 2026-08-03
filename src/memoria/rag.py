"""src.memoria.rag — Fase 2 Track B. Ver amalia-oci-serverless-arch/PLAN_FASE2_TRACK_B.md.

Búsqueda semántica sobre el corpus de licitaciones usando Oracle 23ai AI Vector
Search *in-DB*: tanto los chunks como la pregunta se embeben dentro de la base
de datos (no hay llamada externa a un servicio de embeddings). Ver
``amalia-oci-serverless-arch/inventario_diagramas_amalia.md`` §2 ("RAG in-DB").

Este módulo implementa:

- :func:`buscar_chunks` — búsqueda intra-documento (B3.1): recupera los
  fragmentos más relevantes de UN pliego específico.
- :func:`buscar_precedentes` — búsqueda inter-documento (B3.2): recupera
  fragmentos de precedentes en TODO el corpus histórico, excluyendo el pliego
  actual. Este es el valor diferencial de Track B (memoria institucional).
- :func:`tendencias` — analítica SQL pura (sin vectores) sobre el histórico
  de procesos, agrupada por institución y categoría.

Todas las funciones reciben una conexión ``conn`` ya abierta (oracledb) y no
gestionan su ciclo de vida (eso es responsabilidad de ``src.memoria.db``).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Allowlist estricta para el nombre del modelo de embeddings in-DB.
# VECTOR_EMBEDDING(<modelo> USING :dato AS data) NO admite bind variable para
# el nombre del modelo (es un identificador, no un literal), así que se
# interpola en el SQL. Para evitar inyección SQL, el nombre debe validarse
# contra esta allowlist ANTES de construir cualquier query.
_MODELO_VALIDO = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")


def _validar_modelo(nombre: str) -> str:
    """Valida el nombre de un modelo de embeddings in-DB antes de interpolarlo.

    El nombre del modelo (ej. ``MINILM_L6``) se usa dentro de
    ``VECTOR_EMBEDDING(<modelo> USING :dato AS data)``, y Oracle no permite
    bind variables para ese identificador. Como se interpola directamente en
    el texto SQL, se exige que cumpla una allowlist estricta
    (``^[A-Za-z][A-Za-z0-9_]{0,29}$``) para prevenir inyección SQL.

    Args:
        nombre: Nombre candidato del modelo de embeddings.

    Returns:
        El mismo nombre, sin modificar, si es válido.

    Raises:
        ValueError: Si el nombre no cumple la allowlist.
    """
    if not isinstance(nombre, str) or not _MODELO_VALIDO.match(nombre):
        raise ValueError(
            f"Nombre de modelo de embeddings inválido o potencialmente inseguro: {nombre!r}"
        )
    return nombre


def _lob_a_str(valor: Any) -> str:
    """Convierte defensivamente un valor CLOB de oracledb a ``str``.

    ``oracledb`` puede devolver el contenido de una columna CLOB como un
    objeto LOB (que expone ``.read()``) o, según la configuración del cursor
    (``outputtypehandler``), como un ``str`` directamente. Este helper cubre
    ambos casos sin asumir uno u otro.

    Args:
        valor: Valor devuelto por el cursor para una columna CLOB. Puede ser
            un objeto LOB, un ``str`` o ``None``.

    Returns:
        El contenido como ``str``. Si ``valor`` es ``None``, devuelve
        cadena vacía.
    """
    if valor is None:
        return ""
    if hasattr(valor, "read"):
        return valor.read()
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return str(valor)


def _fecha_a_iso(valor: Any) -> str | None:
    """Convierte un valor de fecha devuelto por oracledb a string ISO 8601.

    Args:
        valor: Valor de fecha/hora devuelto por el cursor (``datetime``,
            ``date`` o ``None``).

    Returns:
        La fecha en formato ISO 8601, o ``None`` si ``valor`` es ``None``.
    """
    if valor is None:
        return None
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    return str(valor)


def buscar_chunks(conn: Any, proceso_id: str, pregunta: str, cfg: Any) -> list[str]:
    """Búsqueda semántica intra-documento (B3.1).

    Recupera los fragmentos de texto más relevantes de UN pliego específico
    (``proceso_id``) respecto a ``pregunta``, calculando el embedding de la
    pregunta *dentro de la base de datos* con ``VECTOR_EMBEDDING`` y
    ordenando por distancia coseno (``VECTOR_DISTANCE``).

    Query base (transcrita de
    ``amalia-oci-serverless-arch/inventario_diagramas_amalia.md`` §2, "RAG
    in-DB")::

        SELECT chunk_text
        FROM   licitaciones_chunks
        WHERE  proceso_id = :pid
        ORDER  BY VECTOR_DISTANCE(embedding,
                  VECTOR_EMBEDDING(MINILM_L6 USING :pregunta AS data), COSINE)
        FETCH  FIRST 5 ROWS ONLY;

    Args:
        conn: Conexión oracledb ya abierta.
        proceso_id: Identificador del pliego sobre el que se busca (ej.
            "96-2026-P0011").
        pregunta: Pregunta en lenguaje natural del usuario.
        cfg: Configuración de la aplicación. Se usan ``cfg.embedding_model_name``
            (nombre del modelo in-DB, default "MINILM_L6") y ``cfg.rag_top_k``
            (cantidad de fragmentos a recuperar, default 5).

    Returns:
        Lista de textos de los chunks más relevantes, ordenados de mayor a
        menor relevancia (menor distancia coseno primero).

    Raises:
        ValueError: Si ``cfg.embedding_model_name`` no cumple la allowlist
            de :func:`_validar_modelo`.
    """
    modelo = _validar_modelo(getattr(cfg, "embedding_model_name", "MINILM_L6"))
    top_k = getattr(cfg, "rag_top_k", 5)

    sql = f"""
        SELECT chunk_text
        FROM   licitaciones_chunks
        WHERE  proceso_id = :pid
        ORDER  BY VECTOR_DISTANCE(embedding,
                  VECTOR_EMBEDDING({modelo} USING :pregunta AS data), COSINE)
        FETCH  FIRST :k ROWS ONLY
    """

    logger.info(
        "buscar_chunks: proceso_id=%s top_k=%s modelo=%s", proceso_id, top_k, modelo
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"pid": proceso_id, "pregunta": pregunta, "k": top_k})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    resultado = [_lob_a_str(fila[0]) for fila in filas]
    logger.info(
        "buscar_chunks: %d chunks recuperados para proceso_id=%s", len(resultado), proceso_id
    )
    return resultado


def buscar_precedentes(
    conn: Any,
    pregunta: str,
    cfg: Any,
    excluir_proceso_id: str | None = None,
    institucion: str | None = None,
    limite: int | None = None,
    distancia_max: float | None = None,
) -> list[dict]:
    """Búsqueda semántica inter-documento (B3.2) — memoria institucional.

    A diferencia de :func:`buscar_chunks` (que busca dentro de un solo
    pliego), esta función busca en TODO el corpus histórico
    (``licitaciones_chunks`` JOIN ``licitaciones_metadata``) para encontrar
    precedentes similares a ``pregunta``. Este es el valor diferencial de
    Track B: permite responder "¿hemos visto algo parecido antes?".

    El pliego actual se excluye explícitamente vía ``excluir_proceso_id``
    para evitar que un pliego se cite a sí mismo como precedente.

    Deduplicación: un mismo ``proceso_id`` puede aportar varios chunks (varias
    filas). Como en el reporte se quieren precedentes distintos (no el mismo
    pliego repetido), se piden más filas de las necesarias
    (``limite * 4``) y luego, en Python, se conserva un único resultado por
    ``proceso_id``: el de menor distancia.

    Args:
        conn: Conexión oracledb ya abierta.
        pregunta: Pregunta o texto en lenguaje natural a comparar semánticamente.
        cfg: Configuración de la aplicación. Se usan ``cfg.embedding_model_name``
            y ``cfg.precedentes_max`` (usado como ``limite`` por defecto).
        excluir_proceso_id: Si se indica, excluye ese ``proceso_id`` de los
            resultados (típicamente el pliego que se está analizando, para
            que no se cite a sí mismo como precedente).
        institucion: Si se indica, filtra por institución (``LIKE``
            case-insensitive vía ``UPPER``). Se espera que el llamador incluya
            los comodines ``%`` si los necesita.
        limite: Cantidad máxima de precedentes distintos a devolver. Si no se
            indica, se usa ``cfg.precedentes_max``.
        distancia_max: Si se indica, descarta precedentes cuya distancia
            coseno sea mayor a este umbral (filtra ruido / precedentes poco
            relacionados).

    Returns:
        Lista de dicts, uno por ``proceso_id`` distinto (deduplicados,
        quedándose con el de menor distancia), ordenados por distancia
        ascendente (más relevante primero). Cada dict tiene las claves:
        ``proceso_id``, ``institucion``, ``titulo``, ``fecha_pub`` (ISO
        string o ``None``), ``url_comprasal``, ``chunk_text`` y
        ``distancia``.

    Raises:
        ValueError: Si ``cfg.embedding_model_name`` no cumple la allowlist
            de :func:`_validar_modelo`.
    """
    modelo = _validar_modelo(getattr(cfg, "embedding_model_name", "MINILM_L6"))
    limite_final = limite if limite is not None else getattr(cfg, "precedentes_max", 3)

    # Se piden más filas de las necesarias (limite * 4) porque varias filas
    # pueden pertenecer al mismo proceso_id; la deduplicación final se hace
    # en Python quedándonos con el mejor (menor distancia) por proceso.
    filas_a_pedir = max(limite_final * 4, limite_final)

    binds: dict[str, Any] = {"pregunta": pregunta, "limite": filas_a_pedir}

    condiciones = []
    if excluir_proceso_id is not None:
        condiciones.append("m.proceso_id <> :excluir")
        binds["excluir"] = excluir_proceso_id
    if institucion is not None:
        condiciones.append("UPPER(m.institucion) LIKE UPPER(:institucion)")
        binds["institucion"] = institucion

    where_sql = ""
    if condiciones:
        where_sql = "WHERE " + " AND ".join(condiciones)

    # NOTA anti-inyección: los fragmentos de SQL de arriba (condiciones) son
    # texto fijo con placeholders de bind (":excluir", ":institucion"); nunca
    # se concatenan VALORES directamente en el string SQL. El único valor
    # interpolado es `modelo`, ya validado por _validar_modelo().
    sql = f"""
        SELECT m.proceso_id,
               m.institucion,
               m.titulo,
               m.fecha_pub,
               m.url_comprasal,
               c.chunk_text,
               VECTOR_DISTANCE(c.embedding,
                   VECTOR_EMBEDDING({modelo} USING :pregunta AS data), COSINE) AS distancia
        FROM   licitaciones_chunks c
        JOIN   licitaciones_metadata m ON c.proceso_id = m.proceso_id
        {where_sql}
        ORDER  BY distancia ASC
        FETCH  FIRST :limite ROWS ONLY
    """

    logger.info(
        "buscar_precedentes: excluir_proceso_id=%s institucion=%s limite=%s distancia_max=%s",
        excluir_proceso_id,
        institucion,
        limite_final,
        distancia_max,
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        filas = cursor.fetchall()
    finally:
        cursor.close()

    mejores_por_proceso: dict[str, dict] = {}
    for proceso_id, inst, titulo, fecha_pub, url_comprasal, chunk_text, distancia in filas:
        if distancia_max is not None and distancia is not None and distancia > distancia_max:
            continue

        existente = mejores_por_proceso.get(proceso_id)
        if existente is not None and existente["distancia"] <= distancia:
            continue

        mejores_por_proceso[proceso_id] = {
            "proceso_id": proceso_id,
            "institucion": inst,
            "titulo": titulo,
            "fecha_pub": _fecha_a_iso(fecha_pub),
            "url_comprasal": url_comprasal,
            "chunk_text": _lob_a_str(chunk_text),
            "distancia": distancia,
        }

    resultado = sorted(mejores_por_proceso.values(), key=lambda r: r["distancia"])[:limite_final]

    logger.info(
        "buscar_precedentes: %d precedentes distintos devueltos (de %d filas crudas)",
        len(resultado),
        len(filas),
    )
    return resultado


def tendencias(conn: Any, dias: int = 90) -> list[dict]:
    """Analítica SQL pura (sin vectores) sobre el histórico de procesos.

    Agrupa los procesos publicados en los últimos ``dias`` días por
    institución y categoría, devolviendo el conteo total y cuántos de ellos
    tuvieron triaje ``'GO'``. No usa ``VECTOR_DISTANCE`` ni
    ``VECTOR_EMBEDDING``: es una consulta analítica convencional sobre
    ``licitaciones_metadata``.

    Args:
        conn: Conexión oracledb ya abierta.
        dias: Ventana de tiempo (en días) hacia atrás desde hoy a considerar,
            según ``fecha_pub``. Por defecto 90.

    Returns:
        Lista de dicts con las claves ``institucion``, ``categoria``,
        ``conteo`` (total de procesos) y ``conteo_go`` (cuántos tuvieron
        ``triaje = 'GO'``), ordenada por ``conteo`` descendente.
    """
    sql = """
        SELECT m.institucion,
               m.categoria,
               COUNT(*) AS conteo,
               SUM(CASE WHEN m.triaje = 'GO' THEN 1 ELSE 0 END) AS conteo_go
        FROM   licitaciones_metadata m
        WHERE  m.fecha_pub >= SYSDATE - :dias
        GROUP  BY m.institucion, m.categoria
        ORDER  BY conteo DESC
    """

    logger.info("tendencias: dias=%s", dias)

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"dias": dias})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    resultado = [
        {
            "institucion": inst,
            "categoria": categoria,
            "conteo": conteo,
            "conteo_go": int(conteo_go) if conteo_go is not None else 0,
        }
        for inst, categoria, conteo, conteo_go in filas
    ]

    logger.info(
        "tendencias: %d combinaciones institución/categoría encontradas", len(resultado)
    )
    return resultado
