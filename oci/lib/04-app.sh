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
    log_step "Copiando .env..."
    [[ -f "${REPO_ROOT}/.env" ]] || log_die "Falta ${REPO_ROOT}/.env"
    scp "${ssh_opts[@]}" "${REPO_ROOT}/.env" "${remote}:${app_dir}/.env" >/dev/null
    log_ok ".env copiado"

    # ── Build de la imagen (modelo ephemeral: no se levanta daemon) ───────────
    log_step "Construyendo imagen Docker en la VM (${compose_file})..."
    log_info "Puede tardar varios minutos (depende del shape); mostrando últimos pasos:"
    ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f ${compose_file} build 2>&1 | tail -20"
    log_ok "Imagen construida"

    # ── Estado del systemd timer ──────────────────────────────────────────────
    log_step "Verificando systemd timer..."
    if ssh "${ssh_opts[@]}" "$remote" "systemctl is-enabled tender-opportunity-tracker.timer" &>/dev/null; then
        log_ok "Timer activo — próximo disparo:"
        ssh "${ssh_opts[@]}" "$remote" \
            "systemctl list-timers tender-opportunity-tracker.timer --no-pager 2>&1 | sed -n '2p'"
    else
        log_warn "Timer no habilitado — la VM no fue creada con ephemeral_container=true"
    fi

    # ── Sesión COMPRASAL inicial ──────────────────────────────────────────────
    log_warn "Si es el primer despliegue, corré el login UNA vez para generar storage_state.json:"
    log_info "  ssh -i $ssh_key $remote"
    log_info "  cd $app_dir && docker compose -f ${compose_file} run --rm tender-opportunity-tracker python -m src.auth.login --manual"
    log_info ""
    log_info "Para disparar una corrida manual ahora (sin esperar al timer):"
    log_info "  ssh -i $ssh_key $remote 'sudo systemctl start tender-opportunity-tracker.service'"
}
