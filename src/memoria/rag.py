"""src.memoria.rag — Fase 2 Track B. Ver docs/planes/fase2-track-b.md.

Búsqueda semántica sobre el corpus de licitaciones usando Oracle 23ai AI Vector
Search *in-DB*: tanto los chunks como la pregunta se embeben dentro de la base
de datos (no hay llamada externa a un servicio de embeddings). Ver
``docs/arquitectura/modelo-de-datos.md`` §2 ("RAG in-DB").

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

from src.portales import PORTAL_ID_DEFAULT as _PORTAL_ID_DEFAULT_V2

logger = logging.getLogger(__name__)

# Allowlist estricta para el nombre del modelo de embeddings in-DB.
# VECTOR_EMBEDDING(<modelo> USING :dato AS data) NO admite bind variable para
# el nombre del modelo (es un identificador, no un literal), así que se
# interpola en el SQL. Para evitar inyección SQL, el nombre debe validarse
# contra esta allowlist ANTES de construir cualquier query.
_MODELO_VALIDO = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")



def _validar_modelo(nombre: str) -> str:
    """Valida el nombre de un modelo de embeddings in-DB antes de interpolarlo.

    El nombre del modelo (ej. ``MULTILINGUAL_E5_SMALL``) se usa dentro de
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
    ``docs/arquitectura/modelo-de-datos.md`` §2, "RAG
    in-DB")::

        SELECT chunk_text
        FROM   licitaciones_chunks
        WHERE  proceso_id = :pid
        ORDER  BY VECTOR_DISTANCE(embedding,
                  VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :pregunta AS data), COSINE)
        FETCH  FIRST 5 ROWS ONLY;

    Args:
        conn: Conexión oracledb ya abierta.
        proceso_id: Identificador del pliego sobre el que se busca (ej.
            "96-2026-P0011").
        pregunta: Pregunta en lenguaje natural del usuario.
        cfg: Configuración de la aplicación. Se usan ``cfg.embedding_model_name``
            (nombre del modelo in-DB, default "MULTILINGUAL_E5_SMALL") y ``cfg.rag_top_k``
            (cantidad de fragmentos a recuperar, default 5).

    Returns:
        Lista de textos de los chunks más relevantes, ordenados de mayor a
        menor relevancia (menor distancia coseno primero).

    Raises:
        ValueError: Si ``cfg.embedding_model_name`` no cumple la allowlist
            de :func:`_validar_modelo`.
    """
    modelo = _validar_modelo(getattr(cfg, "embedding_model_name", "MULTILINGUAL_E5_SMALL"))
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


def buscar_chunks_v2(
    conn: Any,
    proceso_id: str,
    pregunta: str,
    cfg: Any,
    portal_id: str = _PORTAL_ID_DEFAULT_V2,
) -> list[str]:
    """Espejo v2 de :func:`buscar_chunks` — Fase "switch" (plan del portal
    §3.7 punto 4).

    A diferencia de :func:`buscar_precedentes_v2`, esta función no necesita
    pasar por ``cliente_proceso``: busca dentro de UN pliego ya identificado
    por ``proceso_id`` (no filtra por cliente en absoluto, igual que la v1 —
    el chunk es del proceso/portal, no de un cliente, ver
    ``db/schema_v2.sql`` sección ``chunk``). Solo cambia la tabla
    (``chunk`` en vez de ``licitaciones_chunks``) y la clave
    (``proceso_gid`` en vez de ``proceso_id``).
    """
    modelo = _validar_modelo(getattr(cfg, "embedding_model_name", "MULTILINGUAL_E5_SMALL"))
    top_k = getattr(cfg, "rag_top_k", 5)
    proceso_gid = f"{portal_id}:{proceso_id}"

    sql = f"""
        SELECT texto
        FROM   chunk
        WHERE  proceso_gid = :pgid
        ORDER  BY VECTOR_DISTANCE(embedding,
                  VECTOR_EMBEDDING({modelo} USING :pregunta AS data), COSINE)
        FETCH  FIRST :k ROWS ONLY
    """

    logger.info(
        "buscar_chunks_v2: proceso_gid=%s top_k=%s modelo=%s", proceso_gid, top_k, modelo
    )

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"pgid": proceso_gid, "pregunta": pregunta, "k": top_k})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    resultado = [_lob_a_str(fila[0]) for fila in filas]
    logger.info(
        "buscar_chunks_v2: %d chunks recuperados para proceso_gid=%s", len(resultado), proceso_gid
    )
    return resultado


def buscar_precedentes(
    conn: Any,
    pregunta: str,
    cfg: Any,
    cliente_id: str,
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

    ``cliente_id`` es **obligatorio** y no ``None`` por defecto a propósito:
    hasta ahora esta función buscaba en TODO el corpus sin filtrar por
    cliente, así que un cliente podía recibir como precedente un pliego que
    en realidad se analizó para OTRO cliente — con su propio contexto de
    mercado y su propia matriz de riesgos. Es una fuga real entre clientes,
    no hipotética, encontrada en la auditoría de multi-tenant (ver el plan del
    portal, §0). ``licitaciones_metadata.cliente_id`` ya existe como columna
    (no es parte de la PK hoy, que es ``proceso_id`` global — eso es un
    problema aparte, de la Fase 0 completa); alcanza con filtrar por ella para
    cerrar la fuga sin esperar al esquema v2.

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
    modelo = _validar_modelo(getattr(cfg, "embedding_model_name", "MULTILINGUAL_E5_SMALL"))
    limite_final = limite if limite is not None else getattr(cfg, "precedentes_max", 3)

    # Se piden más filas de las necesarias (limite * 4) porque varias filas
    # pueden pertenecer al mismo proceso_id; la deduplicación final se hace
    # en Python quedándonos con el mejor (menor distancia) por proceso.
    filas_a_pedir = max(limite_final * 4, limite_final)

    binds: dict[str, Any] = {
        "pregunta": pregunta,
        "limite": filas_a_pedir,
        "cliente_id": cliente_id,
    }

    # Filtro obligatorio de aislamiento por cliente (NO es una condición de
    # negocio opcional como las de abajo: sin esta línea, cualquier cliente
    # ve precedentes de pliegos analizados para otros clientes).
    condiciones = ["m.cliente_id = :cliente_id"]
    if excluir_proceso_id is not None:
        condiciones.append("m.proceso_id <> :excluir")
        binds["excluir"] = excluir_proceso_id
    if institucion is not None:
        condiciones.append("UPPER(m.institucion) LIKE UPPER(:institucion)")
        binds["institucion"] = institucion

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
        "buscar_precedentes: cliente_id=%s excluir_proceso_id=%s institucion=%s limite=%s distancia_max=%s",
        cliente_id,
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


def buscar_precedentes_v2(
    conn: Any,
    pregunta: str,
    cfg: Any,
    cliente_id: str,
    excluir_proceso_id: str | None = None,
    institucion: str | None = None,
    limite: int | None = None,
    distancia_max: float | None = None,
    portal_id: str = _PORTAL_ID_DEFAULT_V2,
) -> list[dict]:
    """Espejo v2 de :func:`buscar_precedentes` — Fase "switch" (plan del
    portal §3.7 punto 4).

    Mismo comportamiento observable que la v1 (mismos parámetros, mismo
    dict de retorno, misma deduplicación por proceso quedándose con el de
    menor distancia — ver esa función para el detalle del algoritmo, que
    acá se reusa sin cambios) pero sobre las tablas v2, con una diferencia
    de fondo: el JOIN pasa por ``cliente_proceso``, no directo
    ``chunk``→``proceso``. Esto es MÁS estricto que v1 a propósito — un
    cliente solo ve precedentes de procesos que él mismo tiene en su propia
    vista (``cliente_proceso``), no cualquier proceso que exista en el
    portal porque otro cliente lo haya descubierto. v1 ya filtraba por
    ``cliente_id`` (ver el docstring de :func:`buscar_precedentes`), pero
    ese filtro corre sobre una tabla (``licitaciones_metadata``) que el
    último cliente en escribir puede haber pisado — acá es estructuralmente
    imposible: sin fila en ``cliente_proceso``, no hay fila que unir.

    Las claves del dict de retorno son **idénticas** a las de v1
    (``proceso_id``, ``institucion``, ``titulo``, ``fecha_pub``,
    ``url_comprasal``, ``chunk_text``, ``distancia``) aunque las columnas
    subyacentes tengan otro nombre (``url_portal``→``url_comprasal``,
    ``texto``→``chunk_text``) — a propósito, para que
    :func:`src.memoria.service.obtener_precedentes` no tenga que cambiar su
    mapeo a ``Precedente`` según cuál de las dos versiones haya producido
    el dict.

    :param portal_id: portal de origen. Por ahora siempre
        :data:`_PORTAL_ID_DEFAULT_V2`.

    Raises:
        ValueError: Si ``cfg.embedding_model_name`` no cumple la allowlist
            de :func:`_validar_modelo`.
    """
    modelo = _validar_modelo(getattr(cfg, "embedding_model_name", "MULTILINGUAL_E5_SMALL"))
    limite_final = limite if limite is not None else getattr(cfg, "precedentes_max", 3)
    filas_a_pedir = max(limite_final * 4, limite_final)

    binds: dict[str, Any] = {
        "pregunta": pregunta,
        "limite": filas_a_pedir,
        "cliente_id": cliente_id,
    }

    # Filtro obligatorio de aislamiento por cliente — acá vía el JOIN a
    # cliente_proceso, no un WHERE sobre una columna de un hecho global (ver
    # docstring de arriba). condiciones[] son las de negocio, opcionales.
    condiciones: list[str] = []
    if excluir_proceso_id is not None:
        condiciones.append("p.numero_referencia <> :excluir")
        binds["excluir"] = excluir_proceso_id
    if institucion is not None:
        condiciones.append("UPPER(p.institucion) LIKE UPPER(:institucion)")
        binds["institucion"] = institucion

    where_extra = (" AND " + " AND ".join(condiciones)) if condiciones else ""

    # NOTA anti-inyección: igual que en buscar_precedentes — los fragmentos
    # de arriba son texto fijo con placeholders de bind, nunca valores
    # concatenados. El único valor interpolado es `modelo`, ya validado.
    sql = f"""
        SELECT p.numero_referencia,
               p.institucion,
               p.titulo,
               p.fecha_pub_utc,
               p.url_portal,
               c.texto,
               VECTOR_DISTANCE(c.embedding,
                   VECTOR_EMBEDDING({modelo} USING :pregunta AS data), COSINE) AS distancia
        FROM   chunk c
        JOIN   cliente_proceso cp ON cp.proceso_gid = c.proceso_gid
        JOIN   proceso p          ON p.proceso_gid  = c.proceso_gid
        WHERE  cp.cliente_id = :cliente_id{where_extra}
        ORDER  BY distancia ASC
        FETCH  FIRST :limite ROWS ONLY
    """

    logger.info(
        "buscar_precedentes_v2: cliente_id=%s excluir_proceso_id=%s institucion=%s limite=%s distancia_max=%s",
        cliente_id,
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
    for numero_referencia, inst, titulo, fecha_pub, url_portal, texto, distancia in filas:
        if distancia_max is not None and distancia is not None and distancia > distancia_max:
            continue

        existente = mejores_por_proceso.get(numero_referencia)
        if existente is not None and existente["distancia"] <= distancia:
            continue

        mejores_por_proceso[numero_referencia] = {
            "proceso_id": numero_referencia,
            "institucion": inst,
            "titulo": titulo,
            "fecha_pub": _fecha_a_iso(fecha_pub),
            "url_comprasal": url_portal,
            "chunk_text": _lob_a_str(texto),
            "distancia": distancia,
        }

    resultado = sorted(mejores_por_proceso.values(), key=lambda r: r["distancia"])[:limite_final]

    logger.info(
        "buscar_precedentes_v2: %d precedentes distintos devueltos (de %d filas crudas)",
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


def tendencias_v2(conn: Any, cliente_id: str, dias: int = 90) -> list[dict]:
    """Espejo v2 de :func:`tendencias` — Fase "switch" (plan del portal §3.7
    punto 4), con un cambio de firma deliberado: ``cliente_id`` obligatorio.

    ``tendencias`` (v1) agrega por ``triaje = 'GO'`` sobre
    ``licitaciones_metadata``, una tabla de ~una fila por proceso con un solo
    ``cliente_id`` — pero el triaje ("¿es un buen match para ESTE cliente?")
    es intrínsecamente una decisión por cliente, nunca tuvo sentido como
    agregado global. Es la misma conflación que motiva toda esta migración,
    solo que más sutil que la de :func:`buscar_precedentes`. En v2 se vuelve
    insoslayable: ``triaje`` vive en ``cliente_proceso`` (con ``cliente_id``
    en la PK), así que no hay forma de escribir este agregado sin decidir
    explícitamente de qué cliente es. Un agregado *global* de mercado (todos
    los clientes, todo el portal) es una necesidad real y distinta, pero
    tiene que vivir en otro lugar (ver ``kpi_snapshot``/``oportunidad_estado``
    en ``db/schema_v2.sql``), no reintroducirse acá.

    Mismas claves de retorno que v1 (``institucion``, ``categoria``,
    ``conteo``, ``conteo_go``) aunque la columna subyacente sea
    ``categoria_raw`` — mismo motivo que en :func:`buscar_precedentes_v2`:
    que quien consuma el resultado no tenga que distinguir cuál versión
    respondió.
    """
    sql = """
        SELECT p.institucion,
               p.categoria_raw,
               COUNT(*) AS conteo,
               SUM(CASE WHEN cp.triaje = 'GO' THEN 1 ELSE 0 END) AS conteo_go
        FROM   cliente_proceso cp
        JOIN   proceso p ON p.proceso_gid = cp.proceso_gid
        WHERE  cp.cliente_id = :cliente_id
          AND  p.fecha_pub_utc >= SYSDATE - :dias
        GROUP  BY p.institucion, p.categoria_raw
        ORDER  BY conteo DESC
    """

    logger.info("tendencias_v2: cliente_id=%s dias=%s", cliente_id, dias)

    cursor = conn.cursor()
    try:
        cursor.execute(sql, {"cliente_id": cliente_id, "dias": dias})
        filas = cursor.fetchall()
    finally:
        cursor.close()

    resultado = [
        {
            "institucion": inst,
            "categoria": categoria_raw,
            "conteo": conteo,
            "conteo_go": int(conteo_go) if conteo_go is not None else 0,
        }
        for inst, categoria_raw, conteo, conteo_go in filas
    ]

    logger.info(
        "tendencias_v2: %d combinaciones institución/categoría encontradas para cliente_id=%s",
        len(resultado), cliente_id,
    )
    return resultado
