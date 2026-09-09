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

1. **Network** — VCN + subnet pública + Internet Gateway + Security List (SSH abierto a `0.0.0.0/0`).
2. **Compute** — VM `VM.Standard.A1.Flex` (1 OCPU + 6 GB ARM, dentro de Always Free) con Ubuntu 22.04. Cloud-init instala Docker, embebe el watchdog y configura cron.
3. **Budget** — Topic + email subscription + Budget de `$0.01` USD + Alert Rule + Dynamic Group + Policy (para que la VM se autentique con Instance Principal).
4. **App** — `rsync` del repo a `/opt/tender-opportunity-tracker`, copia del `.env`, y obtención de la imagen: la **baja de GHCR** o la construye en la VM según el compose del tenant (ver abajo).

Los OCIDs creados quedan en `oci/state/<tenant>.json` (gitignored).
La SSH key se genera automáticamente en `oci/state/<tenant>_id_ed25519`.

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

# Re-ejecutar deploy (idempotente — actualiza código si la VM ya existe)
./oci/deploy.sh emiliogonzalez
```

## Destruir todo

```bash
./oci/destroy.sh emiliogonzalez
```

Pide doble confirmación. Elimina en orden: instancia → budget → topic → policy → dynamic group → subnet → IGW → VCN. Limpia el state local.

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
├── destroy.sh             ← teardown
├── lib/
│   ├── common.sh          ← helpers (logging, state, ociq wrapper)
│   ├── 01-network.sh      ← VCN + subnet + IGW + sec list
│   ├── 02-compute.sh      ← VM ARM A1 + cloud-init
│   ├── 03-budget.sh       ← budget + alert + dynamic group + policy
│   ├── 04-app.sh          ← rsync + docker compose up
│   ├── 05-adb.sh          ← ADB Always Free + TLS one-way + ACL (manual)
│   ├── 06-onnx-stage.sh   ← bucket + PAR de LECTURA para el modelo ONNX (manual)
│   ├── 07-backup.sh       ← bucket + retención + PAR de ESCRITURA del respaldo (manual)
│   ├── backup/            ← lo que corre EN la VM: script + units del respaldo
│   ├── render-cloud-init.sh ← genera el cloud-init con valores del tenant
│   └── cost-watchdog.sh   ← embebido en la VM, autoapaga si excede cap
└── state/                 ← OCIDs, SSH keys y PARs generados (gitignored)
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
