-- ============================================================================
-- db/index_ivf_v2.sql — Índice vectorial IVF sobre `chunk` (esquema v2, OPT-IN)
-- ============================================================================
-- Espejo de db/index_ivf.sql para el esquema v2 (db/schema_v2.sql) — Fase
-- "switch" de la migración (plan del portal AMALIA §3.7 punto 4). Archivo
-- aparte, no una edición in-place, por el mismo motivo que retention_v2.sql:
-- coexistencia con la v1 hasta la fase "Contract".
--
-- ============================================================================
-- ADVERTENCIA — NO EJECUTAR hasta que el corpus lo justifique
-- ============================================================================
-- Igual que v1: NO correr mientras `chunk` tenga menos de ~50.000 filas (ver
-- db/index_ivf.sql para el razonamiento completo de por qué ese umbral y por
-- qué IVF en vez de HNSW — no se repite acá).
--
-- Mientras la doble escritura del esquema v2 siga activa, `chunk` y
-- `licitaciones_chunks` crecen EN PARALELO con el mismo volumen: la decisión
-- de "cuándo crear el índice" tiene que mirar la tabla que efectivamente
-- van a leer los lectores después del switch (`chunk`), no la que se decida
-- abandonar en la fase "Contract".
--
-- IMPORTANTE — nombre de índice distinto del de v1 (`ivf_chunks`): los
-- nombres de índice son GLOBALES al esquema en Oracle, no por tabla. Correr
-- este script con el mismo nombre que v1 en la misma ADB colisiona con
-- ORA-00955 (ya existe) al crear, o ORA-00957 si se intentara con un nombre
-- duplicado de otra forma.
-- ============================================================================


-- ─────────────────────────────────────────────────────────────
-- 1) Medir el volumen actual del corpus
-- ─────────────────────────────────────────────────────────────
SELECT COUNT(*) AS total_chunks
FROM   chunk;


-- ─────────────────────────────────────────────────────────────
-- 2) Medir latencia ANTES de crear el índice (búsqueda exacta)
-- ─────────────────────────────────────────────────────────────
-- SET TIMING ON
SELECT texto
FROM   chunk
ORDER  BY VECTOR_DISTANCE(embedding,
          VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'servidor rack firewall' AS data), COSINE)
FETCH  FIRST 5 ROWS ONLY;
-- SET TIMING OFF
-- Anotar el "Elapsed" reportado antes de continuar al paso 3.


-- ─────────────────────────────────────────────────────────────
-- 3) Crear el índice IVF (ejecutar SOLO si el paso 1 supera ~50.000 filas)
-- ─────────────────────────────────────────────────────────────
-- 384 = dims de multilingual-e5-small, igual que en `chunk.embedding`
-- (db/schema_v2.sql) y en db/index_ivf.sql. Nombre `ivf_chunks_v2`, no
-- `ivf_chunks` (ver advertencia arriba).
CREATE VECTOR INDEX ivf_chunks_v2 ON chunk (embedding)
  ORGANIZATION NEIGHBOR PARTITIONS
  DISTANCE COSINE
  WITH TARGET ACCURACY 90;


-- ─────────────────────────────────────────────────────────────
-- 4) Medir latencia DESPUÉS de crear el índice
-- ─────────────────────────────────────────────────────────────
-- SET TIMING ON
SELECT texto
FROM   chunk
ORDER  BY VECTOR_DISTANCE(embedding,
          VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'servidor rack firewall' AS data), COSINE)
FETCH  FIRST 5 ROWS ONLY;
-- SET TIMING OFF


-- ─────────────────────────────────────────────────────────────
-- 5) Revertir (DROP INDEX) — para deshacer el opt-in
-- ─────────────────────────────────────────────────────────────
-- DROP INDEX ivf_chunks_v2;

-- ============================================================================
-- Fin de db/index_ivf_v2.sql
-- ============================================================================
