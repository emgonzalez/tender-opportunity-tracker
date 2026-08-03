#!/usr/bin/env bash
# 04-app.sh — Despliega la app en la VM remota
#
# Asume que la VM ya está RUNNING y que cloud-init terminó (instaló docker).
# Pasos:
#   1. Espera a que SSH esté listo
#   2. rsync del repo (excluye .git, data/, secretos)
#   3. Copia el .env local
#   4. Levanta docker compose con el archivo OCI
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

app_deploy() {
    local tenant=$1
    local public_ip ssh_key compose_file
    public_ip=$(state_get "$tenant" public_ip)
    ssh_key="${OCI_DIR}/state/${tenant}_id_ed25519"
    compose_file=$(tenant_get_or "$tenant" docker.compose_file "docker-compose.oci.yml")
    [[ -n "$public_ip" ]] || log_die "public_ip no en state"
    [[ -f "$ssh_key" ]] || log_die "SSH key no encontrada en $ssh_key"
    [[ -f "${REPO_ROOT}/${compose_file}" ]] || log_die "Compose file '${compose_file}' no existe en el repo"

    local ssh_opts=(-i "$ssh_key" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${OCI_DIR}/state/known_hosts" -o LogLevel=ERROR)
    local remote="ubuntu@${public_ip}"
    local app_dir=/opt/tender-opportunity-tracker

    # ── Espera SSH ────────────────────────────────────────────────────────────
    log_step "Esperando que SSH esté listo en $public_ip..."
    local i=0
    until ssh "${ssh_opts[@]}" -o ConnectTimeout=5 "$remote" "echo ok" &>/dev/null; do
        ((i++))
        ((i > 60)) && log_die "SSH no respondió en 5 minutos"
        sleep 5
    done
    log_ok "SSH conectado"

    # ── Espera cloud-init ─────────────────────────────────────────────────────
    log_step "Esperando que cloud-init termine (puede tardar 2-3 min)..."
    ssh "${ssh_opts[@]}" "$remote" "cloud-init status --wait" >/dev/null \
        || log_die "cloud-init falló — revisá /var/log/cloud-init-output.log en la VM"
    log_ok "cloud-init completo"

    # ── Congelar el scheduler durante el deploy ───────────────────────────────
    # Sin esto, una corrida puede dispararse a mitad del despliegue y ejecutarse
    # con código a medio sincronizar o con el .env ANTERIOR (el contenedor toma
    # las variables al arrancar). Eso ya provocó un envío con la política de
    # correo vieja tras arrancar una VM detenida. Se reactiva al final.
    log_step "Deteniendo timer durante el despliegue..."
    ssh "${ssh_opts[@]}" "$remote" \
        "sudo systemctl stop amalia.timer amalia-scrape.timer 2>/dev/null; \
         sudo systemctl stop amalia.service amalia-scrape.service amalia-analyze.service 2>/dev/null; true" \
        >/dev/null 2>&1 || true
    log_ok "Timer detenido (se reactiva al terminar)"

    # ── Sync del repo ─────────────────────────────────────────────────────────
    log_step "Sincronizando código a la VM..."
    rsync -az --delete \
        --exclude '.git' \
        --exclude 'data' \
        --exclude '.env' \
        --exclude 'oci/state' \
        --exclude '__pycache__' \
        --exclude '.pytest_cache' \
        -e "ssh ${ssh_opts[*]}" \
        "${REPO_ROOT}/" "${remote}:${app_dir}/" >/dev/null
    log_ok "Código sincronizado"

    # ── .env ──────────────────────────────────────────────────────────────────
    # La política de correo la decide el TENANT DESTINO, no el .env local: se
    # reescribe NOTIFICATION_OVERRIDE_TO con el valor de tenants.yaml antes de
    # copiar. Así, desplegar a un tenant de validación redirige todo el correo a
    # una casilla de pruebas, y desplegar a producción restaura los destinatarios
    # reales — sin tocar nada a mano y sin que un override local se filtre a prod.
    log_step "Copiando .env..."
    [[ -f "${REPO_ROOT}/.env" ]] || log_die "Falta ${REPO_ROOT}/.env"

    local env_tmp override_to
    env_tmp=$(mktemp "${TMPDIR:-/tmp}/amalia-env.XXXXXX") || log_die "No se pudo crear temporal"
    chmod 600 "$env_tmp"
    # shellcheck disable=SC2064  # se expande ahora a propósito
    trap "rm -f '$env_tmp'" RETURN

    # Variables gobernadas por el tenant: <VAR_ENTORNO> <ruta.en.tenants.yaml> <default>
    local -a tenant_env=(
        "NOTIFICATION_OVERRIDE_TO notification.override_to ''"
        "ENABLE_DRILLDOWN         features.enable_drilldown false"
        "ENABLE_MEMORIA           features.enable_memoria   false"
    )

    # Se eliminan del .env local y se reescriben con el valor del tenant, así el
    # destino del despliegue manda siempre y un residuo local no puede filtrarse.
    cp "${REPO_ROOT}/.env" "$env_tmp"
    local entry var path def val
    for entry in "${tenant_env[@]}"; do
        read -r var path def <<<"$entry"
        [[ "$def" == "''" ]] && def=""
        val=$(tenant_get_or "$tenant" "$path" "$def")
        # normalizar booleanos de YAML (True/False de Python) a minúsculas
        case "$val" in True) val=true ;; False) val=false ;; esac
        grep -v "^[[:space:]]*${var}[[:space:]]*=" "$env_tmp" > "${env_tmp}.n" && mv "${env_tmp}.n" "$env_tmp"
        printf '%s=%s\n' "$var" "$val" >> "$env_tmp"
        [[ "$var" == "NOTIFICATION_OVERRIDE_TO" ]] && override_to="$val"
    done
    chmod 600 "$env_tmp"

    scp "${ssh_opts[@]}" "$env_tmp" "${remote}:${app_dir}/.env" >/dev/null
    if [[ -n "$override_to" ]]; then
        log_ok ".env copiado — MODO VALIDACIÓN: todo el correo se redirige a ${override_to}"
    else
        log_ok ".env copiado — correo a los destinatarios reales de clients/*.yaml (producción)"
    fi
    log_info "  flags del tenant: ENABLE_DRILLDOWN=$(tenant_get_or "$tenant" features.enable_drilldown false | tr 'TF' 'tf') · ENABLE_MEMORIA=$(tenant_get_or "$tenant" features.enable_memoria false | tr 'TF' 'tf')"

    # ── Build de la imagen (modelo ephemeral: no se levanta daemon) ───────────
    log_step "Construyendo imagen Docker en la VM (${compose_file})..."
    log_info "Puede tardar varios minutos (depende del shape); mostrando últimos pasos:"
    ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f ${compose_file} build 2>&1 | tail -20"
    log_ok "Imagen construida"

    # ── Estado del systemd timer ──────────────────────────────────────────────
    log_step "Verificando systemd timer..."
    # Reactivar el scheduler congelado al inicio, ya con el código y el .env
    # definitivos en su lugar. `start` además de `enable` porque un `enable` sin
    # `start` deja el timer inactivo hasta el próximo reinicio — exactamente el
    # fallo de 2026-05-14 documentado en STATUS.md.
    ssh "${ssh_opts[@]}" "$remote" \
        "sudo systemctl enable --now amalia.timer 2>/dev/null; \
         sudo systemctl is-active amalia.timer >/dev/null || sudo systemctl start amalia.timer; true" \
        >/dev/null 2>&1 || true
    if ssh "${ssh_opts[@]}" "$remote" "systemctl is-enabled amalia.timer" &>/dev/null; then
        log_ok "Timer activo — próximo disparo:"
        ssh "${ssh_opts[@]}" "$remote" \
            "systemctl list-timers amalia.timer --no-pager 2>&1 | sed -n '2p'"
    else
        log_warn "Timer no habilitado — la VM no fue creada con ephemeral_container=true"
    fi

    # ── Sesión COMPRASAL inicial ──────────────────────────────────────────────
    log_warn "Si es el primer despliegue, corré el login UNA vez para generar storage_state.json:"
    log_info "  ssh -i $ssh_key $remote"
    log_info "  cd $app_dir && docker compose -f ${compose_file} run --rm amalia python -m src.auth.login --manual"
    log_info ""
    log_info "Para disparar una corrida manual ahora (sin esperar al timer):"
    log_info "  ssh -i $ssh_key $remote 'sudo systemctl start amalia.service'"
}
