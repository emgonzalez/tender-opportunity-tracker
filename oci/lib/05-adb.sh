#!/usr/bin/env bash
# 05-adb.sh — Provisiona Oracle Autonomous Database (ADB) Always Free +
#             wallet mTLS en OCI Vault + IAM mínimo (Dynamic Group + Policy).
#
# Fase 2 · Track B (memoria institucional, docs/planes/fase2-track-b.md §B0.1/B0.2).
# Opt-in puro: solo actúa si `adb.enabled: true` en tenants.yaml#<tenant>.
# Con el default (bloque `adb:` ausente, o `adb.enabled: false`) esta función
# es un no-op — NO altera ningún deploy existente (emiliogonzalez,
# emiliogonzalez_e2, multicomputos siguen sin bloque `adb:` hoy).
#
# ⚠️ Este script TODAVÍA NO está cableado en deploy.sh (a propósito — ver
# oci/README.md → "Oracle ADB (Track B)" para el snippet de integración
# propuesto, y por qué se deja manual por ahora).
#
# Uso manual (hasta que se cablee):
#   export OCI_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # o cd a la raíz del repo
#   export REPO_ROOT="$(cd "$OCI_DIR/.." && pwd)"
#   source "$OCI_DIR/lib/common.sh"
#   source "$OCI_DIR/lib/05-adb.sh"
#   adb_setup <tenant>
#
# Orden esperado end-to-end (Track B completo, ver docs/planes/fase2-track-b.md):
#   1. ./oci/deploy.sh <tenant>        (network + compute + budget + app — sin cambios)
#   2. adb_setup <tenant>              (este script — crea ADB + wallet + IAM)
#   3. db/schema.sql                   (B0.3 — DDL, fuera de este script)
#   4. db/load_onnx.sql                (B0.4 — modelo de embeddings, fuera de este script)
#   5. Poner en .env las variables que este script imprime al final
#
# Idempotencia:
#   - ADB:            se busca primero en state, luego por display-name en el
#                      compartment (excluyendo TERMINATED/TERMINATING). Si existe
#                      y está AVAILABLE o STOPPED, se REUSA (no se crea otra).
#   - Vault / KMS key: se buscan por display-name (lifecycle ACTIVE/ENABLED).
#   - Secret (wallet): si ya existe, se sube una VERSIÓN NUEVA (update-base64)
#                      en vez de fallar o duplicar.
#   - Dynamic Group / Policy: se buscan por nombre exacto antes de crear.
# Todas las búsquedas dejan sus OCIDs en oci/state/<tenant>.json (gitignored).
set -euo pipefail
source "${OCI_DIR:?OCI_DIR debe estar exportado — ver el bloque de uso manual arriba en este archivo}/lib/common.sh"

# ── Helpers locales ───────────────────────────────────────────────────────────

# Bash 3.2 (macOS, donde Emilio corre estos scripts) no tiene `${var,,}` — se
# usa `tr` para todo lower/upper-casing en este archivo, no expansión bash4+.
_adb_upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
_adb_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Los valores booleanos de tenants.yaml llegan como "True"/"False" (repr de
# Python/PyYAML vía tenant_get) cuando la key EXISTE, pero como el string
# default literal (p.ej. "false") cuando la key falta y se usó tenant_get_or.
# Esta función normaliza ambos casos.
_adb_is_true() { [[ "$(_adb_lower "${1:-}")" == "true" ]]; }

# Password aleatoria que cumple la política de ADB admin (12-30 chars, al
# menos 1 mayúscula, 1 minúscula, 1 dígito) y la de wallets (>=8 chars, letra
# + dígito/especial). Se genera en memoria y NUNCA se escribe a disco por
# este script (solo se imprime una vez en la terminal para que el operador
# la guarde en su gestor de secretos).
_adb_gen_password() {
    local pw pw_lc
    pw="Tx$(openssl rand -base64 40 | tr -dc 'A-Za-z0-9' | head -c 22)9!"
    pw_lc=$(_adb_lower "$pw")
    if [[ "$pw_lc" == *admin* ]]; then
        # Defensa extra: astronómicamente improbable, pero regenerar es gratis.
        pw="Tx$(openssl rand -base64 40 | tr -dc 'A-Za-z0-9' | head -c 22)3!"
    fi
    printf '%s' "$pw"
}

# Espera a que la ADB llegue a un estado terminal útil (AVAILABLE o STOPPED).
# Usado solo para REUSAR una ADB existente que esté en transición (la ADB
# recién creada ya usa --wait-for-state AVAILABLE en el propio `create`).
_adb_wait_terminal() {
    local adb_id=$1 timeout=${2:-1800} waited=0 state
    while true; do
        state=$(ociq db autonomous-database get --autonomous-database-id "$adb_id" \
            --query 'data."lifecycle-state"' --raw-output)
        case "$state" in
            AVAILABLE|STOPPED) printf '%s' "$state"; return 0 ;;
            TERMINATED|TERMINATING|UNAVAILABLE)
                log_die "ADB $adb_id en estado terminal inesperado: $state — revisala en la consola OCI" ;;
        esac
        (( waited >= timeout )) && log_die "Timeout (${timeout}s) esperando ADB $adb_id (último estado: $state)"
        log_info "Esperando ADB → AVAILABLE/STOPPED (actual: $state, ${waited}s/${timeout}s)..."
        sleep 15
        waited=$((waited + 15))
    done
}

# ── Función principal ─────────────────────────────────────────────────────────

adb_setup() {
    local tenant=$1

    local enabled
    enabled=$(tenant_get_or "$tenant" adb.enabled "false")
    if ! _adb_is_true "$enabled"; then
        log_warn "ADB deshabilitada para '$tenant' (adb.enabled ausente o false) — skip"
        return 0
    fi

    require_cmd openssl
    require_cmd base64

    # Resuelve OCI_PROFILE si el caller invoca esta función standalone (sin
    # pasar por deploy.sh, que normalmente ya lo exporta).
    export OCI_PROFILE="${OCI_PROFILE:-$(tenant_get "$tenant" profile)}"
    assert_tenancy "$tenant"

    # ── Config del tenant (con defaults documentados en README) ───────────────
    local compartment tenancy
    compartment=$(tenant_get "$tenant" compartment_ocid)
    tenancy=$(tenant_get "$tenant" tenancy_ocid)

    local display_name db_name workload is_free_tier admin_password_secret
    display_name=$(tenant_get "$tenant" adb.display_name)
    [[ -n "$display_name" && "$display_name" != "None" ]] \
        || log_die "adb.display_name es requerido en tenants.yaml cuando adb.enabled=true"

    # db_name: alfanumérico, empieza con letra, máx 30 chars, único en la tenancy.
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

    # Nombres de los recursos auxiliares (Vault/Key/Secret/DG/Policy) —
    # override opcional vía tenants.yaml, default derivado del tenant.
    local vault_display_name key_display_name wallet_secret_name dg_name policy_name policy_compartment
    vault_display_name=$(tenant_get_or "$tenant" adb.vault_display_name "tot-${tenant}-adb-vault")
    key_display_name=$(tenant_get_or "$tenant" adb.key_display_name "tot-${tenant}-adb-wallet-key")
    wallet_secret_name=$(tenant_get_or "$tenant" adb.wallet_secret_name "tot-${tenant}-adb-wallet")
    dg_name=$(tenant_get_or "$tenant" adb.dynamic_group_name "tot-${tenant}-adb-access")
    policy_name=$(tenant_get_or "$tenant" adb.policy_name "tot-${tenant}-adb-access-policy")
    # Default: compartment del propio tenant (NO la tenancy root). A diferencia
    # de 03-budget.sh (que crea su policy en $tenancy), acá preferimos el
    # compartment del tenant porque en tenants corporativos (p.ej.
    # multicomputos) el grupo IAM puede no tener permiso de `manage policies`
    # a nivel de tenancy (ver oci/README.md — nota de Landing Zone). Overridable.
    policy_compartment=$(tenant_get_or "$tenant" adb.policy_compartment_ocid "$compartment")

    log_step "ADB Track B — tenant '$tenant' (display-name: $display_name)"

    # ── 1) Buscar ADB existente (idempotencia) ────────────────────────────────
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
        # ── Resolver password de ADMIN ─────────────────────────────────────────
        if [[ -n "$admin_password_secret" ]]; then
            log_step "Leyendo password de ADMIN desde secret existente ($admin_password_secret)..."
            admin_password=$(ociq secrets secret-bundle get --secret-id "$admin_password_secret" \
                --query 'data."secret-bundle-content".content' --raw-output | base64 -d)
        else
            admin_password=$(_adb_gen_password)
            log_warn "Password de ADMIN generada localmente — NO se guarda en ningún archivo por este script."
            log_warn "Copiala AHORA a tu gestor de secretos; la necesitás para correr db/schema.sql (B0.3):"
            echo "    ADMIN password: ${admin_password}"
        fi

        # ── Crear la ADB ────────────────────────────────────────────────────────
        # NOTA "verificar antes de correr" (oci-cli):
        #   La CLI actual (verificado localmente con oci-cli 3.80.0) expone DOS
        #   familias de flags de cómputo, mutuamente excluyentes:
        #     (a) --cpu-core-count <int>                         [legacy/OCPU]
        #     (b) --compute-model [ECPU|OCPU] --compute-count <float>  [nuevo]
        #   Para Always Free (--is-free-tier true) la doc de OCI fija SIEMPRE
        #   1 OCPU + 20GB sin importar el valor pasado, y el flag "nuevo"
        #   --compute-model/--compute-count está pensado para bases pagas
        #   (ECPU es el modelo recomendado ahí). Usamos la familia (a),
        #   --cpu-core-count 1, que es la documentada para Always Free.
        #   ANTES DE CORRER: si `oci db autonomous-database create --help`
        #   en tu versión de oci-cli marca --cpu-core-count como deprecated o
        #   rechaza la combinación con --is-free-tier, cambiar a:
        #     --compute-model ECPU --compute-count 1 --is-free-tier true
        #   y volver a probar (revisar el mensaje de error, suele ser explícito).
        #
        #   --data-storage-size-in-tbs 1: es el mínimo representable por la API
        #   para instancias Serverless (el flag hermano --data-storage-size-in-gbs
        #   NO aplica a Serverless, solo a Dedicated Exadata — confirmado en
        #   --help). Con is-free-tier=true, OCI ignora el valor real y fija 20GB.
        log_step "Creando ADB Always Free '${display_name}' (esto tarda varios minutos)..."
        adb_id=$(ociq db autonomous-database create \
            --compartment-id "$compartment" \
            --db-name "$db_name" \
            --display-name "$display_name" \
            --db-workload "$workload" \
            --is-free-tier "$is_free_tier" \
            --cpu-core-count 1 \
            --data-storage-size-in-tbs 1 \
            --admin-password "$admin_password" \
            --license-model LICENSE_INCLUDED \
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

    # ── 2) Vault + KMS key (para envolver el secret del wallet) ──────────────
    log_step "Resolviendo Vault (${vault_display_name})..."
    local vault_id
    vault_id=$(state_get "$tenant" adb_vault_id)
    if [[ -z "$vault_id" ]] || ! ociq kms management vault get --vault-id "$vault_id" &>/dev/null; then
        vault_id=$(ociq kms management vault list --compartment-id "$compartment" \
            --query "data[?\"display-name\"=='${vault_display_name}' && \"lifecycle-state\"=='ACTIVE'].id | [0]" \
            --raw-output 2>/dev/null || echo "")
    fi
    if [[ -z "$vault_id" || "$vault_id" == "null" ]]; then
        log_step "Creando Vault tipo DEFAULT (sin costo mensual de HSM dedicado)..."
        vault_id=$(ociq kms management vault create \
            --compartment-id "$compartment" \
            --display-name "$vault_display_name" \
            --vault-type DEFAULT \
            --wait-for-state ACTIVE \
            --query 'data.id' --raw-output)
        log_ok "Vault creado: $vault_id"
    else
        log_ok "Vault ya existe: $vault_id"
    fi
    state_set "$tenant" adb_vault_id "$vault_id"

    local mgmt_endpoint
    mgmt_endpoint=$(ociq kms management vault get --vault-id "$vault_id" \
        --query 'data."management-endpoint"' --raw-output)

    log_step "Resolviendo KMS key (${key_display_name})..."
    local key_id
    key_id=$(state_get "$tenant" adb_kms_key_id)
    if [[ -z "$key_id" ]] || ! ociq --endpoint "$mgmt_endpoint" kms management key get --key-id "$key_id" &>/dev/null; then
        key_id=$(ociq --endpoint "$mgmt_endpoint" kms management key list --compartment-id "$compartment" \
            --query "data[?\"display-name\"=='${key_display_name}' && \"lifecycle-state\"=='ENABLED'].id | [0]" \
            --raw-output 2>/dev/null || echo "")
    fi
    if [[ -z "$key_id" || "$key_id" == "null" ]]; then
        log_step "Creando KMS key AES-256 (protection-mode HSM, default de OCI)..."
        key_id=$(ociq --endpoint "$mgmt_endpoint" kms management key create \
            --compartment-id "$compartment" \
            --display-name "$key_display_name" \
            --key-shape '{"algorithm":"AES","length":32}' \
            --wait-for-state ENABLED \
            --query 'data.id' --raw-output)
        log_ok "Key creada: $key_id"
    else
        log_ok "Key ya existe: $key_id"
    fi
    state_set "$tenant" adb_kms_key_id "$key_id"

    # ── 3) Wallet: generar, subir a Vault (secret), borrar copia local ────────
    log_step "Generando wallet de conexión mTLS..."
    local wallet_password wallet_tmpdir wallet_zip wallet_b64
    wallet_password=$(_adb_gen_password)
    wallet_tmpdir=$(mktemp -d)
    wallet_zip="${wallet_tmpdir}/wallet.zip"
    ociq db autonomous-database generate-wallet \
        --autonomous-database-id "$adb_id" \
        --password "$wallet_password" \
        --file "$wallet_zip" >/dev/null
    log_ok "Wallet generado (copia temporal local, se borra al final de este script)"

    wallet_b64=$(base64 < "$wallet_zip" | tr -d '\n')

    log_step "Subiendo wallet a Vault como secret (${wallet_secret_name})..."
    local secret_id
    secret_id=$(state_get "$tenant" adb_wallet_secret_ocid)
    if [[ -z "$secret_id" ]] || ! ociq vault secret get --secret-id "$secret_id" &>/dev/null; then
        secret_id=$(ociq vault secret list --compartment-id "$compartment" --vault-id "$vault_id" \
            --name "$wallet_secret_name" \
            --query "data[?\"lifecycle-state\"=='ACTIVE'].id | [0]" \
            --raw-output 2>/dev/null || echo "")
    fi
    if [[ -z "$secret_id" || "$secret_id" == "null" ]]; then
        secret_id=$(ociq vault secret create-base64 \
            --compartment-id "$compartment" \
            --vault-id "$vault_id" \
            --key-id "$key_id" \
            --secret-name "$wallet_secret_name" \
            --description "Wallet mTLS de ${db_name} (tender-opportunity-tracker / ${tenant})" \
            --secret-content-content "$wallet_b64" \
            --secret-content-name "wallet-$(date +%Y%m%d%H%M%S)" \
            --wait-for-state ACTIVE \
            --query 'data.id' --raw-output)
        log_ok "Secret creado: $secret_id"
    else
        log_ok "Secret ya existe: $secret_id — subiendo NUEVA VERSIÓN (update, no se duplica el secret)"
        ociq vault secret update-base64 \
            --secret-id "$secret_id" \
            --secret-content-content "$wallet_b64" \
            --secret-content-name "wallet-$(date +%Y%m%d%H%M%S)" \
            --force >/dev/null
        log_ok "Nueva versión del wallet subida"
    fi
    state_set "$tenant" adb_wallet_secret_ocid "$secret_id"
    state_set "$tenant" adb_dsn "${db_name}_high"

    # Limpieza: el wallet solo debe vivir en Vault y, en runtime, en tmpfs de
    # la VM (nunca en disco persistente) — coherente con B1.2 (adb_wallet_dir
    # = /dev/shm/...). Borramos la copia local de aprovisionamiento ya mismo.
    unset wallet_b64
    rm -f "$wallet_zip"
    rmdir "$wallet_tmpdir" 2>/dev/null || true

    # ── 4) IAM mínimo (B0.2): Dynamic Group + Policy ──────────────────────────
    # Statements EXACTOS a aplicar (se imprimen siempre, se crean automáticamente
    # si el perfil OCI tiene permisos; si no, quedan documentados para aplicación
    # manual por un admin de la tenancy/compartment).
    log_step "IAM mínimo — Dynamic Group + Policy para leer el wallet y usar la ADB..."
    local inst_id dg_id="" policy_id=""
    inst_id=$(state_get "$tenant" instance_id)

    local stmt_secret="Allow dynamic-group ${dg_name} to read secret-bundles in compartment id '${compartment}' where target.secret.id = '${secret_id}'"
    local stmt_adb="Allow dynamic-group ${dg_name} to use autonomous-databases in compartment id '${compartment}' where target.autonomous-database.id = '${adb_id}'"

    if [[ -z "$inst_id" ]]; then
        log_warn "instance_id no está en state (¿corriste 02-compute.sh / deploy.sh para este tenant?)."
        log_warn "No se puede crear el Dynamic Group todavía (matching-rule necesita el OCID de la instancia)."
        log_warn "Volvé a correr adb_setup una vez exista la instancia, o creá el DG manualmente."
    else
        dg_id=$(ociq iam dynamic-group list --name "$dg_name" --query 'data[0].id' --raw-output 2>/dev/null || echo "")
        if [[ -n "$dg_id" && "$dg_id" != "null" ]]; then
            log_ok "Dynamic Group ya existe: $dg_id"
        else
            local dg_err; dg_err=$(mktemp)
            if dg_id=$(ociq iam dynamic-group create \
                --compartment-id "$tenancy" \
                --name "$dg_name" \
                --description "Acceso mínimo a ADB Vault/wallet para tender-opportunity-tracker (${tenant})" \
                --matching-rule "instance.id = '${inst_id}'" \
                --query 'data.id' --raw-output 2>"$dg_err"); then
                log_ok "Dynamic Group creado: $dg_id"
            else
                log_warn "No se pudo crear el Dynamic Group automáticamente (¿permisos IAM insuficientes?):"
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
            log_ok "Policy ya existe: $policy_id — actualizando statements (por si cambió el secret/ADB OCID)"
            ociq iam policy update \
                --policy-id "$policy_id" \
                --statements "[\"${stmt_secret}\", \"${stmt_adb}\"]" \
                --force >/dev/null
        else
            local pol_err; pol_err=$(mktemp)
            if policy_id=$(ociq iam policy create \
                --compartment-id "$policy_compartment" \
                --name "$policy_name" \
                --description "Acceso mínimo (read secret-bundles + use autonomous-databases) para ${tenant} — nada de manage all-resources" \
                --statements "[\"${stmt_secret}\", \"${stmt_adb}\"]" \
                --query 'data.id' --raw-output 2>"$pol_err"); then
                log_ok "Policy creada: $policy_id"
            else
                log_warn "No se pudo crear la Policy automáticamente (¿permisos IAM insuficientes?):"
                log_info "$(tail -3 "$pol_err")"
                policy_id=""
            fi
            rm -f "$pol_err"
        fi
        [[ -n "$policy_id" ]] && state_set "$tenant" adb_policy_id "$policy_id"
    fi

    echo ""
    log_step "Policy statements (mínimos — NADA de manage all-resources)"
    echo "  Compartment de la policy: ${policy_compartment}"
    echo "  1) ${stmt_secret}"
    echo "  2) ${stmt_adb}"
    [[ -z "$dg_id" || -z "$policy_id" ]] && log_warn "Aplicá lo anterior manualmente en la consola OCI (Identity → Policies) si no se creó arriba."

    # ── 5) Resumen para el .env ────────────────────────────────────────────────
    # Nombres EXACTOS esperados por src/config.py (Config dataclass, Track B):
    #   ADB_USER, ADB_PASSWORD, ADB_DSN, ADB_WALLET_SECRET_OCID,
    #   ADB_WALLET_DIR (default ya es /dev/shm/amalia_wallet), ADB_WALLET_PASSWORD.
    echo ""
    log_step "Variables para .env (tenant: ${tenant})"
    echo "  ENABLE_MEMORIA=true"
    echo "  ADB_USER=AMALIA                          # usuario de esquema — se crea en db/schema.sql (B0.3), NO en este script"
    echo "  ADB_PASSWORD=<pendiente-de-B0.3>          # password del usuario AMALIA — definila al correr db/schema.sql"
    echo "  ADB_DSN=$(state_get "$tenant" adb_dsn)"
    echo "  ADB_WALLET_SECRET_OCID=${secret_id}"
    echo "  ADB_WALLET_PASSWORD=${wallet_password}    # guardala ahora — no se persiste en ningún archivo por este script"
    echo "  # ADB_WALLET_DIR ya tiene un default correcto (/dev/shm/amalia_wallet) — no hace falta setearla"
    echo ""
    log_ok "adb_setup completo para '${tenant}'. adb_ocid=${adb_id}"
    unset wallet_password
}
