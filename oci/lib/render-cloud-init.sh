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
SWAP_GB=$(tenant_get_or "$TENANT" instance.swap_size_gb "0")
ZRAM_PCT=$(tenant_get_or "$TENANT" instance.zram_percent "0")
EARLYOOM=$(tenant_get_or "$TENANT" instance.earlyoom "false")
SWAPPINESS=$(tenant_get_or "$TENANT" instance.swappiness "")
EPHEMERAL=$(tenant_get_or "$TENANT" instance.ephemeral_container "false")
SCHEDULE_TIME=$(tenant_get_or "$TENANT" instance.schedule_time "07:00")
COMPOSE_FILE=$(tenant_get_or "$TENANT" docker.compose_file "docker-compose.oci.yml")

# Watchdog de costo (siempre presente)
WATCHDOG_CONTENT=$(sed \
    -e "s|__THRESHOLD__|$THRESHOLD|g" \
    -e "s|__TENANCY__|$TENANCY|g" \
    "${OCI_DIR}/lib/cost-watchdog.sh" | base64 | tr -d '\n')

if [[ "$AUTO_STOP" == "True" ]]; then
    CRON_LINE='*/30 * * * * root /usr/local/bin/cost-watchdog.sh'
else
    CRON_LINE='# auto-stop disabled'
fi

# ── Swap (file-backed) ───────────────────────────────────────────────────────
SWAP_BLOCK=""
if [[ "$SWAP_GB" != "0" && -n "$SWAP_GB" ]]; then
    SWAP_BLOCK=$(cat <<YAML
swap:
  filename: /swapfile
  size: "${SWAP_GB}G"
  maxsize: "${SWAP_GB}G"
YAML
)
fi

# ── Paquetes adicionales según features activos ──────────────────────────────
EXTRA_PACKAGES=""
if [[ "$ZRAM_PCT" != "0" && -n "$ZRAM_PCT" ]]; then
    EXTRA_PACKAGES+="
  - zram-tools"
fi
if [[ "$EARLYOOM" == "True" ]]; then
    EXTRA_PACKAGES+="
  - earlyoom"
fi

# ── write_files dinámicos ────────────────────────────────────────────────────
EXTRA_FILES=""

if [[ "$ZRAM_PCT" != "0" && -n "$ZRAM_PCT" ]]; then
    EXTRA_FILES+="
  - path: /etc/default/zramswap
    permissions: '0644'
    content: |
      ALGO=lz4
      PERCENT=${ZRAM_PCT}
      PRIORITY=100"
fi

if [[ "$EARLYOOM" == "True" ]]; then
    EXTRA_FILES+="
  - path: /etc/default/earlyoom
    permissions: '0644'
    content: |
      EARLYOOM_ARGS=\"-m 10 -s 5 -r 60 --prefer '(^|/)(chrome|chromium)\$' --avoid '(^|/)(sshd|systemd|init|dockerd)\$'\""
fi

if [[ -n "$SWAPPINESS" ]]; then
    EXTRA_FILES+="
  - path: /etc/sysctl.d/99-tot.conf
    permissions: '0644'
    content: |
      vm.swappiness=${SWAPPINESS}"
fi

# ── Modo ephemeral: systemd service + timer en lugar de daemon ───────────────
if [[ "$EPHEMERAL" == "True" ]]; then
    EXTRA_FILES+="
  - path: /etc/systemd/system/amalia.service
    permissions: '0644'
    content: |
      [Unit]
      Description=AMALIA — corrida diaria
      Requires=docker.service
      After=docker.service
      OnFailure=amalia-failure.service
      StartLimitIntervalSec=86400
      StartLimitBurst=3

      [Service]
      Type=simple
      WorkingDirectory=/opt/tender-opportunity-tracker
      User=ubuntu
      Group=ubuntu
      ExecStart=/usr/bin/docker compose -f ${COMPOSE_FILE} run --rm amalia python -m src.main --run-now
      TimeoutStartSec=30min
      Restart=on-failure
      RestartSec=1h

  - path: /etc/systemd/system/amalia.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Disparar amalia diariamente a las ${SCHEDULE_TIME} hora El Salvador

      [Timer]
      OnCalendar=*-*-* ${SCHEDULE_TIME}:00
      Persistent=true
      Unit=amalia.service

      [Install]
      WantedBy=timers.target

  - path: /etc/systemd/system/amalia-failure.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Notify failure of amalia.service

      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/amalia-on-failure.sh

  - path: /usr/local/bin/amalia-on-failure.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      LOG=/var/log/amalia-failures.log
      TS=\$(date -Iseconds)
      echo \"[\$TS] amalia.service FAILED — see journalctl -u amalia.service\" | tee -a \"\$LOG\"
      logger -t amalia -p user.err \"Run failed at \$TS\""
fi

# ── runcmd dinámico ──────────────────────────────────────────────────────────
EXTRA_RUNCMD=""
if [[ "$ZRAM_PCT" != "0" && -n "$ZRAM_PCT" ]]; then
    EXTRA_RUNCMD+="
  - systemctl enable zramswap.service
  - systemctl restart zramswap.service"
fi
if [[ "$EARLYOOM" == "True" ]]; then
    EXTRA_RUNCMD+="
  - systemctl enable earlyoom.service
  - systemctl restart earlyoom.service"
fi
if [[ -n "$SWAPPINESS" ]]; then
    EXTRA_RUNCMD+="
  - sysctl --system"
fi
if [[ "$EPHEMERAL" == "True" ]]; then
    # Persistent=true del timer no garantiza catch-up cuando NO hay stamp file
    # previo (primera carga). Disparamos el service explícitamente para que
    # la primera corrida ocurra al final del cloud-init y no al día siguiente.
    EXTRA_RUNCMD+="
  - systemctl daemon-reload
  - systemctl enable --now amalia.timer
  - systemctl start amalia.service --no-block"
fi

# ── Cloud-init final ─────────────────────────────────────────────────────────
cat <<YAML
#cloud-config
package_update: true
package_upgrade: true

# Timezone del sistema: necesario para que el systemd timer (OnCalendar)
# dispare a las HH:MM hora local El Salvador, no UTC.
timezone: America/El_Salvador

${SWAP_BLOCK}

packages:
  - docker.io
  - docker-compose-v2
  - unzip
  - jq
  - rsync${EXTRA_PACKAGES}

write_files:
  - path: /usr/local/bin/cost-watchdog.sh
    permissions: '0755'
    encoding: b64
    content: ${WATCHDOG_CONTENT}
  - path: /etc/cron.d/cost-watchdog
    permissions: '0644'
    content: |
      ${CRON_LINE}${EXTRA_FILES}

runcmd:
  - systemctl enable docker
  - systemctl start docker
  - usermod -aG docker ubuntu
  - mkdir -p /opt/tender-opportunity-tracker/data
  - chown -R ubuntu:ubuntu /opt/tender-opportunity-tracker
  # 'data/' permite escritura para que el container (corre como botuser con
  # UID distinto al de ubuntu) pueda persistir storage_state, screenshots y
  # logs vía bind mount.
  - chmod -R a+rwX /opt/tender-opportunity-tracker/data
  # Anti-reclamación: actividad CPU cada 6h para que OCI no reclame la VM
  - echo '0 */6 * * * root dd if=/dev/urandom bs=1M count=50 of=/dev/null 2>/dev/null' > /etc/cron.d/oci-keepalive
  - chmod 644 /etc/cron.d/oci-keepalive${EXTRA_RUNCMD}
YAML
