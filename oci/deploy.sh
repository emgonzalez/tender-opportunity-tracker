#!/usr/bin/env bash
# deploy.sh — Orquestador de despliegue OCI
#
# Uso: ./oci/deploy.sh <tenant>
# Ejemplo: ./oci/deploy.sh emiliogonzalez
#
# Pasos:
#   1. Network (VCN + subnet + IGW + sec list)
#   2. Compute (VM ARM A1 con cloud-init + cost-watchdog)
#   3. Budget (threshold + alert + dynamic group + policy)
#   4. App (rsync del repo + docker compose up)
set -euo pipefail

# ── Resolver paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OCI_DIR="$SCRIPT_DIR"
export REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Args ──────────────────────────────────────────────────────────────────────
TENANT="${1:-}"
if [[ -z "$TENANT" ]]; then
    echo "Uso: $0 <tenant>"
    echo "Tenants disponibles:"
    python3 -c "import yaml; [print(f'  - {t}') for t in yaml.safe_load(open('${OCI_DIR}/tenants.yaml'))['tenants']]"
    exit 1
fi

source "${OCI_DIR}/lib/common.sh"

# ── Pre-flight ────────────────────────────────────────────────────────────────
log_step "Pre-flight checks"
require_cmd oci
require_cmd jq
require_cmd python3
require_cmd rsync
require_cmd ssh
require_cmd ssh-keygen
require_cmd base64

[[ -f "${OCI_DIR}/tenants.yaml" ]] || log_die "Falta ${OCI_DIR}/tenants.yaml"
mkdir -p "${OCI_DIR}/state"

# Resolver perfil OCI
export OCI_PROFILE
OCI_PROFILE=$(tenant_get "$TENANT" profile)
[[ -n "$OCI_PROFILE" ]] || log_die "Tenant '$TENANT' no encontrado en tenants.yaml"
log_ok "Tenant: $TENANT (profile: $OCI_PROFILE)"

# Validar que el compartment_ocid no sea TBD
COMPARTMENT=$(tenant_get "$TENANT" compartment_ocid)
[[ "$COMPARTMENT" != "TBD" ]] || log_die "compartment_ocid es TBD para $TENANT — definilo en tenants.yaml antes de desplegar"

assert_tenancy "$TENANT"

# ── Confirmación final ────────────────────────────────────────────────────────
SHAPE=$(tenant_get "$TENANT" instance.shape)
# Shapes ".Flex" tienen ocpus/memory configurables; los fijos (E2.1.Micro, etc.) no.
if [[ "$SHAPE" == *.Flex ]]; then
    SHAPE_DESC="$SHAPE ($(tenant_get "$TENANT" instance.ocpus) OCPU, $(tenant_get "$TENANT" instance.memory_gb) GB)"
else
    SHAPE_DESC="$SHAPE (recursos fijos del shape)"
fi
echo ""
echo -e "${C_BOLD}Vas a desplegar en:${C_NC}"
echo "  Tenant:        $TENANT"
echo "  Region:        $(tenant_get "$TENANT" region)"
echo "  Shape:         $SHAPE_DESC"
echo "  Free Tier:     $(tenant_get "$TENANT" free_tier)"
echo "  Cost cap:      \$$(tenant_get "$TENANT" budget.threshold_usd) USD (auto-stop: $(tenant_get_or "$TENANT" budget.auto_stop "false"))"
echo ""
read -rp "  Continuar? [y/N] " confirm
[[ "$confirm" =~ ^[yY]$ ]] || { log_warn "Cancelado por el usuario"; exit 0; }

# ── Pipeline ──────────────────────────────────────────────────────────────────
source "${OCI_DIR}/lib/01-network.sh"
source "${OCI_DIR}/lib/02-compute.sh"
source "${OCI_DIR}/lib/03-budget.sh"
source "${OCI_DIR}/lib/04-app.sh"
# 07-backup.sh es inerte hasta que se lo llama; acá solo se usa `state_backup`.
source "${OCI_DIR}/lib/07-backup.sh"

network_create "$TENANT"
compute_create "$TENANT"
budget_setup   "$TENANT"
app_deploy     "$TENANT"

# ── Respaldo del inventario ───────────────────────────────────────────────────
# Se hace ACÁ y no en otro lado porque este es el único momento en que el state
# cambia: si el archivo con todos los OCIDs no sale de la laptop justo después
# de escribirse, no sale nunca. No es fatal — un despliegue que funcionó no se
# declara roto porque no se pudieron subir 1 kB — pero avisa fuerte.
state_backup "$TENANT" || log_warn "El state quedó SOLO en esta laptop: ${OCI_DIR}/state/${TENANT}.json"

# ── Resumen ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${C_GREEN}${C_BOLD}✓ Despliegue completo${C_NC}"
echo ""
echo "  Public IP:     $(state_get "$TENANT" public_ip)"
echo "  SSH:           ssh -i ${OCI_DIR}/state/${TENANT}_id_ed25519 ubuntu@$(state_get "$TENANT" public_ip)"
echo "  State file:    ${OCI_DIR}/state/${TENANT}.json"
echo ""
echo "  Logs en vivo:  ssh ... 'cd /opt/tender-opportunity-tracker && docker compose -f docker-compose.oci.yml logs -f'"
echo "  Watchdog log:  ssh ... 'tail -f /var/log/cost-watchdog.log'"
echo "  Destroy:       ${OCI_DIR}/destroy.sh $TENANT"
