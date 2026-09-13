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
