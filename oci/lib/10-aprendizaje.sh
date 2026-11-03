#!/usr/bin/env bash
# 10-aprendizaje.sh — ciclo semanal opt-in, dry-run por defecto.
# Corre desde la laptop después de app_deploy; no depende del timer diario.
# Sin flag o con hold_timers retira ambas units. Fallos remotos no rompen deploy.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

_aprendizaje_cron_enabled() {
    local tenant=$1
    [[ "$(tenant_get_or "$tenant" features.enable_aprendizaje_cron false)" == "True" ]] || return 1
    # Fase M: un tenant en sombra no recibe timers nuevos.
    [[ "$(tenant_get_or "$tenant" instance.hold_timers false)" != "True" ]]
}

aprendizaje_cron_sync() {
    local tenant=$1 remote=$2; shift 2
    local -a ssh_opts=("$@")

    if ! _aprendizaje_cron_enabled "$tenant"; then
        # Solo actúa si la unit existe: en una VM que nunca la tuvo no cambia nada.
        if ssh "${ssh_opts[@]}" "$remote" bash -s <<'REMOTE'
set -euo pipefail
if [ -f /etc/systemd/system/amalia-aprendizaje.timer ] || [ -f /etc/systemd/system/amalia-aprendizaje.service ]; then
    if [ -f /etc/systemd/system/amalia-aprendizaje.timer ]; then
        sudo systemctl disable --now amalia-aprendizaje.timer
    fi
    if [ -f /etc/systemd/system/amalia-aprendizaje.service ]; then
        sudo systemctl stop amalia-aprendizaje.service
    fi
    sudo rm -f /etc/systemd/system/amalia-aprendizaje.timer /etc/systemd/system/amalia-aprendizaje.service
    sudo systemctl daemon-reload
    echo apagado
fi
REMOTE
        then
            log_info "Aprendizaje semanal apagado (flag o hold_timers); units retiradas si existían"
        else
            log_warn "No se pudieron retirar las units del aprendizaje semanal — reintentá el deploy"
        fi
        return 0
    fi

    local cliente compose zona aplicar=""
    cliente=$(tenant_get_or "$tenant" aprendizaje.cliente "$(tenant_get_or "$tenant" mercado.cliente "")")
    compose=$(tenant_get_or "$tenant" docker.compose_file "docker-compose.oci.yml")
    zona=$(tenant_get_or "$tenant" instance.timezone "America/El_Salvador")
    [[ "$(tenant_get_or "$tenant" features.aprendizaje_aplicar false)" != "True" ]] || aplicar=" --aplicar"
    # Solo valores seguros para systemd y sed; nunca ejecutar texto del YAML.
    if [[ ! "$cliente" =~ ^[a-z0-9_-]+$ ]] \
            || [[ ! "$compose" =~ ^[a-zA-Z0-9_.-]+\.ya?ml$ ]] \
            || ! python3 -c 'import sys; from zoneinfo import ZoneInfo; ZoneInfo(sys.argv[1])' "$zona" 2>/dev/null \
            || [[ ! "$zona" =~ ^[a-zA-Z0-9_+/-]+$ ]]; then
        log_warn "aprendizaje.cliente/docker.compose_file/instance.timezone inválidos — NO se instala el aprendizaje semanal"
        return 0
    fi

    # Limpieza explícita de temporales, sin instalar traps globales.
    local src="${OCI_DIR}/lib/aprendizaje" tmp copiado=si
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-aprendizaje-units.XXXXXX")
    sed -e "s|__ZONA__|${zona}|g" \
        "${src}/amalia-aprendizaje.timer" > "${tmp}/amalia-aprendizaje.timer"
    sed -e "s|__COMPOSE__|${compose}|g" -e "s|__CLIENTE__|${cliente}|g" -e "s|__APLICAR__|${aplicar}|g" \
        "${src}/amalia-aprendizaje.service" > "${tmp}/amalia-aprendizaje.service"

    log_step "Instalando el aprendizaje semanal (Sun 03:00 ${zona}, cliente ${cliente})..."
    # No es fatal: el despliegue de la app ya terminó bien y amalia.timer ya está
    # activo. Pero se avisa fuerte, con el comando para reintentar.
    if ! ssh "${ssh_opts[@]}" "$remote" "mkdir -p /tmp/amalia-aprendizaje-install" >/dev/null 2>&1 \
            || ! scp "${ssh_opts[@]}" "${tmp}/amalia-aprendizaje.timer" "${tmp}/amalia-aprendizaje.service" \
                 "${remote}:/tmp/amalia-aprendizaje-install/" >/dev/null 2>&1; then
        copiado=no
    fi
    rm -rf "$tmp"
    if [[ "$copiado" == no ]]; then
        log_warn "No se pudieron copiar las units del aprendizaje semanal — NO quedó programado. Reintentá el deploy."
        return 0
    fi

    if ! ssh "${ssh_opts[@]}" "$remote" bash -s >/dev/null 2>&1 <<'REMOTE'
set -euo pipefail
d=/tmp/amalia-aprendizaje-install
sudo install -m 0644 "$d/amalia-aprendizaje.service" "$d/amalia-aprendizaje.timer" /etc/systemd/system/
rm -rf "$d"
sudo systemctl daemon-reload
# `enable --now` puede dejar el timer inactivo (incidente RUN-001): confirmar y forzar.
sudo systemctl enable --now amalia-aprendizaje.timer
sudo systemctl is-active --quiet amalia-aprendizaje.timer || sudo systemctl start amalia-aprendizaje.timer
REMOTE
    then
        log_warn "Falló la instalación del aprendizaje semanal en la VM — NO quedó programado."
        return 0
    fi

    if ssh "${ssh_opts[@]}" "$remote" "systemctl is-active --quiet amalia-aprendizaje.timer" &>/dev/null; then
        log_ok "Aprendizaje semanal programado — próximo disparo:"
        ssh "${ssh_opts[@]}" "$remote" \
            "systemctl list-timers amalia-aprendizaje.timer --no-pager 2>&1 | sed -n '2p'" || true
        log_info "  Probarlo ya: ssh ... 'sudo systemctl start --no-block amalia-aprendizaje.service'"
    else
        log_warn "amalia-aprendizaje.timer quedó instalado pero NO activo — revisá 'systemctl status amalia-aprendizaje.timer'"
    fi
}

# Punto de entrada de deploy.sh; mantiene las opciones SSH del despliegue.
aprendizaje_deploy() {
    local tenant=$1
    aprendizaje_cron_sync "$tenant" "ubuntu@$(state_get "$tenant" public_ip)" \
        -i "${OCI_DIR}/state/${tenant}_id_ed25519" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="${OCI_DIR}/state/known_hosts" -o LogLevel=ERROR
}
