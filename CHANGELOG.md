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
- Documentación de arquitectura v2 en `docs/`: arquitectura,
  inventario con DDL y políticas de reintento, diagramas HLD/LLD en Mermaid, la
  auditoría (`docs/decisiones/0001-rearquitectura-v2.md`) y los planes de ambos tracks.
- **Fase 5 — Respaldo verificado del historial** (opt-in por tenant,
  `backup.enabled` en `tenants.yaml`): timer diario en la VM que empaqueta
  `last_run.json`, `gasto_diario.json` y `data/history/`, los sube a un bucket de
  Object Storage con un **PAR de escritura** (la VM no puede usar Instance
  Principal: `dynamic-groups` está denegado por IAM) y **verifica el respaldo
  bajándolo y restaurándolo** contra el `MANIFEST.sha256` que viaja adentro.
  Retención del lado servidor (30 d diarios / 365 d mensuales / 7 d versiones),
  camino de restauración que **no necesita que la VM exista**
  (`backup_restore <tenant>` desde la laptop) y alerta por correo cuando falla,
  cuando el PAR está por vencer o cuando el mecanismo estuvo caído
  (`src/notifier/ops_alert.py`). `data/storage_state.json` **queda fuera a
  propósito**: es una credencial de sesión y caduca en ~30 min, así que
  respaldarla suma riesgo sin sumar capacidad de restaurar.
- **Fase 6 — Vigía externo de la ADB** (`.github/workflows/adb-keepalive.yml`):
  workflow diario que consulta el estado de la ADB de AMALIA_DEV por el **plano de
  control de OCI** y la arranca si la encuentra detenida — la única capa que no
  depende de que la VM esté encendida. Falla a propósito cuando la encuentra
  detenida, porque eso prueba que el timer de la VM lleva una semana muerto y la
  falla dispara el correo de GitHub. **No conecta a la base**: la ACL de red solo
  admite la VCN y la IP de la VM, y un runner sale por ~5.400 rangos rotativos de
  Azure; por eso **no evita el auto-stop a los 7 días** —eso sigue siendo del
  keepalive de la VM— pero sí cierra el riesgo de que la base se reclame a los 90
  días. Análisis completo en
  [`docs/decisiones/0003-keepalive-adb-fuera-de-la-vm.md`](docs/decisiones/0003-keepalive-adb-fuera-de-la-vm.md).
- **Umbral en el keepalive interno de la ADB** (`src/memoria/keepalive.py`): el
  conteo de fallos consecutivos se persiste en `data/adb_keepalive.json` y se
  alerta **al segundo fallo seguido**, no al primer parpadeo; un éxito resetea la
  racha. `service.ping_con_estado()` devuelve la decisión ya resuelta.

- **Fase 7 — El puerto 22 deja de estar abierto a internet.**
  `oci/scripts/ssh-allow.sh <tenant>` restringe el SSH al `/32` del operador y lo
  refresca cuando su IP residencial dinámica cambia, sin necesidad de redesplegar.
  Es **idempotente** (si el `/32` correcto ya está puesto no hace ninguna llamada
  de escritura) y **falla cerrado**: aborta sin escribir si los servicios de eco no
  coinciden entre sí —el síntoma de un proxy que intercepta TLS y contesta con
  *su* salida—, si contesta uno solo, si la security list tiene una forma
  inesperada o si el resultado calculado no pasa los guardias; nunca deja
  `0.0.0.0/0` ni una regla a medias. Después de escribir **verifica** que el 22
  responda con una conexión TCP nueva y **revierte** si no. Detecta además que la
  security list sea compartida (`emiliogonzalez` y `emiliogonzalez_e2` son el
  mismo OCID) y pide confirmación. El OCID sale de `oci/state/<tenant>.json`,
  **no está incrustado** en el script. Banco de pruebas con stubs del CLI de
  `oci`: `scripts/test_ssh_allow.sh`.

### Changed

- **`ssh_ingress_cidr` pasa de `0.0.0.0/0` a `auto` en los cuatro tenants.**
  `auto` significa "el `/32` de quien está desplegando, resuelto en el momento",
  y se resuelve en un único lugar (`oci/lib/ssh-cidr.sh`) que comparten el deploy
  (`01-network.sh`) y `ssh-allow.sh`. Un `/32` literal en `tenants.yaml` sería
  peor: nace vencido —la IP es dinámica—, con lo cual un redeploy pisaría con esa
  IP vieja el `/32` correcto y el paso siguiente del propio deploy, que entra por
  SSH, se colgaría contra su propia regla; y además versionaría la IP de casa de
  una persona. **El valor no cambia nada por sí solo:** surte efecto en el próximo
  `deploy.sh` o al correr `ssh-allow.sh`.
- **La guardia de gasto del drill-down se lleva en dólares, no en tokens**
  (`DAILY_TOKEN_BUDGET` → `DAILY_BUDGET_USD`; `data/token_budget.json` →
  `data/gasto_diario.json`, con desglose por modelo). El triaje corre en Haiku
  ($1/MTok) y el análisis en Sonnet ($3): un contador único de tokens no se puede
  traducir a dinero. Además, **estimar y cobrar se separaron** — se estima para
  decidir si arrancar, pero al libro se anota el `usage` **real** que devuelve la
  API. Motivos y alternativas descartadas en
  [`docs/decisiones/0002-presupuesto-en-dolares.md`](docs/decisiones/0002-presupuesto-en-dolares.md).
- `send_report()` acepta `resultados_analisis` (opcional): el reporte HTML suma
  una sección de **matriz de riesgos** y otra de **precedentes**, ambas email-safe
  y omitidas si no hay datos (retrocompatible).
- `ResultadoAnalisis` incorpora `precedentes`; `Proceso`/`ProcesoClasificado` no cambian.
- La lectura del estado del pliego pasa por una fachada (`src/memoria/service.py`)
  que consulta ADB con **fallback automático a JSONL**.

### Fixed

- **El triaje se enviaba a 72 DPI.** El tope del rasterizado (`min(1.0, 1568/lado)`)
  medía el lado en **puntos PDF**, no en píxeles: una carta son 612×792 pt
  independientemente de a qué resolución se escaneó, así que en toda página de
  tamaño estándar el zoom quedaba clavado en 1,0. Medido sobre pliegos reales de
  COMPRASAL: escaneos de 155–200 DPI llegaban al modelo a 72 DPI, ilegibles para
  texto legal denso — no era solo un problema de costo, sino de fiabilidad del
  GO/NO-GO. El tope ahora se calcula contra los píxeles del ráster embebido, con
  guarda para que un logo pequeño no reduzca la página entera a una miniatura.
- **El gasto del triaje no se contabilizaba.** La guardia estimaba antes del
  triaje pero solo reservaba tras un veredicto `GO`, así que todo lo que costaban
  los `NO-GO` quedaba fuera del presupuesto — el mayor consumidor del pipeline en
  un corpus mayoritariamente escaneado. Ahora se cobra por etapa.

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
