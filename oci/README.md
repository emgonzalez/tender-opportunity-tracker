# Despliegue OCI

Scripts de despliegue del tender-opportunity-tracker en Oracle Cloud Infrastructure.

## Tenants soportados

Definidos en [`tenants.yaml`](./tenants.yaml):

| Tenant              | Tipo                                          | Shape                 | Cap de costo | Auto-stop |
| ------------------- | --------------------------------------------- | --------------------- | ------------ | --------- |
| `emiliogonzalez`    | Free Tier ARM (preferido, requiere capacidad) | VM.Standard.A1.Flex   | $0.01 USD    | ✅        |
| `emiliogonzalez_e2` | Free Tier x86 (fallback siempre disponible)   | VM.Standard.E2.1.Micro| $0.01 USD    | ✅        |
| `multicomputos`     | Producción (corporativo)                      | VM.Standard.E2.1.Micro| —            | —         |

Los **OCIDs** viven en `tenants.yaml` (no son secretos). Las **credenciales API**
viven en `~/.oci/config` bajo perfiles `EMILIOGONZALEZ` y `MULTICOMPUTOS`.

## Pre-requisitos

```bash
brew install oci-cli jq python3 rsync
```

Y un `.env` válido en la raíz del repo (mismo que usás para `start.sh` local).

Para los tenants que **bajan** la imagen en vez de construirla (todos los x86 — ver
[De dónde sale la imagen](#de-dónde-sale-la-imagen-pull-vs-build)) hace falta además un
PAT de GitHub con scope `read:packages`, porque el paquete es privado:

```bash
printf '%s' '<token>' > oci/state/ghcr_token && chmod 600 oci/state/ghcr_token
```

Vive en `oci/state/` junto a las llaves SSH: gitignored y excluido del rsync. No va en
el `.env` a propósito — ese archivo se monta entero dentro del contenedor, y la app no
tiene por qué poder leer el token del registro.

Si el PAT tiene vencimiento, tenelo presente: la VM guarda ese login y lo reusa para
[actualizarse sola](#que-la-vm-se-actualice-sola-opt-in-apagado-por-defecto). Cuando
expira, el auto-update falla ruidosamente y renovarlo implica redesplegar (es el deploy el
que rehace el `docker login` de la VM).

En un tenant corporativo hace falta además que el grupo del operador tenga las políticas
IAM correctas: ver [`PERMISOS-IAM.md`](PERMISOS-IAM.md) para la lista completa, qué
etapa consume cada permiso y cómo verificarlos sin crear nada.

### Paso 0 — Verificación manual antes del primer deploy (Free Tier)

**Antes** de correr `./oci/deploy.sh emiliogonzalez`, verificá en la consola OCI del tenant personal que el shape `VM.Standard.A1.Flex` aparezca como **"Always Free Eligible"** para tu cuenta:

1. Consola OCI → **Compute → Instances → Create Instance**.
2. En la sección **Image and shape → Change shape → Ampere → A1 Flex**.
3. Si ves un badge verde **"Always Free Eligible"**, podés desplegar sin riesgo.
4. Si **no** aparece (o aparece como Pay As You Go), tu tenant no tiene capacidad Free para A1 Flex. Opciones:
   - Esperar y reintentar (Oracle libera capacidad periódicamente).
   - Cambiar `instance.shape` en `tenants.yaml` a `VM.Standard.E2.1.Micro` (AMD micro x86_64, también Always Free) — pero ojo, en ese caso hay que volver al `Dockerfile` original con Playwright bundled, no `Dockerfile.oci` que asume Chromium ARM.

Cancelá el wizard de la consola (no creés la VM desde ahí — el script lo hace).

### Fallback E2.1.Micro (x86_64) cuando A1 no tiene capacidad

OCI Free Tier ARM tiene capacidad escasa y esporádica ("la lotería"). Si después de varios intentos no aparece capacidad para `VM.Standard.A1.Flex`, podés desplegar el tenant `emiliogonzalez_e2` que usa `VM.Standard.E2.1.Micro` (AMD x86_64, casi siempre disponible) — los dos tenants pueden coexistir en el mismo tenancy.

```bash
./oci/deploy.sh emiliogonzalez_e2
```

**Diferencias vs. A1:**

| | A1.Flex (emiliogonzalez) | E2.1.Micro (emiliogonzalez_e2) |
| --- | --- | --- |
| OCPU | 1 dedicado | 1/8 burstable compartido |
| RAM | 6 GB | **1 GB + 8 GB swap + ZRAM** |
| Arquitectura | ARM Ampere | AMD x86_64 |
| Dockerfile | `Dockerfile.oci` (Chromium sistema ARM) | `Dockerfile` original (Playwright bundled x86) |
| Compose | `docker-compose.oci.yml` | `docker-compose.e2.yml` |
| Container limit | `4g` | `800m` + `memswap_limit: 8800m` |
| Defensa OOM | n/a (RAM suficiente) | ephemeral + ZRAM + earlyoom + swappiness=100 + oom_score_adj |
| Adecuado para LLM local | ✅ | ❌ (1 GB insuficiente) |

**Caveats E2:**
- Scraping ~2-5x más lento por el OCPU burstable compartido.
- Posible OOM bajo peaks de Chromium (mitigado por el combo configurado en `tenants.yaml#emiliogonzalez_e2`).
- NO sirve para LLM local — si esa es la meta, esperá A1.

## Despliegue

```bash
./oci/deploy.sh emiliogonzalez
```

Pasos que ejecuta:

1. **Network** — VCN + subnet pública + Internet Gateway + Security List (SSH abierto solo al `/32` de quien despliega; ver *Acceso SSH* más abajo).
2. **Compute** — VM `VM.Standard.A1.Flex` (1 OCPU + 6 GB ARM, dentro de Always Free) con Ubuntu 22.04. Cloud-init instala Docker, embebe el watchdog y configura cron.
3. **Budget** — Topic + email subscription + Budget de `$0.01` USD + Alert Rule + Dynamic Group + Policy (para que la VM se autentique con Instance Principal).
4. **App** — `rsync` del repo a `/opt/tender-opportunity-tracker`, copia del `.env`, y obtención de la imagen: la **baja de GHCR** o la construye en la VM según el compose del tenant (ver abajo).
5. **Respaldo del inventario** — sube `oci/state/<tenant>.json` al bucket del tenant. No es fatal si falla, pero avisa. Ver *Respaldo del inventario*.

Todo lo que se crea nace **etiquetado** con el bloque `tags:` del tenant (ver *Etiquetas*),
que es lo que permite encontrarlo después sin depender del state file.

Los OCIDs creados quedan en `oci/state/<tenant>.json` (gitignored, respaldado en el paso 5).
La SSH key se genera automáticamente en `oci/state/<tenant>_id_ed25519` — **esa no se
respalda**, es una credencial.

### Idempotencia: reglas del state

`deploy.sh` es idempotente **sobre el state, no sobre los nombres**: reutiliza cada recurso
cuyo OCID está en `oci/state/<tenant>.json` y crea uno solo si el state no tiene ese id. Tres
reglas sostienen esa afirmación (las implementan [`lib/common.sh`](lib/common.sh) y el pre-flight de `deploy.sh`):

1. **Sin red no se crea nada.** Antes de tocar nada se verifican conectividad y credenciales.
   Cada consulta de existencia tiene tres respuestas: *existe*, *no existe* (OCI responde 404)
   e *indeterminado* (red caída, 429, 5xx). Lo transitorio se reintenta; un 401 aborta de
   inmediato. Lo indeterminado **nunca** se toma como «no existe», y una lista que no se pudo
   consultar aborta en vez de leerse como vacía.
2. **Un 404 sobre un id del state aborta.** OCI también responde 404 cuando falta un permiso,
   así que un recurso que el state conoce y OCI no encuentra no se recrea a ciegas. Para
   recrearlo a propósito, `DEPLOY_RECREAR=<clave_del_state>` (p. ej. `reserved_ip_id`): solo ese recurso; `=1` habilita todos.
3. **El state no se pisa.** Escribir un id distinto sobre uno ya guardado se niega salvo
   `DEPLOY_RECREAR` (con la clave, o `=1`). El state se valida antes de usarlo (vacío o corrupto
   aborta) y cada escritura es atómica. Un state **sin `instance_id`** (ausente, `{}` o sin ids) también
   aborta: crearía todo de nuevo. Solo se acepta a propósito, en el primer despliegue de un tenant:
   `DEPLOY_PRIMERA_VEZ=1 ./oci/deploy.sh <tenant>`.
4. **Un create que funcionó se guarda aunque la espera falle** (`oci_crear`): si OCI creó la VCN, el IGW,
   la subnet, la VM o la IP reservada pero no confirmó el estado, el id queda en el state y la
   siguiente corrida lo reutiliza en vez de crear otro.

Además, cada deploy toma un **lock** por tenant (`oci/state/<tenant>.json.lock`, con el PID
del proceso): dos deploys del mismo tenant no corren a la vez. Un lock que quedó de un proceso
muerto se recupera solo; si el PID fue reutilizado por otro proceso, el aviso dice cómo borrarlo. Y antes de tocar nada se guarda una **copia del state**
(`oci/state/<tenant>.json.pre-deploy-<fecha>-<pid>`); se conservan las 5 últimas (se ordenan por nombre).

Sin id en el state, el deploy **no adopta por nombre**: crea el recurso nuevo. Por eso el
inventario tiene su propio respaldo (ver *Respaldo del inventario*).

### Si un despliegue se corta a la mitad

`04-app.sh` **detiene los timers** al empezar para que una corrida no arranque con el
código a medio sincronizar, y los reactiva al final. Si el script muere en el medio
(timeout, Ctrl-C, SSH caído, cualquier error), un `trap` reactiva los timers habilitados
al salir, para que la corrida de las 07:00 no se pierda en silencio. Tras un corte la VM
**puede quedar con un despliegue a medias**: el script lo avisa, y la respuesta es volver a
correr `oci/deploy.sh`.

El `trap` no puede cubrir que muera tu máquina ni que el SSH no vuelva justo al restaurar:
en ese caso lo dice y da el comando manual. Por eso, **después de cada despliegue**:

```bash
ssh ... 'systemctl is-active amalia.timer'     # debe decir: active
```

Se prueba sin VM con `bash scripts/test_deploy_timers.sh`.

### Si un deploy se interrumpe

Al salir con error, `deploy.sh` dice dónde quedó la copia previa del state y recuerda
revisar huérfanos. Qué quedó depende de en qué punto se cortó:

- **Antes de crear algo** (conectividad, credenciales, validación del state): el state está
  intacto. Volver a correr `./oci/deploy.sh <tenant>`.
- **Después de crear un recurso y antes de guardar su id**: el recurso puede existir en OCI sin
  estar en el state. Revisarlo con [`scripts/find-orphans.sh`](scripts/find-orphans.sh)
  (solo lee: lista lo vivo con etiqueta de AMALIA que no figura en el state).
- **State dañado o desalineado**: restaurar la copia previa con
  `cp oci/state/<tenant>.json.pre-deploy-<fecha> oci/state/<tenant>.json` (**sin `-p`**: conservar el
  mtime viejo desordena la rotación), o traer el respaldo del bucket con `state_restore`
  ([`lib/07-backup.sh`](lib/07-backup.sh)), que se niega a pisar un archivo existente salvo
  `STATE_RESTORE_FORCE=1`. Restaurar con el
  deploy detenido: un lock vivo significa que otro deploy sigue corriendo.
- **Regla de SSH con una IP vieja**: si el corte dejó el `/32` de la security list con una IP
  anterior, `./oci/scripts/ssh-allow.sh <tenant>` lo corrige (con `--dry-run` muestra qué haría).
- **Timers**: el deploy los reactiva al salir, también ante un corte
  (ver [Si un despliegue se corta a la mitad](#si-un-despliegue-se-corta-a-la-mitad)). Tras un
  corte, volver a desplegar y comprobar `systemctl is-active amalia.timer` en la VM.

Si el mensaje dice que hay un deploy en curso, comprobar el PID que figura en el lock antes de
borrarlo a mano.

### Variables de entorno del deploy

| Variable | Por defecto | Para qué sirve |
| --- | --- | --- |
| `DEPLOY_RECREAR` | sin definir (desactivada) | `=<clave>[,<clave>…]` (p. ej. `reserved_ip_id`) permite recrear solo esos recursos cuyo id del state dio 404 y reemplazar su id; `=1` los habilita todos. Usarla solo a propósito. |
| `DEPLOY_PRIMERA_VEZ` | sin definir | `=1` permite desplegar un tenant cuyo state no tiene `instance_id` (el primer despliegue). Sin ella, ese state aborta para no duplicar infraestructura. |
| `DEPLOY_REINTENTOS` | `3` | Intentos totales ante un fallo transitorio (red, 429, 5xx) antes de dar la consulta por indeterminada y abortar. |
| `DEPLOY_ESPERA` | `5` (segundos) | Base de la espera entre intentos: 1×, 3× y 6× esa base (5 s, 15 s y 30 s). Con el valor por defecto de `DEPLOY_REINTENTOS` solo se usan las dos primeras. |
| `OCI_CONNECT_TIMEOUT` | `20` (segundos) | Timeout de conexión de cada llamada al CLI de OCI. |
| `OCI_READ_TIMEOUT` | `120` (segundos) | Timeout de lectura por socket. No corta subidas largas: no es un límite por operación. |
| `OCI_MAX_RETRIES` | `2` | Reintentos que hace el propio CLI de OCI dentro de cada llamada. |

## De dónde sale la imagen (pull vs build)

**Lo decide el compose file del tenant, no una key de configuración.** Si declara
`build:`, la VM construye; si no, baja de GHCR la imagen que CI publicó. La idea es que
no exista una opción que pueda quedar desincronizada del compose que efectivamente se usa.

| Compose | Arquitectura | Modo | Por qué |
|---|---|---|---|
| `docker-compose.e2.yml` | x86_64 | **pull** | CI publica esta imagen |
| `docker-compose.oci.yml` | ARM64 | build | CI todavía no publica ARM |

Construir en un E2.1.Micro era el momento de mayor presión de memoria de todo el
despliegue: 1 GB de RAM armando Playwright + Chromium, contra el swap. Bajar la imagen
ya construida y verificada saca ese riesgo del camino.

### Qué imagen se despliega

Por defecto, **la del commit que tenés checkouteado**: `deploy.sh` resuelve
`sha-$(git rev-parse HEAD)`, que es la etiqueta que produce CI. Desplegar es entonces
elegir un commit, y volver atrás es desplegar otro:

```bash
# desplegar el commit actual
./oci/deploy.sh multicomputos_dev

# rollback a un commit anterior (o probar una etiqueta móvil)
AMALIA_IMAGE_TAG=sha-<40-hex> ./oci/deploy.sh multicomputos_dev
```

Tras bajarla, el deploy **verifica qué bajó**: corre `--version` dentro del contenedor y
exige que devuelva el SHA pedido. Sin esa comprobación, "el pull funcionó" y "la VM tiene
el código que pedí" no se distinguen, porque una imagen vieja también corre sin errores.

⚠️ **Solo se puede desplegar un commit que CI haya publicado**, y el job `image` publica
únicamente desde `main`, `develop` y tags. Desde una rama de trabajo construye y verifica,
pero no sube nada — así que un `deploy.sh` apuntando a un commit de rama fallará en el
pull, con ese mensaje.

Cada despliegue deja atrás una imagen de ~2.7 GB, así que al final se borran las de este
repositorio que no son la actual: en un boot volume de 50 GB, el disco lleno rompería la
corrida diaria de una forma nada obvia.

### Que la VM se actualice sola (opt-in, apagado por defecto)

Todo lo anterior sigue exigiendo que una persona alcance la VM por SSH desde su red. Para
cerrar eso hay un segundo timer en la VM, `amalia-pull-deploy.timer`, que corre
[`amalia-pull-deploy.sh`](./lib/amalia-pull-deploy.sh) **90 minutos antes de la corrida
diaria** (05:30 si el reporte sale a las 07:00; la hora se deriva de `schedule_time`, no se
configura aparte). Corre antes para que el reporte de la mañana ya salga con el código
nuevo, y con ese margen porque la corrida puede durar hasta 30 minutos.

**Sin configurar nada, no hace nada.** Y eso es a propósito: el SHA fijo es la
característica, no una limitación — un commit cualquiera no debería poder llegar solo a la
corrida de las 07:00 de un cliente. La auto-actualización se enciende por tenant declarando
un **canal** (una etiqueta móvil de las que publica CI) en `tenants.yaml`:

```yaml
docker:
  compose_file: docker-compose.e2.yml
  channel: develop        # ausente = nunca se auto-actualiza (el default)
```

y redesplegando una vez. El canal viaja en el `.env` de la VM, no en el cloud-init, así que
cambiarlo o apagarlo es editar `tenants.yaml` y volver a desplegar — no recrear la VM.

**La etiqueta es móvil, pero lo que queda anotado nunca lo es.** El script baja
`<repo>:<canal>`, le pregunta a la imagen qué commit la produjo (`--version`, el sello de
Fase 2) y escribe en el `.env` un `sha-<40hex>`, no el canal. Así "qué código produjo el
reporte de esta mañana" se sigue respondiendo con una sola línea, igual que con el
despliegue manual. Si la imagen del canal no trae sello, no se despliega.

**No se pisa con vos.** Se abstiene —sin marcarse como fallo— si hay una corrida en curso,
si el canal no se movió, o si los timers están detenidos, que es exactamente lo que hace
`04-app.sh` mientras despliega. Y un rollback explícito (`AMALIA_IMAGE_TAG=…`) queda
*clavado*: el auto-update lo respeta hasta el próximo despliegue normal, en vez de
deshacerlo a la mañana siguiente.

**Cuando falla, grita.** El caso esperado es el PAT de GHCR vencido. Entonces la unit queda
en `failed`, el motivo va al journal con prioridad `err`, y el próximo `deploy.sh` lo
reporta en pantalla. La VM se queda mientras tanto en la imagen que funcionaba.

```bash
# ¿cuándo es el próximo chequeo?
ssh ... 'systemctl list-timers amalia-pull-deploy.timer'
# ¿qué pasó en los últimos?
ssh ... 'journalctl -u amalia-pull-deploy.service -n 50 --no-pager'
ssh ... 'cat /opt/tender-opportunity-tracker/data/pull-deploy.log'
```

La máquina de decisión (cuándo actualiza, cuándo se abstiene, cuándo grita) se prueba sin
VM ni registro con `bash scripts/test_pull_deploy.sh`.

⚠️ Los timers los instala `cloud-init`, que corre **una sola vez** en la vida de la VM: una
instancia creada antes de esto no los tiene. `deploy.sh` lo detecta y lo dice; se resuelve
recreando la instancia.

## Flags de inteligencia por tenant

La tabla `tenant_env` de [`lib/04-app.sh`](lib/04-app.sh) genera las variables del
entorno desde `features` en [`tenants.yaml`](tenants.yaml), con estos defaults:

- `LLM_TEMPERATURA` ← `features.llm_temperatura`: `0.0`; vacío desactiva el envío de temperatura (`None`).
- `INTEL_C0`…`INTEL_C8` ← `features.intel_c0`…`features.intel_c8`: `off`.
- `INTEL_CLASIF` ← `features.intel_clasif`: `off`.
- `MARCA_VEREDICTO` ← `features.marca_veredicto`: `off`.
- `EMBEDDING_PREFIJOS` ← `features.embedding_prefijos`: `off`; admite `off|on`.

Los flags `INTEL_C0`…`INTEL_C8`, `INTEL_CLASIF` y `MARCA_VEREDICTO` admiten `off|sombra|activo`; un valor inválido se trata
como `off` en la aplicación. `sombra` registra sin alterar lo que ve el usuario.
Desde 2026-11-01, solo `multicomputos_dev` declara `intel_c0`, `intel_clasif`,
`intel_c2` y `marca_veredicto` en `sombra`; los restantes conservan `off` implícito.
`embedding_prefijos` no se declara en ningún tenant: permanece en `off` hasta un PR
aparte, después de re-vectorizar. La temperatura queda sin override por tenant.

## Servicio web (HTTPS para el feedback del correo)

Opt-in por tenant con `web.enabled: true` en `tenants.yaml` (hoy solo `multicomputos_dev`).
Contexto y decisiones: [ADR 0005](../docs/decisiones/0005-servicio-web-residente-para-el-feedback.md).

Qué hace `deploy.sh` con el flag encendido, en este orden:

1. **Security List:** 80 y 443 desde cualquier origen, en el mismo update que el 22.
2. **IP reservada** (`08-web.sh`): suelta la IP efímera y asigna una reservada. **La IP
   pública cambia una vez** y pide confirmación (`WEB_CONFIRM=yes` la omite). El paso
   siguiente, `adb_acl_sync`, actualiza la ACL de la ADB a la IP nueva.
3. **`.env`:** `WEB_HOSTNAME`, `FEEDBACK_BASE_URL` y `FEEDBACK_HMAC_SECRET`. El secreto se
   genera una vez en `oci/state/<tenant>_feedback_hmac.secret` y no cambia entre
   despliegues (si cambiara, los enlaces ya enviados dejarían de validar). DEV y PROD no
   lo comparten.
4. **Registro A** del hostname en la zona de OCI DNS (falla suave: avisa y sigue).
5. **`web` + `caddy`** con `docker compose up -d`, y una prueba de
   `https://<hostname>/healthz` desde afuera.

**Antes del primer deploy:** la zona DNS debe existir y la delegación del dominio debe
resolver; si no, Let's Encrypt no puede validar. **`ENABLE_FEEDBACK` queda en `false`**:
enciéndelo en `features.enable_feedback` solo cuando el `/healthz` externo responda.

`destroy.sh` borra el registro A y libera la IP reservada (no mueren con la instancia).

## Modelo de ejecución (ephemeral via systemd timer)

A partir de este branch, la VM **no** corre el container 24/7. En su lugar:

- `cloud-init` instala un **systemd timer** que dispara a las **07:00 hora El Salvador** (la VM se configura con `timezone: America/El_Salvador`).
- El timer ejecuta `docker compose -f <compose> run --rm tender-opportunity-tracker python -m src.main --run-now`.
- El container existe sólo durante la corrida (~3-5 min), después se destruye con `--rm` y todo el footprint vuelve a cero.
- El scheduler Python interno (`src/scheduler/cron.py`) sigue existiendo pero ya no se usa en producción — el scheduling vive en `systemd`.

**Para disparar manualmente sin esperar al timer:**
```bash
ssh -i oci/state/<tenant>_id_ed25519 ubuntu@<public_ip> \
  'sudo systemctl start tender-opportunity-tracker.service'
```

**Para ver el próximo disparo del timer:**
```bash
ssh ... 'systemctl list-timers tender-opportunity-tracker.timer'
```

## Respaldo verificado del historial

**El problema que resuelve:** `data/history/*.jsonl` es el único dato que AMALIA no puede
volver a generar — COMPRASAL no devuelve el pasado. Hasta acá su única copia vivía en el
boot volume de una VM Always Free, así que *perder la VM* y *perder el historial* eran el
mismo evento.

**La palabra que manda es *verificado*.** Un respaldo que sube archivos y nadie comprueba
es una creencia, no un respaldo. Por eso, después de cada subida el script **baja el
objeto, lo descomprime en un directorio descartable y contrasta cada archivo contra el
`MANIFEST.sha256` que viaja adentro del tarball**. Eso es literalmente una restauración;
si falla, la corrida falla y systemd manda correo. El detalle de cada decisión está en el
encabezado de [`lib/backup/amalia-backup.sh`](lib/backup/amalia-backup.sh) y
[`lib/07-backup.sh`](lib/07-backup.sh).

### Cómo se activa

Opt-in por tenant en [`tenants.yaml`](./tenants.yaml). Sin el bloque `backup:` (o con
`enabled: false`) nada cambia:

```yaml
backup:
  enabled: true
  schedule_time: "09:00"   # dos horas después de la corrida diaria
```

Después, desde la laptop (necesita la CLI de OCI; **no** está cableado en `deploy.sh`, igual
que `05-adb.sh`):

```bash
export OCI_DIR="$PWD/oci" REPO_ROOT="$PWD" OCI_PROFILE=MULTICOMPUTOS
source oci/lib/common.sh && source oci/lib/07-backup.sh
backup_setup multicomputos_dev
```

Es idempotente y hace todo: bucket, retención, PAR, instalación en la VM, habilitación del
timer y **una corrida de respaldo real que tiene que verificarse antes de dar por terminado
el paso**.

### Por qué un PAR y no Instance Principal

Porque no hay alternativa: en el tenant corporativo el permiso `dynamic-groups` está
denegado por IAM (ver [`PERMISOS-IAM.md`](PERMISOS-IAM.md)), y sin dynamic group la VM no
puede autenticarse por Instance Principal. El patrón que sí funciona ya estaba en uso en
[`lib/06-onnx-stage.sh`](lib/06-onnx-stage.sh) — un PAR de **lectura** para que la ADB baje
el modelo ONNX. Esto es su simétrico: un PAR de **escritura** para que la VM suba.

El PAR es `AnyObjectReadWrite` y no de solo escritura porque verificar exige poder bajar lo
subido. Los límites de esa decisión —y por qué la URL vive en `/etc/amalia/backup.par` y no
en el `.env`— están argumentados en el encabezado de [`lib/07-backup.sh`](lib/07-backup.sh).

**Los PAR expiran** (180 días por defecto). Rotarlo es volver a correr `backup_setup`. La VM
avisa sola: warning en el journal desde 30 días antes, correo desde 7 días antes, y **falla
la corrida** si ya venció — nunca "saltea el respaldo en silencio".

### Qué se respalda y qué no

Una **allowlist** explícita, no un "todo menos": `last_run.json`, `gasto_diario.json` y
`history/`. Si aparece en `data/` algo que el script no sabe clasificar, lo dice en el log
para que nadie descubra tarde que un dato nuevo quedaba afuera.

`data/storage_state.json` **queda fuera a propósito**: es la sesión de Playwright contra
COMPRASAL, o sea una credencial, y su valor de restauración es nulo porque la sesión caduca
en ~30 min. Rehacerla es un comando (ver [Login inicial en COMPRASAL](#login-inicial-en-comprasal)).
La consecuencia práctica: **restaurar en una VM nueva incluye rehacer el login.**

### Retención

Del lado del servidor, con *lifecycle rules* del bucket, no del lado de la VM — coherente
con que la VM no pueda borrar. El Always Free de Object Storage son 20 GB para **toda** la
tenancy, compartidos con el bucket del modelo ONNX; sin caducidad el respaldo se comería el
cupo ajeno.

| Prefijo | Qué guarda | Cuánto |
| --- | --- | --- |
| `daily/` | un objeto por día | 30 días |
| `monthly/` | el objeto del día 1 de cada mes | 365 días |
| `state/<tenant>.json` | el inventario de OCI vigente (ver abajo) | **no caduca** |
| `state/history/` | una copia fechada por despliegue | 365 días |
| versiones anteriores | sobrescrituras (el bucket tiene versionado) | 7 días |

### Restaurar

**El camino que importa no necesita que la VM exista** — ese es justamente el escenario.
Se corre desde la laptop, con la CLI de OCI:

```bash
source oci/lib/common.sh && source oci/lib/07-backup.sh
backup_status  multicomputos_dev          # ¿hay respaldos? ¿de cuándo es el último?
backup_restore multicomputos_dev          # baja el más reciente, lo VERIFICA y lo extrae
```

`backup_restore` verifica los checksums **antes** de dejar nada en `oci/state/restore-…/`, e
imprime el `rsync` exacto para llevarlo a una VM nueva. Después de copiarlo hay que rehacer
el login de COMPRASAL, porque la sesión no viaja en el respaldo.

Desde la propia VM, si sigue viva: `amalia-backup.sh list`, `verify <objeto>`,
`restore <objeto> <dir>`, `status`.

### Respaldo del inventario (`oci/state/<tenant>.json`)

Lo de arriba respalda el **dato de la aplicación**. Esto respalda algo mucho más chico y,
cuando falta, igual de caro: el archivo de ~1 kB con los OCIDs de todo lo que el deploy
creó. Está gitignoreado con razón, y hasta la Fase 8 tenía **una sola copia, en la laptop
del operador**.

Perderlo no rompe la VM: rompe la capacidad de apagarla. `destroy.sh` no encuentra nada
que borrar, y `deploy.sh` **no adopta lo que existe — crea uno nuevo**, porque su idempotencia
se apoya en el OCID del state, no en una búsqueda por nombre (ver
[Idempotencia: reglas del state](#idempotencia-reglas-del-state)). `delete_amalia_multicomputos.sh`
fue el precio de esa pérdida.

Se sube **solo, al final de cada `deploy.sh`** —el único momento en que el state cambia— al
mismo bucket del respaldo del historial. No es fatal si falla (un despliegue que funcionó
no se declara roto porque no se pudieron subir 1 kB), pero avisa. A mano:

```bash
source oci/lib/common.sh && source oci/lib/07-backup.sh
state_backup  multicomputos_dev    # sube el inventario
state_restore multicomputos_dev    # lo trae de vuelta (se niega a pisar uno existente)
```

**Qué NO viaja.** Antes de subir, el archivo pasa por un filtro que descarta cualquier
clave que parezca una credencial (`*password*`, `*secret*`, `*token*`, `*_par_url`) y
cualquier valor con la forma de una URL firmada de Object Storage; lo descartado se
imprime. Es una red de contención, no un inventario: `state_set` acepta cualquier clave.
En la misma línea, `06-onnx-stage.sh` dejó de escribir la URL del PAR en el state —ahora va
a `oci/state/<tenant>_onnx.par` (0600), como el PAR de respaldo.

Tampoco viaja **la llave SSH privada**. Es una credencial, y el bucket lo puede leer la
propia VM con su PAR. Consecuencia honesta: con el inventario restaurado se puede
**inventariar y destruir** la infraestructura, **no entrar a la VM**. Si se pierde la
llave, la vía es recrear la instancia.

### Cuándo grita

| Qué falla | Cómo se entera un humano |
| --- | --- |
| La subida o la verificación | `OnFailure=` → journal (`user.err`), `/var/log/amalia-backup.log` y **correo** a los `system_admins_email` (vía [`src/notifier/ops_alert.py`](../src/notifier/ops_alert.py)) |
| El PAR está por vencer | Warning a 30 días; correo diario desde 7 días |
| El PAR venció | La corrida **falla** (no se saltea) |
| Una corrida diaria está en curso | Espera hasta 20 min; si no termina, falla en vez de guardar estado inconsistente |
| El timer dejó de correr | Al volver a correr, si pasaron > 48 h desde el último éxito, avisa por correo. Desde afuera: `backup_status <tenant>` falla si el último respaldo tiene más de 48 h |

Ese último renglón es el punto flojo conocido y está asumido: **un timer apagado no se
delata solo desde adentro de la VM**. `backup_status` es la comprobación externa; conviene
correrla cada tanto o desde CI.

## Mitigaciones de OOM disponibles (opt-in por tenant)

`render-cloud-init.sh` soporta varias capas opt-in que se activan poniendo los campos correspondientes en `tenants.yaml` bajo `instance:`. Útiles para tenants con poca RAM, no necesarias en A1 con 6 GB+.

| Capa | Campo en `tenants.yaml` | Qué hace |
| --- | --- | --- |
| Container ephemeral | `ephemeral_container: true` + `schedule_time: "HH:MM"` | Genera systemd service+timer que dispara `docker compose run --rm` una vez al día (no daemon 24/7) |
| Flags low-memory Chromium | env `LOW_MEMORY_MODE=true` en el compose | `src/scraper/browser.py` aplica `--disable-gpu`, `--js-flags=--max-old-space-size=384`, etc. |
| ZRAM (RAM comprimida) | `zram_percent: 50` | Instala `zram-tools` y configura ALGO=lz4 |
| Swap file | `swap_size_gb: 8` | cloud-init nativo crea `/swapfile` |
| Swappiness alto | `swappiness: 100` | sysctl: prefiere swap antes de fallar allocs |
| earlyoom | `earlyoom: true` | Instala `earlyoom` con `--prefer chrome|chromium --avoid sshd|systemd|dockerd` |
| `oom_score_adj` | Directo en el compose del tenant | Container muere primero ante OOM-kill del kernel |

Todos los campos son **opcionales**: si no están en `tenants.yaml`, el render los omite (gracias a `tenant_get_or` con default).

## Cómo funciona el cap de costo (auto-stop)

Defensa en dos capas:

1. **Budget alert (email):** Oracle manda mail al `alert_email` del tenant cuando el gasto del compartment alcanza el threshold.
2. **Cost watchdog (cierre automático):** un cron en la propia VM corre cada 30 min, consulta `oci usage-api` con la identidad de la instancia (Instance Principal), y si el gasto total del mes ≥ threshold:
   - `docker compose down` (preserva data)
   - **Libera la ephemeral public IPv4** (mitiga el cargo de IPv4 idle ~$0.005/h vigente desde 2025-02)
   - `oci compute instance action --action STOP`

Una vez apagada y sin IP pública, la VM no genera costo. El boot volume queda dentro de los 200 GB de Always Free.
Para limpieza total (incluida red, IGW, etc.) usá `./oci/destroy.sh <tenant>`.

### Riesgos residuales conocidos

Aún con todas las mitigaciones aplicadas, no puedo garantizar 100% costo cero por factores fuera de mi control:

| # | Riesgo | Mitigación implementada | Riesgo residual |
| --- | --- | --- | --- |
| 1 | A1 Flex puede no estar disponible como Always Free según el estado del tenant | Paso 0 manual (ver arriba) | Si A1 no es Always Free y el deploy igual lo crea como PayG, costaría ~$7/mes hasta que el watchdog dispare. **Hacé el paso 0.** |
| 2 | IPv4 pública idle ($0.005/h desde 2025-02) | Watchdog libera la ephemeral al disparar | Antes de que dispare el watchdog (latencia hasta 6h del usage-api) podría acumular ~$0.03 USD |
| 3 | Latencia de `usage-api` (4-6h) | Budget email alert llega antes (~1h) | Reaccionar manualmente si llega un alert antes del auto-stop |
| 4 | Backup automático del boot volume | El script desasigna la backup policy default tras crear la instancia | — |
| 5 | Free Trial activa con créditos $300 | Watchdog dispara igual al primer cargo | Watchdog te apaga aunque Oracle no te esté cobrando porque consume créditos. Conservador pero correcto. |

**Si te mantenés dentro de Always Free (1 OCPU + 6 GB A1 Flex, 50 GB boot, 1 ephemeral IP attached a VM running) el gasto esperado es $0.** El cap de $0.01 es safety net si algo escapa de Always Free por configuración accidental.

## Login inicial en COMPRASAL

La primera vez que la VM corre, no tiene `data/storage_state.json`. Conectate por SSH y corré el login una vez:

```bash
ssh -i oci/state/emiliogonzalez_id_ed25519 ubuntu@<public_ip>
cd /opt/tender-opportunity-tracker
docker compose -f docker-compose.oci.yml run --rm tender-opportunity-tracker python -m src.auth.login --manual
```

A partir de ahí, el scheduler corre solo según `SCHEDULE_MORNING`/`SCHEDULE_AFTERNOON` del `.env`.

## Operación

```bash
# Logs de la app
ssh -i oci/state/emiliogonzalez_id_ed25519 ubuntu@<public_ip> \
  'cd /opt/tender-opportunity-tracker && docker compose -f docker-compose.oci.yml logs -f'

# Logs del watchdog de costos
ssh -i oci/state/emiliogonzalez_id_ed25519 ubuntu@<public_ip> 'tail -f /var/log/cost-watchdog.log'

# Re-ejecutar deploy (reutiliza lo que el state ya tiene y actualiza código; ver «Idempotencia»)
./oci/deploy.sh emiliogonzalez

# Estado de la auto-actualización (ver sección "Que la VM se actualice sola")
ssh ... 'systemctl list-timers amalia-pull-deploy.timer; systemctl is-failed amalia-pull-deploy.service'
```

## Acceso SSH — el 22 abierto solo a tu IP

El puerto 22 **no se puede cerrar**: `lib/04-app.sh` lo usa para todo el
despliegue (rsync del repo, `scp` del `.env`, `docker login`, verificación del
sello). Lo que sí se puede es abrirlo a **una sola IP** en vez de a internet. La
corrida diaria no lo necesita: si el 22 queda mal, se rompe el despliegue, no el
reporte de las 7 AM.

**Cómo se decide ese `/32`.** `tenants.yaml` dice `ssh_ingress_cidr: auto`, que
significa "la IP pública de quien está desplegando, averiguada en el momento".
La resolución vive en un solo lugar, `lib/ssh-cidr.sh`, y la usan los dos caminos
que la escriben:

| Cuándo | Quién escribe la regla |
| --- | --- |
| Al desplegar (`./oci/deploy.sh <tenant>`) | `lib/01-network.sh` |
| Entre despliegues, cuando cambia tu IP | `oci/scripts/ssh-allow.sh <tenant>` |

Si cada uno resolviera la IP por su cuenta, un redeploy podría pisar con un valor
viejo el `/32` que `ssh-allow.sh` acababa de dejar bien — y el paso siguiente del
propio deploy entra por SSH, o sea que se colgaría contra su propia regla.

```bash
# La IP de casa cambió y el SSH dejó de andar:
oci/scripts/ssh-allow.sh multicomputos_dev

# Ver qué haría, sin escribir nada:
oci/scripts/ssh-allow.sh multicomputos_dev --dry-run

# Si la detección automática aborta (proxy que intercepta TLS), la IP real es la
# que ve el sshd de la VM:
ssh -i oci/state/multicomputos_dev_id_ed25519 ubuntu@<ip> 'echo $SSH_CLIENT'
oci/scripts/ssh-allow.sh multicomputos_dev --cidr <esa-ip>/32
```

**Las garantías, y por qué son esas.**

- **Idempotente.** Si el `/32` correcto ya está puesto, no hace *ninguna* llamada
  de escritura. Correrlo dos veces seguidas escribe una sola vez. Importa porque
  con una IP dinámica se corre seguido, y cada escritura es una oportunidad de
  dejar la regla mal.
- **Falla cerrado.** Ante cualquier duda aborta sin escribir: si los servicios de
  eco no coinciden entre sí, si contesta uno solo, si la security list tiene una
  forma inesperada (cero o dos reglas que abren el 22), o si el resultado
  calculado no pasa los guardias. Nunca deja `0.0.0.0/0` ni una regla a medias.
- **Corrobora la IP.** Exige que **dos servicios independientes digan lo mismo**.
  Un proxy que intercepta TLS puede contestar con *su* salida y no con la tuya:
  ese `/32` sería de nadie y el 22 quedaría cerrado para todos.
- **Solo IPv4.** Las VCN de este proyecto no tienen bloque IPv6 y la VM se alcanza
  por IP pública v4, así que el SSH sale por v4 aunque tu red tenga IPv6. Una
  regla `/128` no serviría; la detección fuerza `curl -4` y, si tu red solo llega
  por v6, lo dice en vez de escribir cualquier cosa.
- **Verifica y revierte.** Después de escribir prueba que el 22 responda desde tu
  máquina (una conexión TCP nueva, no la que ya tenías abierta). Si no responde,
  restaura la regla anterior y sale con error.
- **Avisa si la security list es compartida.** `emiliogonzalez_e2` reusa la red de
  `emiliogonzalez`: es el mismo OCID y el cambio afecta a las dos VMs.

**No podés quedarte afuera para siempre.** La regla se edita por el API de OCI,
no por dentro de la VM: mientras tu `~/.oci/config` funcione, volver a correr el
script lo arregla.

El OCID de la security list sale de `oci/state/<tenant>.json` (gitignoreado) y
**no está incrustado en el script**; ver `PERMISOS-IAM.md` § *Dónde vive el OCID
de la security list*. Banco de pruebas —con stubs del CLI de `oci`, sin tocar
nada real—: `bash scripts/test_ssh_allow.sh`.

## Etiquetas: encontrar un recurso sin el state file

Desde la Fase 8 **todo lo que crea `deploy.sh` nace etiquetado** con el bloque `tags:` que
[`tenants.yaml`](./tenants.yaml) declaraba desde el primer día y que ningún script leía.
Se agrega además una clave `tenant`, porque los cuatro tenants comparten el mismo
`project` y varios recursos (topic, budget, dynamic group, policy) viven mezclados en el
root tenancy.

Eso convierte *«¿qué quedó vivo de AMALIA?»* en una consulta:

```bash
./oci/scripts/find-orphans.sh multicomputos_dev          # lo de ese tenant
./oci/scripts/find-orphans.sh multicomputos_dev --todos  # todo el proyecto
```

El script **solo lee** —es seguro contra producción— y hace el cruce que importa: lo que
está etiquetado como de AMALIA pero **no figura en el state file**. Eso es, por
definición, un huérfano: nada lo va a dar de baja solo.

Tres límites honestos, que el propio script imprime:

- Solo ve lo **etiquetado**: los recursos creados antes de la Fase 8 no lo están. Se
  etiquetan al volver a desplegar.
- El índice de búsqueda de OCI tarda unos minutos en ver una etiqueta nueva.
- Los PAR no son recursos indexables ni admiten etiquetas. Por eso `destroy.sh` los revoca
  por **nombre**, que sí guarda el state.

## Destruir todo

```bash
./oci/destroy.sh multicomputos_dev --dry-run     # el plan, sin tocar nada
./oci/destroy.sh multicomputos_dev               # de verdad (pide escribir el tenant)
./oci/destroy.sh multicomputos_dev --with-backups  # …y además borra el bucket de respaldos
```

Orden: instancia → boot volume → ADB → PAR → (bucket de respaldos) → budget → topic →
policies → dynamic groups → subnet → IGW → VCN.

**Tres reglas que antes no se cumplían.** La versión anterior mandaba todo error a
`/dev/null` (`del() { ociq "$@" --force >/dev/null 2>&1 || true; }`) y después borraba el
state igual, imprimiendo *«Tenant limpio»*. Un borrado fallido dejaba el recurso vivo **y**
destruía el único registro de su OCID: la receta exacta de un huérfano, ya pagada una vez
con `delete_amalia_multicomputos.sh`.

1. **Un borrado que falla se ve.** Se imprime el error real de OCI y se acumula.
2. **Si algo falló, el state NO se borra** y el script sale con error. El state es el mapa
   para reintentar; volver a correrlo es seguro, lo ya borrado se saltea.
3. **"No existe" no es un fallo.** Es el resultado buscado.

**Lo que no borra, a propósito:**

| Recurso | Por qué |
| --- | --- |
| `amalia-onnx-staging` | Los nombres de bucket son únicos por **namespace**, no por compartment: lo comparten todos los tenants de la tenancy. Borrarlo le rompería la carga del modelo al otro. Su PAR **sí** se revoca. |
| `amalia-backups-<tenant>` | Es el *deshacer* de esta operación: el historial y el inventario. Borrar la infraestructura y su copia de seguridad en el mismo comando es justo lo que no conviene poder hacer sin escribirlo aparte (`--with-backups`). |

En ambos casos el script imprime el comando exacto para borrarlos a mano.

Banco de pruebas con stubs del CLI —incluida la prueba de que **un borrado fallido no
borra el state**—: `bash scripts/test_destroy.sh`.

## Fase M — migración a Ampere A1 (tenant en sombra, `hold_timers`)

`multicomputos_dev_a1` (ver `tenants.yaml`) no es un tenant de producción nuevo:
es la misma app en una VM más grande, corriendo **en sombra** junto a
`multicomputos_dev` mientras dura la comparación día a día de la migración.
Dos cosas de su configuración no son obvias si se leen solas:

- **`instance.hold_timers: true`** — `04-app.sh` NO enciende `amalia.timer` en
  este tenant, aunque `ephemeral_container: true`. Es a propósito: durante la
  sombra, la corrida diaria la sigue disparando la E2 original, y dos VMs
  corriendo el mismo pipeline el mismo día duplicarían correos y gasto de LLM.
  El log de deploy lo dice explícito (`hold_timers=true — timer dejado APAGADO
  a propósito`). Para el corte final: borrar esa clave (o ponerla en `false`)
  y volver a desplegar, o a mano: `ssh ... 'sudo systemctl enable --now
  amalia.timer'`.
- **Sin bloque `adb:` propio** — este tenant no declara `adb_ocid`. La ADB
  sigue siendo enteramente de `multicomputos_dev` mientras dura la migración;
  agregar la IP de esta VM a su ACL se hace a mano (`ociq db
  autonomous-database update --autonomous-database-id <adb_id>
  --whitelisted-ips [...]`, conservando la VCN y la IP de la E2). Esto es
  seguro porque `adb_acl_sync` (`oci/lib/05-adb.sh`) **agrega, no reemplaza**:
  un redeploy normal de `multicomputos_dev` no le borra la entrada a esta VM,
  ni viceversa.

**Guardas de `destroy.sh` para este escenario:** el script se niega a terminar
una ADB que también aparece en el `adb_ocid` de otro tenant, y se niega a
borrar la VCN de un tenant si algún otro todavía la comparte
(`network.share_from` apuntando a él). Ninguna de las dos guardas depende de
nombrar tenants a mano: recorren `oci/state/*.json` buscando la coincidencia.
Banco de pruebas: `bash scripts/test_destroy.sh` (sección "Guardas de la Fase
M") y `bash scripts/test_adb_acl.sh` (la unión de IPs en sí).

## Agregar un tenant nuevo

1. Configurá un perfil en `~/.oci/config` con sus credenciales:
   ```
   [NUEVOTENANT]
   user=ocid1.user.oc1...
   fingerprint=...
   tenancy=ocid1.tenancy.oc1...
   region=us-ashburn-1
   key_file=/Users/.../nueva.pem
   ```
2. Agregá una entrada en `tenants.yaml`.
3. `./oci/deploy.sh nuevotenant`.

## Estructura

```
oci/
├── README.md              ← este archivo
├── tenants.yaml           ← config no-secreta de cada tenant
├── deploy.sh              ← orquestador
├── destroy.sh             ← teardown (falla ruidoso; no borra el state si algo falló)
├── lib/
│   ├── common.sh          ← helpers (logging, state, etiquetas, ociq wrapper)
│   ├── 01-network.sh      ← VCN + subnet + IGW + sec list
│   ├── 02-compute.sh      ← VM ARM A1 + cloud-init
│   ├── 03-budget.sh       ← budget + alert + dynamic group + policy
│   ├── 04-app.sh          ← rsync + .env + pull/build de la imagen
│   ├── 05-adb.sh          ← ADB Always Free + TLS one-way + ACL (manual)
│   ├── 06-onnx-stage.sh   ← bucket + PAR de LECTURA para el modelo ONNX (manual)
│   ├── 07-backup.sh       ← bucket + retención + PAR de ESCRITURA + respaldo del state
│   ├── backup/            ← lo que corre EN la VM: script + units del respaldo
│   ├── render-cloud-init.sh ← genera el cloud-init con valores del tenant
│   ├── amalia-pull-deploy.sh ← embebido en la VM, auto-actualiza la imagen (opt-in)
│   ├── ssh-cidr.sh        ← ÚNICO lugar donde se resuelve el /32 que abre el 22
│   └── cost-watchdog.sh   ← embebido en la VM, autoapaga si excede cap
├── scripts/
│   ├── ssh-allow.sh       ← refresca ese /32 cuando cambia la IP del operador
│   └── find-orphans.sh    ← SOLO LEE: qué hay vivo con etiqueta de AMALIA y no está en el state
└── state/                 ← OCIDs, SSH keys y PARs generados (gitignored, respaldado al bucket)
```

## Política de correo por tenant

**El destino del despliegue decide quién recibe los correos.** No hay que cambiar
nada a mano al alternar entre validación y producción.

Se configura en `tenants.yaml`:

```yaml
tenants:
  emiliogonzalez_e2:
    notification:
      override_to: tu-correo@ejemplo.com   # validación: TODO el correo va aquí
  multicomputos:
    notification:
      override_to: ""                       # producción: destinatarios de clients/*.yaml
```

| Campo | Efecto |
| --- | --- |
| `notification.override_to` con valor | **Todo** el correo (reportes y alertas) se redirige ahí; se ignoran los destinatarios de `clients/*.yaml`. El asunto se prefija con `[VALIDACIÓN]` y se agrega la cabecera `X-Original-To`. |
| `notification.override_to: ""` o ausente | Comportamiento normal: cada cliente recibe en sus `email_to`. |

**Cómo funciona:** `04-app.sh` reescribe `NOTIFICATION_OVERRIDE_TO` en el `.env`
que copia a la VM, tomando el valor del tenant. Por eso:

- Desplegar a un tenant de validación **nunca** escribe a los clientes reales.
- Desplegar a producción **borra** cualquier override que tuvieras en tu `.env`
  local — un residuo de pruebas no puede filtrarse a producción.

La redirección se aplica en `resolve_recipients()` (`src/notifier/email_sender.py`),
por donde pasan obligatoriamente las tres funciones de envío: no hay ruta alterna
hacia `sendmail`.

### Límites conocidos del endurecimiento

- Un corte justo entre soltar la IP efímera y asignar la reservada deja a la VM sin IP pública (solo
  tiene salida por el IGW): la corrida de las 07:00 falla hasta volver a desplegar, que retoma.
- `destroy.sh` y `scripts/ssh-allow.sh` no toman el lock del deploy.
- Sin red, tras el pre-flight, abortar puede tardar varios minutos (timeouts del CLI × reintentos).
- El cost-watchdog ya no borra la IP **reservada** (solo la efímera); si el servicio quedó sin IP,
  recuperarla con `DEPLOY_RECREAR=reserved_ip_id ./oci/deploy.sh <tenant>`.
