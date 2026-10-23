#!/usr/bin/env bash
# amalia-mercado.sh — corrida SEMANAL de `python -m src.mercado`. Corre EN LA VM.
#
# Plan del portal (docs/planes/portal-amalia.md), Fase 2, KPI 14: que el
# resultado propio (GANADO/PERDIDO) se resuelva solo, sin que alguien se acuerde
# de correr el CLI a mano. Lo dispara amalia-mercado.timer; lo instala
# oci/lib/09-mercado.sh, y SOLO si `features.enable_mercado_cron: true` en
# tenants.yaml (apagado por defecto).
#
# ── Qué corre, y qué NO ───────────────────────────────────────────────────────
#   1. `participados --cliente X` — Playwright + storage_state. El ÚNICO paso con
#      sesión de COMPRASAL, y el único que levanta Chromium.
#   2. `resolver --cliente X`     — solo SQL contra la ADB. Si el paso 1 falló, NO
#      corre: resolver contra un `proceso_participado` viejo no rompe nada, pero
#      tampoco aporta, y un éxito a medias taparía que la sesión está caída.
#
# El barrido completo del registro (`mercado`, ~132 000 filas en ~132 páginas,
# sin reintentos de red) NO va acá a propósito: es pesado, cambia poco de una
# semana a otra y una falla de red a mitad de camino deja el registro a medias
# sin que nadie lo mire. Se corre a mano (mensual), ver oci/lib/09-mercado.sh.
#
# ── Cómo falla ────────────────────────────────────────────────────────────────
# RUIDOSO. Cualquier paso que falle sale != 0 → la unit queda en 'failed' →
# OnFailure=amalia-failure.service (journal + /var/log/amalia-failures.log), y
# además este script manda correo a los system admins vía
# src/notifier/ops_alert.py (mismo patrón que amalia-backup.sh). El caso
# esperado es la sesión de COMPRASAL vencida: NADIE la renueva para este paso
# (la corrida diaria la renueva para sí misma, pero puede estar vencida para el
# domingo). Remedio: `python -m src.auth.login --manual` en la VM.
#
# Uso (lo invoca la unit; a mano solo para diagnosticar):
#   amalia-mercado.sh
set -euo pipefail

# Los AMALIA_* los define la unit; los tests (scripts/test_pull_deploy.sh) los
# sobreescriben para correr con stubs.
APP_DIR="${AMALIA_APP_DIR:-/opt/tender-opportunity-tracker}"
ENV_FILE="${APP_DIR}/.env"
COMPOSE_FILE="${AMALIA_COMPOSE_FILE:-docker-compose.e2.yml}"
CLIENTE="${AMALIA_MERCADO_CLIENTE:-multicomputos}"
# Cuánto esperar a que termine otra corrida antes de rendirse (segundos).
WAIT_MAX_S="${AMALIA_MERCADO_WAIT_MAX_S:-1800}"
WAIT_STEP_S="${AMALIA_MERCADO_WAIT_STEP_S:-30}"
# Por paso. El `participados` recorre TODO lo participado con Chromium en una VM
# de 1 GB: holgado, pero acotado para que un cuelgue no bloquee la semana.
STEP_TIMEOUT_S="${AMALIA_MERCADO_STEP_TIMEOUT_S:-5400}"

log()  { echo "$(date -Iseconds)  $*"; }
skip() { log "SKIP: $*"; exit 0; }

# Best-effort, como en amalia-backup.sh: si docker o SMTP están rotos, el
# journal ya tiene el detalle y el aviso no debe tapar al incidente.
notify() {
    local subject=$1 body=${2:-}
    ( cd "$APP_DIR" && timeout 300 docker compose -f "$COMPOSE_FILE" run --rm --no-deps amalia \
        python -m src.notifier.ops_alert --subject "$subject" --body "$body" ) >/dev/null 2>&1 \
        || log "notify: no se pudo enviar el correo; el journal ya tiene el detalle"
    return 0
}

die() {
    log "FALLO: $*"
    logger -t amalia -p user.err "mercado semanal: $*" 2>/dev/null || true
    notify "AMALIA — mercado semanal FALLÓ" "$* Diagnostico: journalctl -u amalia-mercado.service -n 100"
    exit 1
}

env_get() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" 2>/dev/null \
        | tail -1 | tr -d '\r'
}

# ── Segunda llave: el flag del tenant ─────────────────────────────────────────
# El timer solo se instala con el flag encendido, pero el .env lo reescribe cada
# deploy desde tenants.yaml: si alguien apagó el flag y la unit quedó habilitada
# (deploy cortado, VM restaurada), esto evita que corra igual. No es un fallo.
[[ -f "$ENV_FILE" ]] || die "no hay ${ENV_FILE} (¿la VM no fue desplegada?)"
[[ "$(env_get ENABLE_MERCADO_CRON)" == "true" ]] \
    || skip "ENABLE_MERCADO_CRON no es 'true' en el .env — el tenant no lo tiene encendido"

# ── Exclusión mutua con el resto de las corridas ──────────────────────────────
# En una E2.1.Micro (1 GB) dos Chromium a la vez es un OOM. No se usa Conflicts=
# en la unit porque es simétrico: arrancar esta unit MATARÍA una corrida diaria
# en curso. Se espera, y si no se libera a tiempo se falla ruidoso.
_otra_corrida_activa() {
    local u
    for u in amalia.service amalia-scrape.service amalia-analyze.service amalia-pull-deploy.service; do
        if systemctl is-active --quiet "$u" 2>/dev/null; then
            echo "$u"; return 0
        fi
    done
    return 1
}

esperado=0
while activa=$(_otra_corrida_activa); do
    (( esperado >= WAIT_MAX_S )) \
        && die "${activa} sigue activa tras ${esperado}s; no se corre el mercado encima (RAM de Chromium en 1 GB)."
    log "${activa} en curso — espero ${WAIT_STEP_S}s (llevo ${esperado}s)"
    sleep "$WAIT_STEP_S"
    esperado=$(( esperado + WAIT_STEP_S ))
done

paso() {
    local nombre=$1; shift
    log "→ ${nombre} (cliente=${CLIENTE})"
    ( cd "$APP_DIR" && timeout "$STEP_TIMEOUT_S" docker compose -f "$COMPOSE_FILE" run --rm amalia \
        python -m src.mercado "$nombre" --cliente "$CLIENTE" "$@" )
}

paso participados \
    || die "'participados' falló para ${CLIENTE}. Causa más probable: la sesión de COMPRASAL (storage_state.json) expiró — rehacerla con 'python -m src.auth.login --manual'. 'resolver' NO se corrió."
paso resolver \
    || die "'resolver' falló para ${CLIENTE} (los participados SÍ se sincronizaron). Revisá la conexión a la ADB."

log "OK: participados + resolver para ${CLIENTE}"
