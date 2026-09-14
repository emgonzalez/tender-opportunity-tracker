#!/usr/bin/env bash
# 05-adb.sh — Provisiona Oracle Autonomous Database (ADB) Always Free con
#             TLS one-way (sin wallet mTLS) + ACL de red + IAM mínimo.
#
# Fase 2 · Track B (memoria institucional, docs/planes/fase2-track-b.md §B0.1/B0.2).
# Opt-in puro: solo actúa si `adb.enabled: true` en tenants.yaml#<tenant>.
# Con el default (bloque `adb:` ausente, o `adb.enabled: false`) esta función
# es un no-op — NO altera ningún deploy existente.
#
# ⚠️ Este script TODAVÍA NO está cableado en deploy.sh (a propósito).
#
# Uso manual:
#   export OCI_DIR="$(cd "$(dirname "$0")/.." && pwd)"
#   export REPO_ROOT="$(cd "$OCI_DIR/.." && pwd)"
#   source "$OCI_DIR/lib/common.sh"
#   source "$OCI_DIR/lib/05-adb.sh"
#   adb_setup <tenant>
#
# Orden esperado end-to-end:
#   1. ./oci/deploy.sh <tenant>        (network + compute + budget + app)
#   2. adb_setup <tenant>              (este script — crea ADB + ACL + TLS + IAM)
#   3. db/schema.sql                   (DDL, fuera de este script)
#   4. db/load_onnx.sql                (modelo de embeddings, fuera de este script)
#   5. Poner en .env las variables que este script imprime al final
#
# Modelo de conexión: TLS one-way (sin wallet)
#   La ADB se configura con una ACL que permite conexiones desde la VCN de la
#   VM y con is-mtls-connection-required=false. El driver oracledb thin conecta
#   con el connection string TLS (port 1521) directamente, sin wallet ni
#   certificados de cliente. Esto elimina la necesidad de Vault/KMS para
#   almacenar el wallet (que excedía el límite de 25,600 chars de OCI secrets).
#
# Tiempo de ejecución (primera vez, medido 2026-08-07):
#   - Creación de ADB 23ai Always Free:  ~5-8 min (el paso más lento)
#   - ACL de red:                        ~1-2 min (update + wait AVAILABLE)
#   - TLS one-way:                       ~1-2 min (update + wait AVAILABLE)
#   - Dynamic Group + Policy:            ~10 seg
#   - Total primera vez:                 ~8-13 min
#   - Re-ejecución (idempotente):        ~15-30 seg (solo verifica, no modifica)
#
# Idempotencia:
#   - ADB:             se busca en state, luego por display-name (excl. TERMINATED).
#   - ACL/TLS:         se verifican y solo se aplican si no están ya configurados.
#   - Dynamic Group / Policy: se buscan por nombre exacto antes de crear.
#   Todas las búsquedas dejan sus OCIDs en oci/state/<tenant>.json (gitignored).
set -euo pipefail
source "${OCI_DIR:?OCI_DIR debe estar exportado}/lib/common.sh"

# ── Helpers locales ───────────────────────────────────────────────────────────

# Bash 3.2 (macOS) no tiene ${var,,}
_adb_upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
_adb_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
_adb_is_true() { [[ "$(_adb_lower "${1:-}")" == "true" ]]; }

# Password aleatoria que cumple la política de ADB admin (12-30 chars, al
# menos 1 mayúscula, 1 minúscula, 1 dígito, sin "admin").
_adb_gen_password() {
    local pw pw_lc
    pw="Tx$(openssl rand -base64 40 | tr -dc 'A-Za-z0-9' | head -c 22)9!"
    pw_lc=$(_adb_lower "$pw")
    if [[ "$pw_lc" == *admin* ]]; then
        pw="Tx$(openssl rand -base64 40 | tr -dc 'A-Za-z0-9' | head -c 22)3!"
    fi
    printf '%s' "$pw"
}

# Espera a que la ADB llegue a AVAILABLE o STOPPED.
_adb_wait_terminal() {
    local adb_id=$1 timeout=${2:-1800} waited=0 state
    while true; do
        state=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
            --query 'data."lifecycle-state"' --raw-output)
        case "$state" in
            AVAILABLE|STOPPED) printf '%s' "$state"; return 0 ;;
            TERMINATED|TERMINATING|UNAVAILABLE)
                log_die "ADB $adb_id en estado terminal inesperado: $state" ;;
        esac
        (( waited >= timeout )) && log_die "Timeout (${timeout}s) esperando ADB $adb_id (último estado: $state)"
        log_info "Esperando ADB → AVAILABLE/STOPPED (actual: $state, ${waited}s/${timeout}s)..."
        sleep 15
        waited=$((waited + 15))
    done
}

# ── Función principal ─────────────────────────────────────────────────────────

# adb_acl_sync <tenant> — deja el ACL de la ADB con la VCN y la IP pública ACTUAL
# de la VM.
#
# Vive aparte de adb_setup porque hace falta en dos momentos distintos: al
# provisionar, y cada vez que la instancia se recrea y cambia de IP. deploy.sh la
# llama al final; sin eso, recrear la VM rompía Track B en silencio (ORA-12506)
# hasta que alguien mirara los logs.
adb_acl_sync() {
    local tenant=$1
    local adb_id
    adb_id=$(state_get "$tenant" adb_ocid)
    if [[ -z "$adb_id" ]]; then
        return 0   # el tenant no tiene ADB; nada que sincronizar
    fi
    # Whitelist la VCN de la VM para que la ADB acepte conexiones desde ella.
    local vcn_id
    vcn_id=$(state_get "$tenant" vcn_id)
    if [[ -z "$vcn_id" ]]; then
        # Si el tenant comparte red (share_from), buscar la VCN del tenant padre
        local share_from
        share_from=$(tenant_get_or "$tenant" network.share_from "")
        if [[ -n "$share_from" ]]; then
            vcn_id=$(state_get "$share_from" vcn_id)
        fi
    fi
    [[ -n "$vcn_id" ]] || log_die "No hay vcn_id en state (¿corriste deploy.sh para este tenant?)"

    # La IP pública de la VM también va en la ACL: la VCN OCID solo habilita
    # acceso privado (vía Service Gateway), pero la VM conecta por internet
    # público al endpoint de ADB.
    local vm_ip
    vm_ip=$(state_get "$tenant" public_ip)

    local current_acl
    current_acl=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
        --query 'data."whitelisted-ips"' --raw-output 2>/dev/null || echo "null")

    local acl_entries="\"${vcn_id}\""
    [[ -n "$vm_ip" ]] && acl_entries="${acl_entries},\"${vm_ip}\""

    # Se compara la VCN Y la IP. Mirar solo la VCN era un bug silencioso: al
    # recrear la instancia, la IP pública cambia, la VCN sigue estando, y el
    # script informaba "ACL ya incluye la VCN" sin actualizar nada. Track B
    # quedaba roto con ORA-12506 hasta que alguien leyera los logs. La VCN sola
    # no alcanza porque la VM sale por el Internet Gateway al endpoint público
    # de la ADB, así que llega con su IP pública, no como tráfico de la VCN.
    local acl_ok=1
    echo "$current_acl" | grep -q "$vcn_id" || acl_ok=0
    [[ -n "$vm_ip" ]] && { echo "$current_acl" | grep -q "\"${vm_ip}\"" || acl_ok=0; }

    if [[ "$current_acl" == "null" ]] || (( acl_ok == 0 )); then
        log_step "Configurando ACL de red (VCN + IP pública ${vm_ip:-—})..."
        ociq db autonomous-database update \
            --autonomous-database-id "$adb_id" \
            --whitelisted-ips "[${acl_entries}]" \
            --wait-for-state AVAILABLE \
            --max-wait-seconds 600 \
            --force >/dev/null
        log_ok "ACL configurada"
    else
        log_ok "ACL al día (VCN + ${vm_ip})"
    fi

}

adb_setup() {
    local tenant=$1

    local enabled
    enabled=$(tenant_get_or "$tenant" adb.enabled "false")
    if ! _adb_is_true "$enabled"; then
        log_warn "ADB deshabilitada para '$tenant' (adb.enabled ausente o false) — skip"
        return 0
    fi

    require_cmd openssl

    export OCI_PROFILE="${OCI_PROFILE:-$(tenant_get "$tenant" profile)}"
    assert_tenancy "$tenant"

    # ── Config del tenant ─────────────────────────────────────────────────────
    local compartment tenancy
    compartment=$(tenant_get "$tenant" compartment_ocid)
    tenancy=$(tenant_get "$tenant" tenancy_ocid)

    local display_name db_name workload is_free_tier admin_password_secret
    display_name=$(tenant_get "$tenant" adb.display_name)
    [[ -n "$display_name" && "$display_name" != "None" ]] \
        || log_die "adb.display_name es requerido en tenants.yaml cuando adb.enabled=true"

    local db_name_default
    db_name_default=$(printf '%s' "$display_name" | tr -cd 'A-Za-z0-9' | cut -c1-30)
    [[ "$db_name_default" =~ ^[A-Za-z] ]] || db_name_default="A${db_name_default}"
    db_name=$(tenant_get_or "$tenant" adb.db_name "$db_name_default")

    workload=$(_adb_upper "$(tenant_get_or "$tenant" adb.workload "OLTP")")
    case "$workload" in
        OLTP|DW|AJD|APEX|LH) ;;
        *) log_die "adb.workload inválido: '$workload' (usar OLTP|DW|AJD|APEX|LH)" ;;
    esac

    is_free_tier=$(tenant_get_or "$tenant" adb.is_free_tier "true")
    admin_password_secret=$(tenant_get_or "$tenant" adb.admin_password_secret "")

    # Nombres de recursos IAM (override opcional)
    local dg_name policy_name policy_compartment
    dg_name=$(tenant_get_or "$tenant" adb.dynamic_group_name "tot-${tenant}-adb-access")
    policy_name=$(tenant_get_or "$tenant" adb.policy_name "tot-${tenant}-adb-access-policy")
    policy_compartment=$(tenant_get_or "$tenant" adb.policy_compartment_ocid "$compartment")

    # Etiquetas del tenant → OCI_TAG_ARGS (ver "Etiquetas" en common.sh).
    tenant_tag_args "$tenant"

    log_step "ADB Track B — tenant '$tenant' (display-name: $display_name)"

    # ── 1) Buscar o crear ADB ─────────────────────────────────────────────────
    local adb_id="" adb_state=""
    adb_id=$(state_get "$tenant" adb_ocid)
    if [[ -n "$adb_id" ]] && ociq db autonomous-database get --autonomous-database-id "$adb_id" &>/dev/null; then
        adb_state=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
            --query 'data."lifecycle-state"' --raw-output)
        log_ok "ADB ya registrada en state: $adb_id (estado: $adb_state)"
    else
        adb_id=$(ociq db autonomous-database list --compartment-id "$compartment" \
            --display-name "$display_name" \
            --query "data[?\"lifecycle-state\" != 'TERMINATED' && \"lifecycle-state\" != 'TERMINATING'] | [0].id" \
            --raw-output 2>/dev/null || echo "")
        if [[ -n "$adb_id" && "$adb_id" != "null" ]]; then
            adb_state=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
                --query 'data."lifecycle-state"' --raw-output)
            log_ok "ADB existente encontrada por display-name: $adb_id (estado: $adb_state)"
        else
            adb_id=""
        fi
    fi

    local admin_password=""
    if [[ -z "$adb_id" ]]; then
        if [[ -n "$admin_password_secret" ]]; then
            log_step "Leyendo password de ADMIN desde secret existente ($admin_password_secret)..."
            admin_password=$(ociq secrets secret-bundle get --secret-id "$admin_password_secret" \
                --query 'data."secret-bundle-content".content' --raw-output | base64 -d)
        else
            admin_password=$(_adb_gen_password)
            log_warn "Password de ADMIN generada localmente — NO se guarda en ningún archivo por este script."
            log_warn "Copiala AHORA a tu gestor de secretos; la necesitás para correr db/schema.sql:"
            echo "    ADMIN password: ${admin_password}"
        fi

        log_step "Creando ADB Always Free '${display_name}' con 23ai (esto tarda varios minutos)..."
        adb_id=$(ociq db autonomous-database create \
            --compartment-id "$compartment" \
            --db-name "$db_name" \
            --display-name "$display_name" \
            --db-workload "$workload" \
            --db-version 23ai \
            --is-free-tier "$is_free_tier" \
            --cpu-core-count 1 \
            --data-storage-size-in-tbs 1 \
            --admin-password "$admin_password" \
            --license-model LICENSE_INCLUDED \
            "${OCI_TAG_ARGS[@]}" \
            --wait-for-state AVAILABLE \
            --max-wait-seconds 1800 \
            --query 'data.id' --raw-output)
        adb_state="AVAILABLE"
        log_ok "ADB creada y disponible: $adb_id"
        unset admin_password
    else
        case "$adb_state" in
            AVAILABLE)
                log_ok "Reusando ADB disponible (no se crea otra)"
                ;;
            STOPPED)
                log_warn "ADB existe pero está STOPPED (probable auto-stop por 7 días de inactividad)."
                log_warn "Este script NO la arranca automáticamente. Si la necesitás ya:"
                log_info "  oci --profile ${OCI_PROFILE} db autonomous-database start --autonomous-database-id $adb_id"
                ;;
            *)
                log_step "ADB en transición ($adb_state) — esperando a AVAILABLE/STOPPED..."
                adb_state=$(_adb_wait_terminal "$adb_id" 1800)
                log_ok "ADB llegó a estado: $adb_state"
                ;;
        esac
    fi
    state_set "$tenant" adb_ocid "$adb_id"
    state_set "$tenant" adb_db_name "$db_name"

    adb_acl_sync "$tenant"

    # ── 3) TLS one-way (deshabilitar requisito de mTLS) ───────────────────────
    local mtls_required
    mtls_required=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
        --query 'data."is-mtls-connection-required"' --raw-output)

    if [[ "$mtls_required" == "true" ]]; then
        log_step "Habilitando TLS one-way (deshabilitando mTLS obligatorio)..."
        ociq db autonomous-database update \
            --autonomous-database-id "$adb_id" \
            --is-mtls-connection-required false \
            --wait-for-state AVAILABLE \
            --max-wait-seconds 600 \
            --force >/dev/null
        log_ok "TLS one-way habilitado (sin wallet)"
    else
        log_ok "TLS one-way ya está habilitado"
    fi

    # Obtener el connection string TLS (port 1521, consumer group LOW para Always Free)
    local tls_dsn
    tls_dsn=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
        --query "data.\"connection-strings\".profiles[?\"tls-authentication\"=='SERVER' && \"consumer-group\"=='LOW'].value | [0]" \
        --raw-output)
    if [[ -z "$tls_dsn" || "$tls_dsn" == "null" ]]; then
        log_warn "No se encontró connection string TLS (LOW). Probando con HIGH..."
        tls_dsn=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
            --query "data.\"connection-strings\".profiles[?\"tls-authentication\"=='SERVER'] | [0].value" \
            --raw-output)
    fi
    [[ -n "$tls_dsn" && "$tls_dsn" != "null" ]] || log_die "No hay connection strings TLS disponibles (¿mTLS sigue requerido?)"
    state_set "$tenant" adb_dsn "$tls_dsn"

    # ── 4) IAM mínimo: Dynamic Group + Policy ─────────────────────────────────
    log_step "IAM mínimo — Dynamic Group + Policy para usar la ADB..."
    local inst_id dg_id="" policy_id=""
    inst_id=$(state_get "$tenant" instance_id)

    local stmt_adb="Allow dynamic-group ${dg_name} to use autonomous-databases in tenancy"

    if [[ -z "$inst_id" ]]; then
        log_warn "instance_id no está en state (¿corriste deploy.sh para este tenant?)."
        log_warn "No se puede crear el Dynamic Group todavía."
    else
        dg_id=$(ociq iam dynamic-group list --name "$dg_name" --query 'data[0].id' --raw-output 2>/dev/null || echo "")
        if [[ -n "$dg_id" && "$dg_id" != "null" ]]; then
            log_ok "Dynamic Group ya existe: $dg_id"
        else
            local dg_err; dg_err=$(mktemp)
            if dg_id=$(ociq iam dynamic-group create \
                --compartment-id "$tenancy" \
                --name "$dg_name" \
                --description "Acceso mínimo a ADB para tender-opportunity-tracker (${tenant})" \
                --matching-rule "instance.id = '${inst_id}'" \
                "${OCI_TAG_ARGS[@]}" \
                --query 'data.id' --raw-output 2>"$dg_err"); then
                log_ok "Dynamic Group creado: $dg_id"
            else
                log_warn "No se pudo crear el Dynamic Group automáticamente:"
                log_info "$(tail -3 "$dg_err")"
                dg_id=""
            fi
            rm -f "$dg_err"
        fi
    fi
    [[ -n "$dg_id" ]] && state_set "$tenant" adb_dynamic_group_id "$dg_id"

    if [[ -n "$dg_id" ]]; then
        policy_id=$(ociq iam policy list --compartment-id "$policy_compartment" \
            --query "data[?name=='${policy_name}'].id | [0]" --raw-output 2>/dev/null || echo "")
        if [[ -n "$policy_id" && "$policy_id" != "null" ]]; then
            log_ok "Policy ya existe: $policy_id — actualizando statements"
            ociq iam policy update \
                --policy-id "$policy_id" \
                --statements "[\"${stmt_adb}\"]" \
                --force >/dev/null
        else
            local pol_err; pol_err=$(mktemp)
            if policy_id=$(ociq iam policy create \
                --compartment-id "$policy_compartment" \
                --name "$policy_name" \
                --description "Acceso mínimo (use autonomous-databases) para ${tenant}" \
                --statements "[\"${stmt_adb}\"]" \
                "${OCI_TAG_ARGS[@]}" \
                --query 'data.id' --raw-output 2>"$pol_err"); then
                log_ok "Policy creada: $policy_id"
            else
                log_warn "No se pudo crear la Policy automáticamente:"
                log_info "$(tail -3 "$pol_err")"
                policy_id=""
            fi
            rm -f "$pol_err"
        fi
        [[ -n "$policy_id" ]] && state_set "$tenant" adb_policy_id "$policy_id"
    fi

    echo ""
    log_step "Policy statements (mínimos)"
    echo "  Compartment de la policy: ${policy_compartment}"
    echo "  1) ${stmt_adb}"
    [[ -z "$dg_id" || -z "$policy_id" ]] && log_warn "Aplicá lo anterior manualmente en la consola OCI si no se creó arriba."

    # ── 5) Resumen para el .env ───────────────────────────────────────────────
    echo ""
    log_step "Variables para .env (tenant: ${tenant})"
    echo "  ENABLE_MEMORIA=true"
    echo "  ADB_USER=AMALIA                          # usuario de esquema — se crea en db/schema.sql"
    echo "  ADB_PASSWORD=<password del usuario AMALIA>"
    echo "  ADB_DSN=${tls_dsn}"
    echo ""
    log_ok "adb_setup completo para '${tenant}'. adb_ocid=${adb_id}"
}
