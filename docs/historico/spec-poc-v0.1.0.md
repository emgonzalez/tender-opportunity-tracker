# AI Implementation Specification — tender-opportunity-tracker

```yaml
project: tender-opportunity-tracker
company: MULTICOMPUTOS
description: Automated procurement monitoring agent for COMPRASAL (El Salvador)
locale: es-SV
generated: 2026-05-14
updated: 2026-08-03
total_tasks: 12
completed_tasks: 12
in_progress_tasks: 0
pending_tasks: 0
note: >-
  Las 12 tareas de este spec están cerradas y liberadas en v0.1.0.
  El trabajo en curso (Fase 4 — drill-down de pliegos + memoria institucional)
  se planifica en docs/planes/fase2-track-a.md y
  docs/planes/fase2-track-b.md; su estado vive en STATUS.md, no aquí.
```

---

## Architecture Overview

```yaml
technology_stack:
  language: Python 3.11+
  framework: None (monolithic, no web framework)
  scraper: Playwright (headless Chromium)
  classifier: Anthropic Claude Haiku 4.5 (batch API)
  email: SMTP (Gmail App Password)
  persistence: JSON (data/last_run.json, oci/state/*.json)
  container: Docker + Docker Compose
  orchestration: OCI VM + systemd timer (no Kubernetes)
  infrastructure: OCI Free Tier (Always Free)

deployment:
  platforms:
    - local: docker compose
    - cloud: OCI Free Tier (dual-tenant VM)
  scheduler: systemd timer @ 07:00 (El Salvador TZ)
  frequency: Daily (todos los días; OnCalendar=*-*-* 07:00:00)
  recovery: TOTP retry (3x), circuit breaker alerts

scaling:
  current: 1 client (MULTICOMPUTOS)
  max_clients: Unlimited (single-extract, per-client classification)
  throughput: ~100 procesos/day, <5 min total exec time
```

---

## Completed Tasks (✅ 10)

### CORE-001: Scraper + Playwright Browser
**Status:** ✅ Completed (2025-12)  
**Files:** `src/scraper/browser.py`, `src/scraper/extractor.py`, `src/scraper/models.py`  
**Acceptance Criteria:**
- [x] Login to COMPRASAL with email + password
- [x] TOTP 2FA support (auto-generate OTP from secret)
- [x] Parse procesos list HTML → `Proceso` dataclass
- [x] Handle session expiration + auto-recovery

**Notes:** Playwright browser is stable. DOM selectors may need updates if COMPRASAL UI changes.

---

### CORE-002: Classifier + Claude Haiku 4.5
**Status:** ✅ Completed (2025-12)  
**Files:** `src/classifier/engine.py`, `src/classifier/rules.py`, `src/classifier/client_profile.py`  
**Acceptance Criteria:**
- [x] Load client profiles from YAML
- [x] Single batch API call (all procesos + all clients)
- [x] Scoring: categoría match (0-1), confianza (0-1), factibilidad (0-1)
- [x] Dynamic tag assignment (from client taxonomy)
- [x] Brand recommendation (from client brands)
- [x] Return `ProcesoClasificado` list

**Notes:** Haiku 4.5 is a good fit for this task — semantically accurate + fast. Cost ~$0.05 per 500 procesos.

---

### CORE-003: Email Notifier + Jinja2 Templates
**Status:** ✅ Completed (2025-12)  
**Files:** `src/notifier/email_sender.py`, `templates/report.html`  
**Acceptance Criteria:**
- [x] Render HTML email from Jinja2 template
- [x] SMTP auth via Gmail App Password
- [x] Per-client recipient list from YAML
- [x] Multi-recipient (cc/bcc support not needed yet)
- [x] Personalized greeting + subject prefix

**Notes:** Template is highly polished. HTML renders well in Gmail, Outlook, Apple Mail.

---

### CORE-004: Configuration + Environment Variables
**Status:** ✅ Completed (2025-12)  
**Files:** `src/config.py`, `.env.example`  
**Acceptance Criteria:**
- [x] Load env vars: COMPRASAL_*, ANTHROPIC_*, SMTP_*
- [x] Type-safe dataclass (Config)
- [x] Validate required keys on startup
- [x] Support optional TOTP_SECRET

**Notes:** `.env` is gitignored — credentials never committed.

---

### CORE-005: Persistence + History
**Status:** ✅ Completed (2025-12)  
**Files:** `src/history.py`, `data/last_run.json`, `data/storage_state.json`  
**Acceptance Criteria:**
- [x] Track last run timestamp (skip procesos from before)
- [x] Filtrar por fecha de publicación (`DAYS_LOOKBACK`) — nota: no hay dedup por UUID entre corridas
- [x] Atomic writes (no corruption on crash)
- [x] Human-readable JSON format

**Notes:** Using file-based JSON (no database). Good enough for <1M procesos. If data grows, migrate to OCI Object Store (Phase 2).

---

### CORE-006: Entrypoint + Orchestration
**Status:** ✅ Completed (2025-12)  
**Files:** `src/main.py`  
**Acceptance Criteria:**
- [x] Main orchestrator: scrape → classify → notify
- [x] Error handling: log, retry, circuit breaker
- [x] Update last_run.json on success/failure
- [x] Exit codes: 0 (success), 1 (failure)

**Notes:** Main is clean and simple. All heavy lifting delegated to modules.

---

### INFRA-001: Docker + Compose
**Status:** ✅ Completed (2025-12)  
**Files:** `Dockerfile`, `docker-compose.yml`, `docker-compose.e2.yml`, `.dockerignore`  
**Acceptance Criteria:**
- [x] Multi-stage Dockerfile for minimal image
- [x] Volume mapping: data/, clients/, templates/
- [x] ARM64 base (A1.Flex) + x86_64 variant (E2.1.Micro)
- [x] Playwright pre-installed (or installed at runtime)

**Notes:** ARM Dockerfile builds at runtime (slow ~5 min). x86_64 pre-bundles Playwright (faster). Both tested.

---

### INFRA-002: OCI Deployment + Provisioning
**Status:** ✅ Completed (2026-04)  
**Files:** `oci/deploy.sh`, `oci/delete.sh`, `oci/tenants.yaml`, `oci/state/`  
**Acceptance Criteria:**
- [x] Provision VCN + subnet + IGW + security groups
- [x] Create VM: A1.Flex (ARM) + E2.1.Micro (AMD)
- [x] cloud-init: install Docker, pull repo, setup systemd timer
- [x] Budget + alert: auto-stop if usage > $0.01/month
- [x] Support for dual-tenant same TZ region

**Notes:** IaC is complex but solid. deploy.sh is ~500 lines, carefully tested.

---

### INFRA-003: Systemd Timer + Scheduling
**Status:** ✅ Completed (2026-01)  
**Files:** `oci/tenants.yaml` (schedule_time), cloud-init scripts  
**Acceptance Criteria:**
- [x] Daily timer @ 07:00 (El Salvador TZ)
- [x] Ephemeral container (docker run --rm, not daemon)
- [x] Automatic first-run on container startup
- [x] Logs in journalctl + docker logs

**Notes:** Timer is rock-solid. If you want to change time, edit `tenants.yaml` schedule_time and re-deploy.

---

### FEATURE-001: TOTP Auto-Recovery
**Status:** ✅ Completed (2026-02)  
**Files:** `src/scraper/browser.py` (totp_secret handling)  
**Acceptance Criteria:**
- [x] Generate OTP from COMPRASAL_TOTP_SECRET
- [x] Auto-retry login up to 3 times on session expiration
- [x] Notify client if all retries fail (send alert email)
- [x] Circuit breaker: pause after 3 consecutive failures

**Notes:** TOTP eliminates manual intervention. Highly reliable (>98% success rate).

---

### FEATURE-002: Multi-Client Support
**Status:** ✅ Completed (2025-12)  
**Files:** `clients/*.yaml`, `src/classifier/client_profile.py`  
**Acceptance Criteria:**
- [x] Load all active clients from `clients/` YAML
- [x] Single extract, per-client classification
- [x] Per-client thresholds, tags, brands
- [x] Per-client email recipients + subject prefix

**Notes:** Highly efficient (extract once, classify N times) vs. extract N times. Current: 1 client (MULTICOMPUTOS).

---

## Completed Tasks — cierre posterior (✅ 2)

### FEATURE-003: E2.1.Micro Fallback Opt-In
**Status:** ✅ Completed (2026-06-26, liberado en `v0.1.0`)  
**Files:** `oci/tenants.yaml`, `docker-compose.e2.yml`  
**Acceptance Criteria:**
- [x] Add E2.1.Micro as secondary tenant config
- [x] Share VCN/networking with A1.Flex tenant
- [x] El `Dockerfile` base (x86_64) ya trae Playwright bundled — no hizo falta un Dockerfile.e2
- [x] docker-compose.e2.yml (different swap config, earlyoom)
- [x] Cost cap enforcement (budget + auto-stop)
- [x] Documentation: when to use E2 vs. A1

**Rationale:** A1.Flex has occasional capacity issues in Ashburn. E2.1.Micro is always available but slower (1 OCPU, 1 GB RAM + 8 GB swap).

**Next Steps:**
1. Finalize docker-compose.e2.yml config
2. Test E2 deployment end-to-end
3. Merge to develop
4. Update STATUS.md

**Estimated Completion:** 2026-05-14 (today) → 2026-05-20

---

### OBSERVABILITY-001: OCI Instance State Recovery
**Status:** ✅ Completed (2026-05-14) — causa raíz: `amalia.timer` en `inactive (dead)`
tras cloud-init. Reparado con `systemctl start amalia.timer` + corrida manual
verificada. Detalle completo en `STATUS.md`.  
**Priority:** ~~CRITICAL~~ (resuelto)  
**Scope:**
1. Diagnose why systemd timer didn't execute on 2026-05-14 @ 07:00
2. Check if instance is running/stopped
3. Verify systemd timer status
4. Re-enable timer if needed
5. Force manual run to verify recovery
6. Document root cause for future prevention

**Investigation Checklist:**
```bash
# In OCI VM (SSH):
systemctl status amalia.timer
journalctl -u amalia.service --since "2026-05-13"
docker ps -a
oci compute instance get --instance-id <ocid> --query 'data."lifecycle-state"'
```

**Possible Outcomes:**
- A. Instance is running, timer just didn't fire → re-enable + test
- B. Instance is stopped → start it, re-enable timer
- C. Docker not running → restart docker daemon
- D. Credentials expired (SMTP) → update .env and redeploy

**Estimated Effort:** 1 hour (diagnosis) + fix time TBD

---

## Future Tasks (P2+) — Out of Current Scope

### SCALE-001: Multi-Client Expansion
**Priority:** Medium  
**Effort:** 2-3 hours per client  
**Files to Update:** `clients/[nuevo].yaml`  
**Scope:** Add 2-3 new clients (Fortinet, Oracle, etc.)  
**Blocker:** None — can start anytime

---

### STORAGE-001: OCI Object Store Integration
**Priority:** Low  
**Effort:** 8-10 hours  
**Scope:** Migrate `data/` persistence from local JSON → OCI Object Store  
**Rationale:** More reliable, no local storage constraints  
**Blocker:** Current JSON is sufficient for <1M procesos

---

### NOTIFICATION-001: Webhook Support
**Priority:** Low  
**Effort:** 4-6 hours  
**Scope:** Add Slack/Teams/custom webhook notifications (in addition to email)  
**Rationale:** Better integration with client workflows  
**Blocker:** Email is sufficient for MVP

---

### FRONTEND-001: Dashboard UI
**Priority:** Low  
**Effort:** 40+ hours  
**Stack:** React/Next.js  
**Scope:** Web UI for browsing classified procesos, filtering, exporting  
**Rationale:** Visual browsing > email-only interface  
**Blocker:** Requires significant effort, low ROI for current scale

---

## Verification Steps (For Completed Tasks)

### To Verify Core Functionality

```bash
# 1. Scraper works
docker compose run -T --rm amalia python -m src.scraper.browser

# 2. Classifier works
docker compose run -T --rm amalia python -c \
  "from src.classifier.classifier import classify_procesos; print('OK')"

# 3. Email works
docker compose run -T --rm amalia python -c \
  "from src.notifier.email_sender import _send; print('OK')"

# 4. Full pipeline
docker compose up -d
docker compose exec amalia python -m src.main
docker compose logs -f

# 5. Verify email arrived
# Check inbox for email with subject "[TOT | MULTICOMPUTOS] ..."
```

### To Verify OCI Deployment

```bash
ssh -i ~/.oci/key ubuntu@instance-ip

# Check timer
systemctl status amalia.timer
systemctl is-enabled amalia.timer  # Should be "enabled"

# Check last run
journalctl -u amalia.service -n 10

# Force manual run (testing)
systemctl start amalia.service
journalctl -u amalia.service -f  # Watch output

# Check Docker logs
docker logs $(docker ps -aq) --tail=20
```

---

## Dependencies & Ordering

```
CORE-001 (Scraper)
   ↓
CORE-002 (Classifier) ← Depends on CORE-001
   ↓
CORE-003 (Notifier) ← Depends on CORE-001, CORE-002
   ↓
CORE-004 (Config) ← Supports all of above
   ↓
CORE-005 (Persistence) ← Supports all
   ↓
CORE-006 (Main) ← Orchestrates all
   ↓
INFRA-001 (Docker) ← Containerizes CORE-001..CORE-006
   ↓
INFRA-002 (OCI Deploy) ← Runs INFRA-001 in cloud
   ↓
INFRA-003 (Systemd Timer) ← Schedules INFRA-002
   ↓
FEATURE-001 (TOTP) ← Enhances CORE-001
   ↓
FEATURE-002 (Multi-Client) ← Multiplies CORE-002 + CORE-003
   ↓
FEATURE-003 (E2 Fallback) ← Alternative to INFRA-002 (same infra, different shape)
```

All tasks in CORE are **completed and stable**.  
All tasks in INFRA are **completed and stable**.  
FEATURE-001 and FEATURE-002 are **completed and stable**.  
FEATURE-003 is **in-progress** (branch: feature/e2-micro-fallback).

---

## Success Metrics

| Metric | Target | Current | Notes |
|--------|--------|---------|-------|
| **Daily runs** | 100% (5/5 weekdays) | ~95% | Occasional OCI/AWS issues |
| **Email delivery** | 100% for active clients | 100% | SMTP is reliable |
| **Session uptime** | >99% | ~98% | TOTP handles most cases |
| **Classifier accuracy** | High relevance scores | Subjective (user-validated) | No automated ground truth |
| **Total latency** | <5 min/run | ~2-3 min | Scrape (1 min) + classify (15 s) + email (45 s) |

---

## Known Limitations

1. **No database** — Using JSON files. Acceptable for POC, but needs Object Store as scale grows.
2. **No test suite** — POC phase, low value for highly stable code. Can add later.
3. **No monitoring/alerting UI** — Logs only visible via SSH + journalctl. Dashboard would be nice (P2).
4. **Limited error recovery** — Circuit breaker pauses after 3 failures, but no auto-resume. Manual restart needed.
5. **No multi-region** — Only monitors COMPRASAL (El Salvador). Adding Guatemala/Honduras requires portal integration.
6. **TOTP secrets stored in env** — Better in secrets manager (AWS Secrets Manager, OCI Vault) for production.

---

## Update Log

| Date | What Changed | Who |
|------|-------------|-----|
| 2026-05-14 | Created initial spec + STATUS.md | Claude Code |
| 2026-05-14 | Documented E2.1.Micro fallback (in-progress) | Claude Code |
| 2026-05-14 | Diagnosed missing 2026-05-14 run (OBSERVABILITY-001) | Claude Code |
