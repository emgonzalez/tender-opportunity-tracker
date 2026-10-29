#!/usr/bin/env bash
# 09-mercado.sh — instala (o apaga) la corrida SEMANAL de `python -m src.mercado`
#                 en una VM que YA existe. Corre desde la LAPTOP del operador; lo
#                 que corre en la VM es oci/lib/mercado/amalia-mercado.sh.
#
# Plan del portal (docs/planes/portal-amalia.md), Fase 2, KPI 14.
#
# ── Opt-in por tenant, apagado por defecto ───────────────────────────────────
#   features.enable_mercado_cron: true   → instala units y habilita el timer
#   ausente / false                      → no instala nada; si la VM ya tenía el
#                                          timer (flag apagado después), lo apaga
# Con el flag apagado en una VM que nunca lo tuvo, esto es una sola consulta por
# SSH que no cambia nada: el despliegue se comporta igual que antes de existir
# este archivo.
#
# Un tenant con `instance.hold_timers: true` (en sombra, Fase M) NO lo recibe
# aunque tenga el flag: una VM en sombra no debe disparar nada por sí sola.
#
# ── Por qué acá y no en cloud-init ───────────────────────────────────────────
# cloud-init corre una sola vez en la vida de la VM: activarlo obligaría a
# recrear la instancia. Mismo patrón que _backup_install_on_vm (07-backup.sh):
# plantillas con marcadores, scp, install en /etc/systemd/system, daemon-reload,
# enable --now y verificación. Idempotente.
#
# ── Qué NO programa ──────────────────────────────────────────────────────────
# El barrido completo del registro público (`python -m src.mercado mercado`,
# ~132 000 filas, sin reintentos de red) queda MANUAL, mensual a criterio del
# operador. `resolver` cruza contra lo que ese barrido haya dejado en la ADB, así
# que sin él los GANADO/PERDIDO nuevos dependen de lo ya cargado:
#   ssh ... 'cd /opt/tender-opportunity-tracker && \
#            docker compose -f <compose> run --rm amalia python -m src.mercado mercado'
#
# Uso: lo llama app_deploy (04-app.sh) al final, con el SSH ya abierto:
#   mercado_cron_sync <tenant> <usuario@host> <opciones de ssh...>
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

_mercado_cron_enabled() {
    local tenant=$1
    [[ "$(tenant_get_or "$tenant" features.enable_mercado_cron false)" == "True" ]] || return 1
    # Fase M: un tenant en sombra no recibe timers nuevos.
    [[ "$(tenant_get_or "$tenant" instance.hold_timers false)" != "True" ]]
}

mercado_cron_sync() {
    local tenant=$1 remote=$2; shift 2
    local -a ssh_opts=("$@")

    if ! _mercado_cron_enabled "$tenant"; then
        # Solo actúa si la unit existe: en una VM que nunca la tuvo no cambia nada.
        ssh "${ssh_opts[@]}" "$remote" \
            "if [ -f /etc/systemd/system/amalia-mercado.timer ]; then \
                 sudo systemctl disable --now amalia-mercado.timer >/dev/null 2>&1; echo apagado; fi; true" \
            2>/dev/null | grep -q apagado \
            && log_warn "features.enable_mercado_cron apagado: amalia-mercado.timer DESHABILITADO en la VM"
        return 0
    fi

    local day schedule cliente compose
    day=$(tenant_get_or "$tenant" mercado.schedule_day "Sun")
    schedule=$(tenant_get_or "$tenant" mercado.schedule_time "11:00")
    cliente=$(tenant_get_or "$tenant" mercado.cliente "multicomputos_sv")
    compose=$(tenant_get_or "$tenant" docker.compose_file "docker-compose.oci.yml")
    # Validar antes de escribir en systemd: un OnCalendar inválido deja el timer
    # cargado pero sin próximo disparo, y eso no avisa a nadie.
    if [[ ! "$day" =~ ^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$ ]] \
            || [[ ! "$schedule" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] \
            || [[ ! "$cliente" =~ ^[a-z0-9_-]+$ ]]; then
        log_warn "mercado.schedule_day/schedule_time/cliente inválidos ('${day}' '${schedule}' '${cliente}') — NO se instala el mercado semanal"
        return 0
    fi

    # Sin `trap ... RETURN` a propósito (a diferencia de 07-backup.sh): esta
    # función corre DENTRO de app_deploy, y un trap RETURN es global — pisaría el
    # de app_deploy, que es el que borra el .env temporal con los secretos.
    local src="${OCI_DIR}/lib/mercado" tmp copiado=si
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-mercado-units.XXXXXX")
    sed -e "s|__DAY__|${day}|g" -e "s|__SCHEDULE__|${schedule}|g" \
        "${src}/amalia-mercado.timer" > "${tmp}/amalia-mercado.timer"
    sed -e "s|__COMPOSE__|${compose}|g" -e "s|__CLIENTE__|${cliente}|g" \
        "${src}/amalia-mercado.service" > "${tmp}/amalia-mercado.service"

    log_step "Instalando el mercado semanal (${day} ${schedule}, cliente ${cliente})..."
    # No es fatal: el despliegue de la app ya terminó bien y amalia.timer ya está
    # activo. Pero se avisa fuerte, con el comando para reintentar.
    if ! ssh "${ssh_opts[@]}" "$remote" "mkdir -p /tmp/amalia-mercado-install" >/dev/null 2>&1 \
            || ! scp "${ssh_opts[@]}" "${tmp}/amalia-mercado.timer" "${tmp}/amalia-mercado.service" \
                 "${remote}:/tmp/amalia-mercado-install/" >/dev/null 2>&1; then
        copiado=no
    fi
    rm -rf "$tmp"
    if [[ "$copiado" == no ]]; then
        log_warn "No se pudieron copiar las units del mercado semanal — NO quedó programado. Reintentá el deploy."
        return 0
    fi

    # El script vive en el repo que app_deploy acaba de sincronizar; no se copia a
    # /usr/local/bin para no tener dos originales que diverjan.
    if ! ssh "${ssh_opts[@]}" "$remote" bash -s >/dev/null 2>&1 <<'REMOTE'
set -euo pipefail
d=/tmp/amalia-mercado-install
sudo chmod 0755 /opt/tender-opportunity-tracker/oci/lib/mercado/amalia-mercado.sh
sudo install -m 0644 "$d/amalia-mercado.service" "$d/amalia-mercado.timer" /etc/systemd/system/
rm -rf "$d"
sudo systemctl daemon-reload
# `enable --now` puede dejar el timer inactivo (incidente RUN-001): confirmar y forzar.
sudo systemctl enable --now amalia-mercado.timer
sudo systemctl is-active --quiet amalia-mercado.timer || sudo systemctl start amalia-mercado.timer
REMOTE
    then
        log_warn "Falló la instalación del mercado semanal en la VM — NO quedó programado."
        return 0
    fi

    if ssh "${ssh_opts[@]}" "$remote" "systemctl is-active --quiet amalia-mercado.timer" &>/dev/null; then
        log_ok "Mercado semanal programado — próximo disparo:"
        ssh "${ssh_opts[@]}" "$remote" \
            "systemctl list-timers amalia-mercado.timer --no-pager 2>&1 | sed -n '2p'" || true
        log_info "  Probarlo ya: ssh ... 'sudo systemctl start --no-block amalia-mercado.service'"
    else
        log_warn "amalia-mercado.timer quedó instalado pero NO activo — revisá 'systemctl status amalia-mercado.timer'"
    fi
}
