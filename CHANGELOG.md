# Changelog

Todas las versiones notables de **tender-opportunity-tracker (AMALIA)** se documentan en este archivo.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto se adhiere a [Versionado Semántico](https://semver.org/lang/es/):
`vMAYOR.MENOR.PARCHE` — **MAYOR** = cambios incompatibles, **MENOR** = features
compatibles, **PARCHE** = correcciones.

## [Sin publicar]

_Cambios aún no etiquetados. La **Fase 2** vive en la rama `feature/fase2-track-a`
(PR #5, pendiente de merge a `develop`). **Ambos feature flags nacen en `false`**:
con la configuración por defecto el comportamiento es idéntico a `v0.1.0`._

### Added

- **Fase 2 · Track A — Drill-down de pliegos** (flag `ENABLE_DRILLDOWN`):
  localiza y descarga el PDF del pliego de cada licitación relevante (streaming,
  con guardia de tamaño), lo inspecciona con PyMuPDF (`fitz.open(path)` vía mmap),
  aplica un pre-filtro RegEx contra las señales del cliente, hace **triaje GO/NO-GO
  con Claude Haiku 4.5** y, solo para los GO, un **análisis profundo con Claude
  Sonnet 5** que extrae una **matriz de riesgos** (financieros, técnicos, legales y
  de plazos) con severidad y cita textual de la cláusula.
- **Fase 2 · Track B — Memoria institucional** (flag `ENABLE_MEMORIA`):
  **Oracle Autonomous Database 23ai** con *AI Vector Search* y **embeddings
  generados dentro de la base de datos** (ONNX `all-MiniLM-L6-v2`, 384 dims) —
  sin API externa de embeddings. Incluye chunking con solape, vectorización
  idempotente, RAG intra-documento y **precedentes inter-documento**: pliegos
  históricos similares que responden *"¿ya vimos algo así antes?"*.
- **Dos etapas efímeras** (`python -m src.main --stage scrape|analyze`): separa la
  fase con navegador de la de análisis para que los picos de RAM de Chromium y del
  LLM nunca se sumen en la VM de 1 GB. Handoff durable (manifiesto JSON + PDFs).
- **Guardia de cuota de tokens** (`data/token_budget.json`) con reserva atómica y
  reset diario; si se excede, el pliego pasa a `REQUIERE_REVISION_MANUAL`.
- **Máquina de estados del pliego** idempotente y reanudable, persistida en
  `data/history/{cliente}.analisis.jsonl` y (con Track B) en ADB.
- Scripts SQL en `db/`: esquema idempotente, carga del modelo ONNX, índice
  vectorial IVF opt-in y política de retención.
- Provisioning **opt-in** en `oci/`: ADB Always Free + wallet en Vault + IAM
  mínimo (`05-adb.sh`), systemd de dos etapas (`instance.two_stage`) y red de
  mínimo privilegio (`network.ssh_cidr`, `network.restrict_egress`).
- Suite de pruebas: **64 tests unitarios** (33 de Track A + 31 de Track B) más un
  test de integración contra ADB real, saltado por defecto sin credenciales.
- Documentación de arquitectura v2 en `amalia-oci-serverless-arch/`: arquitectura,
  inventario con DDL y políticas de reintento, diagramas HLD/LLD en Mermaid, la
  auditoría (`CHANGELOG_ARQUITECTURA.md`) y los planes de ambos tracks.

### Changed

- `send_report()` acepta `resultados_analisis` (opcional): el reporte HTML suma
  una sección de **matriz de riesgos** y otra de **precedentes**, ambas email-safe
  y omitidas si no hay datos (retrocompatible).
- `ResultadoAnalisis` incorpora `precedentes`; `Proceso`/`ProcesoClasificado` no cambian.
- La lectura del estado del pliego pasa por una fachada (`src/memoria/service.py`)
  que consulta ADB con **fallback automático a JSONL**.

### Security

- Wallet mTLS de ADB traído de **OCI Vault con Instance Principal** hacia `tmpfs`
  (`/dev/shm`), nunca a disco persistente, y borrado al terminar el proceso.
- Todo el SQL usa *bind variables*; el nombre del modelo de embeddings —único valor
  que debe interpolarse por ser identificador— se valida con allowlist estricta.
- Los PDFs descargados se borran tras analizar; el bucket de Object Storage lleva
  regla de ciclo de vida a 7 días (higiene de costo y de PII).

## [0.1.0] — 2026-06-26

Primera versión etiquetada del POC, verificada end-to-end en producción.

### Added
- **Agente AMALIA**: scraper Playwright con login + 2FA/TOTP automático (`pyotp`),
  extracción de procesos de COMPRASAL y parseo DOM → dataclass `Proceso`.
- **Clasificador** Claude Sonnet en modo batch: scoring multidimensional, tags
  dinámicos y recomendaciones de marca por cliente.
- **Notifier** de email HTML (Jinja2) con diseño card-based, match score unificado,
  chips de urgencia de cierre y responsive < 480px.
- **Despliegue OCI multi-tenant Free Tier** (`oci/deploy.sh`): VCN, subnet, gateway,
  route table, security list, instancia, systemd timer @ 07:00 (TZ El Salvador),
  cap de costo y auto-stop.
- Tenant **E2.1.Micro** como fallback opt-in con modelo de contenedor efímero y
  capacidades anti-OOM (zram + earlyoom + swap).
- `network.share_from` para compartir VCN entre tenants.
- Separación de **reportes diarios** (`email_to`) y **alertas de admin**
  (`system_admins_email`).
- Documentación de autonomía para Claude Code (CLAUDE.md, GUIA, STATUS, spec).

### Changed
- Consolidación **multi-tenant** en `develop`: selección de `docker-compose.yml`
  (ARM A1.Flex) vs `docker-compose.e2.yml` (x86 E2.1.Micro) por tenant en `tenants.yaml`.
- Redesign del reporte diario (formato V1 Hero) + extracción de `fecha_limite`.

### Fixed
- **Despliegue `multicomputos`**: agregados `ephemeral_container`/`schedule_time`
  (sin ellos cloud-init no generaba el systemd timer); `budget.enabled: false` como
  workaround por permisos IAM `in tenancy` faltantes.
- Auth/sesión: `wait_until=domcontentloaded` (en vez de `networkidle`), timeouts de
  validación y login aumentados, screenshot timeout.
- Docker: permisos de escritura de `botuser` en el volumen `data/` montado.
- Scraper: tolera `fecha_publicacion=null` en respuestas de COMPRASAL.
- Varios fixes de `deploy.sh` descubiertos al ejecutar en macOS (paths CLI de budgets,
  flags no soportados, output del build, etc.).

[Sin publicar]: https://github.com/emgonzalez/tender-opportunity-tracker/compare/v0.1.0...develop
[0.1.0]: https://github.com/emgonzalez/tender-opportunity-tracker/releases/tag/v0.1.0
