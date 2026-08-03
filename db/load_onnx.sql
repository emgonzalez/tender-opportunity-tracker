-- ============================================================================
-- db/load_onnx.sql — Carga del modelo de embeddings IN-DATABASE (AMALIA)
-- ============================================================================
-- Fuente de verdad: docs/arquitectura/modelo-de-datos.md
--   Sección 2 ("Esquema físico cerrado", bloque 0) y "Estrategia de indexación
--   vectorial".
-- Tarea del plan: docs/planes/fase2-track-b.md — B0.4 "Carga del modelo ONNX".
--
-- Modelo:        all-MiniLM-L6-v2
-- Nombre en BD:  MINILM_L6   (debe coincidir con EMBEDDING_MODEL_NAME en .env
--                y con el default `embedding_model_name` de src/config.py)
-- Dimensión:     384         (debe coincidir con VECTOR(384, FLOAT32) en
--                db/schema.sql, tabla licitaciones_chunks.embedding)
--
-- ============================================================================
-- NOTA PROMINENTE — cambio de modelo de embeddings
-- ============================================================================
-- Si en el futuro se reemplaza all-MiniLM-L6-v2 por otro modelo ONNX:
--   1. La dimensión del vector casi seguro cambia (no todos los modelos de
--      embeddings usan 384). Hay que actualizar VECTOR(384, FLOAT32) en
--      db/schema.sql (columna licitaciones_chunks.embedding) y en cualquier
--      CREATE VECTOR INDEX (db/index_ivf.sql) a la nueva dimensión.
--   2. TODO el corpus ya vectorizado queda con embeddings del modelo viejo,
--      que NO son comparables (ni siquiera de la misma dimensión) con los
--      del modelo nuevo. Es obligatorio re-vectorizar (recalcular
--      VECTOR_EMBEDDING) todos los chunks existentes en licitaciones_chunks
--      tras el cambio; no se pueden mezclar embeddings de dos modelos en la
--      misma columna/índice.
--   3. Actualizar EMBEDDING_MODEL_NAME en .env y el nombre de modelo usado
--      en las consultas (VECTOR_EMBEDDING(<NUEVO_NOMBRE> USING ...)).
-- ============================================================================


-- ─────────────────────────────────────────────────────────────
-- 0) Prerrequisito: privilegios del usuario de BD
-- ─────────────────────────────────────────────────────────────
-- El usuario que ejecuta LOAD_ONNX_MODEL / DROP_ONNX_MODEL necesita el
-- rol DB_DEVELOPER_ROLE (incluye los privilegios de "create mining model"
-- requeridos por DBMS_VECTOR / DBMS_DATA_MINING en 23ai) además de
-- permisos sobre el DIRECTORY object usado como staging del .onnx.
--
-- Ejecutar UNA VEZ, conectado como ADMIN (o un usuario con privilegio para
-- otorgar roles) en la ADB:
--
--   GRANT DB_DEVELOPER_ROLE TO AMALIA;
--   GRANT CREATE MINING MODEL TO AMALIA;   -- normalmente ya incluido en el rol,
--                                          -- se deja explícito por claridad
--   GRANT READ, WRITE ON DIRECTORY DATA_PUMP_DIR TO AMALIA;
--
-- <AMALIA> = ADB_USER configurado en .env. Ajustar el nombre si difiere.


-- ─────────────────────────────────────────────────────────────
-- 1) Cómo llega el .onnx a la ADB (no hay filesystem accesible)
-- ─────────────────────────────────────────────────────────────
-- Una Autonomous Database serverless no expone un filesystem al que se
-- pueda copiar el archivo directamente (no hay `scp`/`cp` al servidor de
-- BD). La vía soportada es:
--
--   (a) Subir all_MiniLM_L6_v2.onnx a un bucket de OCI Object Storage.
--   (b) Desde la ADB, crear una credencial (una vez) para autenticar
--       contra Object Storage.
--   (c) Usar DBMS_CLOUD.GET_OBJECT para copiar el archivo del bucket al
--       DIRECTORY object DATA_PUMP_DIR (ya existe por defecto en toda
--       ADB; sirve como staging para archivos como este).
--
-- Conectado como el usuario de la aplicación (AMALIA):

-- 1.a) Credencial de acceso a Object Storage.
--      Opción recomendada en OCI: Resource Principal (si el cómputo que
--      ejecuta este script corre dentro de OCI, ej. Cloud Shell o una VM
--      con policy adecuada) — evita manejar un Auth Token/API key.
BEGIN
  DBMS_CLOUD.CREATE_CREDENTIAL(
    credential_name => '<CREDENTIAL_NAME>',   -- ej. 'OBJ_STORAGE_CRED'
    -- Resource Principal (preferido si aplica):
    -- username        => NULL, password => NULL,
    -- auth_type       => 'OCI_RESOURCE_PRINCIPAL'  (si el entorno lo soporta)
    --
    -- Alternativa: Auth Token de usuario/servicio IAM (si no hay Resource
    -- Principal disponible, ej. ejecutando desde fuera de OCI):
    username        => '<OCI_USERNAME_O_OCID>',
    password        => '<OCI_AUTH_TOKEN>'
  );
END;
/

-- 1.b) Copiar el .onnx del bucket al directory de staging.
--      <BUCKET_URL> = URL del objeto en Object Storage, formato:
--      https://objectstorage.<region>.oraclecloud.com/n/<namespace>/b/<bucket>/o/all_MiniLM_L6_v2.onnx
BEGIN
  DBMS_CLOUD.GET_OBJECT(
    credential_name => '<CREDENTIAL_NAME>',
    object_uri      => '<BUCKET_URL>',
    directory_name  => 'DATA_PUMP_DIR'
  );
END;
/

-- Verificar que el archivo llegó al directory antes de continuar:
-- SELECT * FROM DBMS_CLOUD.LIST_FILES('DATA_PUMP_DIR')
--  WHERE object_name = 'all_MiniLM_L6_v2.onnx';


-- ─────────────────────────────────────────────────────────────
-- 2) Carga del modelo ONNX en la BD (DBMS_VECTOR.LOAD_ONNX_MODEL)
-- ─────────────────────────────────────────────────────────────
-- Metadata:
--   function        = 'embedding' (modelo de embeddings, no clasificación)
--   embeddingOutput  = 384         (dims de all-MiniLM-L6-v2)
--   input.input      = ["DATA"]    (nombre de la columna de entrada esperada
--                                   por VECTOR_EMBEDDING(... USING ... AS data))
BEGIN
  DBMS_VECTOR.LOAD_ONNX_MODEL(
    directory  => 'DATA_PUMP_DIR',
    file_name  => 'all_MiniLM_L6_v2.onnx',
    model_name => 'MINILM_L6',
    metadata   => JSON('{"function":"embedding","embeddingOutput":384,"input":{"input":["DATA"]}}')
  );
END;
/


-- ─────────────────────────────────────────────────────────────
-- 3) Verificación
-- ─────────────────────────────────────────────────────────────
-- 3.a) El modelo responde y genera un vector (inspección visual: debe
--      verse una lista de 384 componentes flotantes).
SELECT VECTOR_EMBEDDING(MINILM_L6 USING 'servidor rack firewall' AS data) AS embedding
FROM dual;

-- 3.b) Confirmar la dimensión exacta (384) midiendo el vector generado,
--      sin depender de que el catálogo exponga la dimensión declarada.
SELECT VECTOR_DIMENSION_COUNT(VECTOR_EMBEDDING(MINILM_L6 USING 'servidor rack firewall' AS data))
       AS dims
FROM dual;
-- Resultado esperado: DIMS = 384

-- 3.c) Confirmar que el modelo quedó registrado en el diccionario de datos.
SELECT model_name, mining_function, algorithm
FROM   user_mining_models
WHERE  model_name = 'MINILM_L6';


-- ─────────────────────────────────────────────────────────────
-- 4) Borrar / recargar el modelo (para re-vectorizar con otro modelo,
--    o para reintentar una carga fallida)
-- ─────────────────────────────────────────────────────────────
-- DBMS_VECTOR.DROP_ONNX_MODEL elimina el modelo del diccionario de datos.
-- No borra los embeddings ya calculados y almacenados en
-- licitaciones_chunks.embedding (esos quedan igual hasta que se
-- re-vectoricen explícitamente).
--
-- BEGIN
--   DBMS_VECTOR.DROP_ONNX_MODEL(
--     model_name => 'MINILM_L6',
--     force      => TRUE   -- TRUE ignora errores si el modelo no existe
--   );
-- END;
-- /
--
-- Tras un DROP, repetir el paso 2 (LOAD_ONNX_MODEL) para recargar —
-- reutilizando el mismo .onnx ya presente en DATA_PUMP_DIR, o subiendo
-- uno nuevo repitiendo el paso 1 si se cambia de modelo (ver la NOTA
-- PROMINENTE al inicio de este archivo sobre el impacto de cambiar de
-- modelo de embeddings).

-- ============================================================================
-- Fin de db/load_onnx.sql
-- ============================================================================
