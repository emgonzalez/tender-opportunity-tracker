# Plan de Implementación · Fase 2 — Track B (memoria institucional + infra efímera)

> **Alcance:** añadir **memoria inter-documento** sobre Oracle ADB 23ai (AI Vector Search con embeddings *in-database*) y la **infra efímera de dos etapas** en OCI. Se construye ENCIMA del MVP Track A ya implementado (PR #5), sin romperlo.
> **Prerrequisito:** Track A validado en producción (T9.3 aprobado). Ver `PLAN_FASE2_TRACK_A.md`, `arquitectura_amalia_multimodal.md`, `inventario_diagramas_amalia.md`.
> **Premisa de hierro (igual que Track A):** OCI Always Free, AMD E2.1.Micro (1 OCPU, 1 GB RAM), contenedor efímero, **USD 0.00/mes**.

---

## 0. Cómo leer este plan (idéntico a Track A)

Dos capas de "modelo", no confundirlas:

| Capa | Qué es | Dónde se define |
|---|---|---|
| **Modelo runtime** | El que AMALIA usa en producción | §2 + `.env` |
| **Modelo-agente** | El que **escribe el código** de cada tarea | Etiqueta `[Agente: …]` |

**Tesis de tokens (rector):** cuanto más detallada la tarea, más barato el modelo que la ejecuta. Cada tarea trae archivos exactos, firmas, criterios de aceptación y contexto mínimo.

| Modelo-agente | ID | Cuándo |
|---|---|---|
| **Haiku 4.5** | `claude-haiku-4-5` | Mecánico, sin ambigüedad: deps, config, DDL literal, chunker, tests de fixtures |
| **Sonnet 5** | `claude-sonnet-5` | Lógica no trivial: driver/pool, repositorio SQL, RAG, migración de estado |
| **Opus 4.8** | `claude-opus-4-8` | Integración cruzada, memoria/OOM, infra viva, debugging E2E |

> Dar al agente **solo los archivos de "Archivos"**. Rama por incremento: `feature/fase2b-inc-N-desc` desde `develop`. Commits `feat(memoria): …` / `feat(oci): …`.

⚠️ **Áreas expertas de Emilio** (por `CLAUDE.md`): todo lo de `oci/` y `browser.py`. Esas tareas van con **PR + revisión humana obligatoria**, nunca merge directo.

---

## 1. Qué entra y qué NO

**Entra:** provisioning de ADB Always Free, modelo ONNX de embeddings in-DB, DDL, capa de acceso Python (`oracledb` thin), chunking + vectorización, RAG (intra e inter-documento), migración del estado file-based → DB-as-queue, bloque de "precedentes" en el reporte, infra de dos etapas efímeras, Vault/IAM/Security Lists, tests y validación.

**NO entra:** dashboard web (Fase 3), webhooks (Fase 4), multi-región (Fase 5), rasterización PyMuPDF página-a-página (solo si el costo de tokens en escaneados lo exige), migración del historial de reportes (`save_run`) a ADB — ese sigue file-based.

### El valor que justifica Track B
Track A responde *"¿qué riesgos tiene ESTE pliego?"*. Track B responde lo que Track A **no puede**:
- *"¿Esta institución ya nos impuso cláusulas de penalidad parecidas antes?"*
- *"¿Qué pliegos históricos se parecen a este?"* (precedentes para cotizar)
- *"¿Qué está comprando el mercado este trimestre?"* (tendencias por institución/categoría)

---

## 2. Decisiones técnicas de arquitectura (cerradas antes de codificar)

| Decisión | Elección | Por qué |
|---|---|---|
| **Driver Python** | `oracledb` en **modo thin** (sin Oracle Instant Client) | Instant Client pesa ~200 MB + libaio; **inviable** en 1 GB. Thin es Python puro, soporta mTLS y el tipo `VECTOR`. **Requiere `oracledb >= 2.5`** (verificar última estable en B2.1). |
| **Embeddings** | **In-database** ONNX `all-MiniLM-L6-v2` (384 dims) | Cero costo, cero RAM en la VM, cero proveedor extra. `VECTOR_EMBEDDING(...)` |
| **Índice vectorial** | **Sin índice** al inicio (búsqueda exacta); IVF cuando el corpus lo justifique | Con < ~50k chunks la exacta es precisa y trivial; HNSW exige Vector Memory Pool escaso en Always Free |
| **Cola** | **DB-as-queue** (`FOR UPDATE SKIP LOCKED`) | Sin brokers residentes (anti-patrón en efímero). Concurrencia = 1 por memoria |
| **Conexión** | Pool de **tamaño 1**, wallet mTLS traído de Vault a **tmpfs** | Memoria acotada; cero credenciales en disco persistente |
| **Modelo runtime** | Sin cambios: Haiku 4.5 triaje, Sonnet 5 análisis | Track B no añade llamadas LLM nuevas salvo el resumen de precedentes (Haiku) |

### ⚠️ Trampa de costo específica de ADB Always Free
La instancia **se auto-detiene tras 7 días de inactividad** y puede **reclamarse tras ~90 días** detenida. Con `ENABLE_DRILLDOWN=false` no habría conexiones y la BD moriría sola. → **B2.4 implementa un keepalive** (ping trivial en cada corrida diaria, incluso con el flag off).

---

## 3. Incrementos

### INCREMENTO B0 — Provisioning de ADB (infra) ⚠️ experto

**B0.1 · ADB Always Free en IaC** — `[Agente: Sonnet 5]` · PR + revisión Emilio
- **Archivos:** `oci/tenants.yaml`, `oci/lib/05-adb.sh` (nuevo), `oci/deploy.sh`
- **Spec:** bloque `adb:` por tenant (`enabled`, `display_name`, `db_name`, `workload: DW|OLTP`, `is_free_tier: true`, `admin_password_secret`). `05-adb.sh` crea la ADB **idempotente** (si existe por display_name, reusa), guarda OCID en `oci/state/<tenant>.json`, descarga el wallet y lo sube a Vault (B0.2). Registrar el paso en `deploy.sh` **detrás de un flag** para no alterar deploys actuales.
- **Aceptación:** `./oci/deploy.sh <tenant>` con el flag crea la ADB; re-ejecutar no duplica; OCID en state.
- **Contexto mínimo:** `oci/tenants.yaml`, `oci/deploy.sh`, `oci/lib/03-budget.sh` (patrón de scripts), `oci/README.md`.

**B0.2 · Wallet en Vault + IAM mínimo** — `[Agente: Sonnet 5]` · PR + revisión Emilio
- **Archivos:** `oci/lib/05-adb.sh`, `oci/lib/03-budget.sh` (dynamic group/policy existentes)
- **Spec:** subir el wallet (zip) como **secret de Vault**; ampliar el Dynamic Group de la instancia con políticas **mínimas**: `read secret-bundles` del vault específico, `use autonomous-databases` de esa ADB. Nada de `manage all-resources`.
- **Aceptación:** desde la VM, con Instance Principal, se puede leer el secret y conectar. Documentar el policy statement exacto.

**B0.3 · DDL idempotente** — `[Agente: Haiku 4.5]`
- **Archivos:** `db/schema.sql` (nuevo), `db/README.md`
- **Spec:** transcribir **literalmente** el DDL ya definido en `inventario_diagramas_amalia.md §2` (tablas `licitaciones_metadata`, `licitaciones_chunks`, `presupuesto_diario`, `licitaciones_analisis`), envolviendo cada `CREATE` en un bloque PL/SQL que ignore "ya existe" (ORA-00955) para hacerlo re-ejecutable. **No inventar columnas**: el inventario es la fuente de verdad.
- **Aceptación:** el script corre dos veces seguidas sin error.
- **Contexto mínimo:** solo `inventario_diagramas_amalia.md §2`.

**B0.4 · Carga del modelo ONNX** — `[Agente: Sonnet 5]`
- **Archivos:** `db/load_onnx.sql`, `db/README.md`
- **Spec:** procedimiento documentado para cargar `all_MiniLM_L6_v2.onnx` vía `DBMS_VECTOR.LOAD_ONNX_MODEL` (directory object, metadata JSON con `embeddingOutput: 384`). Incluir de dónde se descarga el ONNX y cómo subirlo al directory (Object Storage → `DBMS_CLOUD.GET_OBJECT`). Verificación: `SELECT VECTOR_EMBEDDING(MINILM_L6 USING 'servidor' AS data) FROM dual;`
- **Aceptación:** la query de verificación devuelve un vector de 384 dims.

---

### INCREMENTO B1 — Capa de acceso a datos

**B1.1 · Deps y config de BD** — `[Agente: Haiku 4.5]`
- **Archivos:** `requirements.txt`, `src/config.py`, `.env.example`
- **Spec:** añadir `oracledb` (**verificar la última estable ≥2.5 por soporte `VECTOR`**; documentar la versión elegida). Config nueva: `enable_memoria` (bool, default `False` — segundo feature flag, independiente de `ENABLE_DRILLDOWN`), `adb_user`, `adb_password_secret`, `adb_dsn`, `adb_wallet_secret_ocid`, `adb_wallet_dir` (default `/dev/shm/amalia_wallet` → tmpfs), `embedding_model_name` (default `MINILM_L6`), `rag_top_k` (default `5`), `chunk_tokens` (default `800`), `chunk_overlap` (default `100`).
- **Aceptación:** `Config().enable_memoria is False`; todas las claves accesibles; `.env.example` documentado.
- **Contexto mínimo:** `src/config.py`, `.env.example`.

**B1.2 · Conexión ADB (thin + wallet desde Vault)** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/__init__.py`, `src/memoria/db.py` (nuevos)
- **Spec:** 
  - `def fetch_wallet(cfg) -> str`: descarga el wallet desde OCI Vault con **Instance Principal** (`oci` SDK; si no hay SDK/entorno OCI, permitir fallback a `adb_wallet_dir` ya presente), lo extrae en `cfg.adb_wallet_dir` (tmpfs) con permisos 600. 
  - `def get_pool(cfg)`: `oracledb` **modo thin**, `oracledb.create_pool(min=1, max=1, increment=0, ...)` con `config_dir`/`wallet_location`/`wallet_password` apuntando al wallet. Singleton por proceso.
  - `@contextmanager def connection(cfg)`: cede una conexión del pool, `commit` al salir OK, `rollback` ante excepción.
  - `def close_pool()`: cierra y **borra el wallet de tmpfs** (llamar al final del proceso).
  - Retries de conexión con `tenacity`: 3 intentos, backoff 2/4/8, reintentar ORA-12170/03113/03114; **no** reintentar ORA-01017 (credenciales).
- **Aceptación:** sin ADB real no se puede conectar → el test verifica que `get_pool` construye los parámetros correctos (mockeando `oracledb.create_pool`) y que `connection` hace rollback ante excepción. Documentar cómo probar contra una ADB real.
- **Contexto mínimo:** `src/config.py`, esta spec. Recalcar **thin mode** (no Instant Client).

**B1.3 · Keepalive de ADB** — `[Agente: Haiku 4.5]`
- **Archivos:** `src/memoria/db.py`
- **Spec:** `def keepalive(cfg) -> bool` — ejecuta `SELECT 1 FROM dual`, loguea y devuelve éxito; captura toda excepción (nunca tumba el pipeline). Se llamará **en cada corrida diaria aunque `enable_memoria` esté off** (evita el auto-stop a 7 días / reclaim a 90).
- **Aceptación:** con `oracledb` mockeado, devuelve True/False sin propagar.

---

### INCREMENTO B2 — Repositorio (metadatos, estado, cola)

**B2.1 · Repositorio de metadatos y estado** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/repo.py` (nuevo)
- **Spec:** funciones con SQL parametrizado (**bind variables siempre**, nunca f-strings):
  - `upsert_proceso(conn, proceso, cliente_id, estado)` → `MERGE` sobre `licitaciones_metadata` por `proceso_id`.
  - `set_estado(conn, proceso_id, estado, error=None)` → `UPDATE` + `actualizado_en`.
  - `get_estado(conn, proceso_id) -> str | None`.
  - `claim_siguiente(conn, worker_id) -> str | None` → la query `FOR UPDATE SKIP LOCKED` **ya escrita** en `inventario_diagramas_amalia.md §2` (transcribirla, no reinventarla).
  - `save_analisis_db(conn, resultado)` → `MERGE` en `licitaciones_analisis` (`matriz`/`anexos` como JSON nativo 23ai).
- **Aceptación:** tests con conexión mockeada verifican el SQL emitido y los binds. Test de integración real queda en B5.
- **Contexto mínimo:** `repo.py`, `inventario_diagramas_amalia.md §2`, `src/scraper/models.py`.

**B2.2 · Chunker** — `[Agente: Haiku 4.5]`
- **Archivos:** `src/memoria/chunker.py` (nuevo)
- **Spec:** `def chunk_text(texto: str, chunk_tokens: int, overlap: int) -> list[tuple[int, str]]` → lista de `(seq, texto)`. Aproximar tokens como `chars/4`. Cortar preferentemente en límites de párrafo/oración (`\n\n`, `. `), respetando el solape. Descartar chunks vacíos o < 50 chars. Puro string, sin dependencias.
- **Aceptación:** texto de 10k chars con chunk 800/overlap 100 produce chunks solapados, contiguos y sin pérdida de contenido.

**B2.3 · Vectorización in-DB** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/vectorizer.py` (nuevo)
- **Spec:** `def vectorizar_proceso(conn, proceso_id, texto_completo, cfg) -> int` (devuelve nº de chunks):
  1. **Idempotencia:** `DELETE FROM licitaciones_chunks WHERE proceso_id = :pid` antes de insertar (re-vectorizar reemplaza).
  2. `chunk_text(...)`.
  3. `executemany` de `INSERT INTO licitaciones_chunks (chunk_id, proceso_id, seq, pagina, chunk_text, embedding) VALUES (:cid, :pid, :seq, :pag, :txt, VECTOR_EMBEDDING(<modelo> USING :txt AS data))` — el embedding se calcula **dentro de la BD**; el nombre del modelo viene de `cfg.embedding_model_name` y se interpola con validación estricta (identificador SQL, allowlist `^[A-Z0-9_]+$`) porque no admite bind.
  4. Insertar por lotes (p. ej. 50) para acotar memoria.
- **Aceptación:** con conexión mockeada, verifica el DELETE previo, el batching y que el SQL usa `VECTOR_EMBEDDING`. Validar que un nombre de modelo malicioso es rechazado.

---

### INCREMENTO B3 — RAG: intra e inter-documento

**B3.1 · Búsqueda semántica intra-documento** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/rag.py` (nuevo)
- **Spec:** `def buscar_chunks(conn, proceso_id, pregunta, cfg) -> list[str]` — transcribir la query de `inventario_diagramas_amalia.md §2` (`ORDER BY VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING(<modelo> USING :pregunta AS data), COSINE) FETCH FIRST :k ROWS ONLY`). El embedding de la pregunta también se calcula in-DB.
- **Aceptación:** SQL correcto con binds; top-k respeta `cfg.rag_top_k`.

**B3.2 · Precedentes inter-documento (el valor de Track B)** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/rag.py`
- **Spec:** 
  - `def buscar_precedentes(conn, pregunta, cfg, excluir_proceso_id=None, institucion=None, limite=5) -> list[dict]` — busca en **todo el corpus histórico** (no un solo pliego), devolviendo `{proceso_id, institucion, titulo, fecha_pub, chunk_text, distancia}` vía `JOIN` con `licitaciones_metadata`. `excluir_proceso_id` evita que el pliego actual se cite a sí mismo. Filtro opcional por institución.
  - `def tendencias(conn, dias=90) -> list[dict]` — agregación SQL simple: conteo de procesos por institución y categoría en el período, y % con triaje GO. **Sin vectores** (es SQL analítico puro).
- **Aceptación:** SQL parametrizado correcto; `excluir_proceso_id` presente en el WHERE.

---

### INCREMENTO B4 — Integración con el pipeline de Track A

**B4.1 · Enganche de vectorización** — `[Agente: Opus 4.8]` · integración
- **Archivos:** `src/drilldown/pipeline.py`, `src/memoria/service.py` (nuevo)
- **Spec:** `service.py` es la **fachada** que aísla a Track A de los detalles de ADB (si `enable_memoria` es False, todo es no-op):
  - `def persistir_memoria(cfg, proceso, resultado, texto_completo) -> None` — abre conexión, `upsert_proceso`, `save_analisis_db`, `vectorizar_proceso`. Envuelto en try/except: **un fallo de ADB nunca debe romper el drill-down** (loguea y sigue).
  - En `pipeline.analizar_pliego_proceso`, tras el análisis exitoso, llamar `persistir_memoria(...)` **solo si `cfg.enable_memoria`**. Para pliegos nativos el texto completo sale de `pdf.extract_text_first_pages(path, n=<todas>)`; para escaneados, de la transcripción de visión (si no está disponible, vectorizar solo lo que haya y loguear).
  - **Disciplina de memoria:** el texto completo del pliego es un string potencialmente grande — procesarlo por chunks y **no** retenerlo tras vectorizar (`del`).
- **Aceptación:** con `enable_memoria=False` el comportamiento de Track A es **byte-idéntico** (test lo verifica); con True y ADB mockeada, se llaman las funciones en orden.
- **Contexto mínimo:** `pipeline.py`, `src/memoria/*`, `config.py`.

**B4.2 · Precedentes en el análisis** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/service.py`, `src/scraper/models.py`
- **Spec:** dataclass `Precedente(proceso_id, institucion, titulo, fecha_pub, extracto, distancia)` y campo `precedentes: list[Precedente]` en `ResultadoAnalisis` (default `[]`, **retrocompatible**). `service.obtener_precedentes(cfg, proceso, resultado) -> list[Precedente]` usa `buscar_precedentes` con una pregunta construida del título + resumen de la matriz. Ejecutar **después** del análisis (no bloquea la ruta crítica) y solo si `enable_memoria`.
- **Aceptación:** `dataclasses.asdict(ResultadoAnalisis(...))` sigue serializando bien con y sin precedentes.

**B4.3 · Precedentes en el reporte** — `[Agente: Haiku 4.5]`
- **Archivos:** `templates/report.html`
- **Spec:** bajo la matriz de riesgos, bloque colapsable "Precedentes" (solo si `ra.precedentes`): lista de hasta 3 con institución, título, fecha y extracto corto. Email-safe (tablas, CSS inline), mismo lenguaje visual que la matriz. Si no hay precedentes, no renderizar nada.
- **Aceptación:** render de prueba con y sin precedentes; no rompe el email actual.
- **Contexto mínimo:** `templates/report.html` (sección de matriz ya existente), `models.py`.

---

### INCREMENTO B5 — Migración del estado (file-based → ADB)

**B5.1 · Adaptador de persistencia dual** — `[Agente: Sonnet 5]`
- **Archivos:** `src/memoria/service.py`, `src/drilldown/pipeline.py`
- **Spec:** el estado del pliego debe poder leerse/escribirse en ADB **sin** eliminar el JSONL (que queda como respaldo y como modo `enable_memoria=False`):
  - `service.estado_de(cfg, client_id, proceso_id) -> str | None` — si `enable_memoria`, consulta ADB; si falla o está off, cae a `history.analisis_estado(...)`.
  - `service.guardar_estado(cfg, client_id, resultado)` — escribe en **ambos** (dual-write) cuando `enable_memoria`.
  - En `pipeline`, sustituir las llamadas directas a `history.*` por las de la fachada.
- **Aceptación:** con flag off, idéntico a hoy; con flag on y ADB caída, el pipeline sigue funcionando vía JSONL (degradación elegante). Test lo cubre.

**B5.2 · Cola durable (opcional, tras validar B5.1)** — `[Agente: Opus 4.8]`
- **Archivos:** `src/drilldown/pipeline.py`, `src/memoria/repo.py`
- **Spec:** reemplazar el barrido lineal de `procesar_drilldown` por consumo de `claim_siguiente` (SKIP LOCKED) cuando `enable_memoria`. Concurrencia sigue en **1**. Permite reanudar tras crash entre etapas (clave para B6).
- **Aceptación:** matar el proceso a mitad y re-ejecutar retoma los no-terminales sin duplicar.

---

### INCREMENTO B6 — Infra: dos etapas efímeras ⚠️ experto

**B6.1 · Separación en dos entrypoints** — `[Agente: Opus 4.8]`
- **Archivos:** `src/main.py`, `src/main_analyze.py` (nuevo) o flags `--stage scrape|analyze`
- **Spec:** exponer dos modos: **scrape** (Playwright: extrae procesos, clasifica, descarga PDFs a disco/Object Storage, persiste estado, sale) y **analyze** (sin navegador: consume la cola, analiza, notifica, sale). Preferir `--stage` sobre un archivo nuevo para no duplicar bootstrap. Con `enable_memoria=False`, `--run-now` sigue haciendo todo en un proceso (Track A intacto).
- **Aceptación:** `--stage scrape` no importa `analysis`/`triage`; `--stage analyze` no importa Playwright. Verificar con `sys.modules` tras la corrida.

**B6.2 · Object Storage como handoff + lifecycle** — `[Agente: Sonnet 5]` · PR + revisión Emilio
- **Archivos:** `src/memoria/objstore.py` (nuevo), `oci/lib/05-adb.sh` o `06-objstore.sh`
- **Spec:** subir/descargar PDFs con el SDK de OCI e Instance Principal (multipart, sin buffer total). Crear el bucket con **lifecycle rule: borrar objetos > 7 días**. Fallback: si el SDK no está disponible, usar disco local (modo actual).
- **Aceptación:** subir y recuperar un PDF de prueba; la regla de lifecycle existe en el bucket.

**B6.3 · systemd: scrape → analyze** — `[Agente: Sonnet 5]` · PR + revisión Emilio
- **Archivos:** `oci/lib/render-cloud-init.sh`, `oci/tenants.yaml`
- **Spec:** generar `amalia-scrape.service` (timer 07:00) y `amalia-analyze.service` encadenado con `OnSuccess=`; ambos `docker compose run --rm ... --stage <x>`. Mantener el modo actual de una sola unidad si el tenant no opta por `two_stage: true`. Incluir la verificación post-cloud-init que ya recomienda `STATUS.md` (`systemctl is-active || start`).
- **Aceptación:** render del cloud-init produce las dos units correctas; `two_stage: false` deja el comportamiento actual.

**B6.4 · Security Lists e IAM de mínimo privilegio** — `[Agente: Sonnet 5]` · PR + revisión Emilio
- **Archivos:** `oci/lib/01-network.sh`, `oci/README.md`
- **Spec:** implementar la tabla de `inventario_diagramas_amalia.md §3`: SSH restringido a CIDR admin (parámetro en `tenants.yaml`, **no** `0.0.0.0/0`), egress 443 (COMPRASAL/Anthropic/OCI) + 1522 (ADB), resto DENY. **Cambio en infra viva** → documentar rollback y coordinar con Emilio antes de aplicar.
- **Aceptación:** deploy en tenant de prueba mantiene SSH funcional desde el CIDR admin y el agente sigue corriendo.

---

### INCREMENTO B7 — Retención y operación

**B7.1 · Job de retención** — `[Agente: Haiku 4.5]`
- **Archivos:** `db/retention.sql`, `src/memoria/service.py`
- **Spec:** según `inventario_diagramas_amalia.md §2` ("Políticas de retención"): purgar `licitaciones_chunks` de procesos cerrados hace > 12 meses (`ON DELETE CASCADE` ya definido); conservar `metadata` y `analisis` como memoria institucional. Ejecutar semanalmente desde el pipeline (o `DBMS_SCHEDULER`).
- **Aceptación:** el DELETE es idempotente y no toca procesos vigentes.

**B7.2 · Índice IVF (condicional)** — `[Agente: Haiku 4.5]`
- **Archivos:** `db/index_ivf.sql`, `db/README.md`
- **Spec:** dejar listo el `CREATE VECTOR INDEX` (ya escrito en el inventario) **sin ejecutarlo**, más una nota: crear solo cuando `SELECT COUNT(*) FROM licitaciones_chunks` supere ~50k. Documentar cómo medir la latencia antes/después.
- **Aceptación:** script válido y documentado como opt-in.

---

### INCREMENTO B8 — Tests y validación

**B8.1 · Tests unitarios** — `[Agente: Haiku 4.5]`
- **Archivos:** `tests/test_chunker.py`, `tests/test_repo_sql.py`, `tests/test_memoria_service.py`
- **Spec:** pytest, **sin red ni BD**. Chunker: solape, límites, descarte de basura. Repo: con conexión mockeada, verificar SQL y binds (incluido el rechazo del nombre de modelo no-allowlist). Service: con `enable_memoria=False` todo es no-op; con ADB que lanza excepción, degrada sin propagar.
- **Aceptación:** `pytest tests/ -q` verde; **los 33 tests de Track A siguen pasando**.

**B8.2 · Test de integración contra ADB real** — `[Agente: Sonnet 5]`
- **Archivos:** `tests/integration/test_adb.py` (marcado `@pytest.mark.integration`, **skip por default** si no hay credenciales)
- **Spec:** contra una ADB real: crear esquema, insertar un proceso, vectorizar un texto, buscar chunks y precedentes, verificar top-k y que `VECTOR_EMBEDDING` devuelve 384 dims. Limpiar al final.
- **Aceptación:** `pytest -m integration` pasa con credenciales; se salta limpio sin ellas.

**B8.3 · Validación E2E + costo** — `[Agente: Opus 4.8]` · juicio
- **Archivos:** ninguno (ejecución + informe)
- **Spec:** en la VM real con `ENABLE_DRILLDOWN=true` y `ENABLE_MEMORIA=true`, correr las **dos etapas**. Verificar: (a) pico de RAM **por etapa** < 800 MB (`docker stats`) y que Chromium no vive durante `analyze`; (b) la ADB sigue en Always Free y **$0.00** en el billing (revisar que no se salió de Free Tier); (c) precedentes aparecen en el email y son pertinentes; (d) reanudación: matar `analyze` a mitad y verificar que retoma; (e) sin PDFs residuales; (f) latencia total de la corrida. Informe con métricas y recomendación de si crear el índice IVF.
- **Aceptación:** las 6 verificaciones + informe.

---

## 4. Grafo de dependencias y reparto

```
B0 (provisioning ADB + DDL + ONNX)     ⚠️ experto
   └─▶ B1 (deps/config, conexión thin, keepalive)
         ├─▶ B2 (repo, chunker, vectorizer)
         │     └─▶ B3 (RAG intra + precedentes/tendencias)
         │           └─▶ B4 (integración pipeline, precedentes en reporte)
         │                 └─▶ B5 (estado dual → cola SKIP LOCKED)
         └─▶ B6 (dos etapas, Object Storage, systemd, red)  ⚠️ experto
B7 (retención, índice) ← B2/B3
B8 (tests) ← todo ; B8.3 al final
```

**Camino crítico:** B0 → B1 → B2 → B3 → B4 → B8.3. **B6 puede ir en paralelo** desde B1 (es infra, no depende de vectores).

**Reparto de modelo-agente:** Haiku 4.5 → B0.3, B1.3, B2.2, B4.3, B7.1, B7.2, B8.1 (7). Sonnet 5 → B0.1, B0.2, B0.4, B1.1, B1.2, B2.1, B2.3, B3.1, B3.2, B4.2, B5.1, B6.2, B6.3, B6.4, B8.2 (15). Opus 4.8 → B4.1, B5.2, B6.1, B8.3 (4). **~27% en el modelo más barato** (menos que el 53% de Track A: Track B tiene más SQL/infra con criterio, menos tarea puramente mecánica).

---

## 5. Riesgos y mitigaciones

| Riesgo | Severidad | Mitigación |
|---|---|---|
| **ADB Always Free se auto-detiene (7d) / se reclama (90d)** | Alta | `keepalive()` en cada corrida diaria, incluso con flags off (B1.3) |
| `oracledb` thin no soporta algo del wallet/VECTOR | Alta | Verificar versión en B1.1 antes de codificar; thin evita Instant Client (200 MB) que sería inviable |
| Texto completo del pliego infla la RAM al vectorizar | Media | Chunking incremental por lotes, `del` tras insertar, concurrencia 1 (B4.1) |
| Fallo de ADB rompe el drill-down ya funcional | Alta | Fachada `service.py` con try/except y degradación a JSONL (B4.1, B5.1) |
| Cambio de Security Lists corta el acceso a la VM viva | Alta | PR + revisión Emilio, rollback documentado, probar en tenant de prueba (B6.4) |
| Cambiar de modelo de embeddings obliga a re-vectorizar | Media | Dimensión fijada (384) en el DDL; documentar el procedimiento de re-vectorización |
| Precedentes irrelevantes (ruido) degradan el reporte | Media | Umbral de distancia + máximo 3 en el email; validar pertinencia en B8.3(c) |
| Costo sale de Always Free sin darnos cuenta | Alta | Verificación explícita de billing en B8.3(b); cost-watchdog existente sigue activo |

---

## 6. Criterio de "listo"

Track B se considera completo cuando:
1. `ENABLE_MEMORIA=false` → sistema **byte-idéntico** al Track A en producción.
2. `ENABLE_MEMORIA=true` → los pliegos analizados se vectorizan en ADB y el email muestra **precedentes pertinentes**.
3. Las dos etapas efímeras corren en OCI con pico de RAM < 800 MB **por etapa**.
4. Billing confirmado en **$0.00**.
5. Suite verde: 33 tests de Track A + los nuevos de Track B.

---

*Documento interno de Multicómputos El Salvador. Track B se construye sobre el MVP Track A (PR #5) sin romperlo; ambos flags son independientes.*
