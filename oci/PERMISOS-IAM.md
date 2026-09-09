# Permisos IAM requeridos para desplegar

Qué necesita el grupo del operador para correr `deploy.sh` de punta a punta, derivado
llamada por llamada de los scripts de esta carpeta. El pipeline y sus etapas están en
[`README.md`](README.md); acá solo viven los permisos.

> Escrito contra el tenant **multicomputos** (Landing Zone corporativo, compartment
> `AMALIA_DEV`, grupo `'Default'/'MC_Internal_Developers'`). Para un tenant personal
> donde el usuario es admin de la tenancy, nada de esto hace falta.

---

## Qué etapa consume qué

| # | Etapa | Familia de permisos |
|---|---|---|
| 1 | `network_create` | `virtual-network-family` |
| 2 | `compute_create` | `instance-family` + `volume-family` |
| 3 | `budget_setup` | `ons-family`, `budgets`, `dynamic-groups` |
| 4 | `app_deploy` | **ninguno** — es rsync/scp/docker sobre SSH |

La ADB de Track B **no** está en este pipeline: `lib/05-adb.sh` se invoca a mano y sus
permisos son el bloque C.

---

## Bloque A — núcleo obligatorio

Ya concedido y verificado: con estos tres statements se desplegó producción el
2026-06-26. Confirmar que siguen vigentes tras cualquier cambio en el tenant.

```
Allow group 'Default'/'MC_Internal_Developers' to manage virtual-network-family in compartment AMALIA_DEV
Allow group 'Default'/'MC_Internal_Developers' to manage instance-family        in compartment AMALIA_DEV
Allow group 'Default'/'MC_Internal_Developers' to manage volume-family          in compartment AMALIA_DEV
```

Qué cubre cada familia:

| Llamada al API | Origen | Familia |
|---|---|---|
| `network vcn create / get` | `lib/01-network.sh:95` | virtual-network-family |
| `network internet-gateway create / get` | `lib/01-network.sh:112` | virtual-network-family |
| `network route-table update` | `lib/01-network.sh:127` | virtual-network-family |
| `network security-list update` | `lib/01-network.sh:175` | virtual-network-family |
| `network subnet create / get` | `lib/01-network.sh:195` | virtual-network-family |
| `network vnic get` | `lib/02-compute.sh:129` | virtual-network-family |
| `iam availability-domain list` | `lib/02-compute.sh:51` | instance-family |
| `compute image list` | `lib/02-compute.sh:63` | instance-family |
| `compute instance launch / get` | `lib/02-compute.sh:104` | instance-family |
| `compute instance list-vnics` | `lib/02-compute.sh:128` | instance-family |
| `compute boot-volume-attachment list` | `lib/02-compute.sh:140` | volume-family |
| `bv volume-backup-policy-assignment get / delete` | `lib/02-compute.sh:147` | volume-family |
| `compute instance terminate` | `destroy.sh:32` | instance-family |

---

## Bloque B — lo que hoy falla

Todos terminan en `in tenancy`, y la política se adjunta al compartment
`AMALIA_DEV`. Un compartment hijo no puede otorgar permisos de tenancy, así que hoy
devuelven `NotAuthorizedOrNotFound (404)`. Concederlos exige una política **nueva en el
compartment raíz**.

### Budget propio y alertas de costo

Hoy se esquiva con `budget.enabled: false` en [`tenants.yaml`](tenants.yaml), apoyándose
en el budget del Landing Zone (`Budget-Dev-InternalAppsMC-Tripwire`). Conceder solo si se
quiere un tope propio con alerta por correo.

```
Allow group 'Default'/'MC_Internal_Developers' to manage budgets    in tenancy
Allow group 'Default'/'MC_Internal_Developers' to manage ons-family in tenancy
```

### Dynamic groups y políticas

Los scripts crean la identidad de la VM (Instance Principal) y la política que la
habilita. Los dynamic groups solo pueden vivir en la raíz de la tenancy — restricción de
OCI, no del script.

```
Allow group 'Default'/'MC_Internal_Developers' to manage dynamic-groups in tenancy
Allow group 'Default'/'MC_Internal_Developers' to manage policies       in compartment AMALIA_DEV
```

**Alternativa sin delegar creación de identidades:** que el admin cree una sola vez los
dynamic groups y las políticas del bloque D, y no conceda estos permisos. Los scripts
detectan que ya existen (`iam dynamic-group list` / `iam policy list`) y siguen adelante.

### Lectura de identidad

No lo usa `deploy.sh`, pero sin esto el diagnóstico a ciegas sale caro: en junio se
perdieron semanas creyendo que la cuenta estaba borrada, cuando era `get_user`
devolviendo 404 por falta de permiso.

```
Allow group 'Default'/'MC_Internal_Developers' to inspect users  in tenancy
Allow group 'Default'/'MC_Internal_Developers' to inspect groups in tenancy
```

---

## Bloque C — condicional según feature flag

Ambos flags están en `false` para producción, así que **hoy no hace falta conceder nada
de este bloque**.

### `ENABLE_MEMORIA` → Autonomous Database

Crear la ADB Always Free, configurar su ACL de red y desactivar mTLS para TLS one-way.
Requiere además agregar un bloque `adb:` al tenant en `tenants.yaml`.

```
Allow group 'Default'/'MC_Internal_Developers' to manage autonomous-database-family in compartment AMALIA_DEV
```

Llamadas: `db autonomous-database create` (`lib/05-adb.sh:169`), `list` (`:143`), `get`
(×8), `update` para ACL y mTLS.

**Opcional — password de ADMIN desde Vault.** Si se define `adb.admin_password_secret`,
el script lee la contraseña de un secret en vez de generarla e imprimirla en pantalla
(`lib/05-adb.sh:160`). Recomendable en un tenant corporativo.

```
Allow group 'Default'/'MC_Internal_Developers' to read secret-bundles in compartment AMALIA_DEV
```

### `ENABLE_DRILLDOWN` → ningún permiso de OCI

El drill-down descarga PDFs de COMPRASAL y llama al API de Anthropic desde dentro del
contenedor. No toca ningún servicio de Oracle.

---

## Bloque D — permisos de la VM en ejecución

No son del operador humano sino de la instancia, vía Instance Principal. Los crean los
scripts si tienen el permiso del bloque B, o los crea el admin a mano.

### Watchdog de costo — requerido si `budget.auto_stop: true`

Dynamic group con regla `instance.id = '<OCID de la instancia>'`, más
(`lib/03-budget.sh:119`):

```
Allow dynamic-group tot-multicomputos-watchdog to read usage-report           in tenancy
Allow dynamic-group tot-multicomputos-watchdog to use instance-family         in tenancy where target.instance.id = '<instance-ocid>'
Allow dynamic-group tot-multicomputos-watchdog to manage public-ips           in tenancy where target.compartment.id = '<compartment-ocid>'
Allow dynamic-group tot-multicomputos-watchdog to read virtual-network-family in tenancy where target.compartment.id = '<compartment-ocid>'
```

> ⚠️ **Inconsistencia conocida.** `render-cloud-init.sh:32` instala el watchdog
> *siempre* y activa su cron cuando `budget.auto_stop: true`, pero `budget_setup` se
> salta entero cuando `budget.enabled: false` (`lib/03-budget.sh:19`) — el caso de
> producción hoy. Resultado: cada 30 minutos la VM corre `cost-watchdog.sh`, sus
> llamadas a `usage-api` e `instance action STOP` fallan por falta de permisos, y el
> script las descarta con `|| true`. **El auto-stop de producción no funciona y falla en
> silencio.** Salidas: conceder estos permisos, o poner `auto_stop: false` y dejar de
> instalar un guardián sin dientes.

### Acceso de la app a la ADB — si `ENABLE_MEMORIA`

```
Allow dynamic-group tot-multicomputos-adb-access to use autonomous-databases in tenancy
```

---

## Fuera del alcance de IAM

Para no conceder de más, esto es lo que el sistema hace sin tocar el API de OCI:

- **Despliegue de la app** — `rsync`, `scp` y `docker compose` por SSH contra la IP
  pública. Solo requiere el puerto 22, que la security list ya abre.
- **Salida a internet** — COMPRASAL, API de Anthropic y SMTP salen por el Internet
  Gateway. Sin NAT gateway ni service gateway.
- **Credenciales de la app** — COMPRASAL, Anthropic y SMTP viajan en el `.env` que
  `app_deploy` copia por `scp`. No usan OCI Vault.

---

## Verificación tras aplicar

Las políticas tardan 1–2 minutos en propagar. Todos los comandos son de solo lectura;
un `200` —aunque devuelva lista vacía— confirma que el permiso aplicó.

```bash
DEV_OCID=ocid1.compartment.oc1..aaaaaaaanhp32bzkqu4u2g626rtlsbtczsv65cbf6bkztpvgawxmwdco32pa  # AMALIA_DEV

# Bloque A
oci --profile MULTICOMPUTOS network vcn list      --compartment-id $DEV_OCID
oci --profile MULTICOMPUTOS compute instance list --compartment-id $DEV_OCID

# Bloque B
oci --profile MULTICOMPUTOS iam dynamic-group list
oci --profile MULTICOMPUTOS iam group list

# Bloque C (solo si se concedió ADB)
oci --profile MULTICOMPUTOS db autonomous-database list --compartment-id $DEV_OCID
```

---

## Dos trampas que ya costaron tiempo

**Cambiaron el dominio y el nombre del grupo.** El tenant usa Identity Domains. Desde
el 2026-09-09 el grupo operador es **`'Default'/'MC_Internal_Developers'`**; antes era
`'OracleIdentityCloudService'/'Developers-InternalApps'` — se movió de dominio *y* se
renombró. Esto importa porque OCI **acepta sin error una política que apunta a un grupo
inexistente y simplemente no concede nada**: no hay mensaje, no hay fallo, el permiso
nunca llega.

⚠️ **Consecuencia de la migración:** toda política que siga escrita con el nombre viejo
—en cualquiera de sus dos mitades— hoy es un no-op silencioso, y el síntoma es idéntico
a "todavía no las aplicaron". No basta con crear las nuevas: hay que revisar las
existentes.

⚠️ **`MC_Internal_Apps` NO es un grupo, es el compartment raíz** (`MC_Internal_Apps` →
`AMALIA` → `AMALIA_DEV`). El parecido de nombres invita a confundirlos, y usarlo como
grupo cae justo en el no-op silencioso de arriba.

Con el grupo en `Default`, `Allow group 'MC_Internal_Developers' to ...` sin calificar
también funciona, porque OCI resuelve contra `Default` los nombres que no llevan dominio.
Aun así este documento califica siempre —`'Default'/'MC_Internal_Developers'`— para que
el statement diga a la vista en qué dominio está el grupo, y para que la próxima mudanza
de dominio se note al leerlo en vez de fallar en silencio.

**Los statements `in tenancy` exigen política en la raíz.** La política vive
adjunta a `AMALIA_DEV`; ahí los statements de compartment funcionan, pero cualquier
`in tenancy` es inválido. Si se conceden los bloques B o D, van en una política nueva
creada en el compartment raíz. Los del bloque A pueden quedarse donde están.
