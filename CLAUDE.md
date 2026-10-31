# CLAUDE.md — tender-opportunity-tracker

## Ejecución de tareas — paralelismo

Cuando se ejecuten tareas planificadas (plan mode o lista de tareas), **lanzar la
mayor cantidad posible de agentes en paralelo** usando el Agent tool con múltiples
invocaciones en un solo mensaje. Reglas:

1. **Independencia:** solo paralelizar tareas que no tengan dependencias entre sí
   (no tocan los mismos archivos, no dependen del output de otra tarea).
2. **Aislamiento:** si dos tareas editan el mismo archivo, ejecutarlas en serie.
   Usar `isolation: "worktree"` cuando haya riesgo de conflicto en archivos cercanos.
3. **Reagrupación:** al terminar las tareas paralelas, verificar que el resultado
   conjunto es coherente antes de continuar con las tareas dependientes.

### Protocolo `[CONGELADO]`

El paralelismo de arriba solo es seguro si está escrito **quién puede escribir qué**.
Hasta acá eso se resolvía a mano, repitiendo en cada encargo un *«no toques `oci/`, hay
otro agente ahí»* — sin garantía de acordarse la próxima vez. Esto lo vuelve regla.

**1 · Quien lanza reparte territorio.** Cada agente recibe en su encargo la lista
explícita de rutas que puede **escribir**. **Todo lo que no esté en tu territorio está
`[CONGELADO]`**: se lee, no se escribe. Lo congelado no se enumera; es el complemento.

> **Territorio:** `tests/`, `CLAUDE.md`
> **`[CONGELADO]`:** todo lo demás — `oci/` lo trabaja otro agente en paralelo.

**2 · Arrancá verificando el worktree.** Un worktree aislado puede venir apuntando a un
commit viejo. **Antes de leer nada:** `git log --oneline -1 develop` y, si no coincide,
`git reset --hard develop`. Analizar un repo que no existe hace meses es un modo de
falla **observado**, no hipotético.

**3 · ¿Necesitás territorio ajeno? SOLICITUD DE INTEGRACIÓN.** No lo edites: tu cambio
se perdería en la reconciliación, o abriría un conflicto que otro tiene que resolver sin
tu contexto. Terminá tu reporte con:

```
SOLICITUD DE INTEGRACIÓN
archivo:     <ruta congelada>
motivo:      <por qué tu trabajo no cierra sin esto>
cambio:      <el texto exacto a aplicar, y dónde>
bloqueante:  sí | no
```

Un diff dentro de un reporte no cuesta nada y no se pierde; una edición en territorio
congelado cuesta un conflicto.

**4 · Reconcilia quien lanzó, nunca los agentes.** De a una rama por vez, corriendo
`ruff check . && pytest -q` después de cada merge, y recién entonces se aplican las
SOLICITUDes pendientes. Un agente no mergea la rama de otro.

**Territorios con dueño permanente** — congelados salvo encargo explícito:

| Ruta | Por qué |
|---|---|
| `oci/`, `src/scraper/browser.py` | Área experta de Emilio: exigen revisión humana |
| `CHANGELOG.md` | Se escribe **una vez**, al reconciliar. N agentes editándolo = N conflictos |
| `.gitignore`, `pyproject.toml` | Cambian a la vez el entorno de todos los demás agentes |

> **Por qué esto es prosa y no una definición de subagente:**
> [`.gitignore`](.gitignore) ignora `.claude/`, así que hoy un `.claude/agents/*.md`
> **no se puede commitear** — quedaría solo en la laptop del operador, la misma falla
> que `oci/state/`. Este archivo sí se versiona, y todo agente ya lo lee.

---

## Documentación — regla de oro

**Cada hecho tiene un único hogar autoritativo; los demás documentos enlazan, no copian.**
El índice de qué vive dónde está en [`docs/README.md`](docs/README.md). Antes de
documentar algo, verificá si el código ya lo expresa (esquema → `db/schema.sql`,
estructura → este archivo): en ese caso **enlazá, no transcribas**.

---

## Business Context

**MULTICOMPUTOS** is a technology solutions integrator based in El Salvador that monitors **COMPRASAL** (the government's public procurement portal) for IT-related tenders (licitaciones) and automatically notifies configured clients with personalized reports.

**tender-opportunity-tracker (AMALIA)** is an intelligent agent that:
1. Logs into COMPRASAL daily (7:00 AM El Salvador time)
2. Extracts procurement processes (procesos) published in the last 24 hours
3. Classifies each process semantically using Claude Haiku 4.5 (multidimensional scoring)
4. Generates dynamic tags and brand recommendations
5. Sends personalized HTML reports via email to each configured client
6. Auto-recovers from session expiration (up to 3 TOTP retries)
7. Runs in OCI Free Tier VMs (ARM A1.Flex and AMD E2.1.Micro) on a daily systemd timer

**Current Status.** El detalle vive en [`STATUS.md`](STATUS.md) y en
[`CHANGELOG.md`](CHANGELOG.md) — acá va solo lo que hace falta para **no apuntarle al
ambiente equivocado**:

- Hay dos ambientes desplegados: **`multicomputos_dev`** (compartment `AMALIA_DEV`, `dev.amalia.multicomputos.com`) y,
  desde el 2026-10-29, **`multicomputos_prod`** (compartment `AMALIA_PROD`, `amalia.multicomputos.com`), ambos con los
  feature flags encendidos. PROD nació con los correos redirigidos (`override_to`) y **sin auto-actualización**: solo
  se actualiza cuando una persona corre `oci/deploy.sh multicomputos_prod`.
- **`multicomputos_dev` y `multicomputos_prod` son los únicos tenants que existen**, en el código y en OCI. El
  2026-09-20 se eliminaron de [`oci/tenants.yaml`](oci/tenants.yaml) las otras tres
  entradas: `emiliogonzalez` y `emiliogonzalez_e2` (infraestructura destruida ese
  mismo día, incluida la ADB `AMALIAE2`) y `multicomputos` (sin infraestructura desde
  el 2026-09-08, se conservaba como plantilla). No asumas que "producción" es
  `multicomputos`: `multicomputos_prod` ya existe y su plantilla fue `multicomputos_dev`.
- La Fase 4 (Track A + Track B) ya está **mergeada a `develop`**; `feature/fase2-track-a`
  y el PR #5 están cerrados. `v0.1.0` es el único tag publicado.

---

## Project Structure

> Este árbol lo verifica [`tests/test_documentacion.py`](tests/test_documentacion.py)
> en cada push: toda ruta que aparezca acá tiene que existir, y todo módulo de
> `src/` tiene que aparecer acá. Si agregás código, agregalo también al árbol.
> Las entradas marcadas **`no versionado`** son artefactos de runtime y quedan
> fuera de esa verificación.

```
tender-opportunity-tracker/
├── src/
│   ├── main.py                      # Entry point; soporta --run-now y --stage scrape|analyze
│   ├── config.py                    # Config: env vars, SMTP, Anthropic API, feature flags
│   ├── history.py                   # Estado persistente: last run + historial JSONL por cliente
│   ├── auth/
│   │   ├── login.py                 # Login interactivo/automático en COMPRASAL
│   │   ├── session.py               # Validación de sesión (storage_state.json)
│   │   └── totp.py                  # Generación de OTP con pyotp
│   ├── scraper/
│   │   ├── browser.py               # Playwright headless: flags de Chromium, low-memory mode
│   │   ├── extractor.py             # API REST de COMPRASAL (JWT) → Proceso dataclass
│   │   ├── models.py                # Dataclasses: Proceso, ProcesoClasificado, MarcaRecomendada, …
│   │   └── export.py                # Exportación masiva CSV/JSON
│   ├── classifier/
│   │   ├── client_profile.py        # ClientProfile: catálogo, reglas, notificación
│   │   ├── alias_clientes.py        # ids de cliente que cambiaron de nombre (`multicomputos` → `multicomputos_sv`): el viejo sigue resolviendo, sin plazo
│   │   ├── engine.py                # Clasificación batch con Claude (scoring, tags, marcas)
│   │   ├── rules.py                 # Construcción dinámica de prompts por cliente
│   │   └── trainer.py               # Perfil de inteligencia histórica (one-shot por cliente)
│   ├── notifier/
│   │   ├── agrupamiento.py          # grupos Para decidir / No competibles / Baja prioridad: regla compartida por el correo y el portal
│   │   ├── email_sender.py          # SMTP + Jinja2: reporte y alertas
│   │   ├── alerts.py                # Sesión expirada y circuit breaker
│   │   ├── ops_alert.py             # Canal alterno: la alerta sale aunque el SMTP esté caído
│   │   └── resend.py                # CLI: reenvía el último reporte desde el historial
│   ├── scheduler/
│   │   └── cron.py                  # Scheduler interno + catch-up (NO se usa en OCI: allí manda systemd)
│   ├── drilldown/                   # [Fase 4, flag ENABLE_DRILLDOWN] análisis de pliegos
│   │   ├── locator.py · downloader.py · pdf.py · prefilter.py
│   │   └── triage.py · analysis.py · quota.py · pipeline.py
│   ├── llm/                         # [Fase 9] proveedor de LLM: Claude ⇄ OCI Generative AI
│   │   ├── contrato.py · modelos.py # vocabulario neutral · (proveedor, etapa) → modelo
│   │   ├── registro.py              # libro append-only: caracteres, tokens y latencia por llamada
│   │   ├── parseo.py                # lee el JSON del modelo tolerando los escapes que emite mal
│   │   └── proveedor_anthropic.py · proveedor_oci.py  # adaptadores: Claude vía API · OCI GenAI (extra `oci`)
│   ├── feedback/                    # [flag ENABLE_FEEDBACK] voto Relevante/Irrelevante de quien recibe el correo
│   │   ├── tokens.py · catalogo.py  # enlaces firmados (HMAC) por destinatario · motivos de "Irrelevante"
│   │   └── repo.py · service.py     # licitaciones_reportadas / licitaciones_feedback · fachada que no propaga fallos
│   ├── web/                         # servicio web residente (FastAPI): semilla del portal; hoy solo el router público de feedback
│   │   ├── app.py · feedback.py     # fábrica `crear_app` · rutas /f/{token}/relevante|irrelevante (GET no escribe, POST sí)
│   │   ├── rutas.py                 # [Fase U, paso B] las URLs en inglés (`/login`, `/logout`, `/portal/operations`…) son las que la app registra y emite; las españolas son alias (GET → 308, POST → mismo handler; `/f/…relevante|irrelevante` permanente): middleware que traduce ruta y query
│   │   ├── seguridad.py · plantillas.py · __main__.py  # encabezados y tope de cuerpo · Jinja2 · `python -m src.web`
│   │   ├── admin.py                 # [Fase G, flag ENABLE_PORTAL] base de la administración: router /portal/admin (índice de solo lectura, 404 uniforme sin rol admin) y `escritura_admin` (acceso → CSRF+Origin+límite → «ver como» rechazado → permiso → servicio+auditoría → 303)
│   │   ├── portal.py · graficos.py  # [flag ENABLE_PORTAL, exige sesión y resuelve el alcance por rol y país] /portal (KPIs 1-6, 15-16 y cambio de estado de oportunidades), /portal/regional (R1, R6) y /portal/operativo (KPIs 11-13), /portal/tema (selector de tema, E12) · SVG server-side (barras, embudo, sparkline, medidor, linea, barras_dias, barra_apilada, linea_doble)
│   │   └── acceso.py · sesion.py · csrf.py · limitador.py  # [flag ENABLE_PORTAL + IDENTIDAD_SECRET] login sin contraseña: /login, /login/code, /logout · middleware que resuelve la cookie a un Alcance · CSRF firmado + Origin · tope de intentos por IP
│   ├── portales/                    # [docs/planes/multicomputos-costa-rica.md] registro de portales de compras (comprasal_sv integrado, sicop_cr declarado) y `portal_de_cliente`: la única fuente del portal de cada cliente
│   ├── identidad/                   # [plan de identidad, docs/planes/portal-fase3-identidad.md] capa de datos sin HTTP; las rutas de login llegan en la entrega 3
│   │   ├── politica.py · repo.py    # roles y límites · SQL de usuarios, membresías, códigos, sesiones y auditoría
│   │   ├── codigos.py · sesiones.py # código de 8 caracteres con HMAC (IDENTIDAD_SECRET) · token opaco con sha256
│   │   ├── mfa.py                   # TOTP de admin_plataforma/admin_organizacion (P4): secreto cifrado en reposo, anti-repetición, códigos de recuperación
│   │   ├── alcance.py · service.py  # `resolver_alcance` (países, permisos por rol y restricción por marca E14) · alta/baja, emitir y canjear código, cerrar sesión · alcance simulado
│   │   ├── ver_como.py              # [E10] ‘ver como’ de admin_plataforma: opciones, validación, inicio/fin auditados del alcance simulado
│   │   └── __main__.py              # `python -m src.identidad alta|baja|purgar`
│   ├── oportunidades/               # [plan de identidad, Entrega 5] pipeline comercial manual: NUEVO → EVALUANDO → OFERTADO → GANADO|PERDIDO, DESCARTADO
│   │   ├── politica.py · repo.py    # transiciones válidas y motivo obligatorio · SQL de `oportunidad_estado`/`oportunidad_evento` con control de concurrencia
│   │   └── service.py               # `cambiar_estado`: estado + evento + auditoría en una transacción; lo ajeno es NO_ENCONTRADO
│   ├── memoria/                     # [Fase 4, flag ENABLE_MEMORIA] memoria institucional (ADB)
│   │   ├── db.py · repo.py · chunker.py · vectorizer.py
│   │   ├── relevancia.py            # [E11.1] definición única en SQL de «relevante», «apto» (match ≥ 80, sin NO-GO) y día de corrida, y la restricción por marca E14
│   │   ├── rag.py · service.py · objstore.py
│   │   ├── keepalive.py             # umbral de fallos del ping a la ADB (estado en data/)
│   │   ├── reimportar.py            # recuperación ante desastre: JSONL → ADB, sin LLM (`python -m src.memoria.reimportar`)
│   │   └── backfill_v2.py           # [plan del portal §3.3 Fase 3] licitaciones_*/proceso_participado → esquema v2, sin LLM (`python -m src.memoria.backfill_v2`)
│   ├── mercado/                     # adjudicaciones (mercado completo, terceros incluidos) y resultado propio, para entrenar a AMALIA
│   │   ├── api_publica.py           # registro público de adjudicaciones (sin sesión; `requests`, no Playwright)
│   │   ├── identidad.py             # id_proveedor de MULTICOMPUTOS en COMPRASAL (match exacto, no por texto)
│   │   ├── repo.py                  # adjudicacion_item / proceso_participado; resolver_resultados (GANADO/PERDIDO)
│   │   ├── participacion.py         # qué procesos participó el cliente (reutiliza classifier/trainer.py, autenticado)
│   │   └── service.py · __main__.py # orquestación · `python -m src.mercado`
│   ├── persistencia/                # [docs/planes/persistencia-adb.md] piezas para mover estado de la VM a la ADB sin romper la corrida si la ADB falla
│   │   ├── fuente.py · cache.py     # selector `<DATO>_FUENTE` (archivo|sombra|adb) · escritura atómica y lectura tolerante
│   │   ├── spool.py · cifrado.py    # cola de salida que se vacía en la corrida siguiente · blobs cifrados con IDENTIDAD_SECRET
│   │   └── sesion.py                # [Fase 5, SESION_FUENTE] sesión de COMPRASAL cifrada en la ADB (sesion_portal_compras, versión optimista): se materializa en /tmp del contenedor y vuelve a la ADB solo si cambió; ADB caída ⇒ login con TOTP
│   └── analytics/                   # [Fase 1, docs/planes/portal-amalia.md] KPIs del portal — capa de datos, sin HTTP
│       ├── operativo.py             # KPIs 11-13 del portal: última corrida, gasto LLM del día vs tope, latencia p50/p95 y error del LLM
│       ├── pais.py                  # KPIs 1-6 de la vista de país: oportunidades/mes, % relevancia, top instituciones, embudo, severidad
│       ├── tablero.py               # [E11] datos de la vista de país: procesos clasificados, votos, tendencia, pipeline
│       └── regional.py              # R1 de la vista regional: oportunidades por país; recibe `clientes` ya filtrado por el alcance
├── tests/                           # Suite pytest — la ejecuta .github/workflows/ci.yml
│   ├── test_documentacion.py        # Verifica que este archivo siga describiendo el repo real
│   └── integration/                 # [markers integration · oracle_local] ADB real y Oracle local
├── db/                              # [Fase 4] DDL idempotente, carga ONNX, índice IVF, retención
│   ├── schema.sql · load_onnx.sql · index_ivf.sql · retention.sql
│   ├── schema_v2.sql                # [plan del portal §3] esquema multi-tenant/multi-país — fase "Expandir", coexiste con schema.sql
│   ├── retention_v2.sql · index_ivf_v2.sql  # [plan del portal §3.7, Fase "switch"] espejos v2 — coexisten con los de arriba hasta "Contract"
│   ├── docker-compose.oracle.yml    # Oracle 23ai Free local: valida el SQL sin la ADB
│   └── README.md                    # Cómo se prepara la base
├── clients/
│   ├── multicomputos_sv.yaml        # Client config (El Salvador; convención `multicomputos_<país>`): categories, brands, tag taxonomy, rules
│   └── [other-clients].yaml         # Additional client configs (active: true/false)
├── oci/
│   ├── tenants.yaml                 # Tenants: VMs, shapes, budgets, feature flags, scheduling
│   ├── deploy.sh                    # Orquestador del provisioning: llama a lib/ en orden
│   ├── destroy.sh                   # Baja de recursos: falla ruidoso, no borra el state si algo falló
│   ├── lib/                         # 01-network · 02-compute · 03-budget · 04-app · 05-adb · 06-onnx · 07-backup · 08-web (IP reservada, DNS, web+caddy) · 09-mercado (timer semanal de src.mercado, flag enable_mercado_cron; units en lib/mercado/)
│   ├── caddy/                       # Caddyfile: TLS automático; reenvía /f/*, /healthz y el portal con su acceso (/acceso, /salir, /portal…); lo demás, 404
│   ├── scripts/      # ssh-allow.sh (el /32 del 22) · find-orphans.sh (huérfanos por etiqueta)
│   ├── state/                       # OCIDs por tenant (no versionado): respaldado al bucket del tenant
│   ├── README.md                    # Cómo se despliega
│   └── PERMISOS-IAM.md              # Qué permisos exige el despliegue
├── scripts/                         # Utilidades de línea de comandos (no son paquete importable)
│   ├── adb_bootstrap.py · calibrar_cuota.py · medir_pliegos.py
│   ├── renombrar_cliente.py         # cambia un `cliente_id` en la ADB en una transacción (dry-run por defecto; verifica conteos o hace ROLLBACK)
│   ├── copiar_adb.py                # copia tablas entre ADB en dos pasos (exportar en la VM de origen, importar en la de destino): una ADB Always Free no se clona
│   ├── semilla_esquema_v2.py        # semilla idempotente de db/schema_v2.sql: portal/organizacion/cliente/cliente_config
│   ├── capturas_portal.py           # [E13] banco visual: renderiza /portal con datos pesados, fotografía claro/oscuro × 1360/390 y mide la altura
│   ├── validate_tracka_e2e.py · validate_trackb_e2e.py · validate_trackb_email.py
│   ├── smoke_oci_genai.py           # [Fase 9] prueba de humo contra OCI Generative AI
│   ├── comparar_llm.py              # [Fase 9] lee el libro de consumo: OCI vs Claude por etapa
│   └── test_ssh_allow.sh · test_pull_deploy.sh · test_destroy.sh · test_deploy_timers.sh · test_adb_acl.sh · test_deploy_resiliencia.sh
├── .github/
│   ├── workflows/ci.yml             # ruff · pytest (3.11/3.12) · bancos de bash · SQL contra Oracle 23ai · imagen a GHCR
│   └── workflows/adb-keepalive.yml  # Vigía de la ADB, fuera de la VM (ver ADR 0003)
├── static/
│   ├── charts/                      # JS vanilla de hover del portal: tooltip.js · bar.js · funnel.js · linea.js (el Dockerfile los copia)
│   └── portal/                      # tablero.js: arrastrar tarjetas del tablero —rellena el formulario «Mover a…», sin fetch— y abrir `<details>` por ancla
├── templates/
│   ├── report.html                  # Jinja2 template for email reports
│   ├── feedback_*.html              # páginas del servicio web de feedback: base, confirmar, irrelevante, gracias, error
│   ├── acceso_*.html                # login del portal: correo, código, TOTP y códigos de recuperación
│   ├── portal_*.html                # portal: base (tokens claro/oscuro), nav, pais, regional y operativo
│   └── portal_pais_*.html           # [E11] parciales de la vista de país: kpis, cierres, tablero, clasificación, votos, análisis · portal_pais_dialogo.html [E13] diálogo de cambio de estado
├── data/                            # Artefactos de runtime (no versionado): last_run.json, history/, export/
├── pyproject.toml                   # Único hogar de dependencias, ruff y pytest
├── docker-compose.yml               # Base: define el servicio `amalia`
├── docker-compose.e2.yml            # Variante E2.1.Micro (mem_limit 800m, LOW_MEMORY_MODE) — Fase M: se retira en M5 al migrar a A1
├── docker-compose.oci.yml           # [obsoleto, Fase M] Variante ARM que construye en la VM — la imagen de Dockerfile ya es multi-arch; se retira en M5
├── docker-compose.a1.yml            # [Fase M] Ampere A1 (4 OCPU/24 GB): imagen multi-arch por pull, sin LOW_MEMORY_MODE, shm_size 1g
├── docker-compose.trainer.yml       # Override del Trainer (monta clients/ como escribible)
├── Dockerfile                       # multi-arquitectura (amd64+arm64): Playwright + Chromium bundled — usa la misma imagen en E2, A1 y CI
├── Dockerfile.oci                   # [obsoleto, Fase M] ARM64 con Chromium del sistema — Dockerfile (multi-arch) lo reemplaza; se retira en M5
├── .env.example                     # Template: COMPRASAL, SMTP, Anthropic credentials
├── start.sh                         # Local dev/deployment helper
├── CONTRIBUTING.md                  # Dev guidelines (branching, commit messages)
├── GUIA_CLAUDE_CODE.md              # Guía para humanos que trabajan con agentes en este repo
├── README.md                        # Quick-start guide
├── STATUS.md                        # Estado actual del sistema (qué está hecho / en curso)
├── CHANGELOG.md                     # Qué cambió y cuándo (Keep a Changelog + SemVer)
├── AMALIA_PitchDeck_Esquema.md      # Pitch ejecutivo (documento de negocio)
└── docs/                            # ── Documentación por rol y ciclo de vida ──
    ├── README.md                    #    ÍNDICE: dónde vive cada hecho (empezar aquí)
    ├── arquitectura/                #    durable: cómo está diseñado
    ├── planes/                      #    efímero: qué se va a construir
    ├── decisiones/                  #    append-only: por qué (ADRs)
    └── historico/                   #    contexto que ya no gobierna
```

---

## Technology Stack

| Component | Technology | Version | Notes |
|-----------|-----------|---------|-------|
| **Runtime** | Python | 3.11+ | Async support (playwright, anthropic) |
| **Scraper** | Playwright | Latest | Headless Chromium, 2FA/TOTP support |
| **Browser Auth** | TOTP lib | `pyotp` | Generates OTP from secret, skips manual input |
| **Classifier** | Claude API | **Haiku 4.5** (`claude-haiku-4-5-20251001`, ver `classifier/engine.py`) | Batch mode (single API call per run) |
| **Email** | SMTP + Jinja2 | Gmail App Password | HTML templates, multi-recipient |
| **Config** | YAML + dataclasses | PyYAML | Type-safe client profiles, client definitions |
| **Orchestration** | Docker Compose | Latest | Local dev + OCI deployment |
| **Infrastructure** | OCI Free Tier | VM.Standard.A1.Flex / E2.1.Micro | systemd timer @ 07:00 (El Salvador TZ) |
| **Persistence** | JSON files | Local filesystem | `data/last_run.json`, `oci/state/*.json` |

---

## Key Business Rules

1. **RB-001:** Runs at **07:00 AM El Salvador time** (UTC-6), **every day** — el timer generado es `OnCalendar=*-*-* 07:00:00`; no hay filtro de días hábiles en el código. (`SCHEDULE_AFTERNOON` existe en config pero `scheduler/cron.py` **no lo usa**: hay una sola pasada diaria.)
2. **RB-002:** Uses **systemd timer** in OCI VM (ephemeral `docker run --rm`, not daemon)
3. **RB-003:** Extracts procesos **published in last 24 hours** (filters by `fecha_publicacion`)
4. **RB-004:** El descarte de procesos viejos es **por fecha de publicación** (`DAYS_LOOKBACK`, filtro en `extractor.py`). ⚠️ **No** existe deduplicación por UUID entre corridas: `history.py` guarda el historial por cliente, pero no una lista de "ya vistos".
5. **RB-005:** ⚠️ El reporte se envía **siempre** que la corrida termine bien, incluso con **cero** procesos relevantes (`send_report` no aborta con lista vacía). El email incluye el conteo de revisados para dar señal de vida.
6. **RB-006:** Recovers from session expiration: up to **3 TOTP retries** before notifying owner
7. **RB-007:** Multi-client single-extract: extracts procesos once, classifies per-client
8. **RB-008:** Tags, scores, and brand recommendations are **dynamically generated per-client** (not cached)

---

## Architecture

```
COMPRASAL Portal
       ↓ (Playwright login + 2FA/TOTP)
   Scraper (browser.py, extractor.py)
       ↓ (DOM parsing → Proceso dataclass)
   History (skip already-seen)
       ↓
   Classifier (Claude Haiku 4.5, single batch call)
       ↓ (per-client: scoring, tags, brand recs)
   Notifier (email_sender.py, Jinja2 template)
       ↓ (SMTP → recipient email inboxes)
   persistence (last_run.json updated)
       ↓
   [Done or Circuit-Breaker Alert]
```

### Data Flow

1. **Scraper** (`scraper/`):
   - Logs in with email + password + TOTP (from `pyotp`)
   - Extracts HTML from COMPRASAL procesos list
   - Parses DOM → `Proceso` dataclass (uuid, titulo, descripcion, fecha_publicacion, etc.)

2. **History** (`history.py`):
   - Loads `data/last_run.json` to check if procesos already seen
   - Tracks last run timestamp for "last 24 hours" filter

3. **Classifier** (`classifier/`):
   - Reads all active `clients/*.yaml` profiles
   - Single Claude API call: batches all procesos + all clients
   - Returns per-client: `ProcesoClasificado` with score, tags, brand_recommendation

4. **Notifier** (`notifier/`):
   - Filters per-client: only relevant (score ≥ threshold) procesos
   - Renders Jinja2 template with client-specific branding
   - Sends SMTP email to `notification.email_to` addresses

5. **Alerts** (in `email_sender.py`):
   - **Session Expired Alert:** If TOTP retries exceeded, emails the client contact
   - **Circuit Breaker Alert:** If 3+ consecutive runs fail, pauses and alerts owner

---

## Code Patterns & Conventions

### Python Naming
- **Dataclasses:** `PascalCase` (e.g., `Proceso`, `ClientProfile`, `ProcesoClasificado`)
- **Functions:** `snake_case` (e.g., `perform_login()`, `extract_procesos()`, `send_report()`)
- **Files:** `snake_case` (e.g., `browser.py`, `email_sender.py`)
- **Config keys:** `snake_case` YAML (e.g., `client_id`, `email_to`, `subject_prefix`)

### Data Classes
All major data structures use `@dataclass` decorators:
```python
@dataclass
class Proceso:
    institucion: str
    descripcion: str                  # objeto/título del proceso tal como viene de COMPRASAL
    fecha_publicacion: datetime
    url_directa: str
    numero_referencia: str = ""       # código del proceso, ej. "96-2026-P0011" (identificador)
    forma_contratacion: str = ""
    tipo_actividad: str = ""
    monto: Optional[float] = None
    fecha_limite: Optional[datetime] = None

@dataclass
class ProcesoClasificado(Proceso):
    es_relevante: bool = False
    categoria_detectada: str = ""
    confianza: float = 0.0            # backward-compat: espeja `factibilidad`
    razon: str = ""
    match_tecnico: float = 0.0        # 0.0–1.0
    match_categoria: float = 0.0      # 0.0–1.0
    factibilidad: float = 0.0         # 0.0–1.0
    tags: list[str] = field(default_factory=list)
    marcas_recomendadas: list[MarcaRecomendada] = field(default_factory=list)
```

> No existe un campo `uuid`: el identificador del proceso es **`numero_referencia`**.
> `models.py` incluye además `MarcaRecomendada` y, en Fase 4,
> `RiesgoItem` / `MatrizRiesgos` / `ResultadoAnalisis` / `Precedente`.

### Configuration Pattern
All runtime config flows through `Config` (singleton-like in `config.py`):
```python
config = Config()
config.smtp_host        # "smtp.gmail.com"
config.smtp_port        # 587
config.smtp_user        # env: SMTP_USER
config.smtp_password    # env: SMTP_PASSWORD
config.anthropic_api_key # env: ANTHROPIC_API_KEY
config.notification_from # env: NOTIFICATION_FROM
```

### Client Profiles
Each client is a YAML file with `ClientProfile` dataclass:
```yaml
client:
  id: multicomputos
  name: MULTICOMPUTOS
  active: true

catalog:
  categories:
    - id: infraestructura
      label: Infraestructura
      items: [Servidores, Almacenamiento]
  tag_taxonomy: [Servidores, Almacenamiento, Firewall, ...]

rules:
  context_note: "Market intelligence..."
  categories: infraestructura, ciberseguridad

notification:
  email_to: [contact@multicomputos.com]
  subject_prefix: "[TOT | MULTICOMPUTOS]"
  greeting: "Estimado Jaasiel"
  report_title: "Reporte de Licitaciones"
```

---

## OCI Deployment

### Infrastructure as Code
`oci/tenants.yaml` defines:
- **Tenant profile:** `multicomputos_dev` — el único. E2.1.Micro en `AMALIA_DEV`,
  perfil `MULTICOMPUTOS`, región `us-ashburn-1`
- **Always-Free resources:** 4 OCPUs + 24 GB RAM per tenant
- **Scheduling:** systemd timer at 07:00 (TZ=America/El_Salvador)
- **Docker:** ephemeral `docker run --rm` (no daemon)
- **Budget:** auto-stop if usage > $0.01/month

### Deployment Script
`oci/deploy.sh` provisions:
1. VCN + subnet + internet gateway + route table + security list
2. Compute instance (shape: A1.Flex or E2.1.Micro)
3. Boot volume (50 GB minimum, 7 GB used)
4. cloud-init: installs Docker, pulls repo, sets up systemd timer
5. Budget + alert email

### Systemd Timer
Runs at 07:00 El Salvador time:
```bash
systemctl list-timers amalia.timer
journalctl -u amalia.timer
journalctl -u amalia.service
```

### Logs
- **Docker logs:** `docker compose logs -f amalia`
- **Systemd logs:** `journalctl -u amalia.service -n 100`
- **Both in one:** SSH to VM, then run either command

---

## Running Locally

### Prerequisites
```bash
python --version              # 3.11+
docker --version              # Latest
docker compose --version      # Latest
```

### Setup
```bash
# 1. Clone and env setup
git clone <repo>
cd tender-opportunity-tracker
cp .env.example .env

# 2. Edit .env with real credentials
# COMPRASAL_EMAIL, COMPRASAL_PASSWORD, ANTHROPIC_API_KEY, SMTP_* vars

# 3. (Optional) Configure TOTP if using 2FA
# COMPRASAL_TOTP_SECRET=base32-encoded-secret (from your 2FA app)

# 4. Run (manual login mode if TOTP not set)
./start.sh
```

### Commands
```bash
docker compose up -d                    # Run in background
docker compose logs -f                  # Live logs
docker compose down                     # Stop
docker compose ps                       # Status

# Inside container (for debugging):
docker compose exec amalia bash
python -m src.main                      # Manual run
python -m src.scraper.browser           # Test login
```

---

## Troubleshooting

### Session Expiration (Most Common Issue)
**Symptom:** Email says "Sesión de COMPRASAL expirada"  
**Cause:** Browser session cookie expired (COMPRASAL sessions last ~30 min)  
**Fix:** 
- If TOTP configured: agent auto-retries 3 times
- If TOTP not configured: operator must manually re-login
- Manual re-login: `docker compose run -T --rm amalia python -m src.auth.login --manual`

### SMTP Auth Failures
**Symptom:** `smtplib.SMTPAuthenticationError`  
**Cause:** Wrong SMTP credentials or 2FA not set up for Gmail  
**Fix:**
- Generate **App Password** in Gmail settings (not your main password)
- Update `.env` with `SMTP_PASSWORD=<app-password>`

### Anthropic API Errors
**Symptom:** `RateLimitError` or `AuthenticationError`  
**Cause:** API key invalid or rate limit exceeded  
**Fix:**
- Verify `ANTHROPIC_API_KEY` in `.env`
- Check Anthropic Console for usage/limits

### Playwright Browser Timeout
**Symptom:** `TimeoutError: waiting for locator`  
**Cause:** COMPRASAL page load took too long or layout changed  
**Fix:**
- Check `wait_until` setting in `browser.py` (should be `"domcontentloaded"` not `"networkidle"`)
- Manual test: `docker compose run -T --rm amalia python -m src.scraper.browser`

---

## Key Files (Quick Reference)

| File | Purpose | Owner |
|------|---------|-------|
| `src/main.py` | Orchestrator: scrape → classify → notify | Claude |
| `src/config.py` | Config singleton | Claude |
| `src/scraper/browser.py` | Playwright: login, TOTP, DOM |  Emilio (expert) |
| `src/scraper/extractor.py` | Parse COMPRASAL HTML → Proceso dataclass | Claude |
| `src/classifier/engine.py` | Claude Haiku 4.5 batch classification | Claude |
| `src/notifier/email_sender.py` | Jinja2 template → SMTP | Claude |
| `clients/multicomputos_sv.yaml` | Main client config (18 brands, 44 tags) | Emilio (domain) |
| `oci/tenants.yaml` | OCI infrastructure config | Emilio (expert) |
| `oci/deploy.sh` | Provision OCI VMs, systemd timer | Emilio (expert) |
| `.env.example` | Credential template | Shared |
| `README.md` | User-facing quick-start | Emilio (last update) |

---

## Naming Conventions

### Branches
| Pattern | Example | Purpose |
|---------|---------|---------|
| `feature/[desc]` | `feature/e2-micro-fallback` | Feature or enhancement |
| `fix/[desc]` | `fix/session-expiration` | Bug fix |
| `docs/[desc]` | `docs/oci-deployment-guide` | Documentation |

**Main branches:** `main` (stable), `develop` (integration), current feature branch

### Commits
```
[type][scope]: brief message

type: feat, fix, chore, docs, refactor, perf, test
scope: auth, scraper, classifier, notifier, oci, config, etc.

Examples:
- feat(oci): tenant E2.1.Micro como fallback opt-in
- fix(scraper): tolera fecha_publicacion=null
- chore(clients): actualiza destinatarios de email
```

---

## Performance Notes

- **Single Claude call per run:** All procesos + all clients in one batch (cost-efficient)
- **No caching:** Tags and scores regenerated daily (freshness > performance)
- **Headless browser:** Fastest mode with DOM extraction
- **SMTP batch:** Sends all emails synchronously in a loop (not async)
- **OCI Free Tier:** 4 OCPU + 6 GB RAM (A1.Flex) or 1 OCPU + 1 GB RAM (E2.1.Micro)
  - E2.1.Micro with 8 GB swap as safety net (via zram + earlyoom)

---

## Future Enhancements (Out of Scope)

- **Phase 2:** Object storage (OCI Object Store) for procesos history
- **Phase 3:** Dashboard UI (React/Next.js) for browsing classified procesos
- **Phase 4:** Webhook notifications (Slack, Teams, custom endpoints)
- **Phase 5:** Multi-region support (multiple COMPRASAL-like portals)
