# Documentación · AMALIA (tender-opportunity-tracker)

Esta carpeta organiza la documentación **por rol y por ciclo de vida**, no por tema.
La regla es una sola:

> **Cada hecho tiene un único hogar autoritativo. Los demás documentos enlazan, no copian.**

"Fuente única de verdad" no significa "un solo archivo": significa que si un dato
cambia, hay **un** lugar donde editarlo. Duplicar un hecho en prosa garantiza que
tarde o temprano diverja del código (ya nos pasó: ver
[`decisiones/0001-rearquitectura-v2.md`](decisiones/0001-rearquitectura-v2.md)).

---

## Dónde vive cada cosa

| Si buscás… | Documento | Ciclo de vida |
|---|---|---|
| Qué es el proyecto y cómo usarlo | [`../README.md`](../README.md) | vivo |
| Cómo trabajar en el repo (agentes) | [`../CLAUDE.md`](../CLAUDE.md) | vivo |
| Cómo trabajar en el repo (humanos) | [`../GUIA_CLAUDE_CODE.md`](../GUIA_CLAUDE_CODE.md), [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | vivo |
| **Cómo está diseñado el sistema** | [`arquitectura/`](arquitectura/) | durable |
| **Esquema exacto de la base de datos** | [`../db/schema.sql`](../db/schema.sql) ← *ejecutable = verdad* | durable |
| **Qué se va a construir** | [`planes/`](planes/) | efímero (se archiva al mergear) |
| **Por qué se decidió algo** | [`decisiones/`](decisiones/) | append-only |
| Qué está hecho / en curso | [`../STATUS.md`](../STATUS.md) | vivo |
| Qué cambió y cuándo | [`../CHANGELOG.md`](../CHANGELOG.md) | append-only |
| Cómo se despliega en OCI | [`../oci/README.md`](../oci/README.md) | vivo |
| Qué permisos IAM exige el despliegue | [`../oci/PERMISOS-IAM.md`](../oci/PERMISOS-IAM.md) | vivo |
| Cómo se prepara la BD | [`../db/README.md`](../db/README.md) | vivo |

---

## Contenido

### `arquitectura/` — cómo está diseñado (durable)
- [`vision-general.md`](arquitectura/vision-general.md) — arquitectura v2: componentes,
  dos etapas efímeras, presupuesto de memoria, Track A vs Track B.
- [`modelo-de-datos.md`](arquitectura/modelo-de-datos.md) — entidades, máquina de
  estados, políticas de reintento, red y seguridad. **No contiene el DDL**: enlaza a
  `db/schema.sql`.
- [`diagramas.md`](arquitectura/diagramas.md) — HLD, LLD y máquina de estados en
  Mermaid (fuente editable). [`diagramas.html`](arquitectura/diagramas.html) es la
  versión navegable.

### `planes/` — qué se va a construir (efímero)
- [`fase2-track-a.md`](planes/fase2-track-a.md) — MVP drill-down de pliegos.
- [`fase2-track-b.md`](planes/fase2-track-b.md) — memoria institucional + infra.
- [`fase8-evaluacion.md`](planes/fase8-evaluacion.md) — insumo para **decidir** la Fase 8
  (Terraform, tests de dialecto Oracle sin ADB, subagentes propios): qué resuelve cada
  frente en este repo, qué cuesta, qué arriesga y en qué orden conviene. **No es una
  aprobación**: nada de los tres frentes está implementado.

> Los planes **se archivan a `historico/` cuando su PR se mergea**. Un plan vivo
> describe trabajo futuro; si describe trabajo hecho, su lugar es `CHANGELOG.md`.

### `decisiones/` — por qué (ADR, append-only)
- [`0001-rearquitectura-v2.md`](decisiones/0001-rearquitectura-v2.md) — auditoría que
  descartó el LLM local y la vector DB residente sobre 1 GB de RAM.
- [`0002-presupuesto-en-dolares.md`](decisiones/0002-presupuesto-en-dolares.md) — por qué
  la guardia de gasto pasó de contar tokens a contar dólares, y por qué se cobra el
  consumo real en vez de la estimación.
- [`0003-keepalive-adb-fuera-de-la-vm.md`](decisiones/0003-keepalive-adb-fuera-de-la-vm.md) —
  por qué el vigía externo de la ADB mira el plano de control en vez de conectarse a la
  base (la ACL no admite runners), qué riesgo cierra y cuál no.

> Un ADR **no se edita** una vez aceptado. Si una decisión se revierte, se escribe
> un ADR nuevo que la supersede.

### `historico/` — contexto que ya no gobierna
- [`spec-poc-v0.1.0.md`](historico/spec-poc-v0.1.0.md) — spec original del POC
  (12 tareas, todas cerradas en v0.1.0). Se conserva como registro.
- [`brief-rediseno-reporte.md`](historico/brief-rediseno-reporte.md) — brief del
  rediseño del email, ya implementado en `templates/report.html`.

---

## Al agregar documentación

1. **¿Ya existe un hogar para este hecho?** Si sí, editá ahí; no crees un documento nuevo.
2. **¿Es un dato que el código ya expresa?** (esquema, firmas, estructura de carpetas)
   → enlazá al código. No lo transcribas.
3. **¿Es una decisión?** → ADR nuevo en `decisiones/`, numerado.
4. **¿Es trabajo futuro?** → `planes/`, y borralo o archivalo cuando se complete.
