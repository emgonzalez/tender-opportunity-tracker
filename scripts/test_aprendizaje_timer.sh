#!/usr/bin/env bash
# Banco local: ssh/scp falsos, sin conexiones ni lectura de credenciales.
set -euo pipefail
RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rc=$?; rm -rf "$TMP"; exit "$rc"' EXIT
export OCI_DIR="$RAIZ_REPO/oci"
source "$OCI_DIR/lib/10-aprendizaje.sh"

# Los scripts remotos se inspeccionan; nunca se ejecutan contra una VM.
ssh() {
    echo "$*" >> "$TMP/llamadas"
    if [[ "$*" == *'bash -s'* ]]; then cat >> "$TMP/remoto"; fi
    return "${SSH_RC:-0}"
}
scp() {
    echo scp >> "$TMP/llamadas"
    for arg in "$@"; do [[ ! -f "$arg" ]] || cp "$arg" "$TMP/"; done
    return "${SCP_RC:-0}"
}
tenant_get_or() {
    case "$2" in
        features.enable_aprendizaje_cron) echo "${FLAG:-False}" ;;
        features.aprendizaje_aplicar) echo "${APLICAR:-False}" ;;
        instance.hold_timers) echo "${HOLD:-False}" ;;
        aprendizaje.cliente) echo "${CLIENTE-cliente_demo}" ;;
        instance.timezone) echo "${ZONA:-America/El_Salvador}" ;;
        docker.compose_file) echo docker-compose.e2.yml ;;
        *) echo "$3" ;;
    esac
}
correr() {
    : > "$TMP/llamadas"; : > "$TMP/remoto"
    rm -f "$TMP/amalia-aprendizaje.service" "$TMP/amalia-aprendizaje.timer"
    aprendizaje_cron_sync demo ubuntu@host.example.com -i clave_falsa > "$TMP/salida" 2>&1
}
# Apagado y hold_timers: no copia ni habilita y retira las dos units.
for caso in apagado hold; do
    FLAG=False HOLD=False
    [[ "$caso" != hold ]] || { FLAG=True; HOLD=True; }
    correr
    ! grep -q scp "$TMP/llamadas"
    ! grep -q 'enable --now' "$TMP/remoto"
    grep -q 'disable --now amalia-aprendizaje.timer' "$TMP/remoto"
    grep -q 'stop amalia-aprendizaje.service' "$TMP/remoto"
    grep -q 'sudo rm -f /etc/systemd/system/amalia-aprendizaje.timer /etc/systemd/system/amalia-aprendizaje.service' "$TMP/remoto"
done
FLAG=True HOLD=False
for APLICAR in False True; do
    correr
    extra=""; [[ "$APLICAR" != True ]] || extra=" --aplicar"
    grep -qxF "ExecStart=/usr/bin/docker compose -f docker-compose.e2.yml run --rm amalia python -m src.aprendizaje ciclo --cliente cliente_demo --segundos 900$extra" "$TMP/amalia-aprendizaje.service"
    grep -qxF 'OnCalendar=Sun *-*-* 03:00:00 America/El_Salvador' "$TMP/amalia-aprendizaje.timer"
    grep -qxF 'Persistent=false' "$TMP/amalia-aprendizaje.timer"
    grep -qxF 'RandomizedDelaySec=120' "$TMP/amalia-aprendizaje.timer"
    grep -q 'enable --now amalia-aprendizaje.timer' "$TMP/remoto"
    ! grep -q 'start amalia-aprendizaje.service' "$TMP/remoto"
    ! grep -q '__' "$TMP/amalia-aprendizaje.service" "$TMP/amalia-aprendizaje.timer"
    cp "$TMP/amalia-aprendizaje.service" "$TMP/anterior"
    correr
    cmp "$TMP/anterior" "$TMP/amalia-aprendizaje.service"
done
ZONA=Europe/Madrid correr
grep -qxF 'OnCalendar=Sun *-*-* 03:00:00 Europe/Madrid' "$TMP/amalia-aprendizaje.timer"
for CLIENTE in '' 'invalido;comando'; do
    correr
    ! grep -q scp "$TMP/llamadas"
    grep -q inválidos "$TMP/salida"
done
unset CLIENTE
ZONA=Zona/Inexistente correr
! grep -q scp "$TMP/llamadas"
SSH_RC=255 correr
grep -q 'NO quedó programado' "$TMP/salida"
SCP_RC=1 correr
grep -q 'NO quedó programado' "$TMP/salida"
echo 'Aprendizaje: todos los escenarios verdes.'
