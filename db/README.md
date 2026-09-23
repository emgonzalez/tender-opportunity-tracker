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

Importa el modelo de embeddings `multilingual-e5-small` (~384 dims, ~118 MB ONNX, 100 idiomas) a ADB.

```sql
BEGIN
  DBMS_VECTOR.LOAD_ONNX_MODEL(
    directory   => 'DATA_PUMP_DIR',
    file_name   => 'multilingual_e5_small.onnx',
    model_name  => 'MULTILINGUAL_E5_SMALL',
    metadata    => JSON('{"function":"embedding","input":{"input":["DATA"]}}'));
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

## Validación local sin ADB (motor real en un contenedor)

El ACL de red de la ADB solo admite la VCN y la IP pública de la VM: **ni una
laptop ni un runner de CI pueden conectarse**. Sin esto, un error de sintaxis en
el SQL de `src/memoria/` recién aparecía después de construir la imagen,
publicarla, desplegarla y correrla en la VM.

`docker-compose.oracle.yml` levanta el **mismo motor 23ai** en su edición Free,
en local, y `tests/integration/test_oracle_local.py` ejecuta ahí el SQL real del
código (no una copia: las sentencias salen de los módulos de `src/memoria/`).

```bash
docker compose -f db/docker-compose.oracle.yml up -d --wait   # ~12 s si la imagen ya está
pytest -m oracle_local -v                                     # ~0,5 s
docker compose -f db/docker-compose.oracle.yml down -v        # limpieza total
```

Sin el contenedor levantado, esos tests **se saltan solos**: `pytest -q` sigue
pasando en una máquina sin Docker.

| Variable | Default | Para qué |
|---|---|---|
| `ORACLE_LOCAL_DSN` | `localhost:1521/FREEPDB1` | apuntar a otro puerto si el 1521 ya está ocupado |
| `ORACLE_LOCAL_USER` / `ORACLE_LOCAL_PASSWORD` | `AMALIA` / `Amalia_Local_2026` | credenciales del contenedor |
| `ORACLE_LOCAL_REQUERIDO` | *(sin definir)* | con `1`, la ausencia del contenedor es un **fallo** en vez de un skip (es lo que usa CI) |

**Qué cubre:** `schema.sql` (aplicación e idempotencia), el `DELETE` de
`retention.sql`, el `CREATE VECTOR INDEX` de `index_ivf.sql`, y las sentencias de
`src/memoria/repo.py`, `rag.py` y `vectorizer.py` — incluidas la cola durable con
`FOR UPDATE SKIP LOCKED`, el `MERGE` con columnas `JSON` nativas y las cuatro
variantes de `WHERE` de `buscar_precedentes`.

**Qué NO cubre** (y por eso sigue existiendo `tests/integration/test_adb.py`):
`DBMS_CLOUD`, el wallet mTLS, el modelo ONNX de embeddings y el dimensionamiento
del Vector Memory Pool. Las tres consultas que invocan `VECTOR_EMBEDDING(...)`
se ejecutan sustituyendo **solo** esa llamada por un literal `TO_VECTOR` de 384
dimensiones; el resto de la sentencia corre tal cual salió del código.

En CI corre como el job `dialecto` de `.github/workflows/ci.yml`, condicionado
por rutas (`src/memoria/**`, `db/**`, `pyproject.toml`) con `dorny/paths-filter`.

Vivió en un workflow aparte hasta el 2026-09-20, con el argumento de que meterlo
en `ci.yml` cuadruplicaría la espera de todos los cambios. **La medición lo
desmintió**: el job tarda 89-125 s y `ci.yml` tarda 170-243 s, así que es el más
corto de los dos — y como los jobs corren en paralelo, traerlo no agrega ni un
segundo de reloj. Lo que se gana es que pueda marcarse como check **obligatorio**:
un workflow con filtro `paths` no reporta ningún check en las PRs que no tocan
esas rutas, y eso dejaría esas PRs esperando para siempre.

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

- **Inventario:** `docs/arquitectura/modelo-de-datos.md` (sección 2)
- **Arquitectura:** `docs/arquitectura/vision-general.md`
- **Proyecto:** `/Users/emiliogonzalez/Documents/MULTICOMPUTOS/POCs/tender-opportunity-tracker`

---

*Script generado por DBA Oracle, 2026-08-02. Reexecutable / idempotent.*
