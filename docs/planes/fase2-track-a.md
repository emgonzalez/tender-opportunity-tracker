# Plan de Implementación · Fase 2 — MVP Track A (drill-down de pliegos)

> **Alcance:** implementar el análisis de pliegos completos (descarga del PDF, triaje, matriz de riesgos) **sin** embeddings, vector DB, ni Oracle ADB. Persistencia file-based (como hoy). Es el camino más corto a valor; Track B (memoria inter-documento con ADB) se difiere.
> **Base:** producción actual v0.1.0 (solo metadatos del listado). Este plan **añade** un pipeline nuevo detrás de un feature flag; no reescribe lo existente.
> Ver `docs/arquitectura/vision-general.md`, `docs/arquitectura/modelo-de-datos.md`, `docs/decisiones/0001-rearquitectura-v2.md`.

---

## 0. Cómo leer este plan (dos capas de "modelo")

Hay **dos** tipos de modelo Claude en juego; no confundirlos:

| Capa | Qué es | Dónde se define |
|---|---|---|
| **Modelo runtime** | El que AMALIA usa **en producción** para triaje/análisis de pliegos | Sección 2 + `.env` |
| **Modelo-agente de implementación** | El que **escribe el código** de cada tarea de este plan | Etiqueta `[Agente: …]` en cada tarea |

### Tesis de optimización de tokens (principio rector)

**Cuanto más detallada está una tarea, más barato es el modelo que puede ejecutarla.** Por eso cada tarea de abajo trae: archivos exactos, firmas de función, criterios de aceptación y una nota de contexto mínimo. Con esa especificidad, la mayoría bajan a **Haiku 4.5** o **Sonnet 5**; solo las de integración cruzada o corrección de memoria quedan en **Opus 4.8**.

Regla de asignación (IDs exactos):

| Modelo-agente | ID | Cuándo | Ejemplos |
|---|---|---|---|
| **Haiku 4.5** | `claude-haiku-4-5` | Tarea mecánica, sin ambigüedad, una sola área | añadir deps, dataclasses, edición de template HTML, utils bien especificados, tests de fixtures |
| **Sonnet 5** | `claude-sonnet-5` | Razonamiento acotado, lógica no trivial, 1–2 archivos | DOM del scraper, PyMuPDF memory-safe, prompts + API + parsing, retries |
| **Opus 4.8** | `claude-opus-4-8` | Integración cruzada, corrección de memoria/OOM, debugging E2E | orquestación de dos etapas, validación E2E real |

> Para cada tarea, dar al agente **solo los archivos listados en "Archivos"** (no el repo entero) — es la palanca de tokens más grande. Efort sugerido: Haiku/tareas mecánicas `medium`; Sonnet `high`; Opus integración `xhigh`.

### Convenciones para el agente ejecutor (todas las tareas)
- Rama por incremento: `feature/fase2-inc-N-descripcion` desde `develop`. Commits `feat(drilldown): …`.
- No tocar código fuera de "Archivos". Si algo obliga a tocar más, parar y reportar.
- Nada de credenciales en el código; todo por `.env` (ya en `.gitignore`).
- Python 3.11, dataclasses `PascalCase`, funciones `snake_case` (convención del repo).

---

## 1. Qué entra y qué NO en el MVP Track A

**Entra:** descarga del PDF del pliego, pre-filtro barato, triaje (Haiku), matriz de riesgos (Sonnet), persistencia file-based, sección de reporte, feature flag, tests, validación E2E local.

**NO entra (Track B / futuro):** Oracle ADB, embeddings, vector search, chunking, OCI Object Storage, OCI Vault, rasterización página-a-página con PyMuPDF, cola `SKIP LOCKED`. Se difieren (ver §5).

**Simplificación clave (elimina el riesgo de OOM del MVP):** Claude acepta PDFs **nativos y escaneados** vía bloque `document` base64 (hasta 32 MB / 100 páginas en modelos de 200k, 600 en 1M). El MVP **envía el PDF directo a Claude** — no rasteriza. PyMuPDF se usa solo para pre-filtro barato (extraer texto, contar páginas, guardia de tamaño), abriendo por ruta (`fitz.open(path)`, mmap).

---

## 2. Decisiones de modelo **runtime** (lo que AMALIA usa en producción)

| Paso | Modelo | ID | Por qué | Config |
|---|---|---|---|---|
| Triaje GO/NO-GO | **Claude Haiku 4.5** | `claude-haiku-4-5` | Barato ($1/$5 por 1M), rápido; decisión binaria sobre texto de las primeras páginas | `MODEL_TRIAGE` |
| Análisis profundo | **Claude Sonnet 5** (default) | `claude-sonnet-5` | Calidad casi-Opus en extracción estructurada a costo Sonnet ($3/$15); 1M contexto → un pliego entra completo | `MODEL_ANALYSIS` |

> **Decisión — análisis: Sonnet 5 por default, con A/B contra Opus 4.8.** La matriz de riesgos es jurídico-técnica y de alto costo de error, pero el volumen es bajo (solo los `GO`), así que el salto a Opus (`claude-opus-4-8`, $5/$25) cuesta centavos al día. Precios: Haiku `$1/$5` · Sonnet 5 `$3/$15` (intro `$2/$10` hasta 2026-08-31) · Opus 4.8 `$5/$25`. Estimado por pliego `GO`: Sonnet ~$0.05–0.12, Opus ~$0.10–0.25 (≈2×). **Plan:** arrancar en Sonnet 5; durante la validación (T9.3) correr un **A/B sobre 3–5 pliegos reales** — misma entrada a Sonnet y a Opus — y comparar la matriz (¿Opus atrapa cláusulas de penalidad ambiguas, requisitos de habilitación implícitos o riesgos "entre líneas" que Sonnet pierde?). Si el diferencial de calidad lo justifica, subir **solo el paso de análisis** a Opus. El cambio es una variable de entorno (`MODEL_ANALYSIS`), no código. El costo de API de Anthropic es independiente del techo de $0.01/mes de OCI (que es de infra).

Reglas de uso de la API (todas verificadas contra el SDK vigente):
- **PDF nativo:** bloque `{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":<b64 sin saltos de línea>}}` **antes** del bloque de texto. Límite 32 MB / 100 páginas → la guardia de tamaño (§Inc 1) debe rechazar antes.
- **Triaje barato:** enviar **solo texto** extraído de las primeras ~15 páginas (no el PDF entero) → mínimo de tokens. Solo los `GO` pagan el PDF completo en Sonnet.
- **Salida estructurada:** instruir JSON explícito y `json.loads()` del primer bloque `text`. (Evita depender de `messages.parse`/`output_config` por si el SDK del repo es viejo — ver T0.1.)
- **Retries:** el SDK reintenta 429/5xx con backoff (default `max_retries=2`); subir a `max_retries=4` en el cliente. Honrar `Retry-After` es automático.
- **Streaming:** el análisis usa `max_tokens` alto → usar `client.messages.stream(...)` + `.get_final_message()` (evita timeout HTTP del SDK).
- **Guardia de cuota:** `client.messages.count_tokens(...)` antes de la llamada a Sonnet; comparar contra presupuesto diario en `data/token_budget.json`; si excede → estado `REQUIERE_REVISION_MANUAL`, no llamar.

---

## 3. Incrementos (orden de ejecución)

Cada incremento es mergeable y deja el sistema funcionando (el flag `ENABLE_DRILLDOWN` arranca en `false`).

### INCREMENTO 0 — Cimientos

**T0.1 · Dependencias** — `[Agente: Haiku 4.5]`
- **Archivos:** `requirements.txt`
- **Spec:** añadir `pymupdf==1.24.10` y `requests==2.32.3`. El repo ya tiene `anthropic==0.49.0` y `tenacity==9.0.0`. Verificar que `anthropic==0.49.0` soporta bloques `document` base64 (sí desde 0.30+); si el agente de una tarea posterior reporta que falta `document`, bump a `anthropic>=0.49` documentado, pero **no** bumpear especulativamente (rompería `classifier/engine.py`).
- **Aceptación:** `pip install -r requirements.txt` limpio; `python -c "import fitz, requests, anthropic"` OK.
- **Contexto mínimo:** solo `requirements.txt`.

**T0.2 · Config y feature flag** — `[Agente: Haiku 4.5]`
- **Archivos:** `src/config.py`, `.env.example`
- **Spec:** añadir al `Config` (siguiendo el patrón existente de lectura de env):
  `enable_drilldown` (bool, default `False`), `model_triage` (default `claude-haiku-4-5`), `model_analysis` (default `claude-sonnet-5`), `pliego_max_mb` (int, default `32`), `pliego_max_pages` (int, default `100`), `triage_pages` (int, default `15`), `daily_token_budget` (int, default `2_000_000`), `drilldown_tmp_dir` (default `/tmp/amalia_pliegos`). Reflejarlas en `.env.example` con comentarios.
- **Aceptación:** `Config().enable_drilldown is False`; todas las claves accesibles.
- **Contexto mínimo:** `src/config.py` (leer cómo se cargan las env actuales), `.env.example`.

**T0.3 · Modelos de datos** — `[Agente: Sonnet 5]`
- **Archivos:** `src/scraper/models.py`
- **Spec:** añadir dataclasses (no romper las existentes `Proceso`/`ProcesoClasificado`):
  ```python
  @dataclass
  class RiesgoItem:
      categoria: str          # "financiero" | "tecnico" | "legal" | "plazos"
      descripcion: str
      severidad: str          # "alta" | "media" | "baja"
      clausula_ref: str = ""  # cita textual del pliego

  @dataclass
  class MatrizRiesgos:
      items: list[RiesgoItem] = field(default_factory=list)
      resumen: str = ""

  @dataclass
  class ResultadoAnalisis:
      proceso_uuid: str        # numero_referencia del Proceso
      estado: str              # ver máquina de estados (inventario_diagramas)
      formato: str = ""        # "NATIVO" | "ESCANEADO"
      triaje: str = ""         # "GO" | "NO-GO"
      triaje_confianza: float = 0.0
      matriz: MatrizRiesgos | None = None
      anexos: dict = field(default_factory=dict)
      modelo_analisis: str = ""
      error: str = ""
      generado_en: str = ""    # ISO8601
  ```
- **Aceptación:** import limpio; `dataclasses.asdict()` serializable a JSON.
- **Contexto mínimo:** `src/scraper/models.py`.

**T0.4 · Esqueleto del módulo** — `[Agente: Haiku 4.5]`
- **Archivos:** crear `src/drilldown/__init__.py`, y archivos vacíos con docstring: `downloader.py`, `pdf.py`, `prefilter.py`, `triage.py`, `analysis.py`, `pipeline.py`.
- **Aceptación:** `python -c "import src.drilldown"` OK.

---

### INCREMENTO 1 — Adquisición del PDF

**T1.1 · Localizar el PDF del pliego** — `[Agente: Sonnet 5]` · ⚠️ área experta (Emilio) — PR con revisión
- **Archivos:** `src/scraper/extractor.py` (o nuevo `src/drilldown/locator.py` si el extractor no debe crecer), `src/scraper/browser.py` (solo lectura para reusar la sesión Playwright)
- **Spec:** dado un `Proceso` (tiene `url_directa`), navegar a la página de detalle con la sesión Playwright existente y extraer la URL de descarga del pliego de condiciones (PDF). Devolver `str | None`. Respetar `wait_until="domcontentloaded"` (patrón del repo). Guardar HTML en `data/debug/` si no encuentra el selector (patrón existente). No abrir diálogos.
- **Aceptación:** sobre un `Proceso` real de prueba devuelve una URL `.pdf` válida; si el layout cambió, no crashea (devuelve `None` + log).
- **Contexto mínimo:** `models.py`, `extractor.py`, primeras ~80 líneas de `browser.py` (cómo se obtiene la page/context).
- **Nota:** COMPRASAL es SPA; los selectores pueden cambiar. Documentar el selector usado.

**T1.2 · Descargador en streaming** — `[Agente: Haiku 4.5]`
- **Archivos:** `src/drilldown/downloader.py`
- **Spec:** `def download_pliego(url: str, dest_dir: str, max_mb: int) -> str | None`:
  - `requests.get(url, stream=True, timeout=30)`.
  - Rechazar si `Content-Length` > `max_mb*1024*1024` → devolver `None` + log `DESCARTADO_TAMANO`.
  - Escribir **chunk a chunk** (`iter_content(65536)`) a `dest_dir/<uuid>.pdf`; **nunca** `response.content` completo en RAM.
  - Si no hay `Content-Length`, ir escribiendo y abortar/borrar si supera el límite acumulado.
  - Crear `dest_dir` si no existe. Devolver la ruta.
- **Aceptación:** descarga un PDF de prueba a disco; rechaza uno > límite sin cargarlo en RAM.
- **Contexto mínimo:** solo `downloader.py` + esta spec.

---

### INCREMENTO 2 — Extracción y pre-filtro (PyMuPDF, memory-safe)

**T2.1 · Utilidades PDF** — `[Agente: Sonnet 5]`
- **Archivos:** `src/drilldown/pdf.py`
- **Spec:** **abrir siempre por ruta** (`fitz.open(path)`, mmap lazy — **nunca** `fitz.open(stream=bytes)`). Funciones:
  - `page_count(path) -> int`
  - `extract_text_first_pages(path, n) -> str` (concatena `page.get_text("text")` de las primeras `n`; cerrar el doc con `doc.close()` en `finally`).
  - `is_scanned(path, sample_pages=3) -> bool` (True si densidad de texto ≈ 0 en las primeras páginas → pliego escaneado).
  - `read_pdf_b64(path) -> str` (base64 **sin saltos de línea**, para el bloque `document` de Claude).
  - Manejar `fitz.FileDataError` → propagar como `PdfCorruptoError` (excepción propia).
- **Aceptación:** sobre un PDF nativo devuelve texto y `is_scanned=False`; sobre uno escaneado `is_scanned=True`; sobre uno corrupto lanza `PdfCorruptoError`.
- **Contexto mínimo:** solo `pdf.py`. Recalcar la regla mmap.

**T2.2 · Pre-filtro RegEx** — `[Agente: Haiku 4.5]`
- **Archivos:** `src/drilldown/prefilter.py`
- **Spec:** `def passes_prefilter(text: str, inclusion_signals: list[str], exclusion_signals: list[str]) -> bool`. Las señales vienen del YAML del cliente (`catalog.inclusion_signals` / `exclusion_signals` — ya existen). Match case-insensitive por substring/word-boundary; True si hay ≥1 inclusion y ninguna exclusion dominante. Si el PDF es escaneado (texto vacío), el pipeline **salta** este filtro (lo decide T6.1, no aquí).
- **Aceptación:** texto con "servidor"/"firewall" pasa; texto con solo "alimentos"/"agrícola" no.
- **Contexto mínimo:** `prefilter.py`, y el bloque `inclusion_signals`/`exclusion_signals` de `clients/multicomputos.yaml`.

---

### INCREMENTO 3 — Triaje (runtime: Haiku 4.5)

**T3.1 · Triaje GO/NO-GO** — `[Agente: Sonnet 5]`
- **Archivos:** `src/drilldown/triage.py`
- **Spec:** `def triage(text_or_pdf_path, scanned: bool, cfg: Config, contexto_cliente: str) -> tuple[str, float, str]` → `(veredicto, confianza, motivo)`.
  - Cliente `anthropic.Anthropic()` (lee `ANTHROPIC_API_KEY` del entorno).
  - `model=cfg.model_triage` (`claude-haiku-4-5`), `max_tokens=512`.
  - Ruta nativa: enviar **texto** (primeras `triage_pages`). Ruta escaneada: enviar bloque `document` base64 del PDF (Claude lo lee con visión) — pero **solo** las primeras páginas no es trivial sin rasterizar, así que para MVP enviar el PDF completo si es escaneado y pequeño; si excede `pliego_max_mb`, marcar `REQUIERE_REVISION_MANUAL`.
  - Prompt (system): *"Eres evaluador técnico de licitaciones IT en El Salvador. Determina si el alcance requiere servicios de infraestructura TI o desarrollo para el portafolio del cliente. Responde SOLO JSON: {\"viabilidad\":\"GO|NO-GO\",\"confianza\":0.0-1.0,\"motivo\":\"...\"}"*. Inyectar `contexto_cliente` (descripción + categorías del YAML).
  - Parsear con `json.loads` del primer bloque `text`; si falla el parseo, reintentar 1 vez con instrucción más estricta; si vuelve a fallar → `("NO-GO", 0.0, "parse_error")`.
  - Cliente con `max_retries=4`.
- **Aceptación:** sobre texto de un pliego de servidores devuelve `("GO", >0.6, ...)`; sobre uno de mobiliario `("NO-GO", ...)`.
- **Contexto mínimo:** `triage.py`, `config.py` (firmas), doc de PDF/vision del skill claude-api si el agente lo tiene; si no, esta spec basta.

---

### INCREMENTO 4 — Análisis profundo (runtime: Sonnet 5)

**T4.1 · Matriz de riesgos** — `[Agente: Sonnet 5]`
- **Archivos:** `src/drilldown/analysis.py`
- **Spec:** `def analizar_pliego(pdf_path: str, cfg: Config, contexto_cliente: str) -> ResultadoAnalisis`.
  - `model=cfg.model_analysis` (`claude-sonnet-5`), `max_tokens=16000`, **streaming** (`client.messages.stream` + `.get_final_message()`).
  - Enviar el PDF completo como bloque `document` base64 (nativo o escaneado — Claude maneja ambos).
  - System prompt: pedir **matriz de riesgos** (financieros, técnicos, legales, plazos) con severidad y **cita textual** de la cláusula, más un `resumen` ejecutivo; salida JSON con el shape de `MatrizRiesgos`/`RiesgoItem` (T0.3). Opcional: borradores de anexos F9/F11 si el pliego los menciona.
  - Parsear a `MatrizRiesgos`; poblar `ResultadoAnalisis(estado="ANALIZADO", modelo_analisis=..., generado_en=...)`.
  - Manejar `anthropic.APIStatusError`/timeout → `ResultadoAnalisis(estado="ERROR_LLM", error=...)`.
- **Aceptación:** sobre un pliego real devuelve `MatrizRiesgos` con ≥1 `RiesgoItem` con `clausula_ref` no vacía.
- **Contexto mínimo:** `analysis.py`, `models.py`, `config.py`.

**T4.2 · Guardia de cuota (pre-flight)** — `[Agente: Sonnet 5]`
- **Archivos:** `src/drilldown/quota.py`, (lee/escribe) `data/token_budget.json`
- **Spec:** `estimate_and_reserve(client, model, messages, cfg) -> bool`. Usar `client.messages.count_tokens(model=..., messages=...)`; leer `data/token_budget.json` (`{fecha, consumido}`); si `consumido + estimado > cfg.daily_token_budget` → devolver `False` (el pipeline marca `REQUIERE_REVISION_MANUAL`). Si OK, sumar y persistir (reset por fecha). Escritura atómica (tmp + rename).
- **Aceptación:** con presupuesto agotado devuelve `False`; reset diario funciona.
- **Contexto mínimo:** solo `quota.py` + esta spec.

---

### INCREMENTO 5 — Persistencia y estado

**T5.1 · Persistir resultado + idempotencia** — `[Agente: Haiku 4.5]`
- **Archivos:** `src/history.py`
- **Spec:** siguiendo el patrón JSONL existente (`data/history/{client_id}.jsonl`): `save_analisis(client_id, ResultadoAnalisis)` (append, escritura atómica) y `analisis_estado(client_id, proceso_uuid) -> str | None` (para saltar procesos ya en estado terminal → idempotencia/reanudación). Estados terminales: `NOTIFICADO`, `DESCARTADO_*`, `REQUIERE_REVISION_MANUAL`. Ver máquina de estados en `docs/arquitectura/modelo-de-datos.md §4`.
- **Aceptación:** guardar y releer un `ResultadoAnalisis`; un uuid en estado terminal se detecta y se salta.
- **Contexto mínimo:** `history.py` (leer patrón JSONL actual), `models.py`.

---

### INCREMENTO 6 — Orquestación (memory-safe)

**T6.1 · Pipeline por pliego** — `[Agente: Opus 4.8]` · integración + corrección de memoria
- **Archivos:** `src/drilldown/pipeline.py`
- **Spec:** `def procesar_pliego(proceso, cfg, contexto_cliente, client_id) -> ResultadoAnalisis`. Orquesta, **un pliego a la vez** (concurrencia=1), con máquina de estados file-based (T5.1):
  1. Si `analisis_estado()` es terminal → saltar (idempotencia).
  2. `locator` → URL del PDF (T1.1). Sin URL → estado `ERROR_DESCARGA`.
  3. `download_pliego` (T1.2). `None` por tamaño → `DESCARTADO_TAMANO`.
  4. `page_count`/`is_scanned` (T2.1). Corrupto → `ERROR_PARSEO`.
  5. Nativo → `extract_text_first_pages` + `passes_prefilter` (T2.2). No pasa → `DESCARTADO_REGEX`, borrar PDF. Escaneado → saltar RegEx.
  6. `quota` pre-flight (T4.2). Excede → `REQUIERE_REVISION_MANUAL`.
  7. `triage` (T3.1). `NO-GO` → `DESCARTADO_TRIAJE`. Confianza baja → `REQUIERE_REVISION_MANUAL`.
  8. `analizar_pliego` (T4.1) → `ANALIZADO`.
  9. `save_analisis` (T5.1); **borrar el PDF de `/tmp`** en `finally` (higiene de disco/PII).
  - **Disciplina de memoria:** el navegador Playwright debe estar **cerrado** antes de esta fase (lo garantiza T6.2). No mantener bytes de PDF en memoria entre pasos: pasar rutas, no `bytes`. Envolver cada llamada en try/except → estado `ERROR_*` sin tumbar el batch.
- **Aceptación:** ejecutado sobre 3 procesos de prueba (1 relevante, 1 descartado por RegEx, 1 escaneado) produce los estados correctos y no deja PDFs en `/tmp`.
- **Contexto mínimo:** todos los módulos de `src/drilldown/`, `history.py`, `models.py`, `config.py`, y `docs/arquitectura/modelo-de-datos.md §4` (estados).

**T6.2 · Integración en main** — `[Agente: Sonnet 5]`
- **Archivos:** `src/main.py`
- **Spec:** detrás de `if cfg.enable_drilldown:`. Tras la clasificación existente, para cada cliente y cada `ProcesoClasificado` **relevante** (score ≥ umbral del cliente), llamar `procesar_pliego(...)`. **Cerrar el navegador Playwright antes** de entrar al bucle de drill-down (aislamiento de memoria en la misma corrida; en OCI esto se refina a dos etapas efímeras — fuera de MVP). Acumular los `ResultadoAnalisis` para pasarlos al notifier (Inc 7). Respetar el circuit breaker existente.
- **Aceptación:** con `ENABLE_DRILLDOWN=false` el comportamiento es idéntico a hoy; con `true`, corre el drill-down solo sobre relevantes.
- **Contexto mínimo:** `src/main.py`, `pipeline.py` (firma), `config.py`.

---

### INCREMENTO 7 — Reporte

**T7.1 · Sección de matriz de riesgos en el email** — `[Agente: Haiku 4.5]`
- **Archivos:** `templates/report.html`, `src/notifier/email_sender.py`
- **Spec:** por cada oportunidad con `ResultadoAnalisis(estado="ANALIZADO")`, renderizar una sección colapsable con la matriz: tabla de `RiesgoItem` (categoría · severidad con chip de color · descripción · cita) + `resumen`. Reusar el diseño email-safe existente (tablas, CSS inline; ver `docs/historico/brief-rediseno-reporte.md`). Chips de severidad: alta rojo, media ámbar, baja verde. Pasar los resultados desde `main.py` → `send_report`.
- **Aceptación:** email de prueba renderiza la matriz en Gmail y no se rompe si `matriz is None` (oportunidad sin drill-down).
- **Contexto mínimo:** `templates/report.html`, `email_sender.py` (firma de `send_report`), `models.py`.

---

### INCREMENTO 8 — Resiliencia

**T8.1 · Retries y manejo de error** — `[Agente: Sonnet 5]`
- **Archivos:** `src/drilldown/downloader.py`, `src/drilldown/triage.py`, `src/drilldown/analysis.py`
- **Spec:** `@tenacity.retry` (ya en deps) en la descarga: 3 intentos, backoff 2/4/8 s, reintentar `Timeout`/`ConnectionError`/5xx, **no** reintentar 404. En las llamadas a Claude, confiar en `max_retries=4` del SDK (backoff + `Retry-After` automáticos). Mapear fallos agotados a estados `ERROR_*` (no excepciones que tumben el batch). Integrar con el circuit breaker existente (N fallos consecutivos → alerta a `system_admins_email`). Ver matriz de retry en `docs/arquitectura/modelo-de-datos.md §1.1`.
- **Aceptación:** una descarga que falla 2 veces y acierta a la 3ª completa; un 404 no reintenta; el circuit breaker existente sigue disparando.
- **Contexto mínimo:** los 3 archivos + `docs/arquitectura/modelo-de-datos.md §1.1`.

---

### INCREMENTO 9 — Tests + validación E2E

**T9.1 · Tests unitarios** — `[Agente: Haiku 4.5]`
- **Archivos:** `tests/test_prefilter.py`, `tests/test_pdf.py`, `tests/test_quota.py`, fixture `tests/fixtures/pliego_min.pdf` (un PDF chico nativo)
- **Spec:** pytest. `prefilter`: casos inclusion/exclusion. `pdf`: `page_count`, `extract_text_first_pages`, `is_scanned=False` sobre el fixture. `quota`: agotado→False, reset por fecha. Sin llamadas de red (nada de Claude aquí).
- **Aceptación:** `pytest tests/ -q` verde.
- **Contexto mínimo:** los módulos bajo prueba + esta spec.

**T9.2 · Test de integración con mocks** — `[Agente: Sonnet 5]`
- **Archivos:** `tests/test_pipeline.py`
- **Spec:** monkeypatch de `triage` y `analizar_pliego` (respuestas fijas) y del `locator`/`download` (ruta a fixture local). Verificar la máquina de estados en 3 rutas: relevante→`ANALIZADO`, RegEx→`DESCARTADO_REGEX`, cuota agotada→`REQUIERE_REVISION_MANUAL`. Verificar que se borra el PDF temporal.
- **Aceptación:** cubre los 3 caminos; sin red.
- **Contexto mínimo:** `pipeline.py` + módulos mockeados.

**T9.3 · Validación E2E local (dry-run real)** — `[Agente: Opus 4.8]` · juicio/debugging
- **Archivos:** ninguno (ejecución + reporte)
- **Spec:** con `.env` real y `ENABLE_DRILLDOWN=true`, correr `docker compose run --rm ... python -m src.main --run-now` sobre un día con ≥1 licitación IT real. Verificar: (a) `docker stats` — pico de RAM del contenedor **< 800 MB** (con el navegador ya cerrado durante el análisis); (b) tokens consumidos vs `token_budget.json` razonables; (c) email recibido con la matriz; (d) sin PDFs residuales en `/tmp`; (e) estados persistidos correctos. Reportar métricas; si el pico de RAM se acerca al límite, recomendar pasar a dos etapas efímeras (Track A→infra, fuera de MVP).
- **A/B análisis Sonnet vs Opus:** sobre **3–5 pliegos `GO` reales**, ejecutar `analizar_pliego` dos veces (una con `MODEL_ANALYSIS=claude-sonnet-5`, otra con `claude-opus-4-8`) sobre la misma entrada y comparar las matrices lado a lado: ¿Opus atrapa cláusulas de penalidad ambiguas, requisitos de habilitación implícitos o riesgos "entre líneas" que Sonnet pierde? Registrar costo real por pliego de cada modelo. **Recomendación de salida:** mantener Sonnet salvo que el diferencial de calidad lo justifique; si sí, cambiar solo `MODEL_ANALYSIS` a Opus (sin tocar código).
- **Aceptación:** corrida real exitosa con las 5 verificaciones; informe de RAM/tokens/costo **+ veredicto del A/B Sonnet-vs-Opus con recomendación de `MODEL_ANALYSIS`**.
- **Contexto mínimo:** todo el pipeline + `docker-compose.e2.yml` (límites de memoria).

---

## 4. Grafo de dependencias

```
Inc0 (T0.1→T0.2→T0.3→T0.4)
   ├─▶ Inc1 (T1.1, T1.2)
   ├─▶ Inc2 (T2.1, T2.2)
   ├─▶ Inc3 (T3.1)        ← depende de T0.2, T0.3, T2.1
   ├─▶ Inc4 (T4.1, T4.2)  ← depende de T0.3, T2.1
   └─▶ Inc5 (T5.1)        ← depende de T0.3
Inc6 T6.1 ← Inc1..Inc5   (integración; Opus)
     T6.2 ← T6.1
Inc7 T7.1 ← T6.2
Inc8 T8.1 ← Inc1,3,4     (puede ir en paralelo a Inc7)
Inc9 T9.1/T9.2 ← módulos respectivos ; T9.3 ← todo (Opus, último)
```

Camino crítico: **Inc0 → Inc2/Inc1 → Inc3/Inc4 → Inc6 → Inc7 → Inc9.3**. Inc5 e Inc8 pueden solaparse.

**Reparto de modelo-agente (resumen):** Haiku 4.5 → T0.1, T0.2, T0.4, T2.2, T5.1, T7.1, T9.1 (7 tareas, las más baratas). Sonnet 5 → T0.3, T1.1, T1.2, T2.1, T3.1, T4.1, T4.2, T6.2, T8.1, T9.2 (10 tareas). Opus 4.8 → T6.1, T9.3 (2 tareas, integración/validación). ~53% de las tareas caen en el modelo más barato precisamente porque están detalladas — esa es la optimización de tokens pedida.

---

## 5. Diferido a Track B / infra (fuera de este MVP)

Cuando el MVP esté validado y la memoria institucional inter-documento sea prioridad:
- **Track B:** Oracle ADB 23ai + embeddings ONNX in-DB + vector search + chunking + cola `SKIP LOCKED` (DDL y diseño ya en `docs/arquitectura/modelo-de-datos.md`).
- **Infra:** dos etapas efímeras (`amalia-scrape` → `amalia-analyze`) en OCI, OCI Object Storage como data-lake, OCI Vault + Instance Principal, Security Lists de mínimo privilegio, lifecycle de borrado. (Área experta Emilio; ver `oci/README.md` y la arquitectura v2.)
- **Rasterización PyMuPDF** página-a-página: solo si se decide abandonar el envío de PDF nativo a Claude por costo de tokens en pliegos escaneados grandes.

---

## 6. Riesgos y mitigaciones del MVP

| Riesgo | Mitigación |
|---|---|
| Selector del pliego cambia (SPA COMPRASAL) | T1.1 guarda HTML en `data/debug/`, no crashea; documentar selector |
| Pliego escaneado grande > 32 MB | Guardia de tamaño (T1.2) + `REQUIERE_REVISION_MANUAL` |
| Costo de tokens de Sonnet sobre PDFs grandes | Triaje barato (solo texto, Haiku) filtra antes; guardia de cuota (T4.2) |
| SDK `anthropic==0.49.0` no soporta algún bloque | Usar `messages.create` + `json.loads` (no `messages.parse`); bump documentado solo si un agente lo reporta |
| Pico de RAM con navegador + análisis | Cerrar Playwright antes del drill-down (T6.2); validar en T9.3; escalar a dos etapas si hace falta |

---

*Documento interno de Multicómputos El Salvador. Plan de la Fase 2 (Mejora 2 del pitch deck) en modo MVP Track A.*
