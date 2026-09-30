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
COMPARTMENT=$(tenant_get "$TENANT" compartment_ocid)
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
# Fase 5 — respaldo verificado del historial. Opt-in, default "false".
BACKUP_ENABLED=$(tenant_get_or "$TENANT" backup.enabled "false")
BACKUP_TIME=$(tenant_get_or "$TENANT" backup.schedule_time "09:00")

# ── Hora del chequeo de imagen nueva (auto-update) ───────────────────────────
# Se DERIVA de schedule_time en vez de ser configurable: 90 minutos antes de la
# corrida diaria. Antes, porque el sentido de auto-actualizarse es que el
# reporte de la mañana ya salga con el código nuevo; 90 minutos, porque la
# corrida puede durar hasta 30 (TimeoutStartSec) y bajar ~975 MB en un
# E2.1.Micro tarda unos minutos: hay margen de sobra para que una cosa termine
# antes de que empiece la otra. Una key más en tenants.yaml solo agregaría una
# forma de configurar las dos horas de manera incoherente.
IFS=: read -r _sched_h _sched_m <<<"${SCHEDULE_TIME}"
_pull_min=$(( (10#${_sched_h:-7} * 60 + 10#${_sched_m:-0} - 90 + 1440) % 1440 ))
PULL_TIME=$(printf '%02d:%02d' $((_pull_min / 60)) $((_pull_min % 60)))

if [[ "$TWO_STAGE" == "True" && "$EPHEMERAL" != "True" ]]; then
    # two_stage solo tiene sentido dentro del modo ephemeral (docker run --rm
    # por corrida). Avisamos por stderr sin tocar el stdout (que es el YAML).
    log_warn "instance.two_stage=true sin instance.ephemeral_container=true en '${TENANT}' — se ignora (no se generan units de dos etapas)." >&2
fi

# Script de auto-actualización (se instala tal cual, sin sustituciones: todo lo
# que necesita saber lo lee del .env de la VM en tiempo de ejecución).

# Watchdog de costo (siempre presente)
WATCHDOG_CONTENT=$(sed \
    -e "s|__THRESHOLD__|$THRESHOLD|g" \
    -e "s|__TENANCY__|$TENANCY|g" \
    -e "s|__COMPARTMENT__|$COMPARTMENT|g" \
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
if [[ "$AUTO_STOP" == "True" ]]; then
    # cost-watchdog.sh llama al binario `oci` (usage-api, compute, network) vía
    # Instance Principal. Nada más en este cloud-init lo instala — hasta el
    # 2026-09-29 el watchdog nunca había corrido de punta a punta en una VM real
    # (auto_stop estuvo en false en los cuatro tenants), así que esta ausencia
    # nunca se notó: el script hacía `oci: command not found`, lo trataba igual
    # que un fallo de permisos y salía en skip silencioso.
    EXTRA_PACKAGES+="
  - python3-pip"
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
      echo \"[\$TS] AMALIA unit FAILED — ver: systemctl --failed y journalctl -u 'amalia*'\" | tee -a \"\$LOG\"
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
      echo \"[\$TS] AMALIA unit FAILED — ver: systemctl --failed y journalctl -u 'amalia*'\" | tee -a \"\$LOG\"
      logger -t amalia -p user.err \"Run failed at \$TS\""
  fi

  # ── Auto-actualización de la imagen (Fase 4) ───────────────────────────────
  # Las units se instalan SIEMPRE en modo ephemeral, pero el que decide si hacen
  # algo es AMALIA_CHANNEL en el .env, que escribe 04-app.sh desde
  # `docker.channel` de tenants.yaml. La separación importa: cloud-init corre una
  # sola vez en la vida de la VM, así que si el canal viviera acá adentro,
  # activarlo o cambiarlo obligaría a recrear la instancia. Viviendo en el .env
  # alcanza con editar tenants.yaml y redesplegar.
  # Sin canal el script sale 0 sin tocar nada, así que instalar esto no cambia
  # el comportamiento de ningún tenant existente.
  EXTRA_FILES+="
  - path: /etc/systemd/system/amalia-pull-deploy.service
    permissions: '0644'
    content: |
      [Unit]
      Description=AMALIA — busca imagen nueva en el canal configurado y la activa
      Requires=docker.service
      After=docker.service network-online.target
      Wants=network-online.target
      OnFailure=amalia-failure.service

      [Service]
      Type=oneshot
      WorkingDirectory=/opt/tender-opportunity-tracker
      User=ubuntu
      Group=ubuntu
      # HOME explícito porque de ahí sale ~/.docker/config.json, donde quedó el
      # 'docker login ghcr.io' que hizo el deploy. Sin credenciales el pull de un
      # paquete privado falla como 'unauthorized', que se confunde con un PAT
      # vencido y manda a diagnosticar al lugar equivocado.
      Environment=HOME=/home/ubuntu
      ExecStart=/opt/tender-opportunity-tracker/oci/lib/amalia-pull-deploy.sh
      # Bajar ~975 MB comprimidos en un E2.1.Micro puede tardar; el margen hasta
      # la corrida de las ${SCHEDULE_TIME} es de 90 minutos, así que 25 acota el
      # cuelgue sin arriesgar un timeout espurio.
      TimeoutStartSec=25min
      # Sin Restart= a propósito: el fallo esperado es un PAT vencido, y
      # reintentarlo en loop solo llenaría el journal. Que quede en 'failed' es
      # justamente la señal que queremos que sobreviva hasta que alguien mire.

  - path: /etc/systemd/system/amalia-pull-deploy.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Buscar imagen nueva a las ${PULL_TIME}, 90 min antes de la corrida de las ${SCHEDULE_TIME}

      [Timer]
      OnCalendar=*-*-* ${PULL_TIME}:00
      Persistent=true
      Unit=amalia-pull-deploy.service

      [Install]
      WantedBy=timers.target"
fi

# ── Fase 5: respaldo verificado del historial ────────────────────────────────
# El script y las units NO se escriben acá adentro: viven en oci/lib/backup/ y se
# embeben tal cual. Ese es su único hogar, y por eso `backup_setup` (07-backup.sh)
# puede instalar EXACTAMENTE lo mismo en una VM que ya existe, sin que haya dos
# copias que puedan divergir.
#
# Lo que NO viaja en el cloud-init es la URL del PAR. El user-data de una
# instancia es legible desde su metadata service, así que una URL firmada ahí
# sería un secreto publicado. La deja `backup_setup` por scp en
# /etc/amalia/backup.par (0600).
#
# Por lo mismo, el timer se instala pero NO se habilita acá: sin PAR el respaldo
# fallaría todos los días y mandaría una alerta diaria por un problema que en
# realidad es "todavía no lo configuraron". Lo habilita `backup_setup`, que es
# quien pone el PAR — el respaldo se enciende cuando puede funcionar.
if [[ "$BACKUP_ENABLED" == "True" ]]; then
    BK_SVC=$(sed "s|__COMPOSE__|${COMPOSE_FILE}|g" "${OCI_DIR}/lib/backup/amalia-backup.service" | base64 | tr -d '\n')
    BK_TMR=$(sed "s|__SCHEDULE__|${BACKUP_TIME}|g" "${OCI_DIR}/lib/backup/amalia-backup.timer" | base64 | tr -d '\n')
    BK_FAIL=$(sed "s|__COMPOSE__|${COMPOSE_FILE}|g" "${OCI_DIR}/lib/backup/amalia-backup-failure.service" | base64 | tr -d '\n')

    EXTRA_FILES+="
  - path: /etc/systemd/system/amalia-backup.service
    permissions: '0644'
    encoding: b64
    content: ${BK_SVC}
  - path: /etc/systemd/system/amalia-backup.timer
    permissions: '0644'
    encoding: b64
    content: ${BK_TMR}
  - path: /etc/systemd/system/amalia-backup-failure.service
    permissions: '0644'
    encoding: b64
    content: ${BK_FAIL}"
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
if [[ "$AUTO_STOP" == "True" ]]; then
    # pip, no el installer oficial (curl | bash desde GitHub): mismo resultado,
    # una dependencia menos en el primer boot (GitHub Raw en vez de solo los
    # mirrors de apt). Ubuntu 22.04 (jammy) es anterior a la restricción PEP 668
    # de Python — no hace falta --break-system-packages acá.
    EXTRA_RUNCMD+="
  - pip3 install --quiet oci-cli"
fi
if [[ "$BACKUP_ENABLED" == "True" ]]; then
    # Directorios que el respaldo necesita poder escribir corriendo como ubuntu:
    # /etc/amalia (donde backup_setup dejará el PAR), /var/lib/amalia (el sello
    # del último respaldo verificado) y su log. Se crean acá para que backup_setup
    # solo tenga que dejar el archivo. El timer queda instalado pero apagado
    # (ver el comentario del bloque de write_files).
    EXTRA_RUNCMD+="
  - install -d -m 0750 -o ubuntu -g ubuntu /etc/amalia /var/lib/amalia
  - install -m 0644 -o ubuntu -g ubuntu /dev/null /var/log/amalia-backup.log
  - systemctl daemon-reload"
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
  # El timer de auto-actualización se habilita en los dos modos. No se dispara
  # ahora: en este punto del primer boot todavía no existe el .env (lo copia
  # 04-app.sh después), así que el script no tendría nada que leer.
  EXTRA_RUNCMD+="
  - systemctl enable --now amalia-pull-deploy.timer
  - systemctl is-active amalia-pull-deploy.timer || systemctl start amalia-pull-deploy.timer"
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
