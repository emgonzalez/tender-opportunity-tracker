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
    local compartment shape boot_size os os_version display
    compartment=$(tenant_get "$tenant" compartment_ocid)
    shape=$(tenant_get "$tenant" instance.shape)
    boot_size=$(tenant_get "$tenant" instance.boot_volume_size_gb)
    os=$(tenant_get "$tenant" instance.os)
    os_version=$(tenant_get "$tenant" instance.os_version)
    display=$(tenant_get "$tenant" instance.display_name)

    # Shapes ".Flex" requieren --shape-config con ocpus/memoryInGBs.
    # Shapes fijos (E2.1.Micro) NO lo aceptan — los recursos están hardcodeados.
    local shape_config_arg=""
    if [[ "$shape" == *.Flex ]]; then
        local ocpus memory_gb
        ocpus=$(tenant_get "$tenant" instance.ocpus)
        memory_gb=$(tenant_get "$tenant" instance.memory_gb)
        shape_config_arg="{\"ocpus\":$ocpus,\"memoryInGBs\":$memory_gb}"
    fi

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

    # ── Availability Domains (lista para fallover) ───────────────────────────
    log_step "Resolviendo Availability Domains..."
    local ad_list_json
    ad_list_json=$(ociq iam availability-domain list --compartment-id "$compartment" --query 'data[*].name' --raw-output)
    local ad_list=()
    while IFS= read -r _ad_line; do
        ad_list+=("$_ad_line")
    done < <(echo "$ad_list_json" | jq -r '.[]')
    [[ ${#ad_list[@]} -gt 0 ]] || log_die "No hay ADs disponibles"
    log_ok "ADs disponibles: ${ad_list[*]}"
    local ad="${ad_list[0]}"  # usado por backup-policy lookup más abajo

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

    # ── Instancia (con fallover entre ADs si "Out of host capacity") ─────────
    log_step "Creando instancia..."
    local inst_id; inst_id=$(state_get "$tenant" instance_id)
    if [[ -n "$inst_id" ]] && ociq compute instance get --instance-id "$inst_id" --query 'data."lifecycle-state"' --raw-output 2>/dev/null | grep -q RUNNING; then
        log_ok "Instancia ya RUNNING: $inst_id"
    else
        local launch_err="" success=0
        for try_ad in "${ad_list[@]}"; do
            log_info "Intentando launch en $try_ad..."
            launch_err=$(mktemp)
            local launch_args=(
                --availability-domain "$try_ad"
                --compartment-id "$compartment"
                --display-name "$display"
                --shape "$shape"
                --image-id "$image_id"
                --boot-volume-size-in-gbs "$boot_size"
                --subnet-id "$subnet_id"
                --assign-public-ip true
                --metadata "{\"ssh_authorized_keys\":\"$ssh_pub\",\"user_data\":\"$cloud_init_b64\"}"
                --wait-for-state RUNNING
                --query 'data.id' --raw-output
            )
            [[ -n "$shape_config_arg" ]] && launch_args+=(--shape-config "$shape_config_arg")

            if inst_id=$(ociq compute instance launch "${launch_args[@]}" 2>"$launch_err"); then
                ad="$try_ad"
                state_set "$tenant" instance_id "$inst_id"
                state_set "$tenant" availability_domain "$ad"
                log_ok "Instancia creada en $ad: $inst_id"
                success=1
                rm -f "$launch_err"
                break
            else
                if grep -q "Out of host capacity" "$launch_err"; then
                    log_warn "$try_ad sin capacidad — probando siguiente AD"
                else
                    log_warn "Launch en $try_ad falló por otro motivo:"
                    cat "$launch_err" >&2
                fi
                rm -f "$launch_err"
            fi
        done
        [[ $success -eq 1 ]] || log_die "Todos los ADs (${ad_list[*]}) están sin capacidad para $shape. Reintentá en unos minutos/horas — Oracle libera capacidad Free Tier esporádicamente. Network ya está creada (sin costo); el reintento solo crea la VM."
    fi

    # ── Public IP ─────────────────────────────────────────────────────────────
    log_step "Obteniendo IP pública..."
    local vnic_id public_ip
    vnic_id=$(ociq compute instance list-vnics --instance-id "$inst_id" --query 'data[0].id' --raw-output)
    public_ip=$(ociq network vnic get --vnic-id "$vnic_id" --query 'data."public-ip"' --raw-output)
    state_set "$tenant" public_ip "$public_ip"
    state_set "$tenant" vnic_id "$vnic_id"
    log_ok "Public IP: $public_ip"

    # ── Desasignar backup policy automática del boot volume ───────────────────
    # Oracle puede asignar una "Bronze" backup policy por default que toma
    # snapshots periódicos. Esos snapshots consumen storage adicional fuera del
    # boot volume y podrían exceder los 200 GB de Always Free con el tiempo.
    log_step "Verificando backup policy del boot volume..."
    local boot_vol_id assignment_id
    boot_vol_id=$(ociq compute boot-volume-attachment list \
        --availability-domain "$ad" \
        --compartment-id "$compartment" \
        --instance-id "$inst_id" \
        --query 'data[0]."boot-volume-id"' --raw-output 2>/dev/null || echo "")
    if [[ -n "$boot_vol_id" && "$boot_vol_id" != "null" ]]; then
        state_set "$tenant" boot_volume_id "$boot_vol_id"
        assignment_id=$(ociq bv volume-backup-policy-assignment get-volume-backup-policy-asset-assignment \
            --asset-id "$boot_vol_id" \
            --query 'data[0].id' --raw-output 2>/dev/null || echo "")
        if [[ -n "$assignment_id" && "$assignment_id" != "null" ]]; then
            ociq bv volume-backup-policy-assignment delete \
                --policy-assignment-id "$assignment_id" --force >/dev/null 2>&1 || true
            log_ok "Backup policy desasignada (no se generarán snapshots)"
        else
            log_ok "No había backup policy asignada"
        fi
    else
        log_warn "No se pudo resolver el boot volume — verificá manualmente la backup policy"
    fi
}
