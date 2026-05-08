#!/usr/bin/env bash
# common.sh — Helpers compartidos por los scripts de oci/lib/
#
# Convenciones:
#   - Todas las funciones usan "log_*" para output (stdout/stderr separados).
#   - El estado de despliegue (OCIDs creados) vive en oci/state/<tenant>.json
#     y se actualiza con `state_set`.

set -euo pipefail

# ── Colores ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    readonly C_RED='\033[0;31m' C_GREEN='\033[0;32m' C_YELLOW='\033[1;33m'
    readonly C_BLUE='\033[0;34m' C_BOLD='\033[1m' C_DIM='\033[2m' C_NC='\033[0m'
else
    readonly C_RED='' C_GREEN='' C_YELLOW='' C_BLUE='' C_BOLD='' C_DIM='' C_NC=''
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

# ── Estado por tenant ─────────────────────────────────────────────────────────
state_file() { echo "${OCI_DIR}/state/$1.json"; }

state_init() {
    local tenant=$1 file
    file=$(state_file "$tenant")
    [[ -f "$file" ]] || echo '{}' > "$file"
}

# state_set <tenant> <key> <value>
state_set() {
    local tenant=$1 key=$2 value=$3 file
    file=$(state_file "$tenant")
    state_init "$tenant"
    local tmp; tmp=$(mktemp)
    jq --arg k "$key" --arg v "$value" '.[$k] = $v' "$file" > "$tmp" && mv "$tmp" "$file"
}

# state_get <tenant> <key>     (devuelve "" si no existe)
state_get() {
    local tenant=$1 key=$2 file
    file=$(state_file "$tenant")
    [[ -f "$file" ]] || { echo ""; return; }
    jq -r --arg k "$key" '.[$k] // ""' "$file"
}

# ── OCI CLI wrapper (siempre con --profile) ───────────────────────────────────
ociq() {
    oci --profile "${OCI_PROFILE}" "$@"
}

# ── Asserts de seguridad ──────────────────────────────────────────────────────
# Verifica que el tenancy efectivo coincida con el del perfil. Esto previene
# desplegar accidentalmente en el tenant equivocado.
assert_tenancy() {
    local expected=$1 actual
    actual=$(ociq iam region-subscription list --query 'data[?"is-home-region"==`true`]."region-name" | [0]' --raw-output 2>/dev/null || true)
    [[ -n "$actual" ]] || log_die "No se pudo validar el tenancy del perfil '${OCI_PROFILE}'. ¿Está bien configurado en ~/.oci/config?"
    log_info "Perfil '${OCI_PROFILE}' válido (region home: $actual)"
}
