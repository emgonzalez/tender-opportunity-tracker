#!/usr/bin/env bash
# 03-budget.sh — Configura Budget + Notification Topic + Email Subscription
#
# Crea (en root tenancy, donde Budgets debe vivir):
#   - Notification Topic
#   - Email subscription (requiere confirmación manual del email)
#   - Budget con threshold y alert rule
#   - Dynamic Group (matches la instancia por su OCID)
#   - Policy: permite a la VM consultar usage-api (para el watchdog interno)
#
# El auto-stop real lo dispara el cost-watchdog dentro de la VM (cron cada 30 min).
# Este script habilita la identidad (Instance Principal) que el watchdog usa.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

budget_setup() {
    local tenant=$1
    local enabled; enabled=$(tenant_get "$tenant" budget.enabled)
    [[ "$enabled" == "True" ]] || { log_warn "Budget deshabilitado para $tenant — skip"; return 0; }

    local tenancy compartment threshold email inst_id
    tenancy=$(tenant_get "$tenant" tenancy_ocid)
    compartment=$(tenant_get "$tenant" compartment_ocid)
    threshold=$(tenant_get "$tenant" budget.threshold_usd)
    email=$(tenant_get "$tenant" budget.alert_email)
    inst_id=$(state_get "$tenant" instance_id)
    [[ -n "$inst_id" ]] || log_die "instance_id no en state — corré 02-compute primero"

    # Etiquetas del tenant → OCI_TAG_ARGS (ver "Etiquetas" en common.sh). Acá
    # importan doble: los cuatro recursos de este script viven en el ROOT
    # TENANCY, mezclados con los de los demás tenants y con los de cualquier
    # otro sistema. La clave `tenant` es lo que después permite borrar los de
    # uno sin tocar los del otro.
    tenant_tag_args "$tenant"

    # ── Notification Topic + Subscription ─────────────────────────────────────
    log_step "Creando Notification Topic..."
    local topic_id; topic_id=$(state_get "$tenant" topic_id)
    if [[ -z "$topic_id" ]] || ! ociq ons topic get --topic-id "$topic_id" &>/dev/null; then
        topic_id=$(ociq ons topic create \
            --compartment-id "$tenancy" \
            --name "tot-${tenant}-cost-alerts" \
            --description "Cost alerts for tender-opportunity-tracker (${tenant})" \
            "${OCI_TAG_ARGS[@]}" \
            --query 'data."topic-id"' --raw-output)
        state_set "$tenant" topic_id "$topic_id"
        log_ok "Topic creado: $topic_id"
    else
        log_ok "Topic ya existe: $topic_id"
    fi

    log_step "Suscribiendo email al topic..."
    local sub_exists
    sub_exists=$(ociq ons subscription list --compartment-id "$tenancy" --query "data[?\"topic-id\"=='$topic_id' && endpoint=='$email'] | length(@)" --raw-output 2>/dev/null || echo 0)
    if [[ "$sub_exists" == "0" ]]; then
        ociq ons subscription create \
            --compartment-id "$tenancy" \
            --topic-id "$topic_id" \
            --protocol EMAIL \
            --subscription-endpoint "$email" >/dev/null
        log_warn "Subscription creada — REVISA $email y confirmá el link de Oracle para activarla"
    else
        log_ok "Subscription ya existe para $email"
    fi

    # ── Budget ────────────────────────────────────────────────────────────────
    log_step "Creando Budget (threshold: \$${threshold} USD)..."
    local budget_id; budget_id=$(state_get "$tenant" budget_id)
    if [[ -z "$budget_id" ]] || ! ociq budgets budget budget get --budget-id "$budget_id" &>/dev/null; then
        # Budgets viven en el root tenancy y "miden" un compartment target.
        budget_id=$(ociq budgets budget budget create \
            --compartment-id "$tenancy" \
            --target-compartment-id "$tenancy" \
            --amount "$threshold" \
            --reset-period MONTHLY \
            --display-name "tot-${tenant}-cost-cap" \
            --description "Auto-cap a \$${threshold} para tender-opportunity-tracker" \
            "${OCI_TAG_ARGS[@]}" \
            --query 'data.id' --raw-output)
        state_set "$tenant" budget_id "$budget_id"
        log_ok "Budget creado: $budget_id"
    else
        log_ok "Budget ya existe: $budget_id"
    fi

    log_step "Configurando Alert Rule..."
    local alert_exists
    alert_exists=$(ociq budgets budget alert-rule list --budget-id "$budget_id" --query 'data | length(@)' --raw-output 2>/dev/null || echo 0)
    if [[ "$alert_exists" == "0" ]]; then
        ociq budgets budget alert-rule create \
            --budget-id "$budget_id" \
            --type ACTUAL \
            --threshold-type PERCENTAGE \
            --threshold 1 \
            --display-name "tot-${tenant}-actual-1pct" \
            --description "Email cuando se consume 1% del cap (≈ \$${threshold} * 1%)" \
            --recipients "$email" \
            --message "Tender Opportunity Tracker en $tenant superó \$${threshold} USD. La instancia se autoapagará por el watchdog interno." >/dev/null
        log_ok "Alert rule creada"
    else
        log_ok "Alert rule ya existe"
    fi

    # ── Dynamic Group + Policy para Instance Principal ────────────────────────
    log_step "Configurando Instance Principal (Dynamic Group + Policy)..."
    local dg_name="tot-${tenant}-watchdog"
    local dg_id; dg_id=$(ociq iam dynamic-group list --query "data[?name=='$dg_name'].id | [0]" --raw-output 2>/dev/null || echo "")
    if [[ -z "$dg_id" || "$dg_id" == "null" ]]; then
        dg_id=$(ociq iam dynamic-group create \
            --compartment-id "$tenancy" \
            --name "$dg_name" \
            --description "Watchdog instances for tender-opportunity-tracker (${tenant})" \
            --matching-rule "instance.id = '$inst_id'" \
            "${OCI_TAG_ARGS[@]}" \
            --query 'data.id' --raw-output)
        log_ok "Dynamic Group creado: $dg_id"
    else
        log_ok "Dynamic Group ya existe: $dg_id"
    fi
    state_set "$tenant" dynamic_group_id "$dg_id"

    local policy_name="tot-${tenant}-watchdog-policy"
    local policy_id; policy_id=$(ociq iam policy list --compartment-id "$tenancy" --query "data[?name=='$policy_name'].id | [0]" --raw-output 2>/dev/null || echo "")
    if [[ -z "$policy_id" || "$policy_id" == "null" ]]; then
        policy_id=$(ociq iam policy create \
            --compartment-id "$tenancy" \
            --name "$policy_name" \
            --description "Permite al watchdog leer usage, autoapagarse y liberar IP pública" \
            --statements "[
                \"Allow dynamic-group $dg_name to read usage-report in tenancy\",
                \"Allow dynamic-group $dg_name to use instance-family in tenancy where target.instance.id = '$inst_id'\",
                \"Allow dynamic-group $dg_name to manage public-ips in tenancy where target.compartment.id = '$compartment'\",
                \"Allow dynamic-group $dg_name to read virtual-network-family in tenancy where target.compartment.id = '$compartment'\"
            ]" \
            "${OCI_TAG_ARGS[@]}" \
            --query 'data.id' --raw-output)
        log_ok "Policy creada: $policy_id"
    else
        log_ok "Policy ya existe: $policy_id"
    fi
    state_set "$tenant" policy_id "$policy_id"
}
