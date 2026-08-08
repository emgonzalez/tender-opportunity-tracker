# Arquitectura de Solución AMALIA · SaaS Serverless Multimodal (v2 — Blindada)

> **Estado:** Diseño técnico Fase 2 (drill-down multimodal + memoria institucional).
> **Revisión:** v2 · auditoría de arquitectura cloud/DevOps (ver `docs/decisiones/0001-rearquitectura-v2.md`).
> **Premisa de hierro:** 100% dentro de OCI Always Free. Instancia **AMD E2.1.Micro (1 OCPU, 1 GB RAM)**, contenedor **efímero** (`docker run --rm`) gatillado por **systemd timer**. Objetivo de costo mensual: **USD 0.00**.

**Objetivo:** Extender AMALIA de un scraper de metadatos a un analista de pliegos completos: descargar el PDF de cada licitación relevante, leerlo (nativo o escaneado), extraer una **Matriz de Riesgos Financieros y Técnicos** y prellenar anexos — sin exceder nunca 1 GB de RAM y sin generar costo.

---

## 0. Correcciones críticas frente al diseño original (resumen)

Esta versión **anula** dos decisiones del HLD/LLD original que eran inviables sobre 1 GB de RAM. El detalle está en `docs/decisiones/0001-rearquitectura-v2.md`; se resumen aquí porque cambian la topología:

| Componente original (descartado) | Por qué es inviable | Reemplazo en v2 |
|---|---|---|
| **Ollama · Llama 3 8B Q4 residente** (daemon LLM local, en el diagrama) | 8B Q4 ≈ 4.7 GB solo de pesos. Sobre 1 GB + swap haría *thrashing* eterno; jamás termina dentro de la ventana del timer. Contradice el texto ("el servidor no ejecuta modelos de IA localmente"). | **Sin LLM local.** Todo el razonamiento se delega a la API de Claude (Haiku 4.5 para triaje/visión barata, Sonnet para análisis profundo). |
| **ChromaDB / Qdrant residente** (vector DB daemon, en el diagrama) | Un daemon residente consume RAM 24/7 y rompe el "reposo absoluto" efímero. Es **redundante**: Oracle ADB 23ai ya es la base vectorial. Dos vector stores = doble mantenimiento y doble estado. | **Un solo vector store: Oracle ADB 23ai AI Vector Search.** Cero daemons residentes en la VM. |
| **Embeddings vía API externa** (Cohere/OpenAI) | Introduce un **segundo proveedor pagado**, rompe el stack mono-vendor (Anthropic) y añade un punto de fallo/costo en la ruta crítica. | **Embeddings dentro de la base de datos** con un modelo ONNX cargado en ADB (`VECTOR_EMBEDDING`). Cero costo, cero RAM en la VM, cero vendor extra. |

Con esto, la VM queda como un **orquestador stateless puro**: sin daemons, sin modelos, sin estado. Todo lo pesado (matemática vectorial, embeddings, razonamiento) vive fuera del 1 GB.

---

## PARTE I: ARQUITECTURA DE LA SOLUCIÓN (Componentes)

Paradigma de **desacoplamiento estricto**. El orquestador no retiene estado, no almacena archivos pesados en RAM y no ejecuta modelos. Es únicamente un director de orquesta que mueve punteros y delega.

### 1. Infraestructura Core (OCI Always Free)

* **Orquestador Efímero (Cómputo):** AMD **E2.1.Micro** (1 OCPU burstable, 1 GB RAM). Ejecuta un contenedor Docker efímero (`docker run --rm`) gatillado por systemd timer. **Malla de seguridad de memoria:** ZRAM (50%), swap de 8 GB en disco, `earlyoom`, `swappiness=100`, `mem_limit=800m`, `oom_score_adj=1000` (ya en `docker-compose.e2.yml` / `tenants.yaml#emiliogonzalez_e2`).
* **Motor RAG y Memoria Persistente (BD):** **Oracle Autonomous Database 23ai** (Always Free, 20 GB). Doble rol: (a) almacén relacional de metadatos y del **estado del pipeline** (cola durable), y (b) **AI Vector Search** nativo — genera embeddings *in-database* (ONNX) y ejecuta `VECTOR_DISTANCE`. Toda la matemática pesada ocurre aquí, no en la VM.
* **Data Lake Temporal (Almacenamiento):** **OCI Object Storage** (Always Free 20 GB). Bandeja de entrada de PDFs y **punto de handoff entre las dos etapas efímeras** (scrape → analyze). Protege el boot volume y desacopla las fases. **Lifecycle policy obligatoria**: auto-borrado de objetos > 7 días (higiene de costo y de PII).
* **Bóveda de Secretos (Seguridad):** **OCI Vault**. Credenciales de BD, wallet mTLS de ADB, API keys (Anthropic). El orquestador los inyecta en memoria **solo durante la ejecución** vía **Instance Principal** (sin llaves estáticas en disco ni en el `.env` de la VM).
* **Notificación:** **OCI Email Delivery** (o SMTP existente) para el reporte y las alertas de circuit-breaker.

### 2. Servicios Cognitivos Delegados (única dependencia externa: Anthropic)

* **Triaje y Visión (barato):** **Claude Haiku 4.5**. Lee imágenes de PDFs escaneados y decide viabilidad (GO/NO-GO) con el mínimo de tokens.
* **Análisis Profundo:** **Claude Sonnet** (última versión disponible; ID configurable). Redacta la matriz de riesgos y prellena anexos sobre los fragmentos exactos recuperados.
* **Embeddings:** **NO externos.** Se generan *in-database* en ADB con un modelo ONNX (`multilingual-e5-small`, 384 dims, 100 idiomas). Ver PARTE III.

> **Regla mono-vendor:** la única API externa de pago es Anthropic. Esto simplifica facturación, cuotas, retries y superficie de seguridad.

---

## PARTE II: MODELO DE EJECUCIÓN — Dos etapas efímeras aisladas en memoria

**Falla original:** correr Playwright/Chromium (300–500 MB pico) y el rasterizado de PDF + payload multimodal en el **mismo** proceso garantiza OOM sobre 800 MB.

**Solución:** partir el pipeline en **dos contenedores efímeros secuenciales**, encadenados por systemd. Cada `docker run --rm` arranca desde el baseline (~0), dándonos **aislamiento de memoria por construcción**:

```
amalia-scrape.service   (Etapa A: navegador)   ──OnSuccess──▶   amalia-analyze.service  (Etapa B: PDFs + LLM)
   Playwright/Chromium                                              PyMuPDF + Claude (sin navegador vivo)
   Descarga PDFs ─▶ Object Storage                                  Lee de Object Storage ─▶ ADB
   Escribe estado en ADB                                            Lee cola de ADB (SKIP LOCKED)
   exit 0  → RAM vuelve a baseline                                  exit 0
```

- La Etapa A **nunca** tiene PyMuPDF ni payloads de imágenes en RAM.
- La Etapa B **nunca** tiene Chromium en RAM.
- El handoff es durable (Object Storage + tabla de estado en ADB), así que si la VM se apaga entre A y B, la Etapa B se reanuda sola en el próximo disparo.

### Presupuesto de memoria (E2.1.Micro, 800 MB de container)

| Consumidor | Etapa A | Etapa B |
|---|---|---|
| Baseline OS + dockerd + systemd | ~300 MB | ~300 MB |
| Chromium headless (LOW_MEMORY_MODE) | ~350–450 MB | — |
| Python runtime + libs | ~120 MB | ~120 MB |
| PyMuPDF (mmap, lazy) | — | ~30–60 MB |
| 1 página rasterizada (150 DPI, JPEG) | — | ~6–15 MB (una a la vez) |
| **Pico estimado** | **~770–870 MB** | **~470–520 MB** | 

Etapa A vive al filo → ZRAM/swap la sostienen. Etapa B queda holgada. **Nunca se suman los dos picos.**

---

## PARTE III: FLUJO DE EJECUCIÓN (Embudo Híbrido Multimodal, memory-safe)

Diseñado para descartar lo antes posible y para **acotar la RAM en cada paso**.

### Fase 1 — Ingestión y detección de formato (Costo $0, RAM mínima) · Etapa A

1. El orquestador descarga el PDF de COMPRASAL **en streaming** (`requests(stream=True)`), validando primero `Content-Length` (rechazar > 100 MB) y escribiendo **chunk a chunk a disco**, nunca `response.content` completo en RAM. Sube el objeto a **Object Storage** (multipart, sin buffer total).
2. Registra el proceso en ADB con estado `DESCARGADO`.

### Fase 1b — Detección de formato · Etapa B

3. **Abrir el PDF desde ruta de archivo, nunca desde bytes en memoria:**
   `fitz.open("/tmp/pliego.pdf")` usa **mmap** (páginas cargan on-demand). `fitz.open(stream=bytes)` carga todo el documento a RAM → **prohibido**.
4. **Bifurcación de formato** (heurística por densidad de texto):
   * *Ruta A (texto nativo):* `page.get_text("text")`. Si la densidad de texto supera el umbral → extrae texto y aplica **RegEx** contra el Diccionario de Capacidades de Multicómputos. Sin match → `DESCARTADO_REGEX`, se borra el objeto.
   * *Ruta B (escaneado):* si no hay capa de texto (fuentes no incrustadas / densidad ~0), se **omite el RegEx** (evita falso negativo) y pasa a Fase 2 en modo visión.

### Fase 2 — Triaje de alcance (Claude Haiku 4.5) · Etapa B

1. Se recortan las **páginas clave** (típicamente 1–15, el "Objeto de la Contratación"), con **cap duro** de páginas.
2. **Rasterizado streaming, una página a la vez** (crítico para RAM):
   - Escalar a **long-edge ≤ 1568 px** (óptimo de Anthropic: mejor ratio calidad/tokens) o ~150 DPI.
   - Codificar **JPEG q≈80** (no PNG), base64, adjuntar, y **liberar el pixmap inmediatamente** (`pix = None`).
   - Nunca acumular la lista completa de páginas rasterizadas en RAM.
3. Prompt de evaluación (Ruta A envía texto; Ruta B envía imágenes):
   *"Actúa como evaluador técnico. Determina si el alcance requiere servicios de infraestructura TI o desarrollo. Responde solo JSON: {\"viabilidad\":\"GO|NO-GO\", \"confianza\":0.0-1.0, \"motivo\":\"...\"}."*
4. `NO-GO` → estado `DESCARTADO_TRIAJE`, termina. `confianza` baja → `REQUIERE_REVISION_MANUAL`.

### Fase 3 — Vectorización *in-database* y persistencia · Etapa B

*(Solo para procesos `GO`. Ruta opcional — ver "Track A vs Track B" abajo.)*

1. Se extrae el texto completo. Si es escaneado, la transcripción proviene de Claude Haiku (visión) página por página; se persiste como texto para no re-OCR.
2. El texto se divide en *chunks* (~800 tokens, solape 100) en la VM (barato, solo strings).
3. Se hace `INSERT` de cada chunk en `LICITACIONES_CHUNKS`. **El embedding se calcula dentro de ADB** con `VECTOR_EMBEDDING(model USING :texto)` — cero llamadas externas, cero RAM en la VM.

### Fase 4 — Búsqueda semántica (RAG delegado a ADB) · Etapa B

1. Para armar la matriz de riesgos, el orquestador **no procesa el PDF en RAM**: hace consultas SQL a ADB.
2. La consulta genera el embedding de la pregunta *también in-DB* y recupera los top-k chunks:
   ```sql
   SELECT chunk_text
   FROM   licitaciones_chunks
   WHERE  proceso_id = :pid
   ORDER  BY VECTOR_DISTANCE(embedding,
             VECTOR_EMBEDDING(minilm_l6 USING :pregunta AS data), COSINE)
   FETCH  FIRST 5 ROWS ONLY;
   ```
3. ADB asume todo el trabajo matemático y devuelve a la VM solo ~5 párrafos (penalidades, multas, specs).

### Fase 5 — Análisis profundo y autodestrucción · Etapa B

1. El orquestador envía los párrafos exactos a **Claude Sonnet** (con guardia de cuota previa, ver PARTE V).
2. Sonnet redacta la **Matriz de Riesgos** y prellena borradores de anexos (F9, F11).
3. Se persiste el resultado (`ANALIZADO`), se dispara la alerta por Email/Webhook (`NOTIFICADO`), el objeto de Object Storage se marca para lifecycle-delete, y el contenedor termina (`exit 0`) → reposo absoluto.

---

## PARTE IV: ¿RAG o contexto largo? — Dos tracks explícitos

Auditoría honesta: **para extraer una matriz de riesgos de un solo pliego, el RAG con embeddings frecuentemente es *over-engineering*.** Un pliego típico entra completo en la ventana de contexto de Sonnet (200k tokens ≈ cientos de páginas). Embeddings + vector DB + round-trips añaden complejidad y latencia por un beneficio marginal en QA de **un** documento.

Por eso se separan dos tracks, y el vector search deja de estar en la ruta crítica:

| | **Track A — Extracción intra-documento (ruta crítica, default)** | **Track B — Memoria institucional inter-documento (asíncrono, opcional)** |
|---|---|---|
| Objetivo | Matriz de riesgos + anexos de **este** pliego | "¿Hemos visto cláusulas/instituciones similares antes?", tendencias, inteligencia histórica |
| Técnica | **Contexto largo**: mandar a Sonnet las secciones relevantes (recortadas por RegEx/estructura), **sin embeddings** si el doc cabe | **AI Vector Search** sobre el corpus histórico (Fases 3–4) |
| Embeddings | No necesarios en el caso común | In-DB (ONNX), poblado fuera de la ruta crítica |
| Cuándo cae a RAG | Solo si el documento excede el presupuesto de tokens/costo del día | Siempre que se consulte memoria cruzada |

**Decisión:** el default operativo es **Track A** (más simple, más barato, menos dependencias). El Track B (embeddings + vector search) se **desacopla** a un poblado asíncrono y se usa para inteligencia de mercado cruzada, no para responder sobre un pliego individual. Esto elimina la dependencia de embeddings de la ruta crítica y mantiene el diseño mono-vendor y de menor superficie.

---

## PARTE V: Blindajes transversales (lo que faltaba)

- **Cola durable = la base de datos** (patrón *DB-as-queue*): no hay Redis/RabbitMQ (serían daemons residentes = anti-patrón aquí). La Etapa B consume trabajo con `SELECT ... FOR UPDATE SKIP LOCKED` sobre la tabla de estado. Concurrencia **= 1** (un solo pliego en RAM a la vez) por diseño de memoria. Ver DDL y máquina de estados en `docs/arquitectura/modelo-de-datos.md`.
- **Idempotencia y reanudación:** cada transición de estado es idempotente; un contenedor que muere a mitad de camino no corrompe nada — el siguiente disparo retoma los estados no-terminales.
- **Retry policies:** backoff exponencial con jitter, honrar `Retry-After` en 429/529, `tenacity` (ya en `requirements.txt`) + retries nativos del SDK de Anthropic y del OCI SDK. Circuit breaker ya existente. Detalle completo en `docs/arquitectura/modelo-de-datos.md`.
- **Guardia de cuota (pre-flight):** antes de una llamada cara a Sonnet, estimar tokens (`chars/4`) y compararlos contra un contador de presupuesto diario en ADB; si se excede → `REQUIERE_REVISION_MANUAL` en vez de gastar.
- **Seguridad:** Instance Principal + Vault (sin llaves estáticas), IAM de mínimo privilegio, egress restringido a 443 hacia COMPRASAL/Anthropic/OCI, wallet mTLS de ADB traído de Vault en runtime, lifecycle-delete de PII en Object Storage.
- **Costo cero — verificación:** ADB Always Free se auto-detiene tras 7 días inactiva (el job diario la mantiene viva); Object Storage 20 GB con lifecycle; egress 10 TB/mes gratis; IPv4 idle mitigada por el cost-watchdog existente. El cap de $0.01 sigue como *safety net*.

---

## Diagrama conceptual (v2, corregido)

```
                         COMPRASAL (portal gubernamental)
                                    │  HTTPS 443
                    ┌───────────────▼───────────────┐
                    │  ETAPA A · amalia-scrape (--rm)│  Playwright/Chromium
                    │  stream download → Object Store│  (sin PyMuPDF ni LLM)
                    │  estado=DESCARGADO en ADB      │
                    └───────────────┬───────────────┘  exit 0 (RAM→baseline)
                                    │ OnSuccess (systemd)
                    ┌───────────────▼───────────────┐
                    │ ETAPA B · amalia-analyze (--rm)│  (sin Chromium)
                    │ cola ADB: SKIP LOCKED, 1 a la  │
                    │ vez → PyMuPDF (mmap, path)     │
                    │  ├─ RegEx (nativo) / visión (esc)
                    │  ├─ Triaje  ──▶ Claude Haiku 4.5│──┐
                    │  ├─ [Track B] chunks → ADB      │  │  única API
                    │  │      embeddings IN-DB (ONNX) │  │  externa de pago
                    │  ├─ RAG SQL VECTOR_DISTANCE(ADB)│  │  = Anthropic
                    │  └─ Análisis ─▶ Claude Sonnet   │──┘
                    │  estado=ANALIZADO → Email       │
                    └───────────────┬───────────────┘  exit 0 (reposo absoluto)
                                    │
        OCI Vault (secretos) · Instance Principal · IAM mínimo privilegio · egress 443
                Oracle ADB 23ai  =  metadatos + estado(cola) + vectores(AI Vector Search)
```

---

*Documento interno de Multicómputos El Salvador. La topología v2 elimina todo componente residente en la VM; el 1 GB de RAM solo hospeda un orquestador stateless.*
