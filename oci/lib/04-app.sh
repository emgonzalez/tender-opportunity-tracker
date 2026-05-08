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
    local public_ip ssh_key
    public_ip=$(state_get "$tenant" public_ip)
    ssh_key="${OCI_DIR}/state/${tenant}_id_ed25519"
    [[ -n "$public_ip" ]] || log_die "public_ip no en state"
    [[ -f "$ssh_key" ]] || log_die "SSH key no encontrada en $ssh_key"

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

    # ── Build + up ────────────────────────────────────────────────────────────
    log_step "Construyendo imagen Docker en la VM..."
    ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f docker-compose.oci.yml build" >/dev/null
    log_ok "Imagen construida"

    log_step "Iniciando contenedor..."
    ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f docker-compose.oci.yml up -d"
    log_ok "Contenedor levantado"

    # ── Sesión COMPRASAL inicial ──────────────────────────────────────────────
    log_warn "Recordá: si no hay storage_state.json en data/ vas a tener que correr el login una vez:"
    log_info "  ssh -i $ssh_key $remote"
    log_info "  cd $app_dir && docker compose -f docker-compose.oci.yml run --rm tender-opportunity-tracker python -m src.auth.login --manual"
}
