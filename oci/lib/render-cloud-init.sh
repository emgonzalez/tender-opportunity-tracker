#!/usr/bin/env bash
# render-cloud-init.sh — Renderiza cloud-init.yaml con valores del tenant
# Variables esperadas:
#   TENANT (nombre del tenant)
#   OCI_DIR (ruta a oci/)
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

: "${TENANT:?TENANT no definido}"

THRESHOLD=$(tenant_get "$TENANT" budget.threshold_usd)
TENANCY=$(tenant_get "$TENANT" tenancy_ocid)
AUTO_STOP=$(tenant_get "$TENANT" budget.auto_stop)

# Embebe el watchdog con valores ya sustituidos
WATCHDOG_CONTENT=$(sed \
    -e "s|__THRESHOLD__|$THRESHOLD|g" \
    -e "s|__TENANCY__|$TENANCY|g" \
    "${OCI_DIR}/lib/cost-watchdog.sh" | base64 | tr -d '\n')

# Cron line — cada 30 min, solo si auto_stop habilitado
if [[ "$AUTO_STOP" == "True" ]]; then
    CRON_LINE='*/30 * * * * root /usr/local/bin/cost-watchdog.sh'
else
    CRON_LINE='# auto-stop disabled'
fi

cat <<YAML
#cloud-config
package_update: true
package_upgrade: true

packages:
  - docker.io
  - docker-compose-v2
  - unzip
  - jq
  - rsync

write_files:
  - path: /usr/local/bin/cost-watchdog.sh
    permissions: '0755'
    encoding: b64
    content: ${WATCHDOG_CONTENT}
  - path: /etc/cron.d/cost-watchdog
    permissions: '0644'
    content: |
      ${CRON_LINE}

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - usermod -aG docker ubuntu
  - mkdir -p /opt/tender-opportunity-tracker/data
  - chown -R ubuntu:ubuntu /opt/tender-opportunity-tracker
  # Anti-reclamación: cron job que genera actividad CPU cada 6h
  # (Free Tier reclama instancias inactivas tras 7 días)
  - echo '0 */6 * * * root dd if=/dev/urandom bs=1M count=50 of=/dev/null 2>/dev/null' > /etc/cron.d/oci-keepalive
  - chmod 644 /etc/cron.d/oci-keepalive
YAML
