#!/usr/bin/env bash
# 01-network.sh — Crea VCN + subnet pública + Internet Gateway + Security List
#
# Idempotente: si los recursos ya existen (registrados en state), los reusa.
# Recursos creados:
#   - VCN
#   - Internet Gateway
#   - Route Table (default, modificada para enviar 0.0.0.0/0 al IGW)
#   - Subnet pública
#   - Security List (default) — permite SSH desde ssh_ingress_cidr y todo egress
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

network_create() {
    local tenant=$1 compartment vcn_cidr subnet_cidr ssh_cidr
    compartment=$(tenant_get "$tenant" compartment_ocid)
    vcn_cidr=$(tenant_get "$tenant" network.vcn_cidr)
    subnet_cidr=$(tenant_get "$tenant" network.subnet_cidr)
    ssh_cidr=$(tenant_get "$tenant" network.ssh_ingress_cidr)
    local display="tot-${tenant}"
    # OCI dnsLabel: max 15 chars, alfanumérico — truncamos el tenant a 12 chars
    local clean="${tenant//[^a-z0-9]/}"
    local dns_prefix="${clean:0:12}"

    # ── VCN ───────────────────────────────────────────────────────────────────
    log_step "Creando VCN..."
    local vcn_id; vcn_id=$(state_get "$tenant" vcn_id)
    if [[ -n "$vcn_id" ]] && ociq network vcn get --vcn-id "$vcn_id" &>/dev/null; then
        log_ok "VCN ya existe: $vcn_id"
    else
        vcn_id=$(ociq network vcn create \
            --compartment-id "$compartment" \
            --cidr-block "$vcn_cidr" \
            --display-name "${display}-vcn" \
            --dns-label "${dns_prefix}vcn" \
            --wait-for-state AVAILABLE \
            --query 'data.id' --raw-output)
        state_set "$tenant" vcn_id "$vcn_id"
        log_ok "VCN creada: $vcn_id"
    fi

    # ── Internet Gateway ──────────────────────────────────────────────────────
    log_step "Creando Internet Gateway..."
    local igw_id; igw_id=$(state_get "$tenant" igw_id)
    if [[ -n "$igw_id" ]] && ociq network internet-gateway get --ig-id "$igw_id" &>/dev/null; then
        log_ok "IGW ya existe: $igw_id"
    else
        igw_id=$(ociq network internet-gateway create \
            --compartment-id "$compartment" \
            --vcn-id "$vcn_id" \
            --is-enabled true \
            --display-name "${display}-igw" \
            --wait-for-state AVAILABLE \
            --query 'data.id' --raw-output)
        state_set "$tenant" igw_id "$igw_id"
        log_ok "IGW creado: $igw_id"
    fi

    # ── Route table: rota 0.0.0.0/0 al IGW ────────────────────────────────────
    log_step "Configurando Route Table..."
    local rt_id
    rt_id=$(ociq network vcn get --vcn-id "$vcn_id" --query 'data."default-route-table-id"' --raw-output)
    ociq network route-table update \
        --rt-id "$rt_id" \
        --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$igw_id\"}]" \
        --force --wait-for-state AVAILABLE >/dev/null
    state_set "$tenant" route_table_id "$rt_id"
    log_ok "Route Table configurada"

    # ── Security List: SSH inbound + all egress ───────────────────────────────
    log_step "Configurando Security List..."
    local sl_id
    sl_id=$(ociq network vcn get --vcn-id "$vcn_id" --query 'data."default-security-list-id"' --raw-output)
    ociq network security-list update --security-list-id "$sl_id" \
        --ingress-security-rules "[{
            \"protocol\":\"6\",
            \"source\":\"$ssh_cidr\",
            \"sourceType\":\"CIDR_BLOCK\",
            \"isStateless\":false,
            \"tcpOptions\":{\"destinationPortRange\":{\"min\":22,\"max\":22}}
        }]" \
        --egress-security-rules '[{
            "protocol":"all",
            "destination":"0.0.0.0/0",
            "destinationType":"CIDR_BLOCK",
            "isStateless":false
        }]' \
        --force --wait-for-state AVAILABLE >/dev/null
    state_set "$tenant" security_list_id "$sl_id"
    log_ok "Security List configurada (SSH desde $ssh_cidr)"

    # ── Subnet pública ────────────────────────────────────────────────────────
    log_step "Creando Subnet pública..."
    local subnet_id; subnet_id=$(state_get "$tenant" subnet_id)
    if [[ -n "$subnet_id" ]] && ociq network subnet get --subnet-id "$subnet_id" &>/dev/null; then
        log_ok "Subnet ya existe: $subnet_id"
    else
        subnet_id=$(ociq network subnet create \
            --compartment-id "$compartment" \
            --vcn-id "$vcn_id" \
            --cidr-block "$subnet_cidr" \
            --display-name "${display}-subnet" \
            --dns-label "${dns_prefix}sub" \
            --prohibit-public-ip-on-vnic false \
            --wait-for-state AVAILABLE \
            --query 'data.id' --raw-output)
        state_set "$tenant" subnet_id "$subnet_id"
        log_ok "Subnet creada: $subnet_id"
    fi
}
