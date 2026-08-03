# CLAUDE.md — tender-opportunity-tracker

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

**Current Status:** `v0.1.0` en producción y funcionando (tenant `multicomputos`). En curso: **Fase 4 — drill-down de pliegos + memoria institucional** (rama `feature/fase2-track-a`, PR #5), con ambos feature flags en `false`, por lo que **no afecta el comportamiento en producción** hasta que se valide.

---

## Project Structure

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
│   │   ├── engine.py                # Clasificación batch con Claude (scoring, tags, marcas)
│   │   ├── rules.py                 # Construcción dinámica de prompts por cliente
│   │   └── trainer.py               # Perfil de inteligencia histórica (one-shot por cliente)
│   ├── notifier/
│   │   ├── email_sender.py          # SMTP + Jinja2: reporte y alertas
│   │   └── resend.py                # CLI: reenvía el último reporte desde el historial
│   ├── scheduler/
│   │   └── cron.py                  # Scheduler interno + catch-up (NO se usa en OCI: allí manda systemd)
│   ├── drilldown/                   # [Fase 4, flag ENABLE_DRILLDOWN] análisis de pliegos
│   │   ├── locator.py · downloader.py · pdf.py · prefilter.py
│   │   └── triage.py · analysis.py · quota.py · pipeline.py
│   └── memoria/                     # [Fase 4, flag ENABLE_MEMORIA] memoria institucional (ADB)
│       ├── db.py · repo.py · chunker.py · vectorizer.py
│       └── rag.py · service.py · objstore.py
├── db/                              # [Fase 4] DDL idempotente, carga ONNX, índice IVF, retención
├── clients/
│   ├── multicomputos.yaml           # Client config: categories, brands, tag taxonomy, rules
│   └── [other-clients].yaml         # Additional client configs (active: true/false)
├── oci/
│   ├── tenants.yaml                 # OCI infrastructure config (VMs, shapes, budgets, scheduling)
│   ├── state/                       # OCI runtime state: instance IPs, credentials
│   ├── deploy.sh                    # Provisioning script: creates VMs, networking, firewall, docker-compose
│   └── delete.sh                    # Cleanup script: destroys all OCI resources
├── templates/
│   └── report.html                  # Jinja2 template for email reports
├── data/
│   ├── last_run.json                # Persistent state: timestamp, success, count
│   ├── storage_state.json           # Placeholder for future object storage state
│   └── export/                      # CSV/JSON exports
├── docker-compose.yml               # Base: define el servicio `amalia`
├── docker-compose.e2.yml            # Variante E2.1.Micro (mem_limit 800m, LOW_MEMORY_MODE)
├── docker-compose.oci.yml           # Variante OCI/ARM
├── docker-compose.trainer.yml       # Override del Trainer (monta clients/ como escribible)
├── Dockerfile                       # x86_64: Playwright + Chromium bundled
├── Dockerfile.oci                   # ARM64 (A1.Flex): Chromium del sistema
├── .env.example                     # Template: COMPRASAL, SMTP, Anthropic credentials
├── start.sh                         # Local dev/deployment helper
├── CONTRIBUTING.md                  # Dev guidelines (branching, commit messages)
├── README.md                        # Quick-start guide (18KB)
└── AMALIA_PitchDeck_Esquema.md      # Historical pitch deck (obsolete context)
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
- **Tenant profiles:** `emiliogonzalez` (ARM A1.Flex), `emiliogonzalez_e2` (AMD E2.1.Micro)
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
| `clients/multicomputos.yaml` | Main client config (18 brands, 44 tags) | Emilio (domain) |
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
