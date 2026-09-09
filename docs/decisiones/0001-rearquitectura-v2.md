# CHANGELOG de Arquitectura · AMALIA (auditoría v1 → v2)

> **Auditor:** Arquitecto Cloud Principal / Ingeniero Senior DevOps (OCI, serverless efímero, optimización de memoria en 1 GB, IA/LLM/RAG).
> **Alcance:** `docs/arquitectura/vision-general.md`, `docs/arquitectura/modelo-de-datos.md` y el HLD/LLD `diagramas_hld_lld_amalia.png`.
> **Fecha:** 2026-08-02.
> **Veredicto:** el concepto (embudo híbrido + delegación cognitiva + BD como cerebro RAG) es sólido, pero el diseño original contenía **contradicciones fatales de viabilidad** sobre 1 GB de RAM y **brechas de implementación** que lo hacían no desplegable. La v2 las cierra.

---

## 1. Debilidades encontradas (por severidad)

### 🔴 CRÍTICAS — hacían la arquitectura inviable o auto-contradictoria

| # | Debilidad | Evidencia | Impacto |
|---|---|---|---|
| C-1 | **LLM local (Ollama · Llama 3 8B Q4) como daemon residente** en el HLD/LLD | La imagen dibuja "OLLAMA CONTAINER — Llama 3 8B Instruct Q4" como *resident service* en la VM de 1 GB | Físicamente imposible: 8B Q4 ≈ 4.7 GB solo de pesos. Sobre 1 GB + swap → *thrashing* eterno, jamás termina en la ventana del timer. Además **contradice** el texto ("el servidor no ejecuta modelos de IA localmente"). |
| C-2 | **Vector DB residente (ChromaDB/Qdrant)** como daemon, **además** de Oracle ADB | La imagen dibuja "VECTOR DB CONTAINER (DAEMON) — ChromaDB/Qdrant" como *resident service* | Daemon 24/7 rompe el "reposo absoluto" efímero y consume RAM permanente. Es **redundante** con ADB AI Vector Search. Dos vector stores = doble estado y doble mantenimiento. |
| C-3 | **Playwright/Chromium y procesamiento de PDF+LLM en el mismo proceso/etapa** | Flujo monolítico en un solo `docker run --rm` | Chromium (300–500 MB pico) + rasterizado de PDF + payload multimodal en RAM simultáneamente ⇒ **OOM casi garantizado** sobre `mem_limit=800m`. |
| C-4 | **PDF cargado completo en memoria antes del chunking** | "analizar... antes de hacer los chunks"; sin especificar cómo se abre el PDF | `fitz.open(stream=bytes)` o `response.content` cargan el documento entero (pliegos escaneados 50–200 MB) a RAM ⇒ OOM. Riesgo directo señalado en el brief. |
| C-5 | **Embeddings vía API externa (Cohere/OpenAI)** en la ruta crítica | HLD "Embeddings API (Cohere/OpenAI)" | Rompe **costo cero** (segunda API de pago), rompe **mono-vendor** (2º proveedor, 2ª cuota, 2º punto de fallo) y añade latencia/retries en la ruta crítica. |

### 🟠 ALTAS — brechas de implementación que bloqueaban el despliegue

| # | Debilidad | Impacto |
|---|---|---|
| A-1 | **Sin políticas de reintento** (429/5xx/timeouts de Anthropic, ADB, Object Storage, COMPRASAL) | Un `Too Many Requests` o timeout tumbaba la corrida entera; sin resiliencia. |
| A-2 | **Sin esquema físico de BD (DDL)**: tipos, PK/FK, dimensión del `VECTOR`, tipo de índice | Imposible implementar; ambigüedad sobre dims de embedding e indexación. |
| A-3 | **Sin idempotencia ni reanudación** ante muerte del contenedor a mitad de camino | Un apagón entre fases perdía o duplicaba trabajo. |
| A-4 | **Sin guardia de cuota/tokens (pre-flight)** | Un pliego anómalo podía disparar un gasto grande sin control. |
| A-5 | **Rasterizado multimodal sin acotar** (DPI, formato, nº de páginas, liberación de memoria) | Acumular páginas en PNG a alto DPI ⇒ cientos de MB en RAM + tokens caros. |
| A-6 | **RAG asumido como obligatorio** para QA de un solo documento | Over-engineering: un pliego cabe en el contexto de Sonnet; embeddings+vector DB añaden complejidad por beneficio marginal en el caso común. |

### 🟡 MEDIAS — buenas prácticas / endurecimiento

| # | Debilidad | Impacto |
|---|---|---|
| M-1 | **SSH abierto a `0.0.0.0/0`** (heredado de `deploy.sh`) | Superficie de ataque amplia para un SaaS B2B. |
| M-2 | **IAM sin definir** (alcance de Instance Principal) | Riesgo de exceso de privilegios. |
| M-3 | **Sin lifecycle de Object Storage** | PDFs (PII) acumulándose; riesgo de costo y de retención de datos sensibles. |
| M-4 | **Egress sin restringir** | Exfiltración potencial; no cumple mínimo privilegio de red. |
| M-5 | **Estados de máquina incompletos** (sin edge cases ni estados de error) | Sin observabilidad del embudo ni ruta para revisión humana. |
| M-6 | **Wallet mTLS de ADB sin ubicación definida** | Riesgo de credenciales en disco persistente. |
| M-7 | **Modelos LLM inconsistentes** entre documentos ("Claude 3.5" vs "Sonnet 4.6") | Ambigüedad de versión. |

---

## 2. Decisiones técnicas tomadas (resoluciones)

### Frente a las críticas

- **C-1 → Eliminado todo LLM local.** El razonamiento se delega 100% a la API de Claude: **Haiku 4.5** (triaje/visión barata) y **Sonnet** (análisis profundo). La VM queda como orquestador *stateless* puro. *(Se prioriza el texto original, que ya era correcto, sobre el diagrama.)*
- **C-2 → Un solo vector store: Oracle ADB 23ai AI Vector Search.** Se elimina ChromaDB/Qdrant. Cero daemons residentes en la VM.
- **C-3 → Pipeline partido en dos etapas efímeras aisladas en memoria** (`amalia-scrape` → `amalia-analyze`, encadenadas por systemd `OnSuccess`). Cada `docker run --rm` arranca en baseline ⇒ **los picos de Chromium y de PDF/LLM nunca coexisten**. Se añadió el presupuesto de memoria por etapa.
- **C-4 → Regla dura: abrir PDFs desde ruta de archivo (`fitz.open(path)`, mmap lazy), nunca desde bytes.** Descarga en **streaming** a disco (`requests(stream=True)`, chunk a chunk), validación de `Content-Length` (rechazo > 100 MB) y subida multipart a Object Storage sin buffer total.
- **C-5 → Embeddings *in-database*.** Modelo ONNX `all-MiniLM-L6-v2` (384 dims) cargado en ADB (`DBMS_VECTOR.LOAD_ONNX_MODEL`); `VECTOR_EMBEDDING(...)` genera vectores dentro de la BD. **Cero costo, cero RAM en la VM, cero proveedor extra.** Stack mono-vendor (solo Anthropic como API de pago).

### Frente a las brechas altas

- **A-1 → Matriz de retry por dependencia** (Anthropic, OCI Object Storage, ADB/oracledb, COMPRASAL, Playwright): backoff exponencial con jitter, honrar `Retry-After`, códigos reintentables/no-reintentables, con `tenacity==9.0.0` (ya en `requirements.txt`) + retries nativos de los SDK. Circuit breaker existente reutilizado.
- **A-2 → DDL completo Oracle 23ai**: `licitaciones_metadata` (con estado de cola), `licitaciones_chunks` (`VECTOR(384, FLOAT32)`), `presupuesto_diario`, `licitaciones_analisis`; PK/FK con `ON DELETE CASCADE`, índices, y decisión de **índice IVF** (mejor que HNSW en Always Free) o sin índice para corpus pequeño.
- **A-3 → Idempotencia + reanudación vía DB-as-queue** (`SELECT ... FOR UPDATE SKIP LOCKED`). Sin brokers residentes (Redis/RabbitMQ serían anti-patrón aquí). Concurrencia = 1 por diseño de memoria.
- **A-4 → Guardia de cuota pre-flight** contra `presupuesto_diario` en ADB; si se excede, `REQUIERE_REVISION_MANUAL` en vez de gastar.
- **A-5 → Rasterizado streaming acotado**: una página a la vez, escala a **long-edge ≤ 1568 px** (óptimo de Anthropic) o ~150 DPI, **JPEG q≈80** (no PNG), liberar pixmap inmediato (`pix=None`), cap duro de páginas (1–15).
- **A-6 → Dos tracks explícitos.** *Track A (default):* extracción intra-documento con **contexto largo** de Sonnet, **sin embeddings**. *Track B (asíncrono/opcional):* memoria institucional inter-documento con vector search. El RAG sale de la ruta crítica.

### Frente al endurecimiento

- **M-1/M-4 → Security Lists de mínimo privilegio**: SSH restringido a IP admin (no `0.0.0.0/0`), egress solo 443 (COMPRASAL/Anthropic/OCI) + 1522 (ADB mTLS), resto DENY.
- **M-2/M-6 → IAM mínimo + Instance Principal**: Dynamic Group de la instancia, políticas acotadas (`read secret-bundles` del vault, `manage objects` solo del bucket de AMALIA, `use autonomous-databases` de la ADB). Wallet mTLS traído de Vault a `tmpfs` (RAM) en runtime, descartado al `exit`. Sin credenciales estáticas.
- **M-3 → Lifecycle policy** de Object Storage: auto-borrado de PDFs > 7 días (costo + PII).
- **M-5 → Máquina de estados completa** con estados de descarte, error (reintentables) y `REQUIERE_REVISION_MANUAL`, más reglas de terminalidad y archivado.
- **M-7 → Nomenclatura de modelos unificada**: Haiku 4.5 (triaje/visión) y Sonnet (análisis), con ID pinneable por configuración.

---

## 3. Cómo se modificaron los archivos

### `docs/arquitectura/vision-general.md` — reescritura completa (v2)
- Nueva **sección 0** con las 3 correcciones de topología (C-1, C-2, C-5) en tabla.
- PARTE I: inventario de componentes corregido (sin LLM local, sin Chroma/Qdrant, embeddings in-DB, Vault + Instance Principal, lifecycle).
- **Nueva PARTE II**: modelo de **dos etapas efímeras aisladas** + **presupuesto de memoria** por etapa.
- PARTE III: flujo memory-safe (streaming download, `fitz.open(path)`/mmap, rasterizado una página a la vez, RAG in-DB).
- **Nueva PARTE IV**: decisión **Track A (contexto largo) vs Track B (vector search)**.
- **Nueva PARTE V**: blindajes transversales (cola durable, idempotencia, retries, guardia de cuota, seguridad, verificación de costo cero).
- Diagrama conceptual redibujado a la topología v2.

### `docs/arquitectura/modelo-de-datos.md` — brechas 🔴 cerradas
- **§1 Secuencia**: secuencia actualizada a dos etapas + **§1.1 tabla de retry policies**, **§1.2 manejo de excepciones**, **§1.3 gestión de cuotas**.
- **§2 ERD**: **DDL completo Oracle 23ai** (4 tablas, PK/FK, `VECTOR(384)`), carga de modelo ONNX, **query de cola `SKIP LOCKED`**, **query RAG in-DB**, decisión de índice **IVF vs HNSW**, y **políticas de retención**.
- **§3 Red/Seguridad**: tablas de **Security Lists** (ingress/egress) e **IAM** de mínimo privilegio + manejo del wallet mTLS.
- **§4 Máquina de estados**: **diagrama de estados completo** con edge cases, estados de error reintentables, reanudación e idempotencia, y archivado end-of-life.

### `docs/decisiones/0001-rearquitectura-v2.md` — nuevo (este archivo).

---

## 4. Deudas / decisiones para el dueño (no bloqueantes)

1. **Alcance de fase:** esta v2 es **Fase 2** (drill-down de pliegos). La producción actual (v0.1.0) solo scrapea metadatos del listado; el multimodal es la "Mejora 2" del pitch deck. Confirmar priorización antes de implementar.
2. **`diagramas_hld_lld_amalia.png` quedó desactualizado**: sigue mostrando Ollama + Chroma/Qdrant. **Debe regenerarse** acorde a la topología v2 (o marcarse como obsoleto). Los `.md` son ahora la fuente de verdad.
3. **Modelo de embeddings:** `all-MiniLM-L6-v2` (384) es un default sólido y ligero. Si se requiere mayor calidad semántica en español legal, evaluar un ONNX multilingüe (p. ej. `multilingual-e5-small`, 384) — misma dimensión, cero cambio de esquema.
4. **Índice vectorial:** arrancar **sin índice** (búsqueda exacta) mientras el corpus sea pequeño; crear el IVF cuando el volumen lo justifique.
5. **SSH `0.0.0.0/0`:** vive en el `deploy.sh` de producción; endurecerlo impacta el flujo operativo actual — coordinar antes de tocar infra viva.
6. **Track B (vector search)** puede diferirse por completo en el MVP de Fase 2: el Track A (contexto largo) entrega la matriz de riesgos sin ADB-vector. Vector search se enciende cuando la inteligencia inter-documento sea prioridad.
