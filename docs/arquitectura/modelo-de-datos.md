# Inventario de Diagramas · Arquitectura AMALIA (v2 — Brechas cerradas)

> **Revisión:** v2. Este documento ya **no** solo lista brechas: las **resuelve** con DDL exacto, políticas de reintento y una máquina de estados implementable. Ver `docs/arquitectura/vision-general.md` y `docs/decisiones/0001-rearquitectura-v2.md`.

Documenta el diseño técnico de AMALIA (SaaS serverless efímero en OCI Always Free) listo para implementar. Cada sección incluye la base lógica y la **definición cerrada** (antes marcada como *gap*).

---

## 1. Diagrama de Secuencia (Sequence Diagram)

**Propósito:** cronología, interacción asíncrona de módulos y ciclo de vida del contenedor efímero — ahora en **dos etapas aisladas en memoria** (ver `docs/arquitectura/vision-general.md`, PARTE II).

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

Toda llamada de red usa **backoff exponencial con jitter** y honra `Retry-After`. Base de librería: `tenacity==9.0.0` (ya en `pyproject.toml`) + retries nativos de cada SDK.

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

> **Fuente única de verdad: [`db/schema.sql`](../../db/schema.sql).**
> El DDL ejecutable vive ahí y **no se replica aquí**: duplicarlo garantizaba
> que la prosa se desincronizara del esquema real. Este documento describe el
> *modelo*; el archivo `.sql` es el contrato.
>
> | Script | Rol |
> |---|---|
> | [`db/schema.sql`](../../db/schema.sql) | DDL idempotente del esquema actual (metadatos, chunks, análisis, feedback, mercado, libro LLM, historial de corridas) |
> | [`db/schema_v2.sql`](../../db/schema_v2.sql) | Esquema v2 multi-tenant/multi-país (fase "Expandir" de la migración descrita en el plan del portal AMALIA, §3). Coexiste con `schema.sql`: nadie lo lee ni lo escribe todavía en producción |
> | [`db/load_onnx.sql`](../../db/load_onnx.sql) | Carga del modelo de embeddings `MULTILINGUAL_E5_SMALL` (384 dims, 100 idiomas) |
> | [`db/index_ivf.sql`](../../db/index_ivf.sql) | Índice vectorial IVF (opt-in, >~50k chunks) |
> | [`db/retention.sql`](../../db/retention.sql) | Purga de chunks vencidos |

**Entidades y relaciones:**

| Tabla | Rol | Claves |
|---|---|---|
| `licitaciones_metadata` | Metadatos del proceso **+ estado del pipeline** (hace de cola durable) | PK `proceso_id` |
| `licitaciones_chunks` | Fragmentos del pliego y su `VECTOR(384, FLOAT32)` para el RAG | PK `chunk_id`; FK → metadata `ON DELETE CASCADE`; UNIQUE (`proceso_id`,`seq`) |
| `licitaciones_analisis` | Matriz de riesgos y anexos (JSON nativo 23ai) | PK `proceso_id`; FK → metadata `ON DELETE CASCADE` |
| `presupuesto_diario` | Contador de tokens para la guardia de cuota | PK `dia` |

El borrado en cascada desde `licitaciones_metadata` es deliberado: purgar un
proceso arrastra sus chunks y su análisis sin dejar huérfanos.

### ✅ Feedback de relevancia

Dos tablas aparte, definidas en [`db/schema.sql`](../../db/schema.sql) (allí está el DDL;
acá solo lo que no se lee en él):

- **`licitaciones_reportadas`** — la foto de cada proceso al enviarlo, por
  **(proceso, cliente)**. No cuelga de `licitaciones_metadata` porque esa tabla es por
  proceso (PK global) y solo se llena con `ENABLE_MEMORIA`. Un proceso que vuelve a salir
  en otro reporte (no hay dedup entre corridas, RB-004) refresca la foto y suma a
  `veces_enviada`.
- **`licitaciones_feedback`** — un voto por **(proceso, cliente, destinatario)**; cambiar de
  opinión sobrescribe la fila y los votos de personas distintas conviven (el desacuerdo es
  dato). Guarda los *códigos* de los motivos, no las etiquetas. **Sin FK** a
  `licitaciones_reportadas` a propósito: si la foto falló al enviar, el voto no se pierde.

Ninguna de las dos entra en la política de retención: son el criterio humano y no se
purgan. Diseño y por qué: [ADR 0005](../decisiones/0005-servicio-web-residente-para-el-feedback.md).

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
          VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :pregunta AS data), COSINE)
FETCH  FIRST 5 ROWS ONLY;
```

### ✅ Estrategia de indexación vectorial

- **Elegido: IVF** (`NEIGHBOR PARTITIONS`) sobre HNSW. HNSW mantiene el grafo en el **Vector Memory Pool**, escaso en Always Free; IVF particiona y escanea por vecindario con footprint menor.
- **Corpus pequeño (< ~50k chunks):** aceptable **sin índice** (búsqueda exacta) — precisión 100%, y con ese volumen la latencia es trivial. Crear el índice IVF solo cuando el volumen lo justifique.
- `VECTOR(384, FLOAT32)`: 384 = dims de `multilingual-e5-small`. Cambiar de modelo obliga a re-vectorizar y ajustar la dimensión.

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
