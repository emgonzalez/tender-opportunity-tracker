# db/ — Esquema AMALIA (Oracle Autonomous Database 23ai)

## Descripción

Este directorio contiene los scripts de infraestructura de base de datos para AMALIA, la plataforma serverless de análisis de licitaciones en OCI Always Free.

**Motor:** Oracle Autonomous Database 23ai con AI Vector Search.

**Tabla:** 4 tablas + índices (ver abajo).

---

## Orden de Ejecución

### 1. **schema.sql** ← OBLIGATORIO

Crea las 4 tablas principales y los índices de metadatos:

- `licitaciones_metadata` — procesos, estado del pipeline, control de reintentos
- `licitaciones_chunks` — fragmentos de texto + embeddings (VECTOR)
- `presupuesto_diario` — contador de tokens consumidos (guardia de cuota)
- `licitaciones_analisis` — matriz de riesgos, anexos (JSON)

**Características:**
- **Idempotente:** cada CREATE está envuelto en un bloque PL/SQL anónimo que suprime `ORA-00955` (objeto ya existe).
- **Quoting seguro:** usa `q'[...]'` para manejar comillas simples en CHECK constraints sin duplicación.
- **Sin FK circulares:** `licitaciones_chunks` y `licitaciones_analisis` tienen FK a `licitaciones_metadata` con `ON DELETE CASCADE`.

### 2. **load_onnx.sql** ← OBLIGATORIO (tarea aparte)

Importa el modelo de embeddings `all-MiniLM-L6-v2` (~384 dims, ~90 MB ONNX) a ADB.

```sql
BEGIN
  DBMS_VECTOR.LOAD_ONNX_MODEL(
    directory   => 'MODELS_DIR',
    file_name   => 'all_MiniLM_L6_v2.onnx',
    model_name  => 'MINILM_L6',
    metadata    => JSON('{"function":"embedding","embeddingOutput":384,"input":{"input":["DATA"]}}'));
END;
/
```

> **Nota:** este script se escribe en otra tarea. Solo menciónalo aquí para contexto del usuario.

### 3. **index_ivf.sql** ← OPCIONAL (tarea aparte)

Crea el índice vectorial IVF (Inverse Faiss Vector) en la columna `embedding` de `licitaciones_chunks`.

```sql
CREATE VECTOR INDEX ivf_chunks ON licitaciones_chunks (embedding)
  ORGANIZATION NEIGHBOR PARTITIONS
  DISTANCE COSINE
  WITH TARGET ACCURACY 90;
```

**Cuándo usarlo:**
- **Corpus pequeño (< ~50k chunks):** búsqueda exacta (sin índice) es suficiente y más precisa.
- **Corpus grande (> 50k chunks):** crear IVF para aceleración. HNSW no es recomendado en Always Free (escasez de Vector Memory Pool).

---

## Cómo Ejecutar

### Opción 1: SQL Developer Web (recomendado para Always Free)

1. Conectar a tu ADB desde la consola OCI → **SQL Developer Web**.
2. Seleccionar el usuario de aplicación (default: `AMALIA`).
3. Abrir `db/schema.sql`, copiar y pegar todo el contenido.
4. Ejecutar (Ctrl+Enter o botón **Run**).
5. Verificar que aparecen los `PL/SQL procedure successfully completed` para cada tabla e índice.

### Opción 2: SQLcl (CLI en la VM)

```bash
# Dentro del contenedor Docker o en la VM:
sqlcl -u AMALIA@adb_alias @/path/to/db/schema.sql

# O con mTLS desde Vault:
sqlcl -u AMALIA@adb_alias \
  --wallet /tmp/wallet.zip \
  @/path/to/db/schema.sql
```

### Opción 3: Python + oracledb (SDK)

```python
import oracledb

# Conexión con mTLS (wallet desde Vault)
connection = oracledb.connect(
    user="AMALIA",
    password=os.environ["ADB_PASSWORD"],
    dsn="adb_alias",
    config_dir="/tmp/wallet",  # tmpfs, descartado al exit
)

cursor = connection.cursor()
with open("db/schema.sql", "r") as f:
    script = f.read()

for stmt in script.split("/"):
    stmt = stmt.strip()
    if stmt:
        cursor.execute(stmt)

connection.commit()
cursor.close()
connection.close()
```

---

## Verificación de Creación

### Listar tablas creadas

```sql
SELECT table_name FROM user_tables 
WHERE table_name IN ('LICITACIONES_METADATA','LICITACIONES_CHUNKS',
                     'PRESUPUESTO_DIARIO','LICITACIONES_ANALISIS')
ORDER BY table_name;
```

**Resultado esperado:**
```
TABLE_NAME
──────────────────────────
LICITACIONES_ANALISIS
LICITACIONES_CHUNKS
LICITACIONES_METADATA
PRESUPUESTO_DIARIO
```

### Listar índices

```sql
SELECT index_name, table_name FROM user_indexes
WHERE table_name IN ('LICITACIONES_METADATA','LICITACIONES_CHUNKS')
ORDER BY table_name, index_name;
```

**Resultado esperado:**
```
INDEX_NAME          TABLE_NAME
───────────────────────────────────────────
IX_LICIT_CLIENTE    LICITACIONES_METADATA
IX_LICIT_ESTADO     LICITACIONES_METADATA
PK_ANALISIS         LICITACIONES_ANALISIS
PK_CHUNK            LICITACIONES_CHUNKS
PK_LICIT            LICITACIONES_METADATA
PK_PRESUP           PRESUPUESTO_DIARIO
```

### Listar columnas de una tabla (ejemplo: licitaciones_metadata)

```sql
DESCRIBE licitaciones_metadata;

-- O con diccionario:
SELECT column_name, data_type, nullable FROM user_tab_columns
WHERE table_name = 'LICITACIONES_METADATA'
ORDER BY column_id;
```

---

## Esquema del Usuario Aplicación (ADB_USER)

Todas las tablas viven en el esquema del usuario de aplicación. Default: `AMALIA` (configurable en `tenants.yaml` / `.env`).

Para cambiar el propietario de las tablas, ejecutar como un DBA y usar `CREATE TABLE AS SELECT ... FROM AMALIA.licitaciones_metadata` en otro esquema (no recomendado — mantener todo en `AMALIA`).

---

## Política de Retención de Datos

- **Object Storage:** lifecycle rule → borrar PDFs > 7 días (higiene de costo + PII).
- **Vectores/metadatos:** `licitaciones_metadata` y `licitaciones_analisis` se conservan como **memoria institucional** (Track B). Los `chunks` de procesos cerrados hace > 12 meses pueden purgarse por job SQL programado:

  ```sql
  DELETE FROM licitaciones_chunks
   WHERE proceso_id IN (
    SELECT proceso_id FROM licitaciones_metadata
     WHERE estado = 'NOTIFICADO' AND actualizado_en < TRUNC(SYSDATE) - 365
  );
  ```

- **PII:** el texto crudo del pliego vive solo durante el análisis; se persisten chunks (anónimos) y resultados, no el PDF.

---

## Mantenimiento

### Rebuild de índices (si es necesario)

```sql
ALTER INDEX ix_licit_estado REBUILD;
ALTER INDEX ix_licit_cliente REBUILD;
```

### Estadísticas (para optimizador)

```sql
BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(
    ownname => 'AMALIA',
    tabname => 'LICITACIONES_METADATA'
  );
END;
/
```

### Monitoreo de espacio

```sql
SELECT segment_name, segment_type, bytes / 1024 / 1024 AS mb
FROM user_segments
WHERE segment_name IN ('LICITACIONES_METADATA','LICITACIONES_CHUNKS',...)
ORDER BY bytes DESC;
```

---

## Troubleshooting

### Error: ORA-00942: table or view does not exist

**Causa:** script no se ejecutó correctamente o usuario no es `AMALIA`.

**Solución:**
- Verificar usuario conectado: `SELECT user FROM dual;` debe ser `AMALIA` (mayúsculas).
- Re-ejecutar `schema.sql`.

### Error: ORA-01408: such column list already indexed

**Causa:** índice duplicado (por retentativa).

**Solución:** es esperado (idempotencia). El script captura y suprime este error.

### Error: ORA-01950: no privileges on tablespace

**Causa:** usuario sin cuota de tablespace.

**Solución:** DBA ejecuta `ALTER USER AMALIA QUOTA UNLIMITED ON DATA;` (o quota específica).

---

## Referencias

- **Inventario:** `amalia-oci-serverless-arch/inventario_diagramas_amalia.md` (sección 2)
- **Arquitectura:** `amalia-oci-serverless-arch/arquitectura_amalia_multimodal.md`
- **Proyecto:** `/Users/emiliogonzalez/Documents/MULTICOMPUTOS/POCs/tender-opportunity-tracker`

---

*Script generado por DBA Oracle, 2026-08-02. Reexecutable / idempotent.*
