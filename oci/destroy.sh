#!/usr/bin/env bash
# destroy.sh — Termina TODOS los recursos del tenant en OCI
#
# Orden inverso: app -> compute -> budget -> network -> identity
# Lee OCIDs de oci/state/<tenant>.json
#
# CUIDADO: irreversible. Pide doble confirmación.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OCI_DIR="$SCRIPT_DIR"
source "${OCI_DIR}/lib/common.sh"

TENANT="${1:-}"
[[ -n "$TENANT" ]] || { echo "Uso: $0 <tenant>"; exit 1; }

export OCI_PROFILE
OCI_PROFILE=$(tenant_get "$TENANT" profile)
STATE=$(state_file "$TENANT")
[[ -f "$STATE" ]] || log_die "No hay state para $TENANT — ¿desplegaste alguna vez?"

echo -e "${C_RED}${C_BOLD}DESTROY $TENANT${C_NC} — esto va a eliminar:"
jq -r 'to_entries | .[] | "  - \(.key): \(.value)"' "$STATE"
echo ""
read -rp "Escribí el nombre del tenant para confirmar: " confirm
[[ "$confirm" == "$TENANT" ]] || { log_warn "Cancelado"; exit 0; }

g() { state_get "$TENANT" "$1"; }
del() { ociq "$@" --force >/dev/null 2>&1 || true; }

# Compute
[[ -n "$(g instance_id)" ]] && { log_step "Terminando instancia..."; del compute instance terminate --instance-id "$(g instance_id)" --preserve-boot-volume false --wait-for-state TERMINATED; log_ok "Instancia terminada"; }

# Budget + alerts + topic
[[ -n "$(g budget_id)" ]] && { log_step "Eliminando budget..."; del budgets budget delete --budget-id "$(g budget_id)"; log_ok "Budget eliminado"; }
[[ -n "$(g topic_id)"  ]] && { log_step "Eliminando topic..."; del ons topic delete --topic-id "$(g topic_id)"; log_ok "Topic eliminado"; }

# Identity
[[ -n "$(g policy_id)"        ]] && { log_step "Eliminando policy..."; del iam policy delete --policy-id "$(g policy_id)"; log_ok "Policy eliminada"; }
[[ -n "$(g dynamic_group_id)" ]] && { log_step "Eliminando dynamic group..."; del iam dynamic-group delete --dynamic-group-id "$(g dynamic_group_id)"; log_ok "Dynamic Group eliminado"; }

# Network
SHARED_FROM=$(g network_shared_from)
[[ -n "$(g subnet_id)" ]] && { log_step "Eliminando subnet..."; del network subnet delete --subnet-id "$(g subnet_id)" --wait-for-state TERMINATED; log_ok "Subnet eliminada"; }

if [[ -n "$SHARED_FROM" ]]; then
    log_warn "VCN/IGW/Route Table/Security List son del tenant '$SHARED_FROM' — no se eliminan acá"
    log_info "Para borrarlos: ./oci/destroy.sh $SHARED_FROM (cuando ya no haya otros tenants usándolos)"
else
    [[ -n "$(g route_table_id)" ]] && {
        log_step "Limpiando route rules..."
        ociq network route-table update --rt-id "$(g route_table_id)" --route-rules '[]' --force --wait-for-state AVAILABLE >/dev/null 2>&1 || true
        log_ok "Route rules limpiadas"
    }
    [[ -n "$(g igw_id)" ]] && { log_step "Eliminando IGW..."; del network internet-gateway delete --ig-id "$(g igw_id)" --wait-for-state TERMINATED; log_ok "IGW eliminado"; }
    [[ -n "$(g vcn_id)" ]] && { log_step "Eliminando VCN..."; del network vcn delete --vcn-id "$(g vcn_id)" --wait-for-state TERMINATED; log_ok "VCN eliminada"; }
fi

# Local cleanup
log_step "Limpiando state local..."
rm -f "$STATE" "${OCI_DIR}/state/${TENANT}_id_ed25519" "${OCI_DIR}/state/${TENANT}_id_ed25519.pub"
log_ok "State eliminado"

echo -e "\n${C_GREEN}${C_BOLD}✓ Tenant $TENANT limpio${C_NC}"
