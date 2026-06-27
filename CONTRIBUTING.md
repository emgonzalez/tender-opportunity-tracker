# Contribuir a tender-opportunity-tracker

## Estructura de branches

Este repositorio sigue una versión simplificada de **GitFlow**.

### Branches permanentes

| Branch        | Propósito                                                                 | Protegida |
| ------------- | ------------------------------------------------------------------------- | --------- |
| `main`        | Estado de **producción**. Cada commit aquí es desplegable.                | ✅        |
| `develop`     | Integración de desarrollo. Base para todas las branches de trabajo.       | ✅        |
| `legacy/poc`  | Snapshot inmutable de la prueba de concepto. **No se hacen commits aquí.** | ✅        |

### Branches de trabajo (efímeras)

| Prefijo      | Origen     | Destino del merge      | Cuándo usarla                                          |
| ------------ | ---------- | ---------------------- | ------------------------------------------------------ |
| `feature/*`  | `develop`  | `develop`              | Nueva funcionalidad.                                   |
| `bugfix/*`   | `develop`  | `develop`              | Bug no crítico detectado en desarrollo.                |
| `release/*`  | `develop`  | `main` y `develop`     | Preparar un release (bumps de versión, last fixes).    |
| `hotfix/*`   | `main`     | `main` y `develop`     | Fix urgente sobre producción.                          |
| `chore/*`    | `develop`  | `develop`              | Tareas de mantenimiento (deps, infra, docs).           |

**Convención de nombres:** `tipo/breve-descripcion-en-kebab-case`
Ejemplos: `feature/multi-tenant-classifier`, `hotfix/comprasal-totp-timeout`, `chore/upgrade-playwright`.

## Flujo típico

```bash
# 1. Sincronizar develop
git checkout develop && git pull

# 2. Crear branch de trabajo
git checkout -b feature/mi-feature

# 3. Trabajar, commitear, pushear
git push -u origin feature/mi-feature

# 4. Abrir PR contra develop en GitHub
gh pr create --base develop
```

## Releases

```bash
# Desde develop, cuando develop está estable
git checkout -b release/v1.0.0 develop
# (bump de versión, changelog, smoke tests)
git checkout main && git merge --no-ff release/v1.0.0
git tag -a v1.0.0 -m "Release v1.0.0"
git checkout develop && git merge --no-ff release/v1.0.0
git push origin main develop --tags
```

## Hotfixes

```bash
git checkout -b hotfix/descripcion main
# (fix + test)
git checkout main && git merge --no-ff hotfix/descripcion
git tag -a v1.0.1 -m "Hotfix: descripción"
git checkout develop && git merge --no-ff hotfix/descripcion
git push origin main develop --tags
```

## Commits

- Mensajes en español o inglés, consistentes dentro de un mismo PR.
- Imperativo presente: `agrega`, `corrige`, `actualiza` / `add`, `fix`, `update`.
- Primera línea ≤ 72 caracteres; cuerpo opcional con detalle.

## Pull Requests

- Siempre contra `develop` (salvo `hotfix/*` y `release/*`).
- Descripción debe explicar **el porqué**, no solo el qué.
- Usar la plantilla provista en `.github/PULL_REQUEST_TEMPLATE.md`.
- Squash-merge preferido para `feature/*` y `bugfix/*`.
- Merge commit (no-ff) para `release/*` y `hotfix/*` (preserva trazabilidad).

## Seguridad

- **Nunca** commitear `.env`, sesiones de Playwright, logs ni datos exportados.
- El `.gitignore` ya excluye estos artefactos; verificar `git status` antes de cada commit.
- Las credenciales se gestionan vía `.env` local o secrets en el entorno de despliegue.
