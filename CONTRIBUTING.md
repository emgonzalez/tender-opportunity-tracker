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

### Checks obligatorios

Desde el 2026-09-22, `main` y `develop` tienen *rulesets* que **exigen** estos
seis checks de `ci.yml` antes de poder mergear:

| Check | Qué garantiza |
|---|---|
| `ruff` | Estilo y errores estáticos |
| `pytest · py3.11` · `pytest · py3.12` | La suite en las dos versiones que declara `requires-python` |
| `bancos de prueba de bash` | Los scripts de `oci/` y `scripts/` |
| `qué cambió` | Resuelve qué rutas se tocaron — ver abajo por qué es obligatorio |
| `SQL de Track B contra Oracle 23ai Free` | El dialecto del SQL de `src/memoria/` contra un motor real |

`imagen x86 → GHCR` **no** es obligatorio a propósito: es un paso de publicación,
no una puerta de calidad. Exigirlo acoplaría el merge a la disponibilidad del
registro sin agregar garantía.

**Por qué `qué cambió` también es obligatorio.** El job del SQL solo corre si
cambiaron `src/memoria/**`, `db/**` o `pyproject.toml`; cuando no, se saltea y
reporta `conclusion=skipped`, que GitHub cuenta como aprobado. Pero si `qué
cambió` **falla**, el job del SQL también queda salteado — por dependencia no
cumplida— y contaría como aprobado sin haber mirado nada. Exigir los dos cierra
ese agujero. Ya pasó una vez: `dorny/paths-filter` necesita `pull-requests: read`
en los PRs y sin ese permiso fallaba, arrastrando al SQL a un verde falso.

**Un check salteado NO cuelga el PR.** Se verificó contra la API: reporta
`status=completed, conclusion=skipped`. Ésa es la razón por la que el SQL de
Track B pudo dejar de ser un workflow aparte —filtrado por `paths`, que no
reporta nada en las PRs que no tocan esas rutas— y volverse un job condicionado
dentro de `ci.yml`.

## Seguridad

- **Nunca** commitear `.env`, sesiones de Playwright, logs ni datos exportados.
- El `.gitignore` ya excluye estos artefactos; verificar `git status` antes de cada commit.
- Las credenciales se gestionan vía `.env` local o secrets en el entorno de despliegue.
