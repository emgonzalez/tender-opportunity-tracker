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
# Fase 2 · Track B (B6.3) — opt-in, default "false": si no está en tenants.yaml
# o está en false, NO cambia nada del comportamiento actual (una sola unit).
# Hay tenants en producción con el modo de una sola unit — es innegociable.
TWO_STAGE=$(tenant_get_or "$TENANT" instance.two_stage "false")

if [[ "$TWO_STAGE" == "True" && "$EPHEMERAL" != "True" ]]; then
    # two_stage solo tiene sentido dentro del modo ephemeral (docker run --rm
    # por corrida). Avisamos por stderr sin tocar el stdout (que es el YAML).
    log_warn "instance.two_stage=true sin instance.ephemeral_container=true en '${TENANT}' — se ignora (no se generan units de dos etapas)." >&2
fi

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
  if [[ "$TWO_STAGE" == "True" ]]; then
    # ── Fase 2 · Track B (B6.3): dos etapas efímeras encadenadas ──────────────
    # Por qué dos etapas y no una: el pico de RAM de Chromium (etapa scrape,
    # navegador headless para extraer/descargar pliegos) y el pico del análisis
    # LLM (etapa analyze, sin navegador) NUNCA deben coexistir en una VM de
    # ~1 GB (E2.1.Micro). Cada `docker compose run --rm` arranca en baseline y
    # se destruye al terminar, así que separarlos en dos `docker run` distintos
    # (en vez de un solo proceso Python que hace todo) garantiza que los dos
    # picos de RAM nunca se sumen — el segundo container ni siquiera existe
    # mientras el primero corre. El handoff entre etapas es durable (manifiesto
    # JSON + PDFs en disco, ver src/main.py: _stage_scrape/_stage_analyze), así
    # que un crash entre etapas no pierde trabajo.
    EXTRA_FILES+="
  - path: /etc/systemd/system/amalia-scrape.service
    permissions: '0644'
    content: |
      [Unit]
      Description=AMALIA — etapa A (scrape): navegador headless, extrae y descarga pliegos
      Requires=docker.service
      After=docker.service
      OnFailure=amalia-failure.service
      # Encadenamiento a la etapa de análisis. OnSuccess= existe en systemd desde
      # hace muchas versiones (bastante antes de la serie 240) y Ubuntu 22.04
      # (jammy) trae systemd 249.11, así que es válido acá sin condiciones.
      # Fallback documentado (no activo) por si algún día esto corriera sobre
      # una imagen con systemd viejo o una versión que no honre OnSuccess=:
      # agregar 'ExecStartPost=/bin/systemctl start --no-block amalia-analyze.service'
      # en la sección [Service] de abajo. Ver oci/README.md.
      OnSuccess=amalia-analyze.service
      StartLimitIntervalSec=86400
      StartLimitBurst=3

      [Service]
      Type=oneshot
      WorkingDirectory=/opt/tender-opportunity-tracker
      User=ubuntu
      Group=ubuntu
      ExecStart=/usr/bin/docker compose -f ${COMPOSE_FILE} run --rm amalia python -m src.main --stage scrape
      # Fallback de encadenamiento (ver comentario en [Unit] OnSuccess= arriba):
      # ExecStartPost=/bin/systemctl start --no-block amalia-analyze.service
      TimeoutStartSec=30min
      Restart=on-failure
      RestartSec=1h

  - path: /etc/systemd/system/amalia-scrape.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Disparar amalia-scrape (etapa A) diariamente a las ${SCHEDULE_TIME} hora El Salvador

      [Timer]
      OnCalendar=*-*-* ${SCHEDULE_TIME}:00
      Persistent=true
      Unit=amalia-scrape.service

      [Install]
      WantedBy=timers.target

  - path: /etc/systemd/system/amalia-analyze.service
    permissions: '0644'
    content: |
      [Unit]
      Description=AMALIA — etapa B (analyze): análisis LLM + notificación, sin navegador
      Requires=docker.service
      After=docker.service
      OnFailure=amalia-failure.service
      StartLimitIntervalSec=86400
      StartLimitBurst=3

      [Service]
      Type=oneshot
      WorkingDirectory=/opt/tender-opportunity-tracker
      User=ubuntu
      Group=ubuntu
      ExecStart=/usr/bin/docker compose -f ${COMPOSE_FILE} run --rm amalia python -m src.main --stage analyze
      TimeoutStartSec=30min
      Restart=on-failure
      RestartSec=1h

  - path: /etc/systemd/system/amalia-failure.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Notify failure of amalia-scrape.service or amalia-analyze.service

      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/amalia-on-failure.sh

  - path: /usr/local/bin/amalia-on-failure.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      LOG=/var/log/amalia-failures.log
      TS=\$(date -Iseconds)
      echo \"[\$TS] AMALIA stage FAILED — see journalctl -u amalia-scrape.service or amalia-analyze.service\" | tee -a \"\$LOG\"
      logger -t amalia -p user.err \"Run failed at \$TS\""
  else
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
  if [[ "$TWO_STAGE" == "True" ]]; then
    # Solo se habilita/dispara el timer de la etapa scrape: amalia-analyze
    # se dispara sola vía OnSuccess= al terminar scrape con éxito (ver arriba).
    # Verificación post-cloud-init (STATUS.md, incidente RUN-001 2026-05-14):
    # un `enable --now` puede dejar el timer en 'inactive (dead)' por una
    # condición de carrera de systemd durante el primer boot. Confirmamos y,
    # si hiciera falta, forzamos el arranque explícitamente.
    EXTRA_RUNCMD+="
  - systemctl daemon-reload
  - systemctl enable --now amalia-scrape.timer
  - systemctl is-active amalia-scrape.timer || systemctl start amalia-scrape.timer
  - systemctl start amalia-scrape.service --no-block"
  else
    # Persistent=true del timer no garantiza catch-up cuando NO hay stamp file
    # previo (primera carga). Disparamos el service explícitamente para que
    # la primera corrida ocurra al final del cloud-init y no al día siguiente.
    EXTRA_RUNCMD+="
  - systemctl daemon-reload
  - systemctl enable --now amalia.timer
  - systemctl start amalia.service --no-block"
  fi
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
