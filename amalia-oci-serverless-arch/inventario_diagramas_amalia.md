# Inventario de Diagramas · Arquitectura AMALIA (v2 — Brechas cerradas)

> **Revisión:** v2. Este documento ya **no** solo lista brechas: las **resuelve** con DDL exacto, políticas de reintento y una máquina de estados implementable. Ver `arquitectura_amalia_multimodal.md` y `CHANGELOG_ARQUITECTURA.md`.

Documenta el diseño técnico de AMALIA (SaaS serverless efímero en OCI Always Free) listo para implementar. Cada sección incluye la base lógica y la **definición cerrada** (antes marcada como *gap*).

---

## 1. Diagrama de Secuencia (Sequence Diagram)

**Propósito:** cronología, interacción asíncrona de módulos y ciclo de vida del contenedor efímero — ahora en **dos etapas aisladas en memoria** (ver `arquitectura_amalia_multimodal.md`, PARTE II).

### 🟢 Base lógica (actualizada)

* **Gatillo:** systemd timer 07:00 (`TZ=America/El_Salvador`).
* **Ciclo de vida:** dos `docker run --rm` encadenados por `OnSuccess` — Etapa A (scrape) y Etapa B (análisis). RAM vuelve a baseline entre etapas.
* **Secuencia principal:**
  1. **[A]** Scraping + descarga en streaming (Playwright) → Object Storage; estado `DESCARGADO`.
  2. **[B]** Detección de formato (PyMuPDF `mmap` desde path).
  3. **[B]** Extracción (texto nativo) o visión página-a-página (escaneado, Claude Haiku).
  4. **[B]** Pre-filtro RegEx (solo Ruta A).
  5. **[B]** Triaje de alcance (Claude Haiku 4.5) → GO/NO-GO.
  6. **[B, Track B opcional]** Chunking (VM) + embeddings **in-DB** (ADB ONNX).
  7. **[B]** Persistencia (ADB: metadatos + vectores).
  8. **[B]** Consulta semántica (RAG vía SQL `VECTOR_DISTANCE`, o contexto largo en Track A).
  9. **[B]** Generación de reporte (Claude Sonnet).
  10. **[B]** Alerta (OCI Email Delivery / SMTP).
  11. **[B]** `exit 0`.

### ✅ Brechas cerradas

#### 1.1 Política de reintentos (Retry Policy) — por dependencia

Toda llamada de red usa **backoff exponencial con jitter** y honra `Retry-After`. Base de librería: `tenacity==9.0.0` (ya en `requirements.txt`) + retries nativos de cada SDK.

| Dependencia | Reintentable | No reintentable | Estrategia | Terminal si agota |
|---|---|---|---|---|
| **Anthropic API** | 408, 429, 500, 502, 503, **529 (overloaded)** | 400, 401, 403, 404, 422 | SDK `max_retries=4`, backoff exp base 1 s, jitter, honra `Retry-After`. Sobre 529, backoff más largo | `ERROR_LLM` → circuit breaker |
| **OCI Object Storage** | Throttling (429), 5xx | 400, 401, 404 | `Retry Strategy` por defecto del OCI SDK (exponencial + jitter) | `ERROR_DESCARGA` |
| **ADB (oracledb, mTLS)** | ORA-12170 (timeout), ORA-03113/03114 (conexión caída), ORA-00060 (deadlock) | ORA-01017 (credenciales), ORA-00942 (objeto inexistente) | Reintento de conexión con backoff; pool de tamaño **1**; `retry_count=3` en connect string | `ERROR_BD` |
| **Descarga COMPRASAL (requests)** | Timeout, ConnectionError, 429, 5xx | 404 (pliego retirado) | `tenacity`: 3 intentos, backoff 2/4/8 s, `stream=True` | `ERROR_DESCARGA` |
| **Playwright / navegación** | Timeout de locator, `net::ERR_*` | Login inválido | `wait_until="domcontentloaded"`, 3 reintentos (patrón existente), TOTP retry x3 | Alerta sesión expirada |

**Guardia de cuota (pre-flight) antes de Sonnet:** estimar tokens (`len(texto)//4`), leer el contador `presupuesto_diario` en ADB; si `consumido + estimado > límite` → **no llamar**, transicionar a `REQUIERE_REVISION_MANUAL`. Evita sobrecostos por documentos anómalos.

**Circuit breaker (ya existente en la app):** N fallos consecutivos de la ruta crítica → pausa + alerta al `system_admins_email`. Se reutiliza tal cual.

#### 1.2 Manejo de excepciones (Error Handling)

| Fallo | Detección | Acción | Estado |
|---|---|---|---|
| Cambio de DOM en COMPRASAL | Selector no encontrado / 0 elementos | Guardar HTML en `data/debug/`, alertar, no crashear el batch | log + alerta |
| PDF corrupto | `fitz.FileDataError` | Marcar y continuar con el siguiente | `ERROR_PARSEO` |
| PDF gigante (> 100 MB) | `Content-Length` pre-descarga | Rechazar sin descargar | `DESCARTADO_TAMANO` |
| Escaneado ilegible | Visión devuelve texto vacío/basura | Umbral de confianza | `REQUIERE_REVISION_MANUAL` |
| Alucinación / baja confianza LLM | `confianza < umbral` en el JSON | No auto-publicar | `REQUIERE_REVISION_MANUAL` |
| Contenedor muere a mitad | Estado no-terminal en ADB al re-arrancar | Reanudar (idempotente) vía cola | — |

#### 1.3 Gestión de cuotas y presupuesto de tokens

Tabla `PRESUPUESTO_DIARIO` en ADB (ver DDL). Cada llamada LLM hace `UPDATE ... SET tokens_consumidos = tokens_consumidos + :n`. La guardia pre-flight lee este valor. Reset diario por fecha. Límite configurable (default conservador, p. ej. equivalente a << $1/día para mantenerse holgado dentro de lo gratuito/experimental).

---

## 2. Diagrama Entidad-Relación (ERD) / Modelo de Datos

**Motor:** Oracle Autonomous Database **23ai** con AI Vector Search y embeddings *in-database* (ONNX).

### 🟢 Base lógica

* **Procesos (metadatos + estado):** código, título, institución, fechas, URLs, resultado del triaje, **estado del pipeline** (cola durable).
* **Chunks (vectores):** fragmentos de texto + embedding (`VECTOR`) para el RAG.
* **Presupuesto:** contador diario de tokens.

### ✅ Esquema físico cerrado (DDL Oracle 23ai)

```sql
-- ─────────────────────────────────────────────────────────────
-- 0) Modelo de embeddings IN-DATABASE (una sola vez, en el setup)
--    all-MiniLM-L6-v2 → 384 dimensiones, ~90 MB ONNX.
--    Se importa a ADB; los embeddings se calculan en la BD, no en la VM.
-- ─────────────────────────────────────────────────────────────
-- BEGIN
--   DBMS_VECTOR.LOAD_ONNX_MODEL(
--     directory   => 'MODELS_DIR',
--     file_name   => 'all_MiniLM_L6_v2.onnx',
--     model_name  => 'MINILM_L6',
--     metadata    => JSON('{"function":"embedding","embeddingOutput":384,"input":{"input":["DATA"]}}'));
-- END;
-- /

-- ─────────────────────────────────────────────────────────────
-- 1) Metadatos + estado del pipeline (tabla-cola durable)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE licitaciones_metadata (
  proceso_id      VARCHAR2(64)  CONSTRAINT pk_licit PRIMARY KEY,   -- "96-2026-P0011"
  cliente_id      VARCHAR2(64)  NOT NULL,
  institucion     VARCHAR2(400),
  titulo          VARCHAR2(1000),
  categoria       VARCHAR2(120),
  modalidad       VARCHAR2(120),
  monto_estimado  NUMBER(14,2),
  fecha_pub       DATE,
  fecha_cierre    DATE,
  url_comprasal   VARCHAR2(1000),
  objeto_uri      VARCHAR2(1000),                 -- URI en Object Storage
  formato         VARCHAR2(16),                   -- NATIVO | ESCANEADO
  triaje          VARCHAR2(8),                    -- GO | NO-GO | NULL
  triaje_conf     NUMBER(3,2),                    -- 0.00–1.00
  estado          VARCHAR2(32)  DEFAULT 'DESCUBIERTO' NOT NULL,
  intentos        NUMBER(3)     DEFAULT 0 NOT NULL,
  ultimo_error    VARCHAR2(1000),
  worker_id       VARCHAR2(64),                   -- lock lógico (SKIP LOCKED)
  creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
  actualizado_en  TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT ck_estado CHECK (estado IN (
    'DESCUBIERTO','DESCARGADO','FORMATO_NATIVO','FORMATO_ESCANEADO',
    'DESCARTADO_REGEX','DESCARTADO_TRIAJE','DESCARTADO_TAMANO',
    'APROBADO','VECTORIZADO','ANALIZADO','NOTIFICADO',
    'REQUIERE_REVISION_MANUAL','ERROR_DESCARGA','ERROR_PARSEO',
    'ERROR_LLM','ERROR_BD')),
  CONSTRAINT ck_triaje CHECK (triaje IN ('GO','NO-GO'))
);
CREATE INDEX ix_licit_estado ON licitaciones_metadata(estado, actualizado_en);
CREATE INDEX ix_licit_cliente ON licitaciones_metadata(cliente_id, fecha_pub);

-- ─────────────────────────────────────────────────────────────
-- 2) Chunks + vectores (Track B: memoria institucional)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE licitaciones_chunks (
  chunk_id     VARCHAR2(80) CONSTRAINT pk_chunk PRIMARY KEY,   -- proceso_id || '#' || seq
  proceso_id   VARCHAR2(64) NOT NULL,
  seq          NUMBER(6)    NOT NULL,
  pagina       NUMBER(5),
  chunk_text   CLOB         NOT NULL,
  embedding    VECTOR(384, FLOAT32),                            -- dims = modelo ONNX
  creado_en    TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT fk_chunk_proc FOREIGN KEY (proceso_id)
    REFERENCES licitaciones_metadata(proceso_id) ON DELETE CASCADE,
  CONSTRAINT uq_chunk UNIQUE (proceso_id, seq)
);

-- Índice vectorial: en Always Free (memoria acotada, corpus pequeño),
-- IVF es preferible a HNSW (HNSW exige Vector Memory Pool grande).
-- Para < ~50k vectores, la búsqueda exacta (sin índice) también es válida y precisa.
CREATE VECTOR INDEX ivf_chunks ON licitaciones_chunks (embedding)
  ORGANIZATION NEIGHBOR PARTITIONS
  DISTANCE COSINE
  WITH TARGET ACCURACY 90;

-- ─────────────────────────────────────────────────────────────
-- 3) Presupuesto de tokens (guardia de cuota)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE presupuesto_diario (
  dia               DATE CONSTRAINT pk_presup PRIMARY KEY,
  tokens_consumidos NUMBER(12) DEFAULT 0 NOT NULL,
  limite_tokens     NUMBER(12) NOT NULL
);

-- ─────────────────────────────────────────────────────────────
-- 4) Resultado del análisis (matriz de riesgos + anexos)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE licitaciones_analisis (
  proceso_id     VARCHAR2(64) CONSTRAINT pk_analisis PRIMARY KEY,
  matriz_riesgos JSON,                              -- 23ai: tipo JSON nativo
  anexos         JSON,
  modelo_llm     VARCHAR2(64),
  generado_en    TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT fk_analisis_proc FOREIGN KEY (proceso_id)
    REFERENCES licitaciones_metadata(proceso_id) ON DELETE CASCADE
);
```

### ✅ Cola durable (DB-as-queue) — sin brokers residentes

```sql
-- La Etapa B toma UN proceso pendiente de forma segura y reanudable.
-- SKIP LOCKED evita bloqueos entre reintentos; concurrencia = 1 por memoria.
UPDATE licitaciones_metadata
   SET estado = 'FORMATO_NATIVO', worker_id = :wid, intentos = intentos + 1,
       actualizado_en = SYSTIMESTAMP
 WHERE proceso_id = (
   SELECT proceso_id FROM licitaciones_metadata
    WHERE estado = 'DESCARGADO' AND intentos < 5
    ORDER BY fecha_cierre NULLS LAST
    FETCH FIRST 1 ROWS ONLY
    FOR UPDATE SKIP LOCKED)
 RETURNING proceso_id INTO :pid;
```

### ✅ RAG in-DB (embedding de la consulta también en la BD)

```sql
SELECT chunk_text
FROM   licitaciones_chunks
WHERE  proceso_id = :pid
ORDER  BY VECTOR_DISTANCE(embedding,
          VECTOR_EMBEDDING(MINILM_L6 USING :pregunta AS data), COSINE)
FETCH  FIRST 5 ROWS ONLY;
```

### ✅ Estrategia de indexación vectorial

- **Elegido: IVF** (`NEIGHBOR PARTITIONS`) sobre HNSW. HNSW mantiene el grafo en el **Vector Memory Pool**, escaso en Always Free; IVF particiona y escanea por vecindario con footprint menor.
- **Corpus pequeño (< ~50k chunks):** aceptable **sin índice** (búsqueda exacta) — precisión 100%, y con ese volumen la latencia es trivial. Crear el índice IVF solo cuando el volumen lo justifique.
- `VECTOR(384, FLOAT32)`: 384 = dims de `all-MiniLM-L6-v2`. Cambiar de modelo obliga a re-vectorizar y ajustar la dimensión.

### ✅ Políticas de retención

- **Object Storage:** lifecycle rule → borrar PDFs > 7 días (higiene de costo + PII).
- **Vectores/metadatos:** `metadata` y `analisis` se conservan como **memoria institucional** (Track B). Los `chunks` de procesos cerrados hace > 12 meses pueden archivarse/purgarse por job SQL programado (`DELETE` con `ON DELETE CASCADE`).
- **PII:** el texto crudo del pliego vive solo el tiempo del análisis; se persisten chunks y resultados, no el PDF.

---

## 3. Diagrama de Red y Seguridad (Network & Security)

### 🟢 Base lógica

* VCN, subnet pública, Internet Gateway.
* OCI Vault (secretos), Object Storage (data lake efímero), OCI Email Delivery.
* Naturaleza efímera del contenedor mitiga exposición de larga duración.

### ✅ Brechas cerradas

**Reglas Egress/Ingress (Security Lists):**

| Dirección | Puerto | Origen/Destino | Uso |
|---|---|---|---|
| Ingress | 22 | **IP administrativa** (no `0.0.0.0/0`) | SSH — endurecer respecto al default abierto |
| Egress | 443 | COMPRASAL, `api.anthropic.com`, endpoints OCI (Object Storage, ADB, Vault) | Único tráfico saliente permitido |
| Egress | 1522 | ADB (mTLS) | Conexión a Autonomous DB |
| — | resto | DENY | Todo lo demás bloqueado |

> Nota de deuda de seguridad: el `deploy.sh` actual abre SSH a `0.0.0.0/0`. Para el perfil SaaS B2B debe restringirse a IP/CIDR admin o pasar por bastion/OCI Bastion service.

**Políticas IAM (Instance/Dynamic Group + mínimo privilegio):**

- **Dynamic Group** = la instancia AMD (por OCID/compartment).
- Políticas mínimas: `read secret-bundles` (Vault) del vault específico; `manage objects` **solo** del bucket de AMALIA; `use autonomous-databases` de la ADB específica. **Sin** `manage all-resources`.
- **Cero credenciales estáticas en la VM:** Instance Principal firma las llamadas; el wallet mTLS de ADB se trae de Vault en runtime a `tmpfs` (RAM, no disco persistente) y se descarta al `exit`.

---

## 4. Diagrama de Máquina de Estados (State Machine)

**Propósito:** ciclo de vida de un documento desde detección hasta decisión final; habilita idempotencia, reanudación y monitoreo del embudo.

### ✅ Estados y transiciones (cerrado)

```
DESCUBIERTO
   │ (Etapa A: descarga OK a Object Storage)
   ▼
DESCARGADO ──(> 100 MB)──▶ DESCARTADO_TAMANO [terminal]
   │ (Etapa B: PyMuPDF mmap)
   ├─(hay capa de texto)──▶ FORMATO_NATIVO
   │                           │ RegEx
   │                           ├─(sin match)──▶ DESCARTADO_REGEX [terminal]
   │                           └─(match)──────▶ (triaje)
   └─(sin texto)─────────────▶ FORMATO_ESCANEADO ─(visión)─▶ (triaje)

(triaje · Claude Haiku 4.5)
   ├─ NO-GO ─────────────────▶ DESCARTADO_TRIAJE [terminal]
   ├─ conf < umbral ─────────▶ REQUIERE_REVISION_MANUAL [terminal-humano]
   └─ GO ────────────────────▶ APROBADO
                                  │ (Track B opcional: chunk + embeddings in-DB)
                                  ▼
                               VECTORIZADO
                                  │ (Sonnet: matriz de riesgos + anexos)
                                  ▼
                               ANALIZADO
                                  │ (Email/Webhook)
                                  ▼
                               NOTIFICADO [terminal-éxito]

Estados de error (reintentables hasta `intentos < 5`, luego terminal + alerta):
   ERROR_DESCARGA · ERROR_PARSEO · ERROR_LLM · ERROR_BD
```

**Reglas:**
- **Terminales de éxito:** `NOTIFICADO`. **Terminales de descarte:** `DESCARTADO_*`. **Terminal-humano:** `REQUIERE_REVISION_MANUAL`.
- **Reanudación:** al arrancar, la Etapa B consume cualquier estado **no-terminal** vía la cola `SKIP LOCKED`. Un crash entre transiciones no pierde trabajo ni duplica (idempotencia por `proceso_id` + guardas de estado).
- **Archivado (end-of-life):** procesos cerrados → job mueve el objeto (si aún existiera) a Archive Storage y purga chunks vencidos.

---

*Documento interno de Multicómputos El Salvador. Todas las brechas 🔴 del inventario original quedan resueltas en esta v2.*
