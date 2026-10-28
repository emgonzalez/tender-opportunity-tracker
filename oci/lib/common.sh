#!/usr/bin/env bash
# common.sh — Helpers compartidos por los scripts de oci/lib/
#
# Convenciones:
#   - Todas las funciones usan "log_*" para output (stdout/stderr separados).
#   - El estado de despliegue (OCIDs creados) vive en oci/state/<tenant>.json
#     y se actualiza con `state_set`.

set -euo pipefail

# Guard: este archivo se sourcea desde múltiples lib/ — solo definir una vez
[[ -n "${_TOT_COMMON_LOADED:-}" ]] && return 0
_TOT_COMMON_LOADED=1

# ── Colores ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[1;33m'
    C_BLUE=$'\033[0;34m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_NC=$'\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_BOLD=''; C_DIM=''; C_NC=''
fi

log_step() { echo -e "\n${C_BLUE}${C_BOLD}▶ $*${C_NC}"; }
log_ok()   { echo -e "  ${C_GREEN}✓${C_NC}  $*"; }
log_warn() { echo -e "  ${C_YELLOW}⚠${C_NC}  $*"; }
log_info() { echo -e "  ${C_DIM}·${C_NC}  $*"; }
log_die()  { echo -e "  ${C_RED}✗${C_NC}  $*" >&2; exit 1; }

# ── Dependencias ──────────────────────────────────────────────────────────────
require_cmd() {
    command -v "$1" &>/dev/null || log_die "Falta el comando '$1'. Instalalo y reintentá."
}

# ── Lectura de tenants.yaml (sin yq, usando python3 que ya está en macOS) ─────
# Uso: tenant_get <tenant> <jq-style.path>     ej: tenant_get emiliogonzalez instance.shape
tenant_get() {
    local tenant=$1 path=$2
    python3 -c "
import sys, yaml
with open('${OCI_DIR}/tenants.yaml') as f:
    data = yaml.safe_load(f)
node = data['tenants']['${tenant}']
for part in '${path}'.split('.'):
    node = node[part]
print(node)
" 2>/dev/null || log_die "No se pudo leer tenants.yaml#${tenant}.${path}"
}

# Variante con default — no muere si la key no existe.
# Uso: tenant_get_or <tenant> <path> <default>
tenant_get_or() {
    local tenant=$1 path=$2 default=$3
    python3 -c "
import yaml
with open('${OCI_DIR}/tenants.yaml') as f:
    data = yaml.safe_load(f)
node = data['tenants']['${tenant}']
for part in '${path}'.split('.'):
    if isinstance(node, dict) and part in node:
        node = node[part]
    else:
        print('''${default}''')
        raise SystemExit
print(node if node is not None else '''${default}''')
" 2>/dev/null || echo "$default"
}

# ── Etiquetas (freeform tags) ─────────────────────────────────────────────────
# `tenants.yaml` declara un bloque `tags:` por tenant desde el primer día. Hasta
# la Fase 8 ningún script lo leía, y esa omisión tenía un costo concreto: la
# única forma de saber qué recursos de OCI son de AMALIA era el state file de la
# laptop. Si se perdía, había que redescubrir todo a mano — que es literalmente
# lo que hizo `delete_amalia_multicomputos.sh`, con OCIDs hardcodeados.
#
# Con las etiquetas puestas, encontrar un recurso pasa a ser una consulta:
#   oci --profile <p> search resource structured-search --query-text \
#     "query all resources where (freeformTags.key = 'project' && \
#      freeformTags.value = 'tender-opportunity-tracker')"
# Eso es lo que hace oci/scripts/find-orphans.sh.
#
# Se agrega SIEMPRE la clave `tenant`: los cuatro tenants comparten el mismo
# `project`, y varios recursos (topic, budget, dynamic group, policy) viven en
# el root tenancy, donde conviven los de todos. Sin `tenant` la consulta dice
# "esto es de AMALIA" pero no "de cuál"; con ella, el borrado selectivo es
# posible. Un `tags.tenant` explícito en tenants.yaml gana sobre el automático.

# tenant_tags_json <tenant> → objeto JSON listo para --freeform-tags
tenant_tags_json() {
    local tenant=$1
    python3 - "$tenant" <<'PY'
import json, os, sys
import yaml

tenant = sys.argv[1]
with open(os.environ["OCI_DIR"] + "/tenants.yaml") as f:
    data = yaml.safe_load(f) or {}
node = (data.get("tenants") or {}).get(tenant) or {}
tags = node.get("tags") or {}
# OCI exige strings en ambos lados; YAML puede devolver bool/int/None.
out = {str(k): ("" if v is None else str(v)) for k, v in tags.items()}
out.setdefault("tenant", tenant)
print(json.dumps(out, sort_keys=True))
PY
}

# tenant_tag_args <tenant> — deja los flags en el array global OCI_TAG_ARGS.
# Se usa así:  ociq network vcn create ... "${OCI_TAG_ARGS[@]}"
# El array NUNCA queda vacío (siempre hay al menos la clave `tenant`), que es
# lo que permite expandirlo bajo `set -u` en bash 3.2 sin ceremonia.
tenant_tag_args() {
    OCI_TAG_ARGS=(--freeform-tags "$(tenant_tags_json "$1")")
}

# ── Estado por tenant ─────────────────────────────────────────────────────────
# El state es lo único que dice qué recursos son de este tenant. Un deploy que
# falla NUNCA debe dejarlo peor de como lo encontró (incidente 2026-10-27: sin red,
# el script creó una VCN nueva y le pisó el `vcn_id`). Por eso:
#   · se valida antes de usarlo y antes de cada escritura;
#   · se escribe de forma atómica (temporal en el MISMO directorio + mv);
#   · un id ya guardado NO se reemplaza por otro distinto salvo DEPLOY_RECREAR (ver abajo);
#   · antes del deploy se deja una copia (`state_snapshot`) y se rota.
state_file() { echo "${OCI_DIR}/state/$1.json"; }

# _recrear_permitido <clave>: DEPLOY_RECREAR=1 habilita recrear/reemplazar CUALQUIER id;
# DEPLOY_RECREAR=vcn_id,igw_id solo los nombrados. Acotarlo evita que, para recrear una
# IP reservada, se habilite de paso el reemplazo silencioso de la VCN o de la instancia.
_recrear_permitido() { [[ "${DEPLOY_RECREAR:-}" == 1 || ",${DEPLOY_RECREAR:-}," == *",$1,"* ]]; }

state_init() {
    local tenant=$1 file
    file=$(state_file "$tenant")
    [[ -f "$file" ]] || echo '{}' > "$file"
}

# state_validar <tenant>: el archivo, si existe, debe ser un objeto JSON no vacío.
# Un archivo vacío haría que el siguiente deploy «recree todo» sin avisar.
state_validar() {
    local tenant=$1 file
    file=$(state_file "$tenant")
    [[ -f "$file" ]] || return 0
    if [[ ! -s "$file" ]] || ! jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
        log_die "El state ${file} está vacío o corrupto. No se despliega nada. Restaurá una copia (cp sin -p: cp ${file}.pre-deploy-<fecha> ${file}) o traé el respaldo con state_restore (lib/07-backup.sh). Ojo: state_restore se niega a pisar un archivo que ya existe (aunque esté corrupto) salvo STATE_RESTORE_FORCE=1."
    fi
}

# state_exigir_previo <tenant>: un state sin `instance_id` (ausente, `{}`, o sin ids) hace que el
# deploy cree TODO de nuevo. Si el tenant ya existe en OCI eso duplica VCN, VM, IP… sin avisar.
# Solo se acepta a propósito: DEPLOY_PRIMERA_VEZ=1.
state_exigir_previo() {
    local tenant=$1 file
    file=$(state_file "$tenant")
    if [[ -n "$(state_get "$tenant" instance_id)" ]]; then
        return 0
    fi
    if [[ "${DEPLOY_PRIMERA_VEZ:-}" == 1 ]]; then
        log_warn "DEPLOY_PRIMERA_VEZ=1: el state de '${tenant}' no tiene instancia; se despliega desde cero."
        return 0
    fi
    log_die "El state ${file} no tiene instance_id (no existe, está en {} o le faltan los ids). Desplegar así crearía TODO de nuevo y, si '${tenant}' ya existe en OCI, lo duplicaría.
     · Si el tenant ya existe: restaurá el state (state_restore en lib/07-backup.sh, o cp sin -p desde ${file}.pre-deploy-<fecha>) o buscá sus recursos con oci/scripts/find-orphans.sh.
     · Si es de verdad el primer despliegue: DEPLOY_PRIMERA_VEZ=1 ./oci/deploy.sh ${tenant}"
}

# ¿Esta clave guarda el identificador de un recurso de OCI? (no se reemplaza a la ligera)
_state_clave_es_id() { [[ "$1" == *_id || "$1" == *_ocid ]]; }

# state_set <tenant> <key> <value>
state_set() {
    local tenant=$1 key=$2 value=$3 file actual tmp
    file=$(state_file "$tenant")
    state_init "$tenant"
    state_validar "$tenant"
    if _state_clave_es_id "$key"; then
        actual=$(jq -r --arg k "$key" '.[$k] // ""' "$file")
        if [[ -n "$actual" && "$actual" != "$value" ]] && ! _recrear_permitido "$key"; then
            log_die "state_set: '${key}' ya apunta a ${actual}; se negó a reemplazarlo por ${value}. Si es a propósito, exportá DEPLOY_RECREAR=${key}."
        fi
    fi
    tmp=$(mktemp "${file}.XXXXXX")
    if jq --arg k "$key" --arg v "$value" '.[$k] = $v' "$file" > "$tmp" \
        && jq -e 'type == "object"' "$tmp" >/dev/null 2>&1; then
        mv "$tmp" "$file"
    else
        rm -f "$tmp"
        log_die "state_set: no se pudo escribir '${key}' en ${file} (el state quedó intacto)."
    fi
}

# state_get <tenant> <key>     (devuelve "" si no existe)
state_get() {
    local tenant=$1 key=$2 file
    file=$(state_file "$tenant")
    [[ -f "$file" ]] || { echo ""; return; }
    jq -r --arg k "$key" '.[$k] // ""' "$file"
}

# state_snapshot <tenant>: copia previa al deploy (`<tenant>.json.pre-deploy-<ts>-<pid>`); conserva
# las 5 últimas. Se ordena por NOMBRE (la fecha va en él), no por mtime: con `cp -p` la copia heredaba
# el mtime del state, quedaba «la más vieja» y la rotación borraba justo la recién hecha.
state_snapshot() {
    local tenant=$1 file snap
    file=$(state_file "$tenant")
    [[ -s "$file" ]] || return 0
    snap="${file}.pre-deploy-$(date +%Y%m%d-%H%M%S)-$$"
    cp "$file" "$snap"
    DEPLOY_SNAPSHOT="$snap"
    # shellcheck disable=SC2012
    ls -1 "${file}".pre-deploy-* 2>/dev/null | sort -r | tail -n +6 | while read -r viejo; do
        [[ "$viejo" == "$snap" ]] || rm -f "$viejo"
    done
    log_info "Copia del state: ${snap}"
}

# ── Lock: un solo deploy por tenant ───────────────────────────────────────────
# El lock es un symlink cuyo destino es el PID: `ln -s` crea y «escribe el PID» en una sola
# operación atómica (con `mkdir` + archivo `pid` había una ventana en la que el lock existía sin
# PID y otro deploy lo daba por huérfano). Un lock cuyo PID ya no existe se recupera solo; uno con
# PID vivo detiene el deploy. Se acepta todavía el formato viejo (directorio con `pid` adentro).
_DEPLOY_LOCK_DIR=""

# _deploy_lock_pid <ruta> → PID dueño del lock (symlink nuevo o directorio viejo); "" si no hay.
_deploy_lock_pid() {
    if [[ -L "$1" ]]; then readlink "$1" 2>/dev/null || true
    elif [[ -d "$1" ]]; then cat "$1/pid" 2>/dev/null || true
    fi
}

deploy_lock() {
    local tenant=$1 dir pid
    dir="$(state_file "$tenant").lock"
    # Con un directorio (formato viejo) en esa ruta, `ln -s` crearía el link ADENTRO y «tendría
    # éxito»: por eso solo se intenta si la ruta no es un directorio real.
    if [[ -d "$dir" && ! -L "$dir" ]] || ! ln -s "$$" "$dir" 2>/dev/null; then
        pid=$(_deploy_lock_pid "$dir")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log_die "Ya hay un deploy de '${tenant}' en curso (PID ${pid}). Dos a la vez pueden pisarse el state.
     Si ese PID NO es un deploy.sh (el sistema reusó el número tras un corte), borrá el lock: rm -rf '${dir}'"
        fi
        log_warn "Lock huérfano de un deploy anterior (PID ${pid:-?}): se recupera."
        rm -rf "$dir"
        ln -s "$$" "$dir" 2>/dev/null || log_die "No se pudo tomar el lock ${dir} (¿otro deploy lo tomó en este instante?)."
    fi
    _DEPLOY_LOCK_DIR=$dir
}

deploy_unlock() {
    [[ -n "$_DEPLOY_LOCK_DIR" ]] || return 0
    # Solo se suelta si sigue siendo nuestro: nunca se borra el lock de otro deploy.
    [[ "$(_deploy_lock_pid "$_DEPLOY_LOCK_DIR")" == "$$" ]] && rm -rf "$_DEPLOY_LOCK_DIR"
    _DEPLOY_LOCK_DIR=""
}

# ── Salida ordenada: una sola ruta para EXIT/INT/TERM/HUP ─────────────────────
# `trap ... EXIT` REEMPLAZA al anterior, y el deploy ya tiene uno (el que reactiva los
# timers, lib/04-app.sh). Para que convivan, quien quiera limpiar al salir se registra
# con `deploy_registrar_salida <función>` y `_deploy_salida` las llama todas.
_DEPLOY_AL_SALIR=()

deploy_registrar_salida() {
    local f
    for f in "${_DEPLOY_AL_SALIR[@]+"${_DEPLOY_AL_SALIR[@]}"}"; do
        [[ "$f" == "$1" ]] && return 0
    done
    _DEPLOY_AL_SALIR+=("$1")
}

_deploy_salida() {
    local rc=$? f i
    # Ignorar (no «restaurar por defecto») las señales mientras se limpia: un segundo Ctrl-C no debe
    # matar la restauración de los timers a medias.
    trap '' INT TERM HUP
    # El código de salida llega a cada handler en DEPLOY_RC: leerlo de `$?` obligaría a fabricarlo con
    # `(exit N)`, que bajo `set -e` cortaría el handler antes de empezar.
    DEPLOY_RC=$rc
    # Al revés del orden de registro: el handler registrado primero (suelta el lock) corre al final,
    # cuando ya se restauraron los timers por SSH.
    for (( i=${#_DEPLOY_AL_SALIR[@]}-1; i>=0; i-- )); do
        f=${_DEPLOY_AL_SALIR[$i]}
        "$f" || true
    done
    return "$rc"
}

deploy_instalar_trap() {
    trap _deploy_salida EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
}

# Lo que se dice al salir de un deploy que NO terminó bien.
_deploy_informar_estado() {
    local rc=${DEPLOY_RC:-$?}
    deploy_unlock
    [[ $rc -eq 0 ]] && return 0
    echo -e "\n  ${C_YELLOW}⚠${C_NC}  El deploy terminó con error (código ${rc})." >&2
    if [[ -n "${DEPLOY_SNAPSHOT:-}" ]]; then
        echo -e "     Copia del state previa al deploy: ${DEPLOY_SNAPSHOT}" >&2
        echo -e "     Si el state quedó mal: cp '${DEPLOY_SNAPSHOT}' <state>.json  (o state_restore, lib/07-backup.sh)." >&2
    fi
    echo -e "     Si falló justo al crear un recurso, pudo haberse creado: revisá oci/scripts/find-orphans.sh." >&2
    return 0
}

# ── OCI CLI wrapper (siempre con --profile) ───────────────────────────────────
# Timeouts explícitos: sin ellos una red caída cuelga el deploy o tarda minutos en fallar.
# `--read-timeout` es por lectura de socket, no por operación: no corta subidas largas.
ociq() {
    oci --profile "${OCI_PROFILE}" --connection-timeout "${OCI_CONNECT_TIMEOUT:-20}" \
        --read-timeout "${OCI_READ_TIMEOUT:-120}" --max-retries "${OCI_MAX_RETRIES:-2}" "$@"
}

# ── «¿Existe?» con TRES respuestas, no dos ────────────────────────────────────
# El anti-patrón que causó el incidente del 2026-10-27:
#     [[ -n "$id" ]] && ociq network vcn get --vcn-id "$id" &>/dev/null || <crear>
# convierte CUALQUIER fallo (red caída, 5xx, 429, credenciales) en «no existe» y crea un
# duplicado. `oci_existe` distingue:
#     0  existe
#     1  no existe: OCI respondió 404 de forma definitiva
#     2  indeterminado: no se pudo confirmar (red, 5xx, 429, error desconocido)
# y NUNCA toma «indeterminado» como «no existe». Un 401 (credenciales) aborta enseguida.
# Ojo: OCI también contesta 404 cuando falta un permiso; por eso `oci_resolver` no recrea
# a ciegas un recurso que el state ya conoce.
_RE_OCI_AUTH="['\"]?status['\"]?: *401|NotAuthenticated"
_RE_OCI_404="['\"]?status['\"]?: *404|NotAuthorizedOrNotFound|ObjectNotFound|BucketNotFound"
_RE_OCI_TRANSITORIO="ConnectTimeout|ConnectionError|ConnectionResetError|RequestException|Max retries|timed out|Name or service not known|nodename nor servname|Temporary failure|Network is unreachable|SSLError|Remote end closed|EOF occurred|['\"]?status['\"]?: *(429|5[0-9][0-9])"

# oci_clasificar_error <archivo con el stderr> → auth | 404 | transitorio | otro
oci_clasificar_error() {
    if grep -qiE "$_RE_OCI_AUTH" "$1"; then echo auth
    elif grep -qiE "$_RE_OCI_404" "$1"; then echo 404
    elif grep -qiE "$_RE_OCI_TRANSITORIO" "$1"; then echo transitorio
    else echo otro
    fi
}

OCI_SALIDA=""   # stdout de la última llamada que tuvo éxito en `oci_existe`

# oci_existe <descripción> <args de oci…>   → 0 existe · 1 no existe (404) · 2 indeterminado
# Reintenta lo transitorio (DEPLOY_REINTENTOS veces, esperas de 1×, 3×, 6× DEPLOY_ESPERA s).
oci_existe() {
    local desc=$1; shift
    local max=${DEPLOY_REINTENTOS:-3} base=${DEPLOY_ESPERA:-5} intento=1 clase espera out err
    out=$(mktemp); err=$(mktemp)
    OCI_SALIDA=""
    while :; do
        if ociq "$@" >"$out" 2>"$err"; then
            OCI_SALIDA=$(cat "$out"); rm -f "$out" "$err"; return 0
        fi
        clase=$(oci_clasificar_error "$err")
        case "$clase" in
            auth)
                sed 's/^/        /' "$err" | tail -4 >&2
                rm -f "$out" "$err"
                log_die "${desc}: OCI rechazó las credenciales (401). Revisá ~/.oci/config y la clave del perfil '${OCI_PROFILE}'. No se tocó nada."
                ;;
            404)
                rm -f "$out" "$err"; return 1 ;;
        esac
        if (( intento >= max )); then
            log_warn "${desc}: no pude confirmarlo tras ${max} intento(s) [${clase}]." >&2
            sed 's/^/        /' "$err" | tail -4 >&2
            rm -f "$out" "$err"; return 2
        fi
        case $intento in 1) espera=$base ;; 2) espera=$((base * 3)) ;; *) espera=$((base * 6)) ;; esac
        log_warn "${desc}: fallo ${clase} (intento ${intento}/${max}); reintento en ${espera}s…" >&2
        sleep "$espera"
        intento=$((intento + 1))
    done
}

# oci_listar <descripción> <args de oci…>  → imprime la salida. Un error NUNCA es «lista vacía»: aborta.
oci_listar() {
    local desc=$1; shift
    local rc=0
    oci_existe "$desc" "$@" || rc=$?
    case $rc in
        0) printf '%s' "$OCI_SALIDA" ;;
        1) log_die "${desc}: OCI respondió 404 al consultar. Revisá permisos y compartment; no se crea nada." ;;
        *) log_die "${desc}: no pude consultar OCI (sin red o error no reconocido; el detalle está arriba). No se crea nada: volvé a correr cuando haya conexión." ;;
    esac
}

# oci_crear <tenant> <clave-del-state> <descripción> <args de oci… create …>
#   Crea un recurso y GUARDA su id aunque la espera (`--wait-for-state`) falle: el CLI, si la espera
#   falla, escribe el último estado conocido —el id, por el `--query data.id`— y sale con error. Antes
#   `set -e` abortaba ANTES del `state_set`, y la próxima corrida creaba otro recurso (huérfano + copia).
#   El comando debe terminar en `--query data.id --raw-output`. Deja el id en OCI_CREADO.
OCI_CREADO=""
OCI_RESOLVER_NOTA=""   # pista opcional que quien llama añade al aborto de `oci_resolver` por un 404
oci_crear() {
    local tenant=$1 key=$2 desc=$3; shift 3
    local out rc=0
    OCI_CREADO=""
    out=$(ociq "$@") || rc=$?
    out=$(printf '%s' "$out" | tail -n 1)
    if [[ "$out" == ocid1.* ]]; then
        state_set "$tenant" "$key" "$out"
    fi
    if (( rc != 0 )); then
        if [[ "$out" == ocid1.* ]]; then
            log_die "${desc}: el create devolvió código ${rc} pero el recurso EXISTE (${out}) y quedó en el state. Volvé a correr el deploy: lo reutiliza y espera a que esté listo."
        fi
        log_die "${desc}: el create falló (código ${rc}) y no devolvió ningún id; el detalle está arriba. Si pudo haberse creado, revisá oci/scripts/find-orphans.sh antes de reintentar."
    fi
    [[ "$out" == ocid1.* ]] || log_die "${desc}: el create terminó bien pero no devolvió un id (recibí '${out}')."
    OCI_CREADO=$out
}

# oci_resolver <tenant> <clave-del-state> <descripción> <args de oci… con @ID@ donde va el id>
#   0  el recurso del state existe → reutilizarlo
#   1  el state no tiene id (o DEPLOY_RECREAR=1 / =<clave> tras un 404) → hay que crearlo
#   —  aborta si no se puede confirmar, o si OCI dice 404 sobre un id que el state ya conoce
oci_resolver() {
    local tenant=$1 key=$2 desc=$3; shift 3
    local id a rc=0
    local -a args=()
    # Dentro de un `if` (cómo se llama esta función) `set -e` no actúa: sin este `||` un `jq` roto
    # dejaría el id vacío y se «crearía» un recurso que ya existe.
    id=$(state_get "$tenant" "$key") || log_die "${desc}: no pude leer '${key}' del state."
    [[ -n "$id" ]] || return 1
    for a in "$@"; do args+=("${a//@ID@/$id}"); done
    oci_existe "$desc" "${args[@]}" || rc=$?
    case $rc in
        0) return 0 ;;
        1)
            if _recrear_permitido "$key"; then
                log_warn "${desc}: ${id} ya no existe (404); DEPLOY_RECREAR=${DEPLOY_RECREAR} → se recreará." >&2
                return 1
            fi
            log_die "${desc}: el state apunta a ${id} pero OCI responde 404 (¿se borró a mano, o falta un permiso?). No se crea nada. Si es a propósito, exportá DEPLOY_RECREAR=${key} (solo este recurso). ${OCI_RESOLVER_NOTA:-}" ;;
        *)
            log_die "${desc}: no pude confirmar que ${id} exista (¿sin red?). No se crea nada: volvé a correr cuando haya conexión." ;;
    esac
}

# ── Asserts de seguridad ──────────────────────────────────────────────────────
# Pre-flight: OCI responde y las credenciales sirven. Corre ANTES de tocar nada, y distingue
# «sin red» de «credenciales malas» (el viejo `|| true` los confundía con «perfil mal configurado»).
assert_tenancy() {
    local rc=0 actual
    oci_existe "Conectividad con OCI (perfil '${OCI_PROFILE}')" iam region-subscription list \
        --query 'data[?"is-home-region"==`true`]."region-name" | [0]' --raw-output || rc=$?
    case $rc in
        0) ;;
        1) log_die "OCI respondió 404 al validar el perfil '${OCI_PROFILE}'. ¿Está bien configurado en ~/.oci/config?" ;;
        *) log_die "Sin conexión con OCI (reintentos agotados). No se tocó nada: volvé a correr cuando haya internet." ;;
    esac
    actual=$OCI_SALIDA
    [[ -n "$actual" ]] || log_die "No se pudo validar el tenancy del perfil '${OCI_PROFILE}'. ¿Está bien configurado en ~/.oci/config?"
    log_info "Perfil '${OCI_PROFILE}' válido (region home: $actual)"
}
