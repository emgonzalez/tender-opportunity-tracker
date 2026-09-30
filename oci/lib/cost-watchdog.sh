#!/usr/bin/env bash
# cost-watchdog.sh — Corre EN LA VM via cron cada 30 min
#
# Usa Instance Principal (sin keys en disco) para consultar el gasto del mes.
# Si supera el threshold, libera TODOS los recursos que generan cargo:
#   1. Para los contenedores Docker (preserva data en /opt/.../data)
#   2. Libera la ephemeral public IPv4 (Oracle cobra ~$0.005/h por IPv4 idle desde 2025-02)
#   3. STOP de la VM (compute deja de cobrar; Always Free no cobra de todos modos)
#
# El boot volume queda preservado dentro de los 200 GB Always Free.
# Para limpieza total (incluida red, IGW, etc.) usá ./oci/destroy.sh desde local.
set -euo pipefail

THRESHOLD_USD="__THRESHOLD__"   # reemplazado por render-cloud-init.sh
TENANCY_OCID="__TENANCY__"
COMPARTMENT_OCID="__COMPARTMENT__"
LOG=/var/log/cost-watchdog.log

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

log "watchdog: iniciando check (threshold=\$${THRESHOLD_USD})"

# ── Auth via Instance Principal (sin keys en disco) ──────────────────────────
export OCI_CLI_AUTH=instance_principal

# ── Identidad propia desde el metadata service ───────────────────────────────
INSTANCE_ID=$(curl -fsS -H 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/instance/id || true)
[[ -n "$INSTANCE_ID" ]] || { log "watchdog: no pude obtener instance-id del metadata service"; exit 1; }

# ── Consulta gasto del mes en curso (UTC), acotado a ESTE compartment ────────
# Dos bugs encontrados en vivo el 2026-09-29, los dos silenciosos (caían en el
# "skip" de abajo en vez de avisar):
#   1. --time-usage-ended con hora/minuto/segundo distinto de 0 —la hora actual,
#      como quedaba antes— la API la rechaza con 400 InvalidParameter cuando
#      --granularity es MONTHLY. Tiene que venir truncado a medianoche.
#   2. Sin --filter por compartmentId, "gasto del mes" era el de TODA la
#      tenancy de MULTICOMPUTOS (Servicios_MC, nagios-poc, zabbix-poc, etc.:
#      $67.98 ese día) contra un threshold pensado solo para este proyecto
#      ($0.79 real en AMALIA_DEV ese mismo día) — hubiera apagado la VM de
#      AMALIA por gasto ajeno.
START="$(date -u +%Y-%m-01T00:00:00Z)"
END="$(date -u +%Y-%m-%dT00:00:00Z)"
FILTER="{\"operator\":\"AND\",\"dimensions\":[{\"key\":\"compartmentId\",\"value\":\"${COMPARTMENT_OCID}\"}]}"

USAGE_JSON=$(oci usage-api usage-summary request-summarized-usages \
    --time-usage-started "$START" \
    --time-usage-ended "$END" \
    --tenant-id "$TENANCY_OCID" \
    --granularity MONTHLY \
    --query-type COST \
    --filter "$FILTER" 2>>"$LOG" || true)

if [[ -z "$USAGE_JSON" ]]; then
    log "watchdog: no se pudo consultar usage-api (¿policy/dynamic-group?). Skip."
    exit 0
fi

CURRENT=$(echo "$USAGE_JSON" | jq '[.data.items[].computed-amount // 0] | add // 0')
log "watchdog: gasto actual=\$${CURRENT} threshold=\$${THRESHOLD_USD}"

EXCEEDED=$(awk -v c="$CURRENT" -v t="$THRESHOLD_USD" 'BEGIN { print (c >= t) ? 1 : 0 }')

if [[ "$EXCEEDED" != "1" ]]; then
    log "watchdog: dentro del cap"
    exit 0
fi

log "watchdog: THRESHOLD SUPERADO — liberando recursos billables"

# ── 1. Apagar contenedores ───────────────────────────────────────────────────
cd /opt/tender-opportunity-tracker || true
docker compose down 2>>"$LOG" || true
log "watchdog: docker stopped"

# ── 2. Liberar la ephemeral public IPv4 ──────────────────────────────────────
# Mitiga el cargo de Oracle por IPv4 idle (~$0.005/h desde 2025-02-01).
# Una ephemeral IP, al ser deleted, se libera completamente y deja de cobrar.
VNIC_ID=$(oci compute instance list-vnics --instance-id "$INSTANCE_ID" \
    --query 'data[0].id' --raw-output 2>>"$LOG" || true)
if [[ -n "$VNIC_ID" ]]; then
    PUBLIC_IP_ID=$(oci network public-ip get-by-private-ip-id \
        --private-ip-id "$(oci network private-ip list --vnic-id "$VNIC_ID" --query 'data[0].id' --raw-output)" \
        --query 'data.id' --raw-output 2>>"$LOG" || true)
    if [[ -n "$PUBLIC_IP_ID" && "$PUBLIC_IP_ID" != "null" ]]; then
        oci network public-ip delete --public-ip-id "$PUBLIC_IP_ID" --force 2>>"$LOG" || true
        log "watchdog: ephemeral public IP liberada ($PUBLIC_IP_ID)"
    else
        log "watchdog: no había public IP attached"
    fi
fi

# ── 3. STOP de la VM (compute deja de facturar) ──────────────────────────────
# Nota: con Always Free el compute no factura de todos modos, pero stop
# garantiza no acumular ningún cargo si por alguna razón salimos del límite.
oci compute instance action --action STOP --instance-id "$INSTANCE_ID" 2>>"$LOG" || true
log "watchdog: instance STOP solicitado — fin"
