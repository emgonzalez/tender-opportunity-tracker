-- ============================================================================
-- db/index_ivf.sql — Índice vectorial IVF sobre licitaciones_chunks (OPT-IN)
-- ============================================================================
-- Fuente de verdad: docs/arquitectura/modelo-de-datos.md
--   Sección 2, bloque "Esquema físico cerrado" (CREATE VECTOR INDEX ivf_chunks)
--   y subsección "Estrategia de indexación vectorial".
-- Tarea del plan: docs/planes/fase2-track-b.md — B7.2 "Índice IVF (condicional)".
--
-- ============================================================================
-- ADVERTENCIA — NO EJECUTAR hasta que el corpus lo justifique
-- ============================================================================
-- Este script queda listo pero es OPT-IN. NO correrlo mientras
-- licitaciones_chunks tenga menos de ~50.000 filas.
--
-- Motivo: por debajo de ese volumen, la búsqueda vectorial EXACTA (es decir,
-- SIN índice, escaneando y ordenando por VECTOR_DISTANCE directamente, como
-- ya hace la query RAG in-DB del inventario) es 100% precisa —no aproximada—
-- y su latencia es trivial en el hardware Always Free. Crear el índice antes
-- de tiempo solo añade overhead de mantenimiento (cada INSERT/UPDATE sobre
-- embedding recalcula la estructura del índice) sin beneficio de latencia
-- medible.
--
-- Por qué IVF y no HNSW (cuando el volumen sí lo justifique):
--   HNSW (Hierarchical Navigable Small World) mantiene el grafo de
--   navegación en el "Vector Memory Pool" (memoria dedicada de la
--   instancia), que en Always Free es muy escaso. IVF (NEIGHBOR PARTITIONS)
--   particiona el espacio vectorial y solo escanea las particiones
--   ("neighbor partitions") relevantes a la consulta, con un footprint de
--   memoria mucho menor — es la opción viable en un tenant Always Free.
--
-- Regla de decisión: correr el paso 1 (conteo) antes de decidir.
-- ============================================================================


-- ─────────────────────────────────────────────────────────────
-- 1) Medir el volumen actual del corpus
-- ─────────────────────────────────────────────────────────────
-- Umbral de referencia: ~50.000 chunks. Por debajo, no crear el índice
-- (ver advertencia arriba). Ejecutar esta query periódicamente para decidir
-- cuándo pasar de búsqueda exacta a indexada.
SELECT COUNT(*) AS total_chunks
FROM   licitaciones_chunks;


-- ─────────────────────────────────────────────────────────────
-- 2) Medir latencia ANTES de crear el índice (búsqueda exacta)
-- ─────────────────────────────────────────────────────────────
-- Correr esta query de referencia (ajustar :pregunta a un texto real) con
-- SET TIMING ON en SQLcl/SQL*Plus para capturar el tiempo de ejecución
-- base, sin índice, y compararlo después de crear el índice IVF.
--
-- SET TIMING ON
SELECT chunk_text
FROM   licitaciones_chunks
ORDER  BY VECTOR_DISTANCE(embedding,
          VECTOR_EMBEDDING(MINILM_L6 USING 'servidor rack firewall' AS data), COSINE)
FETCH  FIRST 5 ROWS ONLY;
-- SET TIMING OFF
-- Anotar el "Elapsed" reportado antes de continuar al paso 3.


-- ─────────────────────────────────────────────────────────────
-- 3) Crear el índice IVF (ejecutar SOLO si el paso 1 supera ~50.000 filas)
-- ─────────────────────────────────────────────────────────────
-- Transcripción literal del inventario. NOTA: 384 = dims de
-- all-MiniLM-L6-v2 (consistente con VECTOR(384, FLOAT32) en
-- db/schema.sql y con MINILM_L6 en db/load_onnx.sql). Si el modelo de
-- embeddings cambia de dimensión, este índice debe recrearse (DROP + CREATE)
-- sobre la columna ya re-vectorizada.
CREATE VECTOR INDEX ivf_chunks ON licitaciones_chunks (embedding)
  ORGANIZATION NEIGHBOR PARTITIONS
  DISTANCE COSINE
  WITH TARGET ACCURACY 90;


-- ─────────────────────────────────────────────────────────────
-- 4) Medir latencia DESPUÉS de crear el índice
-- ─────────────────────────────────────────────────────────────
-- Repetir exactamente la misma query del paso 2, con SET TIMING ON, y
-- comparar el "Elapsed" contra la medición base. Con TARGET ACCURACY 90
-- la búsqueda es aproximada (no garantiza el 100% de recall del escaneo
-- exacto); si la latencia no mejora de forma significativa frente al
-- volumen medido en el paso 1, considerar revertir (paso 5) y seguir con
-- búsqueda exacta.
--
-- SET TIMING ON
SELECT chunk_text
FROM   licitaciones_chunks
ORDER  BY VECTOR_DISTANCE(embedding,
          VECTOR_EMBEDDING(MINILM_L6 USING 'servidor rack firewall' AS data), COSINE)
FETCH  FIRST 5 ROWS ONLY;
-- SET TIMING OFF


-- ─────────────────────────────────────────────────────────────
-- 5) Revertir (DROP INDEX) — para deshacer el opt-in
-- ─────────────────────────────────────────────────────────────
-- DROP INDEX ivf_chunks;

-- ============================================================================
-- Fin de db/index_ivf.sql
-- ============================================================================
