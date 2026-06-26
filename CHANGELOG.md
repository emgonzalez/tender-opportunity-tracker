# Changelog

Todas las versiones notables de **tender-opportunity-tracker (AMALIA)** se documentan en este archivo.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto se adhiere a [Versionado Semántico](https://semver.org/lang/es/):
`vMAYOR.MENOR.PARCHE` — **MAYOR** = cambios incompatibles, **MENOR** = features
compatibles, **PARCHE** = correcciones.

## [Sin publicar]

_Cambios en `develop` aún no etiquetados._

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
