#!/usr/bin/env bash
# find-orphans.sh — ¿Qué hay vivo en OCI que sea de AMALIA, y qué de eso no
#                   figura en el state file?
#
# Uso:
#   ./oci/scripts/find-orphans.sh <tenant>          # recursos de ESE tenant
#   ./oci/scripts/find-orphans.sh <tenant> --todos  # todo lo del proyecto
#   ./oci/scripts/find-orphans.sh <tenant> --json   # salida cruda, para pipes
#
# ── Para qué sirve ────────────────────────────────────────────────────────────
# Es la contracara de las etiquetas que `deploy.sh` pone desde la Fase 8. Antes,
# la única lista de "qué existe" era `oci/state/<tenant>.json` en la laptop; si
# se perdía, había que redescubrir todo a mano, tenant por tenant y servicio por
# servicio. Eso es literalmente lo que hacía `delete_amalia_multicomputos.sh`.
# Con `freeformTags` puestas, es UNA consulta al índice de búsqueda de OCI.
#
# Este script SOLO LEE. No borra ni modifica nada. Es seguro correrlo contra
# producción, y es lo primero que conviene mirar después de un `destroy.sh` que
# reportó fallos.
#
# ── Tres límites honestos ────────────────────────────────────────────────────
#  1. Solo encuentra lo ETIQUETADO. Los recursos creados antes de la Fase 8 no
#     tienen etiquetas y son invisibles acá: para esos, el state file (o la
#     consola) sigue siendo la fuente. Al re-desplegar un tenant, los recursos
#     que se creen de nuevo sí quedan etiquetados.
#  2. El índice de búsqueda de OCI tarda unos minutos en ver una etiqueta nueva.
#     Recién creado un recurso, puede no aparecer todavía.
#  3. No todo es un "resource" indexable. Los PAR (Pre-Authenticated Requests)
#     no lo son y tampoco admiten etiquetas: para esos, el state file es la
#     única pista, y por eso `destroy.sh` los revoca por nombre.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OCI_DIR="${OCI_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
source "${OCI_DIR}/lib/common.sh"

TENANT=""
ALCANCE="tenant"
FORMATO="tabla"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --todos) ALCANCE="proyecto" ;;
        --json)  FORMATO="json" ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) log_die "Opción desconocida: $1" ;;
        *)  TENANT=$1 ;;
    esac
    shift
done
[[ -n "$TENANT" ]] || { echo "Uso: $0 <tenant> [--todos] [--json]"; exit 1; }

require_cmd oci; require_cmd jq

export OCI_PROFILE
OCI_PROFILE=$(tenant_get "$TENANT" profile)

# Las etiquetas que el deploy pone. `project` es igual en los cuatro tenants;
# `tenant` es el que discrimina. Ver "Etiquetas" en oci/lib/common.sh.
PROYECTO=$(tenant_tags_json "$TENANT" | jq -r '.project // "tender-opportunity-tracker"')

if [[ "$ALCANCE" == "proyecto" ]]; then
    CONSULTA="query all resources where (freeformTags.key = 'project' && freeformTags.value = '${PROYECTO}')"
    QUE="todo el proyecto '${PROYECTO}'"
else
    CONSULTA="query all resources where (freeformTags.key = 'tenant' && freeformTags.value = '${TENANT}')"
    QUE="el tenant '${TENANT}'"
fi

log_step "Buscando recursos etiquetados para ${QUE} (perfil ${OCI_PROFILE})..."
RESULTADO=$(ociq search resource structured-search --query-text "$CONSULTA" 2>/dev/null) \
    || log_die "La búsqueda falló. ¿El perfil '${OCI_PROFILE}' tiene 'inspect all-resources in tenancy'?"

if [[ "$FORMATO" == "json" ]]; then
    echo "$RESULTADO"
    exit 0
fi

TOTAL=$(echo "$RESULTADO" | jq '[.data.items[]?] | length')
if [[ "$TOTAL" == "0" ]]; then
    log_warn "Cero recursos etiquetados."
    log_info "Eso puede querer decir dos cosas MUY distintas:"
    log_info "  a) no queda nada vivo — lo esperable después de un destroy limpio; o"
    log_info "  b) los recursos son anteriores a la Fase 8 y no tienen etiquetas."
    log_info "Para distinguirlas, mirá el state file o la consola:"
    log_info "  jq . ${OCI_DIR}/state/${TENANT}.json"
    exit 0
fi

echo ""
echo "$RESULTADO" | jq -r '
  .data.items
  | sort_by(."resource-type")
  | .[]
  | "  \(."resource-type"|.[0:22])\t\(."display-name" // "(sin nombre)")\t\(."lifecycle-state" // "-")"' \
  | column -t -s $'\t' 2>/dev/null || echo "$RESULTADO" | jq -r '.data.items[] | "  \(."resource-type")  \(."display-name")"'
echo ""
log_ok "${TOTAL} recurso(s) etiquetado(s) vivo(s)"

# ── El cruce que interesa: etiquetado pero NO en el state ─────────────────────
STATE=$(state_file "$TENANT")
if [[ ! -f "$STATE" ]]; then
    log_warn "No hay state local para '${TENANT}': todo lo de arriba es huérfano de hecho."
    log_info "Si el inventario está respaldado, traelo antes de decidir nada:"
    log_info "  source ${OCI_DIR}/lib/common.sh && source ${OCI_DIR}/lib/07-backup.sh"
    log_info "  state_restore ${TENANT}"
    exit 0
fi

# Todos los valores del state, como strings: el OCID de un recurso vivo tiene
# que estar entre ellos. Si no está, nada local lo conoce.
OCIDS=$(jq -c '[.[] | tostring]' "$STATE")
HUERFANOS=$(echo "$RESULTADO" | jq -r --argjson ocids "$OCIDS" '
  .data.items[]
  | select(($ocids | index(.identifier // "")) | not)
  | "  · \(."resource-type")  \(."display-name" // "(sin nombre)")\n      \(.identifier)"')

echo ""
if [[ -z "$HUERFANOS" ]]; then
    log_ok "Todos figuran en ${STATE##*/} — no hay huérfanos."
else
    log_warn "Etiquetados como de AMALIA pero AUSENTES del state file:"
    echo "$HUERFANOS"
    echo ""
    log_info "Un huérfano es un recurso que nada va a dar de baja solo. Revisalo y,"
    log_info "si corresponde, borralo a mano con su OCID."
fi
