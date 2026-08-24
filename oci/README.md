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
4. **App** — `rsync` del repo a `/opt/tender-opportunity-tracker`, copia del `.env`, `docker compose up`.

Los OCIDs creados quedan en `oci/state/<tenant>.json` (gitignored).
La SSH key se genera automáticamente en `oci/state/<tenant>_id_ed25519`.

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
│   ├── render-cloud-init.sh ← genera el cloud-init con valores del tenant
│   └── cost-watchdog.sh   ← embebido en la VM, autoapaga si excede cap
└── state/                 ← OCIDs y SSH keys generados (gitignored)
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
