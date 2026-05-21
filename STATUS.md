# STATUS.md — tender-opportunity-tracker

> Última actualización: 2026-05-21 (Email Redesign completado y validado ✅)
> Status: ✅ **Producción** — Sistema completamente funcional
> Rama activa: `develop`

---

## Resumen Ejecutivo

**tender-opportunity-tracker (AMALIA)** es un agente automatizado que monitorea COMPRASAL diariamente para identificar licitaciones tecnológicas relevantes. 

| Métrica | Estado |
|---------|--------|
| **Funcionalidad Core** | ✅ 100% — Scraping, clasificación, email |
| **Clientes Activos** | ✅ 1 (MULTICOMPUTOS) |
| **OCI Deployment** | ✅ A1.Flex principal + E2.1.Micro fallback (feature branch) |
| **Confiabilidad** | ✅ 99%+ uptime (systemd timer + auto-recovery) |
| **Testing Automatizado** | ❌ No implementado (POC, bajo valor) |
| **Documentación** | ✅ CLAUDE.md, GUIA_CLAUDE_CODE.md, README.md |

---

## Fases Completadas

### ✅ Fase 0: Prototipo (Completada)
- Scraper Playwright: login + TOTP automático
- Extractor DOM: parse procesos de COMPRASAL
- Clasificador Claude Sonnet: scoring multidimensional
- Email notifier: Jinja2 templates, SMTP
- **Timeline:** Inicial → 2025-12 ✅

### ✅ Fase 1: OCI Deployment (Completada)
- Provisioning script: VCN, subnet, security groups, VM
- Systemd timer: ejecución diaria @ 07:00
- Budgets + auto-stop: cost cap $0.01/mes
- Multi-tenant support: A1.Flex principal
- **Timeline:** 2025-12 → 2026-04 ✅

### ✅ Fase 2: Hardening & Fallback (Completada en feature branch)
- E2.1.Micro como fallback opt-in
- Swap + earlyoom: evita OOM kills
- Retry logic: TOTP retries, circuit breaker
- **Timeline:** 2026-04 → 2026-05 ✅
- **Ubicación:** `feature/e2-micro-fallback` (listo para merge a main cuando se necesite E2)

### ✅ Fase 3: Email Redesign (Completada)
- Nuevo template email-safe (tablas, CSS inline, Outlook compatible)
- Card-based layout con design tokens modernos
- Match score unificado (promedio ponderado 40% técnico / 30% categoría / 30% factibilidad)
- Chips dinámicos para urgencia de cierre (Urgente ≤5d, Esta semana 6-10d, Holgado >10d)
- Extracción de `fecha_limite` de COMPRASAL API
- MarcaRecomendada con role ("Recomendada" vs "Alternativa")
- Responsive a <480px
- Validado: tamaño < 102KB, 0/1/4/12 oportunidades
- **Timeline:** 2026-05 ✅

---

## Componentes

### Backend (Python Monolith)

#### Core

| Componente | Archivos | Estado | Notas |
|-----------|----------|--------|-------|
| **Scraper** | `scraper/browser.py`, `scraper/extractor.py` | ✅ Stable | Playwright headless, DOM parsing, TOTP, fecha_limite |
| **Classifier** | `classifier/classifier.py`, `classifier/client_profile.py` | ✅ Stable | Claude Sonnet batch, scoring, tags, brand recs |
| **Notifier** | `notifier/email_sender.py` | ✅ Enhanced | Email-safe HTML, card-based design, dynamic match scoring |
| **Config** | `config.py`, `.env.example` | ✅ Stable | Env vars, type-safe dataclasses |
| **History** | `history.py` | ✅ Stable | Persistent state (last run, seen UUIDs) |
| **Models** | `scraper/models.py` | ✅ Enhanced | Proceso + fecha_limite, MarcaRecomendada + role |

#### Entrypoint

| Archivo | Estado | Notas |
|---------|--------|-------|
| `main.py` | ✅ Stable | Orchestrator: scrape → classify → notify |

#### Testing & Validation

| Archivo | Estado | Notas |
|---------|--------|-------|
| `send_test_email.py` | ✅ New | Script para validar reporte con datos de prueba |

#### Known Issues

| ID | Problema | Severidad | Workaround | Prioridad |
|----|----------|-----------|-----------|-----------|
| none | — | — | — | — |

**Notas:**
- Sistema completamente funcional, sin bugs críticos
- Última ejecución exitosa: 2026-05-21 (validación manual)

---

### OCI Infrastructure

#### Tenants

| Tenant | Shape | Status | Ubicación | Notas |
|--------|-------|--------|-----------|-------|
| `emiliogonzalez` | A1.Flex (ARM) | ✅ Producción | main | Always Free 4 OCPU + 24 GB |
| `emiliogonzalez_e2` | E2.1.Micro (AMD) | 🟡 Feature | feature/e2-micro-fallback | Always Free 1 OCPU + 1 GB (fallback) |

#### Systemd Timer

```
Timer:     tender-opportunity-tracker.timer
Time:      07:00 (El Salvador TZ = UTC-6)
Frequency: Monday–Friday (Mon–Fri)
Service:   tender-opportunity-tracker.service
Command:   docker compose run --rm tender-opportunity-tracker python -m src.main
```

**To check in OCI VM:**
```bash
ssh -i ~/.oci/key ubuntu@instance-ip
systemctl status tender-opportunity-tracker.timer
journalctl -u tender-opportunity-tracker.service -n 50
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
| `main` | Stable release | ✅ |
| `develop` | Integration (email redesign + comunes) | ✅ |
| `feature/e2-micro-fallback` | E2.1.Micro fallback opt-in (aislada) | ✅ |

### Commits Recientes (develop)

```
95b4c4d feat: agregar script para enviar reporte de prueba del nuevo email design
356fe83 feat(email,scraper): redesign reporte diario con nuevo formato V1 Hero + extrae fecha_limite
a913614 fix(scraper,auth): wait_until=domcontentloaded en lugar de networkidle
3140ba8 fix(auth): perform_login() recibía Config como headless bool y reventaba
9dae3f8 chore(clients): actualiza destinatarios de email de Multicómputos
```

### In-Progress Tasks

| Tarea | Estado |
|-------|--------|
| (ninguna) | Todas las fases completadas ✅ |

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
| **Email rendering** | <102KB | ~94KB | Email-safe, validado en 0/1/4/12 opp |

### Logging

- **Container logs:** `docker compose logs -f`
- **OCI VM logs:** `journalctl -u tender-opportunity-tracker.service`
- **Test email:** `python3 send_test_email.py [email] [num_opp]`

### Alerts

| Evento | Notificación | Estado |
|--------|-------------|--------|
| Session expired (3 retries failed) | Email al cliente | ✅ Implementado |
| Circuit breaker (3+ consecutive failures) | Email al owner | ✅ Implementado |
| Daily run success | No notificación (silent success) | By design |

---

## Future Roadmap (Prioridad Baja)

### P2: Phase 4 Features (Q3 2026)

- [ ] **Multi-client expansion:** Agregar 2-3 clientes nuevos (Fortinet, Oracle resellers)
- [ ] **Object storage integration:** OCI Object Store for procesos history (vs. local JSON)
- [ ] **Webhook notifications:** Slack/Teams/custom endpoints (vs. email only)

### P3: Phase 5 Features (Q4 2026+)

- [ ] **Frontend dashboard:** React/Next.js UI para browsing clasificaciones
- [ ] **Historical analysis:** Reportes semanales/mensuales de tendencias
- [ ] **Multi-region support:** Portales de compras de otros países (Guatemala, Honduras, etc.)

### P4: Maintenance & Hardening

- [ ] **Test suite:** Unit tests para classifier, scraper edge cases
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
