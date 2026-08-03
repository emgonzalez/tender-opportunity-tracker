#!/usr/bin/env bash
# 01-network.sh — Crea VCN + subnet pública + Internet Gateway + Security List
#
# Idempotente: si los recursos ya existen (registrados en state), los reusa.
#
# Modo compartido (network.share_from: <otro-tenant>):
#   Reusa VCN/IGW/Route Table/Security List del otro tenant (útil cuando
#   Free Tier limita VCNs por region). Solo crea una subnet propia con
#   su CIDR (que debe estar dentro del VCN CIDR del tenant compartido).
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

network_create() {
    local tenant=$1 compartment subnet_cidr ssh_cidr share_from
    compartment=$(tenant_get "$tenant" compartment_ocid)
    subnet_cidr=$(tenant_get "$tenant" network.subnet_cidr)
    ssh_cidr=$(tenant_get "$tenant" network.ssh_ingress_cidr)
    share_from=$(tenant_get_or "$tenant" network.share_from "")
    local display="tot-${tenant}"

    # ── Fase 2 · Track B (B6.4) — campos opt-in de mínimo privilegio ─────────
    # network.ssh_cidr: si el tenant lo define, SOBREESCRIBE ssh_ingress_cidr
    # (ej. "190.x.x.x/32" en vez de 0.0.0.0/0). Si NO lo define, se usa
    # ssh_ingress_cidr tal como ya se leía arriba — comportamiento IDÉNTICO al
    # actual (todos los tenants hoy tienen ssh_ingress_cidr: 0.0.0.0/0).
    local ssh_cidr_override
    ssh_cidr_override=$(tenant_get_or "$tenant" network.ssh_cidr "")
    if [[ -n "$ssh_cidr_override" ]]; then
        ssh_cidr="$ssh_cidr_override"
    fi

    # network.restrict_egress: default false → egress "all/0.0.0.0/0" actual,
    # sin cambios. Si es true, reemplaza el egress abierto por el mínimo
    # necesario para que AMALIA funcione: 443 (COMPRASAL, api.anthropic.com,
    # endpoints OCI como Object Storage/ADB/Vault), 1522 (ADB mTLS) y DNS
    # (53/udp). Nada más sale de la VM.
    #
    # ⚠️  ADVERTENCIA MUY VISIBLE ⚠️
    # Restringir egress puede romper `apt update`/`apt upgrade` (paquetes via
    # HTTP/HTTPS a mirrors de Ubuntu, puertos 80/443 pero a hosts arbitrarios
    # no cubiertos por "0.0.0.0/0:443" si el mirror usa otro puerto) y los
    # `docker pull` (Docker Hub/GHCR también por 443, pero resolución DNS y
    # rutas pueden variar). El bloque de 443 abierto a 0.0.0.0/0 cubre la
    # mayoría de estos casos porque HTTPS es HTTPS sin importar el destino,
    # pero SIEMPRE probá primero en un tenant de prueba (nunca directo en
    # producción) y verificá `cloud-init` completo + `docker compose pull`
    # antes de aplicar esto a un tenant con una VM viva.
    local restrict_egress
    restrict_egress=$(tenant_get_or "$tenant" network.restrict_egress "false")

    # dnsLabel: prefer un valor explícito de tenants.yaml; si no, derivarlo
    # del nombre del tenant (max 15 chars total, 12 prefix + 3 suffix).
    local dns_prefix
    dns_prefix=$(tenant_get_or "$tenant" network.dns_prefix "")
    if [[ -z "$dns_prefix" ]]; then
        local clean="${tenant//[^a-z0-9]/}"
        dns_prefix="${clean:0:12}"
    fi

    local vcn_id

    if [[ -n "$share_from" ]]; then
        # ── Modo compartido: reusar VCN/IGW/RT/SL del otro tenant ─────────────
        log_step "Modo compartido — reutilizando network de tenant '$share_from'"
        local src_vcn src_igw src_rt src_sl
        src_vcn=$(state_get "$share_from" vcn_id)
        src_igw=$(state_get "$share_from" igw_id)
        src_rt=$(state_get  "$share_from" route_table_id)
        src_sl=$(state_get  "$share_from" security_list_id)
        [[ -n "$src_vcn" ]] || log_die "El tenant '$share_from' no tiene vcn_id en state — desplegalo primero o eliminá 'share_from'"
        ociq network vcn get --vcn-id "$src_vcn" &>/dev/null \
            || log_die "VCN del tenant '$share_from' ya no existe en OCI ($src_vcn)"

        state_set "$tenant" vcn_id "$src_vcn"
        state_set "$tenant" igw_id "$src_igw"
        state_set "$tenant" route_table_id "$src_rt"
        state_set "$tenant" security_list_id "$src_sl"
        state_set "$tenant" network_shared_from "$share_from"
        log_ok "VCN compartida:           $src_vcn"
        log_ok "IGW compartido:           $src_igw"
        log_ok "Route Table compartida:   $src_rt"
        log_ok "Security List compartida: $src_sl"

        vcn_id="$src_vcn"
    else
        # ── VCN ───────────────────────────────────────────────────────────────
        local vcn_cidr
        vcn_cidr=$(tenant_get "$tenant" network.vcn_cidr)

        log_step "Creando VCN..."
        vcn_id=$(state_get "$tenant" vcn_id)
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

        # ── Internet Gateway ──────────────────────────────────────────────────
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

        # ── Route table: rota 0.0.0.0/0 al IGW ────────────────────────────────
        log_step "Configurando Route Table..."
        local rt_id
        rt_id=$(ociq network vcn get --vcn-id "$vcn_id" --query 'data."default-route-table-id"' --raw-output)
        ociq network route-table update \
            --rt-id "$rt_id" \
            --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$igw_id\"}]" \
            --force --wait-for-state AVAILABLE >/dev/null
        state_set "$tenant" route_table_id "$rt_id"
        log_ok "Route Table configurada"

        # ── Security List: SSH inbound + egress ───────────────────────────────
        log_step "Configurando Security List..."
        local sl_id egress_rules
        sl_id=$(ociq network vcn get --vcn-id "$vcn_id" --query 'data."default-security-list-id"' --raw-output)

        if [[ "$restrict_egress" == "True" ]]; then
            # Mínimo privilegio: 443 (HTTPS: COMPRASAL, Anthropic, OCI), 1522
            # (ADB mTLS) y 53/udp (DNS). Ver advertencia arriba antes de aplicar.
            egress_rules='[
                {
                    "protocol":"6",
                    "destination":"0.0.0.0/0",
                    "destinationType":"CIDR_BLOCK",
                    "isStateless":false,
                    "tcpOptions":{"destinationPortRange":{"min":443,"max":443}}
                },
                {
                    "protocol":"6",
                    "destination":"0.0.0.0/0",
                    "destinationType":"CIDR_BLOCK",
                    "isStateless":false,
                    "tcpOptions":{"destinationPortRange":{"min":1522,"max":1522}}
                },
                {
                    "protocol":"17",
                    "destination":"0.0.0.0/0",
                    "destinationType":"CIDR_BLOCK",
                    "isStateless":false,
                    "udpOptions":{"destinationPortRange":{"min":53,"max":53}}
                }
            ]'
        else
            # Default actual: egress abierto sin restricciones.
            egress_rules='[{
                "protocol":"all",
                "destination":"0.0.0.0/0",
                "destinationType":"CIDR_BLOCK",
                "isStateless":false
            }]'
        fi

        ociq network security-list update --security-list-id "$sl_id" \
            --ingress-security-rules "[{
                \"protocol\":\"6\",
                \"source\":\"$ssh_cidr\",
                \"sourceType\":\"CIDR_BLOCK\",
                \"isStateless\":false,
                \"tcpOptions\":{\"destinationPortRange\":{\"min\":22,\"max\":22}}
            }]" \
            --egress-security-rules "$egress_rules" \
            --force --wait-for-state AVAILABLE >/dev/null
        state_set "$tenant" security_list_id "$sl_id"
        log_ok "Security List configurada (SSH desde $ssh_cidr; egress $([[ "$restrict_egress" == "True" ]] && echo "restringido a 443/1522/53" || echo "abierto"))"
    fi

    # ── Subnet pública (siempre se crea, sea modo solo o compartido) ─────────
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
