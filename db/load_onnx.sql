-- ============================================================================
-- db/load_onnx.sql — Carga del modelo de embeddings IN-DATABASE (AMALIA)
-- ============================================================================
-- Fuente de verdad: docs/arquitectura/modelo-de-datos.md
--   Sección 2 ("Esquema físico cerrado", bloque 0) y "Estrategia de indexación
--   vectorial".
-- Tarea del plan: docs/planes/fase2-track-b.md — B0.4 "Carga del modelo ONNX".
--
-- Modelo:        multilingual-e5-small (Oracle pre-built augmented ONNX)
-- Nombre en BD:  MULTILINGUAL_E5_SMALL (debe coincidir con EMBEDDING_MODEL_NAME
--                en .env y con el default `embedding_model_name` de src/config.py)
-- Dimensión:     384         (debe coincidir con VECTOR(384, FLOAT32) en
--                db/schema.sql, tabla licitaciones_chunks.embedding)
-- Idiomas:       100 (incluye español — crítico para licitaciones COMPRASAL)
--
-- Origen:        https://adwc4pm.objectstorage.us-ashburn-1.oci.customer-oci.com/
--                  p/.../b/OML-ai-models/o/multilingual_e5_small_augmented.zip
-- Tamaño zip:    77 MB   |   .onnx extraído: 118 MB
--
-- NOTA: el modelo augmented de Oracle ya incluye tokenizer + post-processing
-- integrados en el grafo ONNX, por lo que NO requiere prefijos "query:" /
-- "passage:" al usarlo con VECTOR_EMBEDDING — los embeddings se generan
-- correctamente pasando el texto directamente.
--
-- ============================================================================
-- NOTA PROMINENTE — cambio de modelo de embeddings
-- ============================================================================
-- Si en el futuro se reemplaza multilingual-e5-small por otro modelo ONNX:
--   1. La dimensión del vector puede cambiar. Actualizar VECTOR(384, FLOAT32)
--      en db/schema.sql y en CREATE VECTOR INDEX (db/index_ivf.sql).
--   2. TODO el corpus ya vectorizado queda con embeddings del modelo viejo;
--      es obligatorio re-vectorizar todos los chunks existentes.
--   3. Actualizar EMBEDDING_MODEL_NAME en .env y el nombre de modelo usado
--      en las consultas (VECTOR_EMBEDDING(<NUEVO_NOMBRE> USING ...)).
-- ============================================================================


-- ─────────────────────────────────────────────────────────────
-- 0) Prerrequisito: privilegios del usuario de BD
-- ─────────────────────────────────────────────────────────────
-- Ejecutar UNA VEZ, conectado como ADMIN:
--
--   GRANT DB_DEVELOPER_ROLE TO AMALIA;
--   GRANT CREATE MINING MODEL TO AMALIA;
--   GRANT READ, WRITE ON DIRECTORY DATA_PUMP_DIR TO AMALIA;
--   GRANT EXECUTE ON DBMS_CLOUD TO AMALIA;
--   GRANT EXECUTE ON DBMS_VECTOR TO AMALIA;


-- ─────────────────────────────────────────────────────────────
-- 1) Cómo llega el .onnx a la ADB (no hay filesystem accesible)
-- ─────────────────────────────────────────────────────────────
-- Una Autonomous Database serverless no expone un filesystem directamente.
-- La vía soportada es:
--
--   (a) Descargar multilingual_e5_small_augmented.zip del bucket OML-ai-models
--       de Oracle a una VM con acceso a OCI Object Storage.
--   (b) Extraer multilingual_e5_small.onnx del zip.
--   (c) Subir el .onnx a un bucket propio (ej. amalia-onnx-staging) creando
--       un PAR (Pre-Authenticated Request) de escritura.
--   (d) Desde la ADB, usar DBMS_CLOUD.GET_OBJECT con credential_name => NULL
--       y la URL del PAR de lectura para copiar al DATA_PUMP_DIR.
--
-- Ejemplo con PAR (credential NULL = sin credencial, el PAR ya autentica):
BEGIN
  DBMS_CLOUD.GET_OBJECT(
    credential_name => NULL,
    object_uri      => '<PAR_URL_DEL_BUCKET>',  -- ej. https://objectstorage.us-ashburn-1.oraclecloud.com/p/<TOKEN>/n/<NS>/b/<BUCKET>/o/multilingual_e5_small.onnx
    directory_name  => 'DATA_PUMP_DIR',
    file_name       => 'multilingual_e5_small.onnx'
  );
END;
/

-- Verificar que el archivo llegó al directory antes de continuar:
-- SELECT object_name, bytes FROM DBMS_CLOUD.LIST_FILES('DATA_PUMP_DIR')
--  WHERE object_name = 'multilingual_e5_small.onnx';


-- ─────────────────────────────────────────────────────────────
-- 2) Carga del modelo ONNX en la BD (DBMS_VECTOR.LOAD_ONNX_MODEL)
-- ─────────────────────────────────────────────────────────────
-- Metadata mínima: el modelo augmented ya declara internamente la dimensión
-- de salida y el tokenizer; solo se especifica function e input.
-- NOTA: embeddingOutput NO se pasa — el modelo augmented lo auto-detecta.
BEGIN
  DBMS_VECTOR.LOAD_ONNX_MODEL(
    directory  => 'DATA_PUMP_DIR',
    file_name  => 'multilingual_e5_small.onnx',
    model_name => 'MULTILINGUAL_E5_SMALL',
    metadata   => JSON('{"function":"embedding","input":{"input":["DATA"]}}')
  );
END;
/


-- ─────────────────────────────────────────────────────────────
-- 3) Verificación
-- ─────────────────────────────────────────────────────────────
-- 3.a) El modelo responde y genera un vector de 384 componentes.
SELECT VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'servidor rack firewall' AS data) AS embedding
FROM dual;

-- 3.b) Confirmar la dimensión exacta (384).
SELECT VECTOR_DIMENSION_COUNT(VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'servidor rack firewall' AS data))
       AS dims
FROM dual;
-- Resultado esperado: DIMS = 384

-- 3.c) Confirmar registro en el diccionario de datos.
SELECT model_name, mining_function, algorithm
FROM   user_mining_models
WHERE  model_name = 'MULTILINGUAL_E5_SMALL';

-- 3.d) Verificar discriminación semántica en español.
-- El par "similar" debe tener menor distancia que el par "distinto".
SELECT
  VECTOR_DISTANCE(
    VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'Adquisición de servidores rack y firewall' AS data),
    VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'Compra de equipos de red para data center' AS data),
    COSINE) AS dist_similar,
  VECTOR_DISTANCE(
    VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'Adquisición de servidores rack y firewall' AS data),
    VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING 'Servicio de limpieza de oficinas' AS data),
    COSINE) AS dist_distinto
FROM dual;
-- Esperado: dist_similar < dist_distinto (ratio ≥ 1.5x)


-- ─────────────────────────────────────────────────────────────
-- 4) Borrar / recargar el modelo
-- ─────────────────────────────────────────────────────────────
-- BEGIN
--   DBMS_VECTOR.DROP_ONNX_MODEL(
--     model_name => 'MULTILINGUAL_E5_SMALL',
--     force      => TRUE
--   );
-- END;
-- /

-- ============================================================================
-- Fin de db/load_onnx.sql
-- ============================================================================
