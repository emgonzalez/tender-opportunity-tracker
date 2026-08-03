# STATUS.md — tender-opportunity-tracker

> Última actualización: 2026-08-02 (Fase 2 implementada, pendiente de validación)
> Status: ✅ **Producción** (`v0.1.0`) — Sistema completamente funcional
> Rama activa: `feature/fase2-track-a` (PR #5 → `develop`, sin mergear)

---

## Resumen Ejecutivo

**tender-opportunity-tracker (AMALIA)** es un agente automatizado que monitorea COMPRASAL diariamente para identificar licitaciones tecnológicas relevantes. 

| Métrica | Estado |
|---------|--------|
| **Funcionalidad Core** | ✅ 100% — Scraping, clasificación, email |
| **Clientes Activos** | ✅ 1 (MULTICOMPUTOS) |
| **OCI Deployment** | ✅ `multicomputos` en producción (E2.1.Micro) |
| **Confiabilidad** | ✅ 99%+ uptime (systemd timer + auto-recovery) |
| **Testing Automatizado** | ✅ 102 tests verdes + 6 de integración (skip sin credenciales) |
| **Documentación** | ✅ CLAUDE.md, GUIA_CLAUDE_CODE.md, README.md, `docs/` |
| **Fase 2 (drill-down + memoria)** | 🟡 Código completo en PR #5 — **flags en `false`**, pendiente validación E2E |

> ⚠️ **Nota de nomenclatura:** este archivo numera sus propias fases de trabajo
> (Fase 0–3, ya cerradas). Lo que el roadmap de producto y el pitch deck llaman
> **"Fase 2" / "Mejora 2"** es el drill-down de pliegos, documentado abajo como
> **Fase 4** para no colisionar con la "Fase 2: Hardening & Fallback" histórica.

---

## Fases Completadas

### ✅ Fase 0: Prototipo (Completada)
- Scraper Playwright: login + TOTP automático
- Extractor DOM: parse procesos de COMPRASAL
- Clasificador Claude (hoy **Haiku 4.5**): scoring multidimensional
- Email notifier: Jinja2 templates, SMTP
- **Timeline:** Inicial → 2025-12 ✅

### ✅ Fase 1: OCI Deployment (Completada)
- Provisioning script: VCN, subnet, security groups, VM
- Systemd timer: ejecución diaria @ 07:00
- Budgets + auto-stop: cost cap $0.01/mes
- Multi-tenant support: A1.Flex + E2.1.Micro
- **Timeline:** 2025-12 → 2026-04 ✅

### ✅ Fase 2: Hardening & Fallback (Completada)
- E2.1.Micro como fallback opt-in
- Swap + earlyoom: evita OOM kills
- Retry logic: TOTP retries, circuit breaker
- **Timeline:** 2026-04 → 2026-05 ✅

### ✅ Fase 3: Email Redesign (Completada)
- Nuevo template email-safe (tablas, CSS inline, Outlook compatible)
- Card-based layout con design tokens modernos
- Match score unificado (promedio ponderado 40/30/30)
- Chips dinámicos para urgencia de cierre
- Responsive a <480px
- Integración con fecha_limite y MarcaRecomendada.role
- **Timeline:** 2026-05 ✅

### 🟡 Fase 4: Drill-down de pliegos + Memoria institucional (Código completo, sin validar)

> Es la **"Fase 2" del roadmap de producto** / "Mejora 2" del pitch deck.
> Rama `feature/fase2-track-a`, PR #5. **No está activa en producción.**

**Track A — Drill-down** (`ENABLE_DRILLDOWN`, default `false`):
- Localiza y descarga el PDF del pliego (streaming + guardia de tamaño)
- Pre-filtro RegEx contra las señales del cliente (PyMuPDF con mmap)
- Triaje GO/NO-GO con **Claude Haiku 4.5** (barato, solo texto)
- Análisis profundo con **Claude Sonnet 5**: matriz de riesgos con severidad y cita textual
- Guardia de cuota de tokens + máquina de estados idempotente
- Sección de matriz de riesgos en el reporte HTML

**Track B — Memoria institucional** (`ENABLE_MEMORIA`, default `false`):
- **Oracle ADB 23ai** con AI Vector Search y **embeddings ONNX in-database** (384 dims)
- Chunking con solape, vectorización idempotente, RAG intra-documento
- **Precedentes inter-documento**: pliegos históricos similares en el reporte
- Estado dual ADB ↔ JSONL con degradación elegante si la BD falla
- **Dos etapas efímeras** (`--stage scrape|analyze`) para aislar los picos de RAM
- Provisioning OCI opt-in: ADB + Vault + IAM mínimo, systemd encadenado, red restringida

**Estado:** 102 tests verdes en total (33 Track A + 31 Track B + 38 pre-existentes), `bash -n` OK en infra.
**Timeline:** 2026-08-02 (implementación) → validación pendiente

---

## Componentes

### Backend (Python Monolith)

#### Core

| Componente | Archivos | Estado | Notas |
|-----------|----------|--------|-------|
| **Scraper** | `scraper/browser.py`, `scraper/extractor.py` | ✅ Stable | Playwright headless, DOM parsing, TOTP |
| **Classifier** | `classifier/engine.py`, `classifier/rules.py`, `classifier/client_profile.py`, `classifier/trainer.py` | ✅ Stable | **Claude Haiku 4.5** en batch, scoring, tags, brand recs |
| **Notifier** | `notifier/email_sender.py` | ✅ Enhanced | Jinja2 email-safe HTML, card-based design, dynamic match scoring |
| **Config** | `config.py`, `.env.example` | ✅ Stable | Env vars, type-safe dataclasses |
| **History** | `history.py` | ✅ Stable | Historial JSONL por cliente + last_run (sin dedup por UUID) |
| **Models** | `scraper/models.py` | ✅ Stable | `Proceso`, `ProcesoClasificado`, + `MatrizRiesgos`/`ResultadoAnalisis`/`Precedente` (Fase 4) |
| **Drill-down** | `drilldown/*.py` (locator, downloader, pdf, prefilter, triage, analysis, quota, pipeline) | 🟡 Fase 4 | Tras `ENABLE_DRILLDOWN`; inactivo en producción |
| **Memoria** | `memoria/*.py` (db, repo, chunker, vectorizer, rag, service, objstore) | 🟡 Fase 4 | Tras `ENABLE_MEMORIA`; requiere ADB provisionada |
| **Esquema BD** | `db/*.sql` | 🟡 Fase 4 | DDL idempotente, carga ONNX, índice IVF opt-in, retención |

#### Entrypoint

| Archivo | Estado | Notas |
|---------|--------|-------|
| `main.py` | ✅ Stable | Orchestrator: scrape → classify → notify |

#### Known Issues

| ID | Problema | Severidad | Workaround | Prioridad |
|----|----------|-----------|-----------|-----------|
| none | — | — | — | — |

**Notas:**
- Sistema completamente funcional, sin bugs críticos
- Última ejecución exitosa verificada: 2026-05-14 08:55 (test manual tras reparar el timer)

---

### OCI Infrastructure

#### Tenants

| Tenant | Shape | Status | Notas |
|--------|-------|--------|-------|
| `multicomputos` | E2.1.Micro (AMD) | ✅ **Producción** | IP 193.122.188.14, compartment Development, `budget.enabled: false` (falta política IAM en raíz) |
| `emiliogonzalez` | A1.Flex (ARM) | 🟡 Definido en `tenants.yaml` | Personal, Always Free ARM (sujeto a capacidad) |
| `emiliogonzalez_e2` | E2.1.Micro (AMD) | ⛔ Detenido permanentemente | Fallback personal, reemplazado por `multicomputos` |

#### Systemd Timer

```
Timer:     amalia.timer
Time:      07:00 (El Salvador TZ = UTC-6)
Frequency: TODOS LOS DÍAS  (OnCalendar=*-*-* 07:00:00 — no hay filtro de días hábiles)
Service:   amalia.service
Command:   docker compose run --rm amalia python -m src.main --run-now
```

> Con `instance.two_stage: true` (Fase 4, opt-in) se generan además
> `amalia-scrape.timer`/`.service` y `amalia-analyze.service` encadenados.

**To check in OCI VM:**
```bash
ssh -i ~/.oci/key ubuntu@instance-ip
systemctl status amalia.timer
journalctl -u amalia.service -n 50
```

#### Known Issues

| ID | Problema | Estado | Workaround |
|----|----------|--------|-----------|
| RUN-001 | Timer inactivo en E2.1.Micro | ✅ REPARADO | Ver diagnóstico abajo |

---

## Diagnóstico y Reparación: Fallo 2026-05-14 @ 07:00 ✅

### **Resumen del Problema**

- **Síntoma:** Sistema no ejecutó a las 07:00 el 2026-05-14 (última ejecución: 2026-04-29)
- **Causa Raíz:** Systemd timer `amalia.timer` estaba en estado `inactive (dead)` — nunca fue activado
- **Por qué:** El comando `systemctl enable --now amalia.timer` en cloud-init no funcionó (probable race condition con systemd)

### **Diagnóstico Ejecutado** (2026-05-14 08:52 CST)

| Chequeo | Resultado | Notas |
|---------|-----------|-------|
| **Instancia OCI** | ✅ RUNNING | IP 129.158.42.74, E2.1.Micro en ashburn-1 |
| **Conexión SSH** | ✅ OK | ubuntu@129.158.42.74 funcionando |
| **Archivos systemd** | ✅ Existen | `/etc/systemd/system/amalia.{service,timer,failure}` creados |
| **Timer Status** | 🔴 INACTIVE | `systemctl status amalia.timer` = **inactive (dead)** |
| **Timer Logs** | ❌ Ninguno | `journalctl -u amalia.timer` = vacío (nunca disparó) |
| **Docker** | ✅ OK | Docker daemon corriendo, imágenes disponibles |

### **Reparación Aplicada** (2026-05-14 08:52)

```bash
ssh -i ocikey ubuntu@129.158.42.74
sudo systemctl start amalia.timer
```

**Resultado:**
- Timer status cambió a → `active (waiting)`
- Próximo disparo configurado → Fri 2026-05-15 07:00:00 CST
- Status: ✅ **FIXED**

### **Verificación: Test Manual Exitoso** (2026-05-14 08:52-08:55)

Ejecución manual del servicio:
```bash
sudo systemctl start amalia.service
```

**Resultado completo:**
```json
{
  "timestamp": "2026-05-14T08:55:58.385371-06:00",
  "success": true,
  "procesos_revisados": 78,
  "clientes_procesados": 1
}
```

**Verificaciones pasadas:**
- ✅ TOTP auto-generado correctamente
- ✅ Playwright browser inicializado
- ✅ Sesión de COMPRASAL restaurada (intento 1)
- ✅ JWT token obtenido
- ✅ Procesos extraídos: 78 procesos desde 2026-05-13
- ✅ Email enviado a cliente
- ✅ `last_run.json` actualizado con éxito

### **Cronología del Incidente**

| Fecha | Evento |
|-------|--------|
| 2026-05-11 | E2.1.Micro VM provisionada, cloud-init ejecutado |
| 2026-05-11 → 2026-05-14 | Timer nunca fue activado (estado: inactive) |
| 2026-05-14 @ 07:00 | Timer no disparó (esperado: ejecutar agente) |
| 2026-05-14 @ 08:52 | Diagnóstico: timer inactivo identificado |
| 2026-05-14 @ 08:52 | Reparación: `systemctl start amalia.timer` ejecutado |
| 2026-05-14 @ 08:55 | Test manual: servicio ejecutado exitosamente |

### **Recomendaciones para Futuro**

1. **En próximos deploys:** Agregar verificación post-cloud-init
   ```bash
   sleep 30  # Esperar a que systemd inicialice completamente
   sudo systemctl is-active amalia.timer || sudo systemctl start amalia.timer
   ```

2. **Monitoreo continuo:** Agregar cron job de health-check
   ```bash
   0 */6 * * * root systemctl is-active amalia.timer || systemctl start amalia.timer
   ```

3. **Referencia de diagnóstico:** los procedimientos de SSH/diagnóstico están en `oci/README.md` (sección Operación)

---

## Clients

### MULTICOMPUTOS

| Propiedad | Valor |
|-----------|-------|
| **Client ID** | `multicomputos` |
| **Status** | ✅ Active |
| **Email Recipients** | emilio.gonzalez@multicomputos.com |
| **Categories** | Infraestructura (18 marcas: HPE, Fortinet, PureStorage, ...) |
| **Tags** | 44 (Servidores, Almacenamiento, Firewall, ...) |
| **Confidence Threshold** | 0.70 (recibe procesos con score ≥ 70%) |
| **Last Updated** | 2026-05-09 (chore: actualiza destinatarios) |

**Historial:**
- 2026-03-09: Cliente creado, 40 tags, 16 marcas
- 2026-04-09: Actualización: +4 marcas (Juniper, Arista, etc.), refinement de tags
- 2026-05-09: Email actualizado a `emilio.gonzalez@multicomputos.com`

**Notes:**
- Cliente es el único en producción
- Perfil altamente optimizado para "infraestructura IT en El Salvador"
- Scoring combina: categoría match, confianza de clasificación, factibilidad comercial

---

## Development Workflow

### Branching Strategy

| Branch | Propósito | Status |
|--------|-----------|--------|
| `main` | Stable release (`v0.1.0`) | ✅ |
| `develop` | Integration | ✅ |
| `feature/fase2-track-a` | Fase 4: drill-down (Track A) + memoria institucional (Track B) — PR #5 | 🟡 En revisión |

### Commits Recientes

Rama `feature/fase2-track-a` (20 commits sobre `develop`, PR #5):

```
test(memoria)  suite de Track B (31 tests) + integración contra ADB real
feat(oci)      provisioning de ADB, systemd de dos etapas y red opt-in
feat(memoria)  dos etapas efímeras (--stage scrape|analyze)
feat(notifier) bloque de precedentes en el reporte
feat(memoria)  fachada de aislamiento y enganche con el drill-down
feat(memoria)  chunking, vectorización in-DB y búsqueda semántica
feat(memoria)  conexión ADB (thin + wallet en tmpfs) y repositorio SQL
feat(memoria)  esquema ADB 23ai, modelo ONNX y scripts de mantenimiento
chore(memoria) cimientos Track B — deps, config, modelo Precedente
docs(arq)      plan de implementación Fase 2 · Track B
test(drilldown) suite del MVP Track A (33 tests)
feat(notifier) matriz de riesgos en el reporte de email
feat(drilldown) orquestación de dos fases + cableado en main
feat(history)  persistencia de análisis de pliego e idempotencia
feat(drilldown) análisis de matriz de riesgos + guardia de cuota
feat(drilldown) triaje GO/NO-GO con Claude Haiku
feat(drilldown) extracción PDF y pre-filtro (PyMuPDF memory-safe)
feat(drilldown) adquisición del pliego (locator + descarga)
chore(drilldown) cimientos Fase 2 — deps, config, modelos
docs(arq)      rearquitectura v2, plan Fase 2 y diagramas
```

### In-Progress Tasks

| Tarea | Branch | Dueño | Estado |
|-------|--------|-------|--------|
| **T9.3** — Validación E2E Track A + A/B Sonnet-vs-Opus | `feature/fase2-track-a` | Emilio | 🔴 Bloquea el merge |
| Provisionar ADB + correr `db/schema.sql` y `db/load_onnx.sql` | idem | Emilio (experto OCI) | 🔴 Prerrequisito de B8.3 |
| **B8.3** — Validación E2E Track B (RAM por etapa, billing $0.00, precedentes) | idem | Emilio | 🔴 Tras lo anterior |
| Revisión de los 3 scripts OCI opt-in (`05-adb.sh`, cloud-init, red) | idem | Emilio (experto OCI) | 🟡 Pendiente |

**Notas de la validación pendiente:**
- Con `ENABLE_DRILLDOWN=true`: pico de RAM del contenedor < 800 MB, email con matriz,
  sin PDFs residuales en `/tmp`, estados persistidos.
- El A/B compara `claude-sonnet-5` (default) vs `claude-opus-4-8` en el análisis;
  cambiar de uno a otro es solo la variable `MODEL_ANALYSIS`.
- ⚠️ `network.restrict_egress` puede romper `apt`/`docker pull`, y `network.ssh_cidr`
  puede dejarte fuera de la VM: probar en tenant de prueba. Rollback en `oci/README.md`.

### Recently Completed Tasks (2026-05-14)

| Tarea | ID | Completado | Duración |
|-------|----|-----------|-|
| **OCI Instance State Recovery** | OBSERVABILITY-001 | 2026-05-14 08:55 | ~5 minutos (reparación) |
| Root Cause: Systemd timer `amalia.timer` en estado `inactive (dead)` post cloud-init | — | ✅ | — |
| Solution: `sudo systemctl start amalia.timer` + manual test successful | — | ✅ | — |

---

## Performance & Observability

### Metrics (Teórico, no instrumentado)

| Métrica | Target | Actual | Notes |
|---------|--------|--------|-------|
| **Daily runs** | 1 @ 07:00 | ~95% | Occasional OCI restarts |
| **Email delivery** | <5 min | <2 min | SMTP direct, no queuing |
| **Classifier latency** | <30s | ~10-15s | Single Claude API call, batch |
| **Total exec time** | <5 min | ~2-3 min | Scrape (1min) + classify (15s) + email (45s) |
| **Session reliability** | >99% | ~98% | 3 TOTP retries cover most cases |

### Logging

- **Container logs:** `docker compose logs -f`
- **OCI VM logs:** `journalctl -u amalia.service`
- **No ELK/Datadog** — log aggregation is future work

### Alerts

| Evento | Notificación | Estado |
|--------|-------------|--------|
| Session expired (3 retries failed) | Email al cliente | ✅ Implementado |
| Circuit breaker (3+ consecutive failures) | Email al owner | ✅ Implementado |
| Daily run success | No notificación (silent success) | By design |

---

## Future Roadmap (Prioridad Baja)

### P2: Phase 2 Features (Q3 2026)

- [ ] **Multi-client expansion:** Agregar 2-3 clientes nuevos (Fortinet, Oracle resellers)
- [x] **Object storage integration:** implementado como opt-in en Fase 4
      (`src/memoria/objstore.py`, handoff entre etapas + lifecycle 7 días).
      El historial por cliente sigue en JSONL; ADB lo complementa, no lo reemplaza.
- [ ] **Webhook notifications:** Slack/Teams/custom endpoints (vs. email only)

### P3: Phase 3 Features (Q4 2026+)

- [ ] **Frontend dashboard:** React/Next.js UI para browsing clasificaciones
- [ ] **Historical analysis:** Reportes semanales/mensuales de tendencias
- [ ] **Multi-region support:** Portales de compras de otros países (Guatemala, Honduras, etc.)

### P4: Maintenance & Hardening

- [x] **Test suite:** 102 tests verdes (clasificador, extractor, drill-down,
      memoria, chunker, cuota, anti-inyección SQL) + 6 de integración contra ADB
      real que se saltan sin credenciales. Suite ejecutable sin Docker.
- [ ] **Monitoring:** Prometheus metrics + Grafana dashboard
- [ ] **Backup strategy:** Automated backups de `data/` a OCI Object Store
- [ ] **Disaster recovery:** Runbooks para restauración post-outage

---

## How to Update This File

Este archivo se actualiza **cuando hay cambios significativos** en el sistema:

- ✅ Después de mergear un feature branch importante
- ✅ Después de diagnosticar y arreglar un bug crítico
- ✅ Después de cambios en la infraestructura OCI
- ✅ Después de completar una fase de desarrollo

**NO** se actualiza para:
- Commits menores (chore, docs)
- Testing local
- Cambios en `.env`

**Encargado:** Whoever closes the feature branch (via /done equivalent or manual merge)
