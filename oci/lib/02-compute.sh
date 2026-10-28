#!/usr/bin/env bash
# 02-compute.sh — Crea la instancia ARM Ampere A1 con cloud-init embebido
#
# Recursos:
#   - SSH key (genera un par dedicado en oci/state/<tenant>_id_ed25519 si no existe)
#   - Imagen Ubuntu 22.04 ARM (resuelta dinámicamente por shape compatibility)
#   - Instance VM.Standard.A1.Flex
#   - Reserved Public IP (Always Free, asociada al VNIC primary)
#
# Idempotente: la instancia del state se reutiliza (RUNNING tal cual; STOPPED se arranca);
# nunca se crea otra porque una consulta falló.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

# _instancia_estado <id> → deja el lifecycle-state en INSTANCIA_ESTADO. Aborta si OCI no
# lo confirma: un error de red NUNCA se lee como «hay que crear otra VM».
_instancia_estado() {
    local rc=0
    oci_existe "Instancia" compute instance get --instance-id "$1" \
        --query 'data."lifecycle-state"' --raw-output || rc=$?
    case $rc in
        0) INSTANCIA_ESTADO=$OCI_SALIDA ;;
        1) log_die "Instancia: OCI responde 404 sobre $1 mientras se esperaba su estado. No se crea nada." ;;
        *) log_die "Instancia: no pude consultar el estado de $1 (¿sin red?). No se crea nada: volvé a correr cuando haya conexión." ;;
    esac
}

# _instancia_esperar <id> <estado> — sondea hasta llegar a <estado> (tope DEPLOY_ESPERA_INSTANCIA s).
_instancia_esperar() {
    local id=$1 objetivo=$2 tope=${DEPLOY_ESPERA_INSTANCIA:-900} paso=${DEPLOY_PASO_INSTANCIA:-15} t=0
    while :; do
        _instancia_estado "$id"
        [[ "$INSTANCIA_ESTADO" == "$objetivo" ]] && return 0
        (( t >= tope )) && log_die "Instancia $id sigue en ${INSTANCIA_ESTADO} tras ${tope}s esperando ${objetivo}. No se crea otra: revisala en la consola."
        log_info "Instancia en ${INSTANCIA_ESTADO}; espero ${objetivo}…"
        sleep "$paso"; t=$((t + paso))
    done
}

compute_create() {
    local tenant=$1
    local compartment shape boot_size os os_version display
    compartment=$(tenant_get "$tenant" compartment_ocid)
    shape=$(tenant_get "$tenant" instance.shape)
    boot_size=$(tenant_get "$tenant" instance.boot_volume_size_gb)
    os=$(tenant_get "$tenant" instance.os)
    os_version=$(tenant_get "$tenant" instance.os_version)
    display=$(tenant_get "$tenant" instance.display_name)

    # Etiquetas del tenant → OCI_TAG_ARGS (ver "Etiquetas" en common.sh).
    tenant_tag_args "$tenant"

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

    # ── Imagen Ubuntu (filtrada por shape — OCI devuelve solo arquitecturas compatibles) ─
    log_step "Resolviendo imagen $os $os_version compatible con $shape..."
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
    # gzip antes del base64: OCI limita el metadata a 32 000 bytes y el cloud-init
    # ya pasa de 60 kB (los scripts de auto-actualización y respaldo van
    # embebidos). cloud-init detecta el magic de gzip y descomprime solo.
    # ⚠️ Esto compra margen, no lo resuelve: comprimido quedan ~31 kB, a menos de
    # 1 kB del techo. Cualquier script nuevo embebido vuelve a romperlo. El
    # arreglo de fondo es dejar de embeber y referenciar los scripts del repo,
    # que app_deploy ya sincroniza a /opt/tender-opportunity-tracker.
    cloud_init_b64=$(TENANT="$tenant" "${OCI_DIR}/lib/render-cloud-init.sh" | gzip -9 | base64 | tr -d '\n')
    local ci_bytes=${#cloud_init_b64}
    if (( ci_bytes > 32000 )); then
        log_die "El cloud-init comprimido ocupa ${ci_bytes} bytes y OCI no acepta más de 32000.
     Hay que sacar scripts de write_files y referenciarlos desde el repo."
    fi
    (( ci_bytes > 28000 )) && log_warn "cloud-init en ${ci_bytes}/32000 bytes — queda poco margen"
    log_ok "cloud-init listo ($(echo "$cloud_init_b64" | wc -c | tr -d ' ') bytes b64)"

    # ── Instancia (con fallover entre ADs si "Out of host capacity") ─────────
    log_step "Creando instancia..."
    # La instancia del state se REUTILIZA en cualquier estado recuperable: antes, un error de red
    # o una VM STOPPED caían en el `else` y lanzaban una VM nueva. `oci_resolver` aborta si no
    # puede confirmar, o ante un 404 sobre el id del state (salvo DEPLOY_RECREAR=1).
    local inst_id="" crear=1
    if oci_resolver "$tenant" instance_id "Instancia" compute instance get --instance-id @ID@ \
        --query 'data."lifecycle-state"' --raw-output; then
        inst_id=$(state_get "$tenant" instance_id)
        INSTANCIA_ESTADO=$OCI_SALIDA
        crear=0
        # Si se reutiliza, el boot volume se busca en el AD donde se creó, no en el primero.
        local ad_state; ad_state=$(state_get "$tenant" availability_domain)
        [[ -n "$ad_state" ]] && ad="$ad_state"
        case "$INSTANCIA_ESTADO" in
            RUNNING)
                log_ok "Instancia ya RUNNING: $inst_id" ;;
            STOPPED|STOPPING)
                [[ "$INSTANCIA_ESTADO" == STOPPING ]] && _instancia_esperar "$inst_id" STOPPED
                log_warn "Instancia $inst_id detenida: se arranca (no se crea otra)."
                ociq compute instance action --instance-id "$inst_id" --action START \
                    --wait-for-state RUNNING >/dev/null
                log_ok "Instancia arrancada (RUNNING): $inst_id" ;;
            STARTING|PROVISIONING|CREATING_IMAGE|MOVING)
                _instancia_esperar "$inst_id" RUNNING
                log_ok "Instancia ya RUNNING: $inst_id" ;;
            TERMINATED|TERMINATING)
                if [[ "${DEPLOY_RECREAR:-}" == "1" ]]; then
                    log_warn "Instancia $inst_id está ${INSTANCIA_ESTADO}; DEPLOY_RECREAR=1 → se crea una nueva."
                    crear=1; ad="${ad_list[0]}"
                else
                    log_die "La instancia del state ($inst_id) está ${INSTANCIA_ESTADO}. No se crea otra a ciegas. Si es a propósito, exportá DEPLOY_RECREAR=1."
                fi ;;
            *)
                log_die "La instancia $inst_id está en un estado inesperado ('${INSTANCIA_ESTADO}'). No se crea otra: revisala en la consola." ;;
        esac
    fi
    if (( crear == 1 )); then
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
                "${OCI_TAG_ARGS[@]}"
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
                    # No es falta de capacidad: el request es inválido o los
                    # permisos faltan, y eso va a fallar IGUAL en todos los ADs.
                    # Seguir el bucle solo sirve para terminar culpando a la
                    # capacidad —que fue lo que pasó cuando el cloud-init superó
                    # los 32 kB de metadata— y mandar a alguien a "reintentar en
                    # unas horas" por algo que no se arregla esperando.
                    log_warn "Launch en $try_ad falló por un motivo que NO es capacidad:"
                    cat "$launch_err" >&2
                    rm -f "$launch_err"
                    log_die "El launch falló por un error del request, no por capacidad.
     Reintentar no va a servir: el mismo request falla en todos los ADs.
     El detalle de OCI está arriba. Causas frecuentes:
       · metadata (cloud-init) por encima de los 32 000 bytes
       · falta de permisos sobre instance-family o sobre la subnet
       · imagen o shape no disponibles en la región"
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
    # Todo este bloque es solo un aviso: nunca falla el deploy. Pero un error de consulta
    # se informa como tal, no como «no hay backup policy».
    local boot_vol_id="" assignment_id="" rc_bv=0 rc_pol=0
    oci_existe "Boot volume de la instancia" compute boot-volume-attachment list \
        --availability-domain "$ad" \
        --compartment-id "$compartment" \
        --instance-id "$inst_id" \
        --query 'data[0]."boot-volume-id"' --raw-output || rc_bv=$?
    (( rc_bv == 0 )) && boot_vol_id=$OCI_SALIDA
    if [[ -n "$boot_vol_id" && "$boot_vol_id" != "null" ]]; then
        state_set "$tenant" boot_volume_id "$boot_vol_id"

        # El boot volume NO hereda las etiquetas de la instancia: OCI lo crea
        # como recurso aparte durante el launch y sin nada puesto. Y es el
        # huérfano más caro de todos — sobrevive a la instancia si alguna vez se
        # termina con --preserve-boot-volume true, sigue consumiendo los 200 GB
        # de Always Free, y no aparece en ninguna lista de instancias. Por eso
        # se etiqueta en un paso propio.
        ociq bv boot-volume update --boot-volume-id "$boot_vol_id" \
            "${OCI_TAG_ARGS[@]}" --force >/dev/null 2>&1 \
            && log_ok "Boot volume etiquetado" \
            || log_warn "No se pudo etiquetar el boot volume (seguí: no es fatal)"

        oci_existe "Backup policy del boot volume" \
            bv volume-backup-policy-assignment get-volume-backup-policy-asset-assignment \
            --asset-id "$boot_vol_id" \
            --query 'data[0].id' --raw-output || rc_pol=$?
        (( rc_pol == 0 )) && assignment_id=$OCI_SALIDA
        if (( rc_pol != 0 )); then
            log_warn "No pude consultar la backup policy del boot volume ($([[ $rc_pol -eq 1 ]] && echo "404: ¿permisos?" || echo "¿sin red?")) — verificá manualmente que no tenga una asignada"
        elif [[ -n "$assignment_id" && "$assignment_id" != "null" ]]; then
            if ociq bv volume-backup-policy-assignment delete \
                --policy-assignment-id "$assignment_id" --force >/dev/null; then
                log_ok "Backup policy desasignada (no se generarán snapshots)"
            else
                log_warn "No se pudo desasignar la backup policy ${assignment_id} — desasignala a mano (genera snapshots)"
            fi
        else
            log_ok "No había backup policy asignada"
        fi
    elif (( rc_bv != 0 )); then
        log_warn "No pude consultar el boot volume ($([[ $rc_bv -eq 1 ]] && echo "404: ¿permisos?" || echo "¿sin red?")) — verificá manualmente la backup policy"
    else
        log_warn "No se pudo resolver el boot volume — verificá manualmente la backup policy"
    fi
}
