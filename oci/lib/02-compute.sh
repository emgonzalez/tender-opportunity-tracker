#!/usr/bin/env bash
# 02-compute.sh — Crea la instancia ARM Ampere A1 con cloud-init embebido
#
# Recursos:
#   - SSH key (genera un par dedicado en oci/state/<tenant>_id_ed25519 si no existe)
#   - Imagen Ubuntu 22.04 ARM (resuelta dinámicamente por shape compatibility)
#   - Instance VM.Standard.A1.Flex
#   - Reserved Public IP (Always Free, asociada al VNIC primary)
#
# Idempotente: si la instancia existe en state y está RUNNING, no la recrea.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

compute_create() {
    local tenant=$1
    local compartment shape ocpus memory_gb boot_size os os_version display
    compartment=$(tenant_get "$tenant" compartment_ocid)
    shape=$(tenant_get "$tenant" instance.shape)
    ocpus=$(tenant_get "$tenant" instance.ocpus)
    memory_gb=$(tenant_get "$tenant" instance.memory_gb)
    boot_size=$(tenant_get "$tenant" instance.boot_volume_size_gb)
    os=$(tenant_get "$tenant" instance.os)
    os_version=$(tenant_get "$tenant" instance.os_version)
    display=$(tenant_get "$tenant" instance.display_name)

    local subnet_id; subnet_id=$(state_get "$tenant" subnet_id)
    [[ -n "$subnet_id" ]] || log_die "subnet_id no en state — corré 01-network primero"

    # ── SSH keypair ───────────────────────────────────────────────────────────
    log_step "Verificando SSH keypair..."
    local ssh_key="${OCI_DIR}/state/${tenant}_id_ed25519"
    if [[ ! -f "$ssh_key" ]]; then
        ssh-keygen -t ed25519 -f "$ssh_key" -N "" -C "tot-${tenant}@$(hostname)" -q
        log_ok "Keypair generado: $ssh_key"
    else
        log_ok "Keypair ya existe: $ssh_key"
    fi
    local ssh_pub; ssh_pub=$(cat "${ssh_key}.pub")

    # ── Availability Domain ───────────────────────────────────────────────────
    log_step "Resolviendo Availability Domain..."
    local ad
    ad=$(ociq iam availability-domain list --compartment-id "$compartment" --query 'data[0].name' --raw-output)
    log_ok "AD: $ad"

    # ── Imagen Ubuntu ARM ─────────────────────────────────────────────────────
    log_step "Resolviendo imagen Ubuntu $os_version ARM compatible con $shape..."
    local image_id
    image_id=$(ociq compute image list \
        --compartment-id "$compartment" \
        --operating-system "$os" \
        --operating-system-version "$os_version" \
        --shape "$shape" \
        --sort-by TIMECREATED --sort-order DESC \
        --query 'data[0].id' --raw-output)
    [[ -n "$image_id" && "$image_id" != "null" ]] || log_die "No hay imagen $os $os_version compatible con $shape en este compartment"
    log_ok "Imagen: $image_id"

    # ── Cloud-init renderizado ────────────────────────────────────────────────
    log_step "Renderizando cloud-init..."
    local cloud_init_b64
    cloud_init_b64=$(TENANT="$tenant" "${OCI_DIR}/lib/render-cloud-init.sh" | base64 | tr -d '\n')
    log_ok "cloud-init listo ($(echo "$cloud_init_b64" | wc -c | tr -d ' ') bytes b64)"

    # ── Instancia ─────────────────────────────────────────────────────────────
    log_step "Creando instancia..."
    local inst_id; inst_id=$(state_get "$tenant" instance_id)
    if [[ -n "$inst_id" ]] && ociq compute instance get --instance-id "$inst_id" --query 'data."lifecycle-state"' --raw-output 2>/dev/null | grep -q RUNNING; then
        log_ok "Instancia ya RUNNING: $inst_id"
    else
        inst_id=$(ociq compute instance launch \
            --availability-domain "$ad" \
            --compartment-id "$compartment" \
            --display-name "$display" \
            --shape "$shape" \
            --shape-config "{\"ocpus\":$ocpus,\"memoryInGBs\":$memory_gb}" \
            --image-id "$image_id" \
            --boot-volume-size-in-gbs "$boot_size" \
            --subnet-id "$subnet_id" \
            --assign-public-ip true \
            --metadata "{\"ssh_authorized_keys\":\"$ssh_pub\",\"user_data\":\"$cloud_init_b64\"}" \
            --wait-for-state RUNNING \
            --query 'data.id' --raw-output)
        state_set "$tenant" instance_id "$inst_id"
        log_ok "Instancia creada: $inst_id"
    fi

    # ── Public IP ─────────────────────────────────────────────────────────────
    log_step "Obteniendo IP pública..."
    local vnic_id public_ip
    vnic_id=$(ociq compute instance list-vnics --instance-id "$inst_id" --query 'data[0].id' --raw-output)
    public_ip=$(ociq network vnic get --vnic-id "$vnic_id" --query 'data."public-ip"' --raw-output)
    state_set "$tenant" public_ip "$public_ip"
    state_set "$tenant" vnic_id "$vnic_id"
    log_ok "Public IP: $public_ip"
}
