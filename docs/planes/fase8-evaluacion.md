# Fase 8 — evaluación de los tres frentes estructurales

> **Qué es este documento:** el material para **decidir**, no el plan para construir.
> Nada de lo que sigue está aprobado ni implementado. El plan de adopción marca la
> Fase 8 como *«opcional · ≈semanas · decidir antes de AMALIA_PROD»* y dice que
> conviene decidirla **conscientemente, no por omisión**. Esto es el insumo de esa
> decisión.
>
> **Estado del que parte:** Fases 0–6 en `develop`; `AMALIA_DEV` desplegado y vivo;
> `AMALIA_PROD` **no existe** — es exactamente la bifurcación que no conviene cruzar
> distraído.
>
> **Línea base verificada al escribir esto:** `ruff check .` → *All checks passed*;
> `pytest -q` → **247 passed, 6 skipped** (18.6 s, venv limpio con `pip install -e '.[dev]'`).

---

## 0. Cómo se midió

Las afirmaciones de este documento sobre el frente 2 y el frente 3 **no son de
memoria**: salen de tres prototipos desechables que se escribieron en `/tmp`, se
ejecutaron, y se tiraron. No hay código nuevo en el repositorio.

| Prueba | Qué hizo | Resultado |
|---|---|---|
| `probe_dialecto.py` | Levantó `gvenzl/oracle-free:23-slim` local, aplicó `db/schema.sql`, capturó el SQL de `src/memoria/` con un `conn` mock y lo pasó por `cursor.parse()` | schema **6/6 bloques OK en 0.12 s**; **5 de 8** sentencias parsean; las 3 restantes fallan con `ORA-40284: model does not exist` |
| `probe_sin_modelo.py` | Sustituyó `VECTOR_EMBEDDING(…)` por un literal `TO_VECTOR('[0,…]',384,FLOAT32)` | las 3 vectoriales **parsean**; controles negativos atrapados: `ORA-00904` (columna), `ORA-00942` (tabla), `ORA-03047` (`LIMIT` de Postgres) |
| `probe_ejecucion.py` | Ejecutó de verdad `upsert_proceso` → `claim_siguiente` → `set_estado` contra el contenedor | **todo el camino de la cola durable pasa en 0.37 s**, incluido el `MERGE` idempotente, el `FOR UPDATE SKIP LOCKED` y el `CHECK` (`ORA-02290` con un estado inventado) |
| `probe_rutas2.py` | Auditó rutas y enlaces de `CLAUDE.md`/`README.md`/`docs/README.md` | 26 enlaces relativos, **0 rotos**; 1 ruta del árbol inexistente (`oci/delete.sh`); **10 entradas de la raíz no mencionadas** |

Medición de arranque del contenedor Oracle (imagen ya en caché, Mac arm64):
**10 s hasta `healthcheck.sh` en verde**. Imagen: **1.54 GB**.

**No se tocó OCI.** Ni una llamada a la API, ni un SSH.

---

## 1. Terraform sustituyendo `oci/lib/*.sh`

### 1.1 El problema, con nombre y apellido

El estado del despliegue es un JSON escrito a mano por `state_set`
([`oci/lib/common.sh:71-93`](../../oci/lib/common.sh)) y vive en `oci/state/<tenant>.json`,
que está **ignorado por git** ([`.gitignore:41`](../../.gitignore)). Es decir: **la única
copia del inventario de infraestructura está en la laptop del operador**, sin respaldo.
(La Fase 5 respalda el *historial de la app* a Object Storage; el estado de OCI no.)

De ahí salen tres fallas concretas, todas verificables hoy:

**(a) Sin state, `deploy.sh` no adopta: duplica.** El patrón de idempotencia de todos
los `lib/*.sh` es *«si el OCID está en state y OCI confirma que existe, saltear; si no,
crear»* (p. ej. [`oci/lib/01-network.sh:91-93`](../../oci/lib/01-network.sh)). No hay
ninguna búsqueda por nombre ni por etiqueta. Perder el JSON no hace que el próximo
`deploy.sh` encuentre la VCN: hace que cree **una segunda**.

**(b) `destroy.sh` miente por diseño.** Toda eliminación pasa por
`del() { ociq "$@" --force >/dev/null 2>&1 || true; }`
([`oci/destroy.sh:29`](../../oci/destroy.sh)) — errores tragados, stderr al vacío — y al
final hace `rm -f "$STATE"` ([`oci/destroy.sh:60`](../../oci/destroy.sh)) e imprime
*«Tenant limpio»*. Si un borrado falló, el recurso queda vivo **y el único registro de
su OCID se acaba de borrar**.

**(c) Hay recursos que `destroy.sh` ni siquiera intenta.** Compara las claves que
escriben los scripts contra las que lee `destroy.sh`:

| Recurso | Lo escribe | ¿Lo borra `destroy.sh`? |
|---|---|---|
| instancia, budget, topic, policy, dynamic group, subnet, RT, IGW, VCN | 01/02/03 | ✅ |
| `adb_ocid`, `adb_dynamic_group_id`, `adb_policy_id` | [`05-adb.sh`](../../oci/lib/05-adb.sh) | ❌ |
| `onnx_par_url` | [`06-onnx-stage.sh`](../../oci/lib/06-onnx-stage.sh) | ❌ |
| `backup_bucket`, `backup_par_name` | [`07-backup.sh`](../../oci/lib/07-backup.sh) | ❌ |
| `boot_volume_id`, `security_list_id` | 02 / 01 | ❌ |

La ADB, el bucket de respaldos, el bucket de staging del ONNX y los dos PAR son
**huérfanos por construcción**: nada en el repo los da de baja.

**(d) Y ya pasó.** En la raíz del repositorio, hoy, hay
[`delete_amalia_multicomputos.sh`](../../delete_amalia_multicomputos.sh) — 246 líneas
escritas a mano para eliminar el tenant `multicomputos` de producción, con el OCID de
la instancia y el del compartment **hardcodeados en las líneas 16-17** y una batería de
`oci ... list` para redescubrir VCN, subnets, IGW, route tables y security lists.
Existe porque `destroy.sh` no servía: no había state. Junto a él quedaron
`network_diagnostics.sh` y `ELIMINACION_AMALIA_README.md`. Ese es el costo real de la
falla, ya pagado una vez.

**Detalle barato y revelador:** `oci/tenants.yaml` declara un bloque `tags:`
(`managed-by: oci-deploy-script`, `environment: dev`) para los **cuatro** tenants
(líneas 51, 111, 188, 239) y **ningún script lo lee jamás** — `grep -rn "freeform-tags" oci/`
no devuelve nada. La forma más barata de encontrar un recurso huérfano —listar por
etiqueta— está declarada y no implementada.

### 1.2 Qué costaría

Terraform **no reemplaza `oci/lib/`**: reemplaza el tercio que crea recursos. El resto
es configuración *dentro* de la VM, y ahí Terraform no tiene nada que decir.

| Bloque | Líneas hoy | ¿Terraform? |
|---|---:|---|
| `01-network.sh` | 207 | ✅ ~80 líneas HCL |
| `02-compute.sh` | 160 | ⚠️ ~60 HCL, **perdiendo el failover entre ADs** (§1.3) |
| `03-budget.sh` | 131 | ⚠️ moot: `budget.enabled: false` y `dynamic-groups` denegado |
| `05-adb.sh` | 355 | 🔶 el recurso ADB sí; habilitar TLS, ACL, DSN, DDL y ONNX no |
| `06-onnx-stage.sh` | 126 | 🔶 bucket y PAR sí; la descarga y el `LOAD_ONNX_MODEL` no |
| `07-backup.sh` | 344 | 🔶 bucket + lifecycle + PAR sí; instalar units en la VM no |
| `04-app.sh` + `render-cloud-init.sh` + `amalia-pull-deploy.sh` + `backup/*` | 1 439 | ❌ es rsync/SSH/systemd |
| **Total `oci/`** | **2 655** | **~700–900 líneas migrables** |

Desglose de esfuerzo, con la infraestructura de `AMALIA_DEV` **ya creada** (o sea, hay
que importar, no crear):

| Tarea | Estimación |
|---|---|
| Módulos `network` + `compute` + backend remoto, `plan` limpio | 1,5–2 días |
| `terraform import` de los ~8 recursos vivos de `multicomputos_dev` sin recrear la VM | 1–1,5 días · **es la parte que sale mal** |
| Módulo `database` (ADB free tier + TLS + ACL) | 0,5–1 día |
| Módulo `objectstorage` (2 buckets, lifecycle, 2 PAR con rotación) | 0,5–1 día |
| Recablear `deploy.sh` para que orqueste `terraform apply` + las etapas que quedan en bash | 1 día |
| Documentar, borrar lo que muere, actualizar `oci/README.md` y `PERMISOS-IAM.md` | 0,5 día |
| **Total** | **5–7 días de trabajo enfocado** |

A eso se suma el **backend remoto**, que el plan pide explícitamente y con razón
(«NexoPay commiteó su tfstate al repositorio y eso no se copia»). El tfstate contiene
la contraseña ADMIN de la ADB y las URLs de los PAR **en claro**: si termina en git, es
peor que el JSON actual, no mejor.

### 1.3 Qué se rompe

1. **El failover entre availability domains desaparece.** `02-compute.sh:86-122` recorre
   los ADs y, si OCI contesta *«Out of host capacity»* —lo habitual en Always Free—,
   prueba el siguiente. Terraform no expresa eso: falla el `apply` entero y hay que
   editar una variable y reintentar a mano. **Es una regresión operativa real en un
   tenant Free Tier.**
2. **La tolerancia a un tenant parcialmente autorizado desaparece.** `05-adb.sh:303-306`
   avisa y sigue cuando no puede crear el Dynamic Group, porque en este tenant
   `dynamic-groups` está **denegado** ([`oci/PERMISOS-IAM.md:29`](../../oci/PERMISOS-IAM.md),
   bloque B). Terraform no tiene «intentá y seguí»: hay que sacar el recurso del
   `.tf`. Es viable, pero el conocimiento *«esto se intenta y puede no salir»* se pierde
   del código.
3. **Dos toolchains en vez de una.** El operador ya necesita `oci`, `jq`, `python3`,
   `rsync`, `ssh`. Se le suma `terraform` y su ciclo de vida propio.
4. **`terraform import` sobre infraestructura viva es la fase peligrosa.** Un import mal
   hecho hace que el primer `plan` proponga **destruir y recrear** la instancia de
   `AMALIA_DEV`. Con `plan` antes de `apply` es detectable — pero hay que mirarlo.

**Si no se hace:** cada ambiente nuevo sigue dependiendo de un JSON en una laptop, y
crear `AMALIA_PROD` **duplica** el problema en vez de sumarlo: dos estados, dos copias
únicas, dos oportunidades de repetir `delete_amalia_multicomputos.sh`.

### 1.4 Recomendación

🟡 **Antes de AMALIA_PROD — pero no como está planteado.** Partir en dos y hacer
primero la mitad barata:

**Paso 1 (≈4–6 h, hacerlo ya, independiente de Terraform):** cerrar el agujero sin
cambiar de herramienta.
- Aplicar el bloque `tags:` que `tenants.yaml` ya declara, como `--freeform-tags` en
  cada `create`. Con eso un recurso huérfano se encuentra con un `list --query`.
- Que `destroy.sh` deje de tragar errores: `del()` sin `2>&1`, y **no borrar el state si
  algún borrado falló**.
- Sumar a `destroy.sh` los recursos que hoy ignora (ADB, buckets, PAR, boot volume).
- Subir `oci/state/<tenant>.json` al bucket de respaldos que la Fase 5 **ya creó**. Es
  un archivo de 1 kB, no contiene secretos (solo OCIDs), y elimina el «única copia».

Eso captura, en medio día, la mayor parte del valor que Terraform promete.

**Paso 2 (5–7 días, sin urgencia):** evaluar Terraform de verdad cuando exista una
segunda razón además del estado — por ejemplo cuando `AMALIA_PROD` y `AMALIA_DEV`
tengan que divergir y volver a converger. Hoy `tenants.yaml` **ya es** el archivo de
variables por ambiente que el plan dice que Terraform traería: agregar `AMALIA_PROD` es
agregar un bloque YAML de ~1,5 kB, igual que se hizo con `multicomputos_dev`. Ese
argumento del plan, hoy, **no aplica**.

---

## 2. Tests de dialecto Oracle sin ADB

### 2.1 El problema

`src/memoria/` emite **≈13 sentencias SQL distintas en 17 puntos de ejecución**
(`repo.py` 6, `vectorizer.py` 7, `rag.py` 3, `db.py` 1), todas como **strings crudos**
a `cursor.execute()`. Y son de la clase difícil: `MERGE … USING (SELECT … FROM dual)`
([`repo.py:102`](../../src/memoria/repo.py)), un **bloque PL/SQL anónimo** con cursor
explícito y `FOR UPDATE SKIP LOCKED` + `WHERE CURRENT OF`
([`repo.py:267`](../../src/memoria/repo.py)), y AI Vector Search 23ai —
`VECTOR_EMBEDDING` / `VECTOR_DISTANCE` — en [`rag.py:145,253`](../../src/memoria/rag.py)
y [`vectorizer.py:181`](../../src/memoria/vectorizer.py).

Lo que las verifica hoy es [`tests/test_repo_sql.py`](../../tests/test_repo_sql.py):
8 tests con `MagicMock` que comprueban que **el string contiene la palabra `MERGE`**
(línea 57). Un SQL sintácticamente inválido pasa esos tests sin despeinarse. La única
verificación real es [`tests/integration/test_adb.py`](../../tests/integration/test_adb.py)
— los **6 skipped** de la línea base — que exige credenciales de una ADB a la que
**ni una laptop ni un runner de CI pueden conectarse**: la ACL admite la VCN y la IP de
la VM, y nada más (ver [ADR 0003](../decisiones/0003-keepalive-adb-fuera-de-la-vm.md)).
Resultado: un error de SQL cuesta hoy un ciclo completo de despliegue.

### 2.2 La sorpresa: el techo es mucho más alto que «compilar el dialecto»

Antes de opinar, se midió. Tres hallazgos que cambian el análisis:

**(a) SQLAlchemy no sirve acá.** El dialecto Oracle de SQLAlchemy compila
*expresiones de SQLAlchemy Core* a SQL Oracle; no valida strings crudos. Usarlo exigiría
reescribir las 13 consultas como expresiones Core — y `VECTOR_EMBEDDING`, el bloque
PL/SQL y `WHERE CURRENT OF` no tienen construcción Core. Sería una reescritura grande
para obtener una verificación peor. **Descartado.**

**(b) `db/docker-compose.oracle.yml` ya resuelve casi todo, y es el mismo motor.**
El contenedor `gvenzl/oracle-free:23-slim` (1,54 GB, multiplataforma, **10 s hasta
healthy** con la imagen en caché) aplicó `db/schema.sql` **6/6 bloques en 0,12 s**,
incluidos `VECTOR(384, FLOAT32)` y `JSON` nativo.

**(c) No hace falta «compilar»: se puede *ejecutar*.** El camino completo de la cola
durable —`upsert_proceso` (INSERT), `upsert_proceso` otra vez (MERGE idempotente),
`claim_siguiente` (el bloque PL/SQL), cola vacía → `None`, `set_estado`— corrió contra
el contenedor **en 0,37 s**, y el `CHECK` atrapó un estado inventado con `ORA-02290`.
Eso es una verdadera prueba de integración, no un chequeo de dialecto.

Para lo vectorial, `cursor.parse()` (round-trip de PARSE sin ejecutar) falla con
`ORA-40284: model does not exist` porque el ONNX no está cargado. Dos salidas, ambas
probadas:

- **Hermética (recomendada):** sustituir `VECTOR_EMBEDDING(…)` por un literal
  `TO_VECTOR('[0,…]', 384, FLOAT32)`. Las 3 consultas **parsean**, y se sigue
  verificando tabla, columnas, JOIN, alias, `VECTOR_DISTANCE`, `ORDER BY` y
  `FETCH FIRST :bind`. Lo único que deja de cubrirse es el nombre del modelo.
- **Completa:** cargar el `.onnx` de 118 MB en el contenedor con
  `DBMS_VECTOR.LOAD_ONNX_MODEL` (en el contenedor **hay filesystem**, así que es un
  `docker cp` — más fácil que en la ADB, que necesita `DBMS_CLOUD` + PAR). Fricción
  real: la URL del modelo **no está en el repo** — `06-onnx-stage.sh:56` la espera en
  `ONNX_SOURCE_URL`, y el PAR propio vive en `oci/state`, o sea en la laptop.

Los controles negativos confirman que el parse no es decorativo: atrapa columna
inexistente (`ORA-00904`), tabla mal escrita (`ORA-00942`) y sintaxis de Postgres
(`LIMIT 5` → `ORA-03047`). Y un hallazgo lateral útil: `FETCH FIRST :k ROWS ONLY`
**es legal** en Oracle — el bind de [`rag.py:151`](../../src/memoria/rag.py) está bien.

### 2.3 Qué cuesta

| Tarea | Estimación |
|---|---|
| `conftest.py` con fixture de sesión: levanta el contenedor, aplica `db/schema.sql`, hace skip si no hay Docker | 2–3 h |
| Reescribir `tests/test_repo_sql.py` (los 8 tests de `MagicMock`) contra el contenedor | 3–4 h |
| Helper de sustitución del literal + tests de parse para las 3 vectoriales | 2 h |
| Job `oracle` en `.github/workflows/ci.yml` (service container o `docker run` + healthcheck) | 2–3 h |
| **Total** | **1,5 días** |

En CI: **+1,5 GB de pull** y **~1,5–3 min** de wall-clock en un job nuevo y paralelo, que
no toca la ruta crítica de `lint`/`test`. Los jobs `lint` y `test` siguen en 3.11/3.12
sin Docker.

### 2.4 Qué se rompe

- **Nada de producción.** Es puramente aditivo: tests nuevos y un job nuevo.
- **Riesgo real: un job que tarda y a veces se cae.** Un contenedor Oracle en CI
  introduce una fuente de intermitencia. Mitigación: `continue-on-error: false` pero
  **fuera de** `needs:` del job `image`, para que un contenedor caprichoso no bloquee la
  publicación.
- **Lo que sigue sin cubrirse, y hay que decirlo:** el contenedor no trae `DBMS_CLOUD`,
  no reproduce el wallet mTLS ni el Vector Memory Pool dimensionado (el propio
  `docker-compose.oracle.yml` lo advierte), y `parse()` **no ejecuta**: no atraparía,
  por ejemplo, que `:txt` en `VECTOR_EMBEDDING(… USING :txt AS data)`
  ([`vectorizer.py:181`](../../src/memoria/vectorizer.py)) llegue como CLOB cuando el
  chunk supera 4 000 caracteres. Eso solo lo atrapa la variante con el modelo cargado.

**Si no se hace:** Track B queda verificable únicamente desde la VM, contra la ADB real.
Con dos ambientes eso significa que cada cambio en `src/memoria/` compite por la única
ventana de despliegue. Y como Track B está detrás de `ENABLE_MEMORIA`, el código se
degrada en silencio hasta el día que alguien lo encienda.

### 2.5 Recomendación

🟢 **Hacerlo ahora.** Es el más barato de los tres (1,5 días), el único puramente
aditivo, el único cuyo andamiaje (`db/docker-compose.oracle.yml`, commit `9937601`) **ya
está escrito y pagado**, y el que convierte 8 tests decorativos en 8 tests reales. Ir
por la variante hermética; dejar la carga del ONNX como paso opcional para cuando la
URL del modelo esté en un secreto de CI.

---

## 3. Subagentes propios

### 3.1 El problema

[`CLAUDE.md:3-14`](../../CLAUDE.md) **ya exige** máximo paralelismo con agentes y
`isolation: "worktree"`, pero no define qué puede escribir un subagente, cómo se
reintegra su trabajo, ni qué está congelado. El protocolo existe hoy como una frase
ad-hoc en cada prompt.

**Evidencia de esta misma sesión:** el worktree aislado desde el que se escribió este
documento arrancó apuntando a `058bed2` (merge del PR #4, anterior a `pyproject.toml`,
a `docs/`, a `db/` y a `src/memoria/`) en vez de a `develop`. Hubo que hacer
`git reset --hard develop` antes de poder leer nada de lo que el análisis necesitaba.
Un agente menos suspicaz habría analizado un repositorio que no existe desde hace meses.
Y el encargo incluyó a mano *«no toques nada fuera de `docs/`, otro agente trabaja en
`oci/`»* — que es, literalmente, el `[CONGELADO]` que el frente propone formalizar,
dicho en prosa y sin garantía de que se repita la próxima vez.

**Y hay deriva medible.** El primer párrafo que cualquier agente lee,
[`CLAUDE.md:40`](../../CLAUDE.md), dice:

> *«`v0.1.0` en producción y funcionando (tenant `multicomputos`). En curso: Fase 4 …
> (rama `feature/fase2-track-a`, PR #5)»*

Ambas mitades son falsas: la infraestructura de `multicomputos` se eliminó el 2026-09-08
(la prueba está en la raíz del repo), y el PR #5 se mergeó en `cda2aef`. El árbol de
«Project Structure» cita `oci/delete.sh`, que **no existe** (es `oci/destroy.sh`), y
**no menciona** `pyproject.toml`, `.github/`, `tests/`, `scripts/` ni `oci/lib/` — o
sea, todo el aparato de las Fases 0 y 1 es invisible para quien lee el mapa.

*(Honestidad sobre la medición: el prototipo también marcó `data/`, `data/last_run.json`,
`data/storage_state.json` y `data/export/` como inexistentes. Son **falsos positivos**:
son artefactos de runtime ignorados por git, ausentes en un worktree limpio. La deriva
real del árbol es `oci/delete.sh`.)*

### 3.2 Qué cuesta — y dónde el frente se equivoca de herramienta

Los tres subagentes propuestos no valen lo mismo:

| Propuesto | Veredicto | Por qué |
|---|---|---|
| **Explorador barato** | ❌ **Ya existe** | El harness trae `Explore` («read-only search agent for broad fan-out searches») y `general-purpose`. Definir uno propio duplica lo que ya viene. |
| **Test-runner en Haiku** | ⚠️ **Marginal** | La suite tarda **18,6 s** y se invoca con una línea. Un subagente no ahorra tiempo: ahorra *contexto* (la salida de pytest). Beneficio real pero chico. |
| **Auditor de RB-001…RB-008** | 🔶 **Valioso — pero la mitad no es un agente** | Ver abajo. |

El auditor es la idea buena, y el prototipo mostró por qué conviene partirla:

- **La mitad barata es determinista.** «Toda ruta citada en `CLAUDE.md` existe» y «todo
  enlace relativo de `docs/` resuelve» son **~40 líneas de pytest**, corren en
  milisegundos, entran en el CI que ya existe y no cuestan un token. El prototipo se
  escribió en minutos y encontró `oci/delete.sh` y las 10 ausencias de la raíz. Los 26
  enlaces relativos, en cambio, están **todos sanos** — la disciplina de `docs/README.md`
  funciona; el que se desactualiza es el árbol ASCII de `CLAUDE.md`.
- **La mitad cara es semántica.** «RB-005: el reporte se envía siempre, incluso con cero
  procesos» no se verifica con un `grep`; hay que leer `send_report` y razonar. Eso sí
  es trabajo de LLM — y es, además, trabajo **episódico**: se corre antes de un release,
  no en cada push.

Nótese que el propio bloque RB de `CLAUDE.md` ya está **anotado con las correcciones de
una auditoría previa** (RB-001 aclara que `SCHEDULE_AFTERNOON` existe pero no se usa;
RB-004 que no hay dedup por UUID; RB-005 que el reporte sale igual con lista vacía). O
sea: la auditoría ya se hizo una vez, a mano, y encontró tres divergencias. Eso es
evidencia de que el problema **reincide**, no de que esté resuelto.

| Tarea | Estimación |
|---|---|
| Protocolo `[CONGELADO]` + SOLICITUD DE INTEGRACIÓN en `CLAUDE.md` | 1–2 h |
| Quitar `.claude/` del `.gitignore` **con cuidado** y versionar solo `.claude/agents/` | 0,5–1 h · **ver §3.3** |
| `tests/test_documentacion.py` (rutas + enlaces) | 1–2 h |
| Poner al día `CLAUDE.md:40` y el árbol de estructura | 1 h |
| Definición del auditor semántico de RB-001…RB-008 + una pasada de calibración | 3–4 h |
| Definición del test-runner | 0,5 h |
| **Total** | **1–1,5 días** |

### 3.3 Qué se rompe

- **`.gitignore:38` es `.claude/`** — hoy una definición de subagente **no se puede
  commitear**: quedaría en la laptop del operador, exactamente la misma falla que
  `oci/state/`. Hay que desbloquear el path, y hacerlo **sin** empezar a versionar
  `.claude/settings.local.json` ni los worktrees de agentes que viven bajo
  `.claude/worktrees/`. Un `!.claude/agents/` mal puesto puede meter basura al repo.
- **Riesgo de teatro.** Un auditor LLM que reporta divergencias que nadie corrige es
  peor que no tenerlo: da la sensación de cobertura sin la cobertura.
- **`CLAUDE.md` engorda.** Es el archivo que se carga en cada sesión; un protocolo largo
  se paga en tokens en todas.

**Si no se hace:** el paralelismo que `CLAUDE.md` ya ordena sigue siendo seguro solo
porque quien escribe el prompt se acuerda de decirlo. Y `CLAUDE.md` sigue derivando —
con dos ambientes en vez de uno, el mapa desactualizado deja de ser un detalle: un
agente que crea que producción es `multicomputos` puede apuntarle a un tenant
equivocado.

### 3.4 Recomendación

🟡 **Partido en tres, y solo dos valen la pena:**

- 🟢 **Ahora (≈2–3 h):** el protocolo `[CONGELADO]` / SOLICITUD DE INTEGRACIÓN en
  `CLAUDE.md`, y poner al día `CLAUDE.md:40` y el árbol. Es lo que hace seguro el
  paralelismo que el archivo ya pide, y no requiere ningún subagente.
- 🟢 **Ahora (≈1–2 h):** `tests/test_documentacion.py`. Es la mitad del auditor que no
  necesita un LLM, entra en el CI existente y evita que la deriva vuelva.
- 🟡 **Antes de AMALIA_PROD:** el auditor semántico de RB-001…RB-008, como agente
  versionado, para correr antes de cada release.
- ❌ **No hacer:** el explorador barato (`Explore` ya existe). El test-runner, solo si
  sobra tiempo — la suite dura 18 s.

---

## 4. Orden recomendado

| # | Frente | Cuándo | Esfuerzo | Por qué en ese orden |
|---|---|---|---|---|
| **1** | **Protocolo `[CONGELADO]` + `test_documentacion.py` + `CLAUDE.md` al día** (parte de F3) | **ahora** | **≈4 h** | Es lo más barato del documento y **protege todo lo demás**: los otros dos frentes se van a ejecutar con agentes en paralelo sobre este repo. Arreglar el mapa antes de mandar a nadie a caminarlo. |
| **2** | **Tests contra Oracle local** (F2) | **ahora** | **1,5 días** | Puramente aditivo, andamiaje ya pagado, techo mucho más alto de lo que el plan suponía: no «compila el dialecto», **ejecuta la cola durable en 0,37 s**. Convierte 8 tests decorativos en 8 reales. |
| **3** | **Higiene de estado en `oci/`** (mitad barata de F1) | **ahora** | **4–6 h** | Etiquetas + `destroy.sh` honesto + `oci/state` respaldado. Captura casi todo el valor de Terraform en medio día. |
| **4** | **Auditor semántico RB-001…RB-008** (resto de F3) | antes de PROD | 3–4 h | Cuando haya dos ambientes, la deriva de `CLAUDE.md` empieza a costar despliegues. |
| **5** | **Terraform** (F1 completo) | **decidir en PROD, no antes** | 5–7 días | El argumento de «AMALIA_PROD como archivo de variables» **ya lo cumple `tenants.yaml`**. El argumento del estado huérfano lo cubre el punto 3 por 1/10 del costo. Queda el argumento genuino —`plan` antes de `apply`— que no urge mientras la infraestructura sea una VM, una ADB y dos buckets. |
| — | **Explorador barato** (parte de F3) | **nunca** | — | `Explore` y `general-purpose` ya vienen con el harness. |

**Los puntos 1, 2 y 3 suman ≈2,5 días** y son mutuamente independientes: tocan
`CLAUDE.md`+`tests/`, `tests/`+`.github/`, y `oci/` respectivamente. Se pueden
paralelizar — con el punto 1 hecho primero, que es justamente lo que los vuelve seguros.

**El argumento contra Terraform, en una línea:** no es que esté mal; es que **el 80 % de
su valor acá se obtiene por el 10 % de su costo**, y comprometerse a una segunda
toolchain justo antes de crear el segundo ambiente agrega riesgo en el peor momento.
Reevaluarlo cuando `AMALIA_PROD` exista y funcione.

---

## 5. Dónde este análisis es incierto

Lo que **sí** se comprobó ejecutando está en §0. Lo que sigue **no** se comprobó:

1. **Nada de esto se probó en un runner de GitHub Actions.** Los 10 s de arranque del
   contenedor y los 0,37 s de ejecución son de una Mac arm64 con la imagen en caché. En
   un runner x86 hay que sumar el pull de 1,54 GB y contar con que Oracle Free pide
   ~2 GB de RAM. La estimación de **1,5–3 min** es una extrapolación, no una medición.
2. **La variante con el modelo ONNX cargado no se probó.** La URL del modelo no está en
   el repo (`06-onnx-stage.sh` la espera en `ONNX_SOURCE_URL`) y no hay copia local. Que
   `DBMS_VECTOR.LOAD_ONNX_MODEL` funcione en `gvenzl/oracle-free:23-slim` es una
   inferencia razonable —23ai Free trae AI Vector Search y el contenedor sí tiene
   filesystem— **pero no está verificada**.
3. **Ninguna afirmación sobre el proveedor Terraform de OCI está verificada.** Que
   `oci_database_autonomous_database` soporte free tier + TLS-only + `whitelisted_ips`,
   y que `oci_objectstorage_preauthrequest` sirva para los PAR, sale de memoria. **Antes
   de comprometerse a los 5–7 días hay que leer la documentación del proveedor.**
4. **El backend remoto es el punto más flojo del análisis de Terraform.** La ruta natural
   —Object Storage por la API compatible con S3— exige *Customer Secret Keys*, que son
   una operación de IAM sobre el propio usuario. Con `dynamic-groups` denegado y un
   Landing Zone con Identity Domains restrictivo, **no se sabe si el tenant lo permite**.
   Es exactamente la clase de bloqueo que ya costó dos PRs de IAM
   (`2717cf3`, `8a515db`). **Verificarlo antes de estimar nada.**
5. **Las estimaciones en días son de trabajo enfocado, no de calendario**, y ninguna
   incluye la revisión humana que `CLAUDE.md` exige para todo lo de `oci/` (área experta
   de Emilio). Para el frente 1, esa revisión no es un redondeo: es una parte grande.
6. **`terraform import` sobre `AMALIA_DEV` vivo se estimó en 1–1,5 días sin haberlo
   intentado nunca en este tenant.** Es la línea del presupuesto con más varianza.
7. **El costo del auditor semántico de RB-001…RB-008 no se midió.** Cuántos tokens
   consume una pasada, y qué tasa de falsos positivos tiene, se sabrá recién con la
   pasada de calibración que la estimación incluye.
8. **La deriva de `CLAUDE.md` se midió con un prototipo tosco** que produjo 4 falsos
   positivos de 5 en el árbol de estructura (§3.1). El test que se propone escribir
   necesita una allowlist de rutas de runtime; el número «1 ruta rota» es correcto, pero
   el prototipo que lo produjo no es el test.
