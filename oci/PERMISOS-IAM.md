# Permisos IAM requeridos para desplegar

Qué necesita el grupo del operador para correr `deploy.sh` de punta a punta, derivado
llamada por llamada de los scripts de esta carpeta. El pipeline y sus etapas están en
[`README.md`](README.md); acá solo viven los permisos.

> Escrito contra el tenant **multicomputos** (Landing Zone corporativo, compartment
> `AMALIA_DEV`, grupo `'Default'/'MC_Internal_Developers'`). Para un tenant personal
> donde el usuario es admin de la tenancy, nada de esto hace falta.

---

## Estado verificado el 2026-09-09

> **Causa raíz de todo el bloqueo: la API key del CLI pertenecía a un usuario del dominio
> viejo.** No faltaban políticas. El perfil `MULTICOMPUTOS` de `~/.oci/config` autenticaba
> como `…oorjhl4q…`, un usuario de `OracleIdentityCloudService`, mientras que los permisos
> estaban concedidos al grupo `'Default'/'MC_Internal_Developers'`, cuyo miembro es otro
> usuario (`…aeckjuua…`). Generar una API key sobre el usuario de `Default` y apuntar el
> perfil a ella abrió todo. **No hizo falta pedirle nada al administrador del Landing Zone.**

Auditado con la identidad nueva (solo lectura, más sondas de `create` con parámetros
inválidos por construcción que no crearon nada):

| Bloque | Estado en `AMALIA_DEV` |
|---|---|
| **A** — virtual-network-family, instance-family, volume-family | ✅ `manage` confirmado |
| **C** — autonomous-database-family, object-family, secret-family | ✅ |
| **C** — generative-ai-family (Fase 9, `LLM_PROVIDER=oci`) | ✅ verificado el 2026-09-19 con una llamada real |
| **B** — `in tenancy`: budgets, ons-family, dynamic-groups, inspect users/groups, policies | ❌ denegado |

`manage` se probó con un `create` de CIDR imposible: devuelve **400 InvalidParameter**
—IAM autorizó, falló la validación— frente al **404 NotAuthorizedOrNotFound** de antes.

El acceso alcanza toda la rama `MC_Internal_Apps` (incluidos `AMALIA`, `AMALIA_DEV` y
`AMALIA_PROD`) más `Apps_shared_services`, `Servicios_MC`, `nagios-poc`, `zabbix-poc` y los
compartments personales. **Se perdió el acceso a `Development`, `Dev-InternalAppsMC`,
`Dev-POCs` y `Dev-Sandbox`**, que dependían del grupo del dominio viejo: son dos conjuntos
de permisos disjuntos, uno por identidad.

### El Bloque B sigue faltando — y no bloquea

Ninguno de sus statements es necesario para desplegar `multicomputos_dev`:

- **budgets y ons-family** — `budget.enabled: false` en `tenants.yaml`, y `03-budget.sh:19`
  hace `return 0` si no está en `True`. El Landing Zone ya provee su propio budget.
- **dynamic-groups** — `05-adb.sh` avisa y continúa si no puede crearlo (línea ~303). La app
  se conecta a la ADB con usuario y contraseña sobre TLS, no por instance principal.
- **inspect users/groups y policies** — solo comodidad para verificar; nada del pipeline los usa.

Pedirlos tiene sentido únicamente si más adelante se quiere el auto-stop por presupuesto
propio o el acceso a la ADB por instance principal.

## Qué etapa consume qué

| # | Etapa | Familia de permisos |
|---|---|---|
| 1 | `network_create` | `virtual-network-family` |
| 2 | `compute_create` | `instance-family` + `volume-family` |
| 3 | `budget_setup` | `ons-family`, `budgets`, `dynamic-groups` |
| 4 | `app_deploy` | **ninguno** — es rsync/scp/docker sobre SSH |
| 5 | `state_backup` (respaldo del inventario, Fase 8) | `object-family` en el compartment |
| — | `destroy.sh` | las mismas familias que se usaron para crear, más `autonomous-database-family` y `object-family` si el tenant los tiene |
| — | `scripts/find-orphans.sh` | `inspect all-resources in tenancy` (solo lectura) |

La ADB de Track B **no** está en este pipeline: `lib/05-adb.sh` se invoca a mano y sus
permisos son el bloque C.

---

## Bloque A — núcleo obligatorio

Ya concedido y verificado: con estos tres statements se desplegó producción el
2026-06-26. Confirmar que siguen vigentes tras cualquier cambio en el tenant.

```
Allow group 'Default'/'MC_Internal_Developers' to manage virtual-network-family in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
Allow group 'Default'/'MC_Internal_Developers' to manage instance-family        in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
Allow group 'Default'/'MC_Internal_Developers' to manage volume-family          in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
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
| `bv boot-volume update` (etiquetar el boot volume) | `lib/02-compute.sh` | volume-family |
| `compute instance terminate` | `destroy.sh` | instance-family |
| `bv boot-volume get / delete` | `destroy.sh` | volume-family |

**Las etiquetas no piden permisos nuevos.** Desde la Fase 8 cada `create` lleva
`--freeform-tags`, pero eso es un campo del mismo `create`: si el `create` está permitido,
la etiqueta va. La única llamada verdaderamente nueva es el `bv boot-volume update` que
etiqueta el boot volume (OCI no le propaga las etiquetas de la instancia), y está dentro de
`volume-family`, que ya estaba concedida. Si aun así fallara, el deploy **avisa y sigue**:
un boot volume sin etiqueta es un problema del día que haya que buscarlo, no del despliegue.

---

### Dos permisos que la Fase 8 hace deseables (ninguno bloquea el deploy)

**`object-family` — respaldo del inventario.** `deploy.sh` termina subiendo
`oci/state/<tenant>.json` (~1 kB de OCIDs, sin secretos) al bucket del tenant, para que
deje de existir en una sola copia. Es el mismo `manage object-family` que ya pide el
bloque C para el modelo ONNX; si el tenant no lo tiene, el deploy **avisa y sigue**, y el
state queda solo en la laptop — que es exactamente el riesgo que esto viene a cerrar.

```
Allow group 'Default'/'MC_Internal_Developers' to manage object-family in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
```

**`inspect all-resources` — búsqueda de huérfanos.** `oci/scripts/find-orphans.sh` usa el
índice de búsqueda de OCI para listar todo lo etiquetado como de AMALIA y cruzarlo contra
el state. Es **solo lectura** y no participa de ningún despliegue, pero sin este statement
la consulta devuelve vacío y el script no puede distinguir *«no quedó nada»* de *«no puedo
ver nada»* (por eso lo dice explícitamente cuando el resultado es cero).

```
Allow group 'Default'/'MC_Internal_Developers' to inspect all-resources in tenancy
```

Termina en `in tenancy`, así que aplica la misma trampa del bloque B: la política tiene que
vivir en la **raíz**. Un alcance por compartment (`in compartment …:AMALIA_DEV`) sirve para
buscar dentro de ese compartment, que es el caso normal; el `in tenancy` hace falta para
encontrar lo que se creó en el root —topic, budget, dynamic groups, policies— que es
justamente donde un huérfano pasa más desapercibido.

---

## Bloque B — lo que hoy falla

Todos terminan en `in tenancy`, y por eso la política **tiene que vivir en la raíz**.
Un compartment hijo no puede otorgar permisos de tenancy, así que hoy
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
Allow group 'Default'/'MC_Internal_Developers' to manage policies       in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
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

Ambos flags están en `false` para producción, así que para **producción** hoy no hace falta
conceder nada de este bloque. ⚠️ El tenant `multicomputos_dev` (→ `AMALIA_DEV`) los tiene
**ambos en `true`**: para desplegarlo, este bloque sí es obligatorio.

### `ENABLE_MEMORIA` → Autonomous Database

Crear la ADB Always Free, configurar su ACL de red y desactivar mTLS para TLS one-way.
Requiere además agregar un bloque `adb:` al tenant en `tenants.yaml`.

```
Allow group 'Default'/'MC_Internal_Developers' to manage autonomous-database-family in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
```

Llamadas: `db autonomous-database create` (`lib/05-adb.sh:169`), `list` (`:143`), `get`
(×8), `update` para ACL y mTLS.

**Opcional — password de ADMIN desde Vault.** Si se define `adb.admin_password_secret`,
el script lee la contraseña de un secret en vez de generarla e imprimirla en pantalla
(`lib/05-adb.sh:160`). Recomendable en un tenant corporativo.

```
Allow group 'Default'/'MC_Internal_Developers' to read secret-bundles in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
```

### `ENABLE_MEMORIA` → Object Storage, solo para cargar el modelo ONNX

Una ADB serverless no expone filesystem, así que el `.onnx` de embeddings llega por Object
Storage: se sube a un bucket propio, se le genera un PAR de lectura y la base lo copia a
`DATA_PUMP_DIR` con `DBMS_CLOUD.GET_OBJECT`. El procedimiento está en
[`db/load_onnx.sql`](../db/load_onnx.sql), sección 1.

**El permiso lo necesita el operador, no la base.** `GET_OBJECT` se invoca con
`credential_name => NULL`: el PAR autentica por sí solo y la ADB nunca presenta
credenciales de OCI. Lo que requiere permiso es crear el bucket, subir el objeto y generar
el PAR.

```
Allow group 'Default'/'MC_Internal_Developers' to manage object-family in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
```

Llamadas: `os bucket create`, `os object put`, `os preauth-request create`. **No salen de
ningún script** — a diferencia del resto de este documento, el procedimiento es manual
(`db/load_onnx.sql` paso 1c). El `os ns get` que también hace falta ya funciona sin
política adicional.

Es de **una sola vez**: cargado el modelo, queda dentro de la BD y el bucket puede borrarse.

💡 **Se puede evitar del todo.** Un PAR es una URL HTTPS firmada, y a la ADB le da igual en
qué compartment —o incluso en qué tenant— vive el bucket. Si ya tenés `object-family` en
otro lado (por ejemplo un tenant personal), subí ahí el ONNX y no pidas este bloque: ahorra
una ronda con el administrador del Landing Zone.

### `ENABLE_DRILLDOWN` → ningún permiso de OCI

El drill-down descarga PDFs de COMPRASAL y llama al API del proveedor de LLM desde dentro
del contenedor. Con `LLM_PROVIDER=anthropic` —el default de los cuatro tenants— no toca
ningún servicio de Oracle. Con `oci`, ver la sección siguiente.

### `LLM_PROVIDER=oci` → Generative AI · **ya concedido, no hay nada que pedir**

```
Allow group 'Default'/'MC_Internal_Developers' to use generative-ai-family in compartment MC_Internal_Apps:AMALIA:AMALIA_DEV
```

Este statement está acá **como documentación de qué permiso se está ejerciendo**, no como
un pedido al administrador del Landing Zone. **Verificado el 2026-09-19** con una llamada
real —no una sonda de solo lectura—: un `chat` contra
`meta.llama-4-scout-17b-16e-instruct` en `AMALIA_DEV`
(`ocid1.compartment.oc1..aaaaaaaanhp32bzkqu4u2g626rtlsbtczsv65cbf6bkztpvgawxmwdco32pa`)
con el perfil `MULTICOMPUTOS` **respondió**, en 0,7–0,9 s. El permiso ya estaba concedido
sin que nadie lo pidiera.

Lo mismo vale para la VM: la app **no** usa Instance Principal para esto —`dynamic-groups`
está denegado, ver Bloque B— sino la misma API key del operador, que `lib/04-app.sh` le
inyecta en el `.env` desde `oci/state/<tenant>_genai.pem`. O sea: el permiso que ejerce la
VM es exactamente el que se verificó arriba, no uno nuevo.

> ⚠️ **Esto no es Always Free.** GenAI on-demand se cobra por transacción y el tenant no
> tiene ninguna guardia automática: `budget.enabled: false` y `auto_stop: false` en
> `multicomputos_dev`, y el `cost-watchdog.sh` de la VM falla por permisos y se descarta
> con `|| true` (ver la nota del Bloque D). La única guardia real es `DAILY_BUDGET_USD`,
> dentro de la app.

#### `OCI_FAMILIA` no pide permisos nuevos, pero decide adónde va el pliego

El statement de arriba cubre **las tres familias**: `use generative-ai-family` no distingue
entre `meta.*`, `xai.*` y `google.*`. Desde el lado de IAM no hay nada que pedir al cambiar
`OCI_FAMILIA`. La razón para documentarlo acá igual es que **el permiso no es la parte
sensible**: lo es dónde termina el contenido del pliego.

| `OCI_FAMILIA` | Modelo | ¿El pliego se queda dentro de OCI? |
|---|---|---|
| `nativo` (default) | `meta.llama-4-scout` / `-maverick` | **Sí** — Oracle sirve la inferencia |
| `xai` | `xai.grok-4.6` | **No** — sale a servidores de xAI |
| `google` | `google.gemini-2.5-flash` | **No** — sale a servidores de Google |

OCI *expone* los modelos de xAI y de Google, pero no los *corre*: la inferencia sale por
llamadas externas al tercero. Con esas dos familias, documentos de licitación de clientes
—que AMALIA descarga de COMPRASAL y no le pertenecen— pasan por un cuarto actor que
**ningún contrato vigente contempla**. Era el motivo del veto original; hoy es configurable,
pero con `nativo` en los cuatro tenants y un WARNING al arrancar que nombra al tercero.

Que el modelo sea "de OCI" en la consola no implica que el dato se quede en OCI. Es la misma
clase de supuesto que la trampa del 404 de más abajo: la etiqueta del servicio no describe
por dónde viaja la llamada.

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

## Dónde vive el OCID de la security list

`oci/scripts/ssh-allow.sh` —el que restringe el puerto 22 al `/32` del operador—
necesita el OCID de la security list del tenant. **No está incrustado en el
script a propósito:** un OCID hardcodeado es la forma más fácil de escribirle a
la red equivocada el día que se recrea una VCN. Sale de donde lo dejó el deploy:

```bash
jq -r .security_list_id oci/state/<tenant>.json
```

Ese archivo está gitignoreado (`oci/state/*`), así que el valor **no se versiona**.
Si desplegaste desde otra máquina y no lo tenés, se recupera por API:

```bash
VCN=$(oci --profile MULTICOMPUTOS network vcn list --compartment-id $DEV_OCID \
        --query 'data[?"display-name"==`tot-multicomputos_dev-vcn`].id | [0]' --raw-output)
oci --profile MULTICOMPUTOS network vcn get --vcn-id "$VCN" \
    --query 'data."default-security-list-id"' --raw-output
```

o se le pasa a mano: `oci/scripts/ssh-allow.sh <tenant> --security-list-id ocid1.securitylist...`

**Permiso necesario:** ninguno nuevo. `network security-list get/update` ya está
cubierto por `manage virtual-network-family` del **Bloque A** — el mismo permiso
que usa `lib/01-network.sh` para crear la regla. Esto importa por una razón
operativa: **la regla de SSH se edita por el API de OCI, no por dentro de la VM.**
Aunque el `/32` quede mal y el 22 deje de responder, mientras el perfil de
`~/.oci/config` funcione se arregla volviendo a correr el script. No hay forma de
quedarse afuera para siempre.

**Cuidado con las security lists compartidas:** `emiliogonzalez_e2` reusa la red de
`emiliogonzalez` (`network.share_from`), así que ambos `state/*.json` apuntan al
MISMO OCID y una escritura cambia el SSH de las dos VMs. El script lo detecta
—compara los OCID de todos los tenants del state— y pide confirmación explícita.

---

## Fuera del alcance de IAM

Para no conceder de más, esto es lo que el sistema hace sin tocar el API de OCI:

- **Despliegue de la app** — `rsync`, `scp` y `docker compose` por SSH contra la IP
  pública. Solo requiere el puerto 22, restringido al `/32` del operador (ver
  arriba). Cerrar el 22 del todo rompería el despliegue: `lib/04-app.sh` lo usa
  para sincronizar el repo, copiar el `.env`, hacer `docker login` y verificar el
  sello. La corrida diaria, en cambio, no depende de SSH.
- **Salida a internet** — COMPRASAL, el API del proveedor de LLM y SMTP salen por el
  Internet Gateway. Sin NAT gateway ni service gateway. Con `LLM_PROVIDER=oci` la
  inferencia también sale por ahí: el endpoint de GenAI está en **otra región**
  (`us-chicago-1`) y se alcanza por HTTPS público, no por la red interna de OCI.
  Con `OCI_FAMILIA=xai` o `google` la llamada además **no termina en Oracle**: sigue
  hasta xAI o Google (ver la tabla del Bloque C).
- **Credenciales de la app** — COMPRASAL, la del LLM (sea Anthropic o la API key de OCI)
  y SMTP viajan en el `.env` que `app_deploy` copia por `scp`. No usan OCI Vault.

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
oci --profile MULTICOMPUTOS os bucket list -ns $(oci --profile MULTICOMPUTOS os ns get --raw-output --query data) \
                                           --compartment-id $DEV_OCID
```

---

## Cinco trampas que ya costaron tiempo

**La API key puede pertenecer a un usuario de OTRO dominio — y es invisible desde el CLI.**
Esta costó una tarde entera. Al migrar el tenant a Identity Domains, el usuario de la
Consola pasó a `Default` con un OCID **nuevo**, pero `~/.oci/config` siguió apuntando al
usuario viejo de `OracleIdentityCloudService`, que aún existía y aún pertenecía a su grupo
viejo. Resultado: el CLI funcionaba —tenía `manage` completo en los compartments `Dev-*`—
pero era, literalmente, otra persona que la de la Consola.

El síntoma engaña: uno mira los permisos en la Consola, los ve correctos, y el CLI insiste
en `NotAuthorizedOrNotFound`. La conclusión natural —"faltan políticas, hay que pedirle al
administrador"— es la equivocada.

Cómo descartarlo en dos minutos, sin permisos especiales: comparar el `user` de
`~/.oci/config` con el OCID que muestra **My profile** en la Consola. Si difieren, el
arreglo no es IAM: es generar una API key sobre el usuario correcto. Señal delatora de que
son identidades distintas: los permisos que el CLI sí tiene y los que la Consola muestra
son **conjuntos disjuntos**, no uno subconjunto del otro.

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

**Los statements `in tenancy` exigen política en la raíz.** Un compartment hijo no puede
otorgar permisos de tenancy. Como los bloques B y D los necesitan, la política de este
documento va entera en la raíz — y de ahí sale la tercera trampa.

**Desde la raíz, el nombre pelado del compartment no resuelve.** `AMALIA_DEV` está tres
niveles abajo (`MC_Internal_Apps` → `AMALIA` → `AMALIA_DEV`), y OCI resuelve nombres sin
ruta solo contra hijos directos del compartment donde está adjunta la política. Escrito
`in compartment AMALIA_DEV` en una política de la raíz, no lo encuentra. Por eso todos los
statements de este documento llevan la ruta completa
`MC_Internal_Apps:AMALIA:AMALIA_DEV`, o en su defecto `in compartment id <OCID>`.

Esto es lo que distingue el caso de `AMALIA_DEV` del de `Dev-InternalAppsMC`, que ya
funciona: ese está a solo dos niveles y bajo `Development`, cubierto por la política que
ya existe.

**GenAI en una región sin el servicio devuelve 404 del MODELO, no un error de región.**
Ésta costó una hora el 2026-09-19 y no es de IAM, pero se disfraza de IAM: contra
`us-ashburn-1` —la región donde viven las VMs y la que figura en `tenants.yaml`— **todos**
los modelos devolvían `404 Entity with key <modelo> not found`. El mensaje nombra el
modelo, así que las dos hipótesis naturales son "escribí mal el ID" y
"`NotAuthorizedOrNotFound`, me falta `use generative-ai-family`". Las dos son falsas.

La causa real es que **OCI Generative AI no sirve inferencia en Ashburn**. La tenancy
`multicomputos` está suscrita a Chicago, Frankfurt, Londres y Phoenix; con
`us-chicago-1` la misma llamada, con el mismo ID y el mismo perfil, funciona. ⚠️ La
tenancy `emiliogonzalez` **solo tiene Ashburn**: ahí GenAI no se puede usar sin suscribir
otra región primero.

Cómo distinguirlo en un minuto, sin tocar permisos:

```bash
# Catálogo: es GLOBAL y responde igual desde cualquier región → no prueba nada
oci --profile MULTICOMPUTOS generative-ai model-collection list-models \
    --compartment-id $DEV_OCID --region us-ashburn-1

# Inferencia: falla para TODOS los modelos si la región no tiene el servicio
oci --profile MULTICOMPUTOS generative-ai-inference chat-result --region us-ashburn-1 ...
```

Si el `list-models` anda y el `chat-result` falla **para todos los modelos por igual**, no
es el ID ni el permiso: es la región. Un problema de permisos no discrimina por región, y
un ID mal escrito no rompe los otros modelos.
