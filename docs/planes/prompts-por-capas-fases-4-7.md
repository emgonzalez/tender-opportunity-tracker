# Plan pendiente: política y aprendizaje por capas (fases 4-7)

**Estado: PENDIENTE.** No se ejecuta hasta que AMALIA opere con un segundo país con datos reales
(hoy solo `multicomputos_sv` sobre COMPRASAL; `multicomputos_cr` está inactivo y SICOP no tiene extractor,
ver [`multicomputos-costa-rica.md`](multicomputos-costa-rica.md)). Todas las fases de este documento que tocan la
ADB necesitan **orden explícita** del usuario (DDL y DML se ordenan paso a paso).

## 1. Origen y decisión de arquitectura

Análisis del 2026-11-04 (agente Opus, solo lectura) sobre la pregunta «¿puede la ADB ser la fuente de la verdad de
todo lo relacionado con los prompts, siempre separado por cliente, compartiendo lo aprendido entre países?».

**Decisión:** híbrido.

| Qué | Dónde vive | Por qué |
|---|---|---|
| Texto de las instrucciones y esquemas JSON | **Código versionado** (PR, revisión, tests de paridad) | Reproducibilidad: el replay debe poder reconstruir lo que vio el modelo; la ADB caída no puede dejar la corrida sin prompt; escribir en la base no debe poder inyectar instrucciones; sin duplicar el texto por país |
| Política y glosario editables («qué cuenta como TI», términos por marca, calibraciones) | **ADB, por capas con herencia**: global → organización → portal → cliente | Se corrige sin desplegar, se audita, se versiona y se revierte |
| Lo aprendido (ejemplos, términos, calibraciones, decisiones) | **ADB con campo de alcance** | Compartir entre países sin contaminar |
| Versión del prompt usada | `salida.prompt` de cada `decision_capa` | Ya existe (fase 2, PR #189) |

## 2. Lo que ya está hecho (fases 1-3, mergeadas el 2026-11-04)

| Fase | PR | Qué dejó |
|---|---|---|
| 1 | #187 | `Portal.pais_nombre` y `descripcion_portal()`; los prompts de triaje legado, análisis y clasificación toman portal y país del registro; SV idéntico byte a byte (goldens en `tests/test_prompts_portal.py`) |
| 2 | #189 | Bloque `prompt` (`hash`, `app`, versión del perfil) en `salida` de `decision_capa`; la caché de rescate incorpora el hash del prompt; el replay registra hashes por capa y avisa de mezcla de versiones; `rescate` usa la zona horaria del portal |
| 3 | #188 | `client.extends` con perfil base `clients/base/multicomputos.yaml`; SV y CR resueltos idénticos a antes; ADB y modo sombra ven el perfil resuelto |

Lo que **no** hay todavía, verificado en el esquema `db/schema_v2.sql`: ningún nivel compartido en la base (la
semilla de términos vive en código, `src/aprendizaje/semilla.py`), y ninguna tabla de aprendizaje tiene `portal_id` ni
alcance (`ejemplo_marca`, `marca_termino`, `marca_calibracion`, `decision_capa`).

## 3. Disparadores: cuándo empezar

Empezar la fase 4 cuando se cumpla **al menos una**:

1. SICOP (u otro portal) tiene extractor integrado y `multicomputos_cr` pasa a `active: true`.
2. Hay procesos de un segundo portal en `proceso` con volumen para evaluar (orden de magnitud: ≥ 200 procesos
   relevantes etiquetados por portal; ajustar al ver los datos).
3. Se incorpora un cliente de **otra organización** (cambia el riesgo de fuga: ver §6).

Antes de arrancar, **medir el estado real** (no asumir): conteos por `cliente_id` en `ejemplo_marca`, `marca_termino`,
`marca_calibracion` y `decision_capa`; y qué diferencias de formato muestra el nuevo portal en una muestra de pliegos.

## 4. Fases pendientes

### Fase 4 — Tabla `politica_capa` (DDL: requiere orden explícita)

**Objetivo.** Un lugar en la ADB para la política editable por capa, con herencia y versionado.

**Diseño propuesto** (a validar con el esquema vigente al empezar):
- `politica_capa(politica_id, alcance, ambito_id, clave, version, contenido JSON, activa, creado_por, creado_en)`.
  - `alcance` ∈ `global | organizacion | portal | cliente`; `ambito_id` = `NULL` | `organizacion_id` | `portal_id` | `cliente_id`.
  - `clave` identifica el fragmento (p. ej. `criterio_ti`, `glosario_ti`, `senales_inclusion`), no el prompt entero.
  - Append-only con una sola `activa` por `(alcance, ambito_id, clave)`, igual que `cliente_config` (`db/schema_v2.sql:144`):
    imitar su índice único parcial y su `registrar_version`.
- **Resolver** `politica.resolver(cliente_id, clave)` en `src/aprendizaje/` o módulo nuevo: orden de precedencia
  `cliente > portal > organizacion > global`; listas se reemplazan, diccionarios se fusionan (misma regla que
  `extends` de la fase 3, para que haya una sola semántica).
- **Congelado por corrida:** el resolver se invoca una vez al inicio, el resultado se guarda en el contexto de la corrida
  y su hash va a `salida.prompt` (ampliar el bloque que dejó la fase 2 con `politica: {clave: version}`).
- **Fallback:** ADB caída → caché local `data/cache/politica/` → política horneada en el repo (mismo patrón y mismas
  garantías que `CLIENTES_FUENTE`). Flag `POLITICA_FUENTE=archivo|sombra|adb` (default `archivo`).
- **Qué entra primero:** solo el criterio «qué cuenta como TI» (señales de inclusión/exclusión) que hoy vive en el perfil
  del cliente. No mover el texto de las instrucciones.
- **Dónde se inyecta:** en `pipeline.contexto_cliente` (hoy no incluye `inclusion_signals`: el triaje C0 no las ve, hallazgo
  del piloto 4) y en `rules.py`. Fase previa necesaria si aún no se hizo: inyectar esas señales en el contexto del triaje.
- **Portal y admin:** pantalla de solo lectura en `/portal/admin` y, después, edición con versión base (conflicto
  optimista) reutilizando `escritura_admin`.

**Seguridad.** El contenido se escapa y se delimita como dato no confiable al insertarlo; longitud máxima por clave;
validación de esquema por `clave`; nunca se insertan textos libres sin límite. Escritura solo con permiso `admin`.

**Verificación (criterio de salida).**
1. Con `POLITICA_FUENTE=archivo` el comportamiento es idéntico al actual (paridad de prompts, test).
2. Modo `sombra`: compara lo resuelto de la ADB contra el archivo y registra diferencias sin aplicarlas.
3. Prueba de ADB caída (DSN inválido, nunca contraseña errónea): la corrida cae al respaldo y no falla.
4. Reversión: reactivar una versión archivada deja la política anterior en la siguiente corrida.

**Riesgos.** Duplicar la semántica de `extends` y de la herencia en ADB (mitigar con una sola función de fusión
compartida). Drift entre archivo y ADB (mitigar con la sombra). Cambiar prompts por edición accidental (mitigar con
versiones, revisión y el bloque `prompt` en cada decisión).

**Requiere:** DDL en `db/schema_v2.sql` (+ espejo para Oracle local y el banco SQL de CI), orden del usuario para aplicarlo
a DEV y, aparte, a PROD.

### Fase 5 — `alcance` y `portal_id` en aprendizaje (DDL + posible DML: requiere orden explícita)

**Objetivo.** Que los ejemplos, términos y calibraciones sepan de dónde vienen y para quién valen.

- Columnas `alcance` (`cliente | portal | organizacion | global`, default `cliente`) y `portal_id` (nullable) en
  `ejemplo_marca` (`db/schema_v2.sql:984`), `marca_termino` (`:1040`) y `marca_calibracion` (`:1076`); `decision_capa`
  solo `portal_id` (para análisis por portal; el alcance no aplica a una decisión).
- **Retrocompatibilidad:** filas actuales = `alcance='cliente'` y `portal_id` del cliente (backfill idempotente por SQL,
  con dry-run y conteos antes/después; DML a orden explícita).
- **Índices únicos a revisar:** `marca_termino` es único por `(cliente_id, marca_id, termino, tipo)`; con alcance
  debe pasar a incluir `alcance` y `ambito`; `ux_marca_calibracion_activa` hoy trata `marca_id NULL` como «global del
  cliente» (no del sistema): redefinir sin cambiar el significado de las filas viejas.
- **Esquema de calibración por portal:** `(cliente, marca, portal)`; umbrales de `veredicto_marca` leen la calibración
  más específica disponible.
- **No usar columnas libres** para el alcance; enumerar con CHECK, como las variantes de `decision_capa`.

**Verificación.** Migración idempotente re-ejecutable; conteos idénticos por cliente tras el backfill; el reporte de
deriva y el recálculo del histórico dan el mismo resultado que antes sobre SV; tests contra Oracle 23ai local.

**Requiere:** DDL + backfill (órdenes separadas), actualizar `src/aprendizaje/repo.py` y las pantallas
`/portal/admin/learning`.

### Fase 6 — Vecinos y semilla por alcance (solo código, primero en sombra)

**Objetivo.** Usar lo compartido sin contaminar.

- `vecinos.py` (hoy filtra por `cliente_id` y `marca_id`, `:20-55`): prioridad **mismo portal → misma organización →
  global**; **nunca otra organización**. Tope por alcance para que un país con más datos no ahogue al otro (p. ej. no más
  de la mitad de los vecinos de un portal distinto).
- La semilla de términos (`semilla.py`) se carga como `alcance='global'` con origen `semilla`; el diccionario público
  de marcas sigue siendo el piso determinista.
- Etiquetar en el prompt de vecinos el portal de origen del caso similar («caso similar de SICOP»), para que el modelo
  pueda desconfiar.
- Flag por capa `off|sombra|activo` (patrón INTEL_*): en sombra se calcula con alcance ampliado, se registra en
  `decision_capa` variante `sombra` y se compara contra el resultado actual.

**Verificación.** Replay histórico en sombra por portal; la precisión por portal no baja respecto de «solo local»; mide
cuántos vecinos compartidos aparecen y con qué etiqueta (alerta si la tasa de acuerdo con la etiqueta local es baja).

### Fase 7 — Promoción de lo local a global con holdout por portal (solo código)

**Objetivo.** Que un término o ejemplo aprendido en un país suba a global solo si ayuda en los demás.

- Candidato local → evaluación con **holdout temporal por portal** (`evaluacion.puede_promover`, `promocion.py:16-53`):
  debe mejorar o no empeorar en cada portal evaluado; si no hay datos suficientes en un portal, no se promueve.
- Registro auditable de cada promoción (quién/qué/cuándo/métricas) y **rollback operativo** (ya existe para
  calibraciones; extender a términos y ejemplos).
- Revisión humana obligatoria para promociones a `global` mientras haya pocos portales.
- Lo específico del portal **nunca** se promueve: formato y estructura del PDF, jerga y tipos de proceso
  (`taxonomia_modalidad` ya es por portal), moneda, zona horaria, instituciones y `senal_mercado`.

**Verificación.** Pruebas con datos sintéticos de dos portales: un término bueno en ambos sube; uno que solo funciona en
uno se queda local; el rollback restituye el estado anterior.

## 5. Qué sí y qué no se comparte entre países

| Compartible (candidato a global/organización) | Propio del portal |
|---|---|
| Taxonomía de marcas y reglas de términos fuerte/débil/excluye/ajena (nombres públicos de producto) | Formato y estructura del PDF del pliego |
| Cláusulas típicas de marca y fabricante («carta del fabricante», «proveedor único») | Jerga y tipos de proceso (`taxonomia_modalidad`) |
| Errores recurrentes del selector C2 y del triaje | Moneda y zona horaria |
| Criterio general de «qué es TI» | Instituciones y `senal_mercado` |

## 6. Riesgos transversales

- **Fuga entre clientes.** Hoy todos los clientes son de la misma organización. Si entra otra, el alcance
  `organizacion` y la regla «nunca otra organización» pasan a ser de seguridad, no de comodidad: añadir test de
  aislamiento antes de aceptar el primer cliente externo.
- **Sesgo hacia el país con más datos** (SV). Topes por alcance y calibración por portal.
- **Ejemplos de un portal que confunden a otro** (otro idioma/jerga). Etiquetar el origen en el prompt; promover solo
  con holdout por portal.
- **Pérdida de reproducibilidad.** Cada decisión debe poder reconstruirse: versión de prompt (fase 2) + versiones de
  política (fase 4) + calibración usada.
- **Costo de caché de prefijo.** No verificado si OCI cachea prefijos; antes de reordenar los prompts para dejar el
  prefijo estable, medirlo con el libro de consumo (`src/llm/registro.py`).

## 7. Lo que NO se pudo verificar al redactar este plan

- Que OCI tenga caché de prefijos de prompt.
- Que exista un test explícito de paridad byte a byte con `off` además de los goldens de la fase 1.
- El contenido de `membresia_org` y de `ver_como` respecto a la visibilidad regional (se infiere que los regionales
  ven todos los países por la organización).
- El estado real de la ADB de producción.

## 8. Orden recomendado al empezar

1. Medir (§3) y decidir el disparador.
2. Fase 4 con solo el criterio de TI; modo sombra una semana de corridas diarias.
3. Fase 5 (DDL) con backfill en dry-run, luego aplicado.
4. Fase 6 en sombra; compararla en replay por portal.
5. Fase 7 cuando existan al menos dos portales con holdout suficiente.

Cada paso con su PR, CI verde, validación de `develop` y orden explícita para todo DDL/DML en DEV y, por separado, PROD.
