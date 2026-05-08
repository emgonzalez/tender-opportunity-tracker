# Despliegue OCI

Scripts de despliegue del tender-opportunity-tracker en Oracle Cloud Infrastructure.

## Tenants soportados

Definidos en [`tenants.yaml`](./tenants.yaml):

| Tenant            | Tipo                     | Cap de costo | Auto-stop |
| ----------------- | ------------------------ | ------------ | --------- |
| `emiliogonzalez`  | Free Tier (personal)     | $0.01 USD    | ✅        |
| `multicomputos`   | Producción (corporativo) | —            | —         |

Los **OCIDs** viven en `tenants.yaml` (no son secretos). Las **credenciales API**
viven en `~/.oci/config` bajo perfiles `EMILIOGONZALEZ` y `MULTICOMPUTOS`.

## Pre-requisitos

```bash
brew install oci-cli jq python3 rsync
```

Y un `.env` válido en la raíz del repo (mismo que usás para `start.sh` local).

### Paso 0 — Verificación manual antes del primer deploy (Free Tier)

**Antes** de correr `./oci/deploy.sh emiliogonzalez`, verificá en la consola OCI del tenant personal que el shape `VM.Standard.A1.Flex` aparezca como **"Always Free Eligible"** para tu cuenta:

1. Consola OCI → **Compute → Instances → Create Instance**.
2. En la sección **Image and shape → Change shape → Ampere → A1 Flex**.
3. Si ves un badge verde **"Always Free Eligible"**, podés desplegar sin riesgo.
4. Si **no** aparece (o aparece como Pay As You Go), tu tenant no tiene capacidad Free para A1 Flex. Opciones:
   - Esperar y reintentar (Oracle libera capacidad periódicamente).
   - Cambiar `instance.shape` en `tenants.yaml` a `VM.Standard.E2.1.Micro` (AMD micro x86_64, también Always Free) — pero ojo, en ese caso hay que volver al `Dockerfile` original con Playwright bundled, no `Dockerfile.oci` que asume Chromium ARM.

Cancelá el wizard de la consola (no creés la VM desde ahí — el script lo hace).

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
