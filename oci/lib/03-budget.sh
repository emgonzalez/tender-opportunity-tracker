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
    # Mismo bug del CLI que el de la Alert Rule más abajo (ver esa nota): `ons
    # subscription list` también imprime stdout vacío con exit 0 cuando no hay
    # ninguna suscripción — confirmado en vivo el 2026-09-29 con `raw-request`
    # (`{"data": []}`), y causó que el script reportara "ya existe" una
    # suscripción que nunca se había creado (el email de confirmación de Oracle
    # nunca salió porque nunca hubo nada que confirmar).
    local sub_exists
    sub_exists=$(ociq ons subscription list --compartment-id "$tenancy" --query "data[?\"topic-id\"=='$topic_id' && endpoint=='$email'] | length(@)" --raw-output 2>/dev/null || echo 0)
    [[ -n "$sub_exists" ]] || sub_exists=0
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
        # Budgets viven en el root tenancy (compartment-id) pero "miden" un
        # compartment target — que tiene que ser el DE ESTE TENANT, no la
        # tenancy entera. Verificado en vivo el 2026-09-29: con target=tenancy,
        # el budget de $0.01 medía TODO el gasto de MULTICOMPUTOS (Servicios_MC,
        # nagios-poc, zabbix-poc, etc. — $67.98 ese día), no solo AMALIA_DEV, y
        # el watchdog habría apagado la VM de AMALIA por gasto ajeno al proyecto.
        budget_id=$(ociq budgets budget budget create \
            --compartment-id "$tenancy" \
            --target-compartment-id "$compartment" \
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
    # Verificado el 2026-09-28: `alert-rule list` sobre un budget SIN alert rules
    # devuelve `{"data": []}` (confirmado con `raw-request`, HTTP 200), pero el
    # comando tipado del CLI imprime stdout vacío para ese caso puntual — no un
    # error, `$?` sigue en 0 — así que el `|| echo 0` de abajo nunca dispara y
    # `alert_exists` queda en "" en vez de "0". Sin este default explícito, la
    # comparación de abajo cae al `else` y el script reporta "ya existe" una
    # alert rule que nunca se creó — pasó una vez en producción, no es hipotético.
    local alert_exists
    alert_exists=$(ociq budgets budget alert-rule list --budget-id "$budget_id" --query 'data | length(@)' --raw-output 2>/dev/null || echo 0)
    [[ -n "$alert_exists" ]] || alert_exists=0
    if [[ "$alert_exists" == "0" ]]; then
        ociq budgets budget alert-rule create \
            --budget-id "$budget_id" \
            --type ACTUAL \
            --threshold-type PERCENTAGE \
            --threshold 80 \
            --display-name "tot-${tenant}-actual-80pct" \
            --description "Email cuando se consume 80% del cap (≈ \$${threshold} * 80%)" \
            --recipients "$email" \
            --message "$tenant consumió el 80% del cap de \$${threshold} USD este mes." >/dev/null
        log_ok "Alert rule creada"
    else
        log_ok "Alert rule ya existe"
    fi

    # ── Dynamic Group + Policy para Instance Principal ────────────────────────
    # Nombres overridables (mismo patrón que adb.dynamic_group_name en 05-adb.sh):
    # si el admin del Landing Zone ya creó estos recursos a mano con otro nombre
    # (Bloque D de PERMISOS-IAM.md), `budget.dynamic_group_name`/`budget.policy_name`
    # en tenants.yaml apuntan ahí y el script los detecta en vez de intentar crear
    # de nuevo — lo cual fallaría sin `manage dynamic-groups in tenancy`.
    log_step "Configurando Instance Principal (Dynamic Group + Policy)..."
    local dg_name; dg_name=$(tenant_get_or "$tenant" budget.dynamic_group_name "tot-${tenant}-watchdog")
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

    local policy_name; policy_name=$(tenant_get_or "$tenant" budget.policy_name "tot-${tenant}-watchdog-policy")
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
