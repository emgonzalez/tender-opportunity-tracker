#!/usr/bin/env bash
# amalia-backup.sh — Respaldo VERIFICADO del historial de AMALIA. Corre EN LA VM.
#
# ── Por qué existe ────────────────────────────────────────────────────────────
# El historial de corridas (data/history/*.jsonl) es el único dato que AMALIA no
# puede volver a generar: COMPRASAL no expone el pasado. Hasta ahora la única
# copia vivía en el boot volume de una VM Always Free, así que "perder la VM" y
# "perder el historial" eran el mismo evento.
#
# ── Por qué un PAR y no Instance Principal ────────────────────────────────────
# Lo natural sería que la VM se autenticara con Instance Principal. No se puede:
# en el tenant corporativo el permiso `dynamic-groups` está denegado por IAM, y
# sin dynamic group no hay Instance Principal. La alternativa soportada es un PAR
# (Pre-Authenticated Request): una URL firmada contra la que se hace PUT/GET con
# curl, sin CLI de OCI ni credenciales en disco más allá de la propia URL.
# El PAR lo crea `oci/lib/07-backup.sh` desde la laptop del operador y lo deja en
# AMALIA_BACKUP_PAR_FILE. Los PAR EXPIRAN: ver "Caducidad" abajo.
#
# ── Por qué la verificación es una restauración de verdad ─────────────────────
# Un HTTP 200 en el PUT prueba que el servidor recibió algo, no que ese algo se
# pueda restaurar. Así que después de subir se BAJA el objeto, se descomprime en
# un directorio descartable y se contrasta cada archivo contra el MANIFEST.sha256
# que viaja adentro del propio tarball. Es literalmente una restauración, solo
# que a /tmp. Si eso falla, el script sale con error y systemd dispara
# amalia-backup-failure.service (journal + /var/log + correo).
#
# ── Qué NO entra al respaldo, y por qué ───────────────────────────────────────
# `data/storage_state.json` es la sesión de Playwright contra COMPRASAL: una
# CREDENCIAL, no un dato. Queda fuera por dos razones que apuntan al mismo lado:
#   1. Valor de restauración nulo — la sesión de COMPRASAL caduca en ~30 min de
#      inactividad, así que para cuando alguien restaure ya no sirve. Rehacerla
#      es un comando documentado (`python -m src.auth.login --manual`).
#   2. Costo de seguridad real — meterla en el bucket convertiría al respaldo en
#      material sensible y obligaría a tratar la URL del PAR como una credencial
#      de acceso a COMPRASAL, no solo de acceso al historial.
# Screenshots, debug y export tampoco entran: son derivados, se regeneran, y son
# lo único que pesa (el respaldo baja de decenas de MB a unos pocos).
#
# La lista de lo que SÍ entra es una ALLOWLIST explícita, no un "todo menos X":
# así nada nuevo se filtra al bucket por accidente. El precio es que un archivo
# durable que se agregue mañana quedaría fuera en silencio, y por eso el script
# AVISA cuando encuentra en data/ algo que no sabe clasificar.
#
# ── Caducidad del PAR ─────────────────────────────────────────────────────────
# Un PAR vencido haría que el respaldo dejara de funcionar. El script no espera a
# ese momento: compara la fecha de expiración guardada junto a la URL y
#   - a < 30 días  → warning en el journal
#   - a <=  7 días → además manda correo (7 avisos diarios, molestos y finitos)
#   - vencido      → falla, no "salta el respaldo"
# Rotar = volver a correr `backup_setup <tenant>` desde la laptop; es idempotente.
#
# Uso:
#   amalia-backup.sh run                     # snapshot + subida + verificación
#   amalia-backup.sh verify <objeto>         # baja y verifica un respaldo puntual
#   amalia-backup.sh restore <objeto> <dir>  # restaura a un directorio
#   amalia-backup.sh list [prefijo]          # lista lo que hay en el bucket
#   amalia-backup.sh status                  # antigüedad del último respaldo OK
#   amalia-backup.sh alert <asunto> [texto]  # lo usa la unit OnFailure=
set -euo pipefail

# ── Parámetros (con los defaults de la VM; los tests los sobreescriben) ───────
DATA_DIR="${AMALIA_DATA_DIR:-/opt/tender-opportunity-tracker/data}"
APP_DIR="${AMALIA_APP_DIR:-/opt/tender-opportunity-tracker}"
PAR_FILE="${AMALIA_BACKUP_PAR_FILE:-/etc/amalia/backup.par}"
STATE_DIR="${AMALIA_BACKUP_STATE_DIR:-/var/lib/amalia}"
COMPOSE_FILE="${AMALIA_COMPOSE_FILE:-docker-compose.e2.yml}"
LOG_FILE="${AMALIA_BACKUP_LOG:-/var/log/amalia-backup.log}"

# Cuánto esperar a que termine una corrida diaria antes de rendirse (segundos).
WAIT_MAX_S="${AMALIA_BACKUP_WAIT_MAX_S:-1200}"
WAIT_STEP_S="${AMALIA_BACKUP_WAIT_STEP_S:-30}"
# Umbrales de aviso por caducidad del PAR (días).
PAR_WARN_DAYS="${AMALIA_BACKUP_PAR_WARN_DAYS:-30}"
PAR_ALERT_DAYS="${AMALIA_BACKUP_PAR_ALERT_DAYS:-7}"
# A partir de cuántas horas sin un respaldo exitoso se considera que el mecanismo
# estuvo caído. Detecta el fallo más traicionero: que nadie note que no corre.
STALE_HOURS="${AMALIA_BACKUP_STALE_HOURS:-48}"

STAMP_FILE="${STATE_DIR}/last-backup.json"

# ── Logging ───────────────────────────────────────────────────────────────────
# A journal (systemd lo captura de stdout) y, si se puede, a un archivo. El
# `|| true` es a propósito: no poder escribir el log no es razón para no respaldar.
log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_FILE"; } 2>/dev/null || true; }
warn() { log "WARN: $*"; command -v logger >/dev/null && logger -t amalia-backup -p user.warning "$*" || true; }
die()  { log "ERROR: $*"; command -v logger >/dev/null && logger -t amalia-backup -p user.err "$*" || true; exit 1; }

# ── Portabilidad ──────────────────────────────────────────────────────────────
# El script corre en Ubuntu (VM) pero la suite de tests lo ejecuta en la laptop
# del desarrollador, que puede ser macOS. Las tres utilidades que difieren.
_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

# MD5 en base64 para la cabecera Content-MD5: Object Storage la valida del lado
# servidor y rechaza con 400 si el cuerpo llegó corrupto. Es una comprobación de
# integridad de tránsito que no cuesta nada, además de la verificación real.
_md5_b64() {
    if command -v openssl >/dev/null 2>&1; then
        openssl dgst -md5 -binary "$1" | base64 | tr -d '\n'
    else
        python3 -c "
import base64, hashlib, sys
h = hashlib.md5()
with open(sys.argv[1], 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
print(base64.b64encode(h.digest()).decode())
" "$1"
    fi
}

# Días (float, con signo) desde ahora hasta un instante ISO-8601 UTC. En shell
# portable la aritmética de fechas es un pantano; python3 está garantizado en la
# VM (cloud-init lo necesita) y en cualquier máquina que corra este repo.
_days_until() {
    python3 -c "
import sys
from datetime import datetime, timezone
raw = sys.argv[1].replace('Z', '+00:00')
try:
    when = datetime.fromisoformat(raw)
except ValueError:
    print('nan'); raise SystemExit
if when.tzinfo is None:
    when = when.replace(tzinfo=timezone.utc)
print(f'{(when - datetime.now(timezone.utc)).total_seconds() / 86400:.3f}')
" "$1"
}

_lt() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 < b + 0) }'; }

# ── PAR ───────────────────────────────────────────────────────────────────────
PAR_URL=""
PAR_EXPIRES=""

par_load() {
    [[ -f "$PAR_FILE" ]] || die "No existe ${PAR_FILE}. El respaldo no está configurado: corré 'backup_setup <tenant>' (oci/lib/07-backup.sh) desde la laptop."
    PAR_URL=$(sed -n 's/^url=//p' "$PAR_FILE" | head -1)
    PAR_EXPIRES=$(sed -n 's/^expires=//p' "$PAR_FILE" | head -1)
    [[ -n "$PAR_URL" ]] || die "${PAR_FILE} no tiene una línea 'url='."
    # El PAR apunta a la raíz de objetos del bucket y se completa con el nombre
    # del objeto; sin la barra final las URLs quedarían pegadas.
    [[ "$PAR_URL" == */ ]] || PAR_URL="${PAR_URL}/"
}

par_check_expiry() {
    [[ -n "$PAR_EXPIRES" ]] || { warn "El PAR no declara fecha de expiración — no se puede avisar antes de que caduque."; return 0; }
    local days; days=$(_days_until "$PAR_EXPIRES")
    [[ "$days" == "nan" ]] && { warn "Fecha de expiración ilegible en ${PAR_FILE}: '${PAR_EXPIRES}'"; return 0; }
    if _lt "$days" 0; then
        die "El PAR venció el ${PAR_EXPIRES}. NO se respaldó nada. Rotalo: backup_setup <tenant>."
    fi
    if _lt "$days" "$PAR_ALERT_DAYS"; then
        warn "El PAR vence en ${days} días (${PAR_EXPIRES}) — rotalo ya con backup_setup <tenant>."
        notify "PAR de respaldo por vencer (${days} días)" \
               "El PAR que usa el respaldo diario de AMALIA vence el ${PAR_EXPIRES}. Cuando venza, el respaldo dejará de subir. Rotarlo: backup_setup <tenant> desde oci/lib/07-backup.sh."
    elif _lt "$days" "$PAR_WARN_DAYS"; then
        warn "El PAR vence en ${days} días (${PAR_EXPIRES})."
    fi
}

# ── Correo de alerta ──────────────────────────────────────────────────────────
# Reusa el notificador de la app en vez de reimplementar SMTP en shell: la
# configuración de correo tiene un solo hogar (src/notifier/email_sender.py).
# Nunca es fatal: si docker o la imagen no están sanos, el journal ya registró
# el problema y no queremos que el aviso tape al incidente.
notify() {
    local subject=$1 body=${2:-}
    command -v docker >/dev/null 2>&1 || { log "notify: sin docker, solo journal"; return 0; }
    [[ -f "${APP_DIR}/${COMPOSE_FILE}" ]] || { log "notify: no encuentro ${APP_DIR}/${COMPOSE_FILE}, solo journal"; return 0; }
    ( cd "$APP_DIR" && timeout 300 docker compose -f "$COMPOSE_FILE" run --rm --no-deps amalia \
        python -m src.notifier.ops_alert --subject "$subject" --body "$body" ) >/dev/null 2>&1 \
        || log "notify: no se pudo enviar el correo (revisá docker/SMTP); el journal ya tiene el detalle"
    return 0
}

# ── Guarda de concurrencia ────────────────────────────────────────────────────
# Respaldar a mitad de una corrida capturaría un last_run.json de un momento y un
# history.jsonl de otro. Esperamos a que la corrida diaria termine; si no termina
# en WAIT_MAX_S, fallamos ruidosamente en vez de guardar algo inconsistente.
_any_run_active() {
    command -v systemctl >/dev/null 2>&1 || return 1
    local u
    # amalia-mercado.service (semanal) escribe en la ADB, pero puede refrescar
    # data/storage_state.json al usar la sesión: se espera igual que al resto.
    for u in amalia.service amalia-scrape.service amalia-analyze.service amalia-mercado.service; do
        systemctl is-active --quiet "$u" 2>/dev/null && return 0
    done
    return 1
}

wait_for_idle() {
    local waited=0
    while _any_run_active; do
        (( waited >= WAIT_MAX_S )) && die "Hay una corrida de AMALIA activa desde hace ${waited}s. No se respalda estado a mitad de corrida."
        log "Corrida de AMALIA en curso — esperando ${WAIT_STEP_S}s (llevo ${waited}s)"
        sleep "$WAIT_STEP_S"
        waited=$(( waited + WAIT_STEP_S ))
    done
}

# ── Antigüedad del último respaldo ────────────────────────────────────────────
# El modo de falla más peligroso no es "el respaldo falló" (eso grita), es "el
# respaldo dejó de correr" (eso calla). Al arrancar comparamos contra el sello
# del último éxito: si pasaron más de STALE_HOURS, alguien tiene que enterarse.
check_staleness() {
    [[ -f "$STAMP_FILE" ]] || { log "No hay sello de respaldo previo (¿primera corrida?)"; return 0; }
    local prev days hours
    prev=$(sed -n 's/.*"utc"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$STAMP_FILE" | head -1)
    [[ -n "$prev" ]] || return 0
    days=$(_days_until "$prev")
    [[ "$days" == "nan" ]] && return 0
    hours=$(awk -v d="$days" 'BEGIN { printf "%.1f", -d * 24 }')
    if _lt "$STALE_HOURS" "$hours"; then
        warn "El último respaldo exitoso fue hace ${hours} h (${prev}) — el mecanismo estuvo caído."
        notify "Respaldo de AMALIA estuvo caído ${hours} h" \
               "El último respaldo exitoso registrado en la VM es del ${prev}. Se está ejecutando uno ahora, pero conviene revisar por qué el timer no corrió (systemctl list-timers amalia-backup.timer)."
    else
        log "Último respaldo exitoso hace ${hours} h"
    fi
}

# ── Armado del snapshot ───────────────────────────────────────────────────────
# Allowlist: lo que se respalda, con la justificación de por qué es irrecuperable.
#   last_run.json     — timestamp/éxito/conteo/versión de la última corrida
#   gasto_diario.json — libro mayor del gasto del drill-down (Fase 4)
#   history/          — EL dato: un JSONL por cliente con cada corrida y cada
#                       proceso clasificado. COMPRASAL no lo devuelve nunca más.
BACKUP_ALLOW=(last_run.json gasto_diario.json history)
# Lo que se ignora a sabiendas. Todo lo que no esté ni acá ni arriba dispara un
# aviso, para que un dato nuevo no quede fuera del respaldo en silencio.
BACKUP_IGNORE=(storage_state.json screenshots debug export stage_handoff.json
               medicion_pliegos.json .gitkeep)

_in_list() {
    local needle=$1; shift
    local x; for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
    return 1
}

report_unknowns() {
    local entry base
    for entry in "$DATA_DIR"/* "$DATA_DIR"/.[!.]*; do
        [[ -e "$entry" ]] || continue
        base=$(basename "$entry")
        _in_list "$base" "${BACKUP_ALLOW[@]}" && continue
        _in_list "$base" "${BACKUP_IGNORE[@]}" && continue
        [[ "$base" == *.log || "$base" == diag*.json ]] && continue
        warn "data/${base} NO entra al respaldo: no está en la allowlist. Si es un dato durable, agregalo a BACKUP_ALLOW en amalia-backup.sh."
    done
}

# stage <dir-destino>  →  copia la allowlist y deja MANIFEST.json + MANIFEST.sha256
stage_snapshot() {
    local snap=$1 name copied=0
    mkdir -p "${snap}/data"
    for name in "${BACKUP_ALLOW[@]}"; do
        if [[ -e "${DATA_DIR}/${name}" ]]; then
            cp -R "${DATA_DIR}/${name}" "${snap}/data/${name}"
            copied=$(( copied + 1 ))
        fi
    done
    (( copied > 0 )) || die "No hay nada que respaldar en ${DATA_DIR} — ¿es el directorio correcto?"

    # MANIFEST.json primero, para que el MANIFEST.sha256 también lo cubra: si
    # alguien altera los metadatos del respaldo, la verificación lo detecta.
    local files bytes version
    files=$(find "${snap}/data" -type f | wc -l | tr -d ' ')
    bytes=$(find "${snap}/data" -type f -exec cat {} + 2>/dev/null | wc -c | tr -d ' ')
    version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${DATA_DIR}/last_run.json" 2>/dev/null | head -1)
    cat > "${snap}/MANIFEST.json" <<JSON
{
  "esquema": 1,
  "utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$(hostname 2>/dev/null || echo desconocido)",
  "data_dir": "${DATA_DIR}",
  "app_version": "${version:-desconocida}",
  "archivos": ${files},
  "bytes": ${bytes}
}
JSON

    # Rutas RELATIVAS al snapshot: así el MANIFEST verifica igual acá, en la VM
    # nueva o en la laptop, sin depender de dónde se haya extraído.
    local -a rutas=()
    while IFS= read -r f; do rutas+=("$f"); done < <(
        cd "$snap" && find . -type f ! -name MANIFEST.sha256 | LC_ALL=C sort
    )
    ( cd "$snap" && _sha256 "${rutas[@]}" > MANIFEST.sha256 )
}

# ── Object Storage vía PAR (curl, sin CLI de OCI) ─────────────────────────────
# Se transmite desde/hacia disco (-T / -o): en una VM de 1 GB no se carga un
# archivo entero en memoria ni se pasa por una variable de shell.
os_put() {
    local file=$1 object=$2 md5
    md5=$(_md5_b64 "$file")
    curl -fsS --retry 3 --retry-delay 5 --retry-connrefused \
         -X PUT -T "$file" \
         -H "Content-Type: application/gzip" \
         -H "Content-MD5: ${md5}" \
         "${PAR_URL}${object}" >/dev/null
}

os_get() {
    local object=$1 dest=$2
    curl -fsS --retry 3 --retry-delay 5 --retry-connrefused \
         -o "$dest" "${PAR_URL}${object}"
}

os_list() {
    local prefix=${1:-}
    curl -fsS "${PAR_URL}?fields=name,size,timeCreated&limit=1000${prefix:+&prefix=${prefix}}"
}

# ── Verificación ──────────────────────────────────────────────────────────────
# Extrae el tarball en un directorio descartable y contrasta cada archivo contra
# el MANIFEST.sha256 que viaja adentro. Esto es una restauración completa: si
# pasa, restaurar de verdad es cambiar el directorio destino.
verify_tarball() {
    local tarball=$1 workdir=$2
    mkdir -p "$workdir"
    tar -xzf "$tarball" -C "$workdir" \
        || { log "El tarball no se puede descomprimir"; return 1; }
    [[ -f "${workdir}/MANIFEST.sha256" ]] \
        || { log "El tarball no trae MANIFEST.sha256"; return 1; }
    [[ -f "${workdir}/MANIFEST.json" ]] \
        || { log "El tarball no trae MANIFEST.json"; return 1; }
    ( cd "$workdir" && _sha256 -c MANIFEST.sha256 >/dev/null 2>&1 ) \
        || { log "Los checksums del MANIFEST no coinciden con lo extraído"; return 1; }
    return 0
}

# ── Comandos ──────────────────────────────────────────────────────────────────
cmd_run() {
    par_load
    # Esperar ANTES que avisar: los avisos levantan un contenedor para mandar
    # correo, y hacerlo mientras la corrida diaria tiene Chromium abierto es
    # justo la suma de picos de RAM que la VM de 1 GB no tolera.
    wait_for_idle
    par_check_expiry
    check_staleness
    report_unknowns

    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-backup.XXXXXX")
    # shellcheck disable=SC2064  # se expande ahora a propósito
    trap "rm -rf '$tmp'" EXIT

    local ts snap tarball object tier
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    snap="${tmp}/snapshot"
    tarball="${tmp}/amalia-data-${ts}.tar.gz"

    log "Armando snapshot desde ${DATA_DIR}"
    stage_snapshot "$snap"
    tar -czf "$tarball" -C "$snap" . || die "No se pudo empaquetar el snapshot"
    log "Snapshot listo: $(wc -c < "$tarball" | tr -d ' ') bytes"

    # Un objeto por día. El del día 1 va al prefijo mensual, que la política de
    # retención del bucket conserva un año en vez de un mes; así hay profundidad
    # histórica sin guardar 365 copias de un archivo que casi no cambia.
    if [[ "$(date -u +%d)" == "01" ]]; then tier=monthly; else tier=daily; fi
    object="${tier}/amalia-data-${ts}.tar.gz"

    log "Subiendo ${object}"
    os_put "$tarball" "$object" || die "Falló la subida de ${object} (¿PAR vencido o revocado?)"

    # ── La verificación ───────────────────────────────────────────────────────
    log "Verificando: bajando ${object} y restaurándolo en un directorio descartable"
    os_get "$object" "${tmp}/verify.tar.gz" || die "Se subió ${object} pero NO se puede bajar. El respaldo no sirve."

    local a b
    a=$(_sha256 "$tarball" | awk '{print $1}')
    b=$(_sha256 "${tmp}/verify.tar.gz" | awk '{print $1}')
    [[ "$a" == "$b" ]] || die "Lo que bajó de ${object} no es byte a byte lo que se subió (${a} != ${b})."

    verify_tarball "${tmp}/verify.tar.gz" "${tmp}/restore" \
        || die "El respaldo ${object} NO se puede restaurar: la verificación falló."

    local nfiles; nfiles=$(find "${tmp}/restore/data" -type f | wc -l | tr -d ' ')
    log "OK — ${object} verificado: se restauró y ${nfiles} archivos coinciden con su checksum"

    mkdir -p "$STATE_DIR" 2>/dev/null || true
    cat > "$STAMP_FILE" <<JSON
{
  "utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "objeto": "${object}",
  "sha256": "${a}",
  "archivos": ${nfiles},
  "verificado": true
}
JSON
    log "Sello escrito en ${STAMP_FILE}"
}

cmd_verify() {
    local object=${1:-}
    [[ -n "$object" ]] || die "Uso: $0 verify <objeto>   (ej: daily/amalia-data-20260909T090000Z.tar.gz)"
    par_load
    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-verify.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT
    os_get "$object" "${tmp}/o.tar.gz" || die "No se pudo bajar ${object}"
    verify_tarball "${tmp}/o.tar.gz" "${tmp}/x" || die "${object} NO es restaurable"
    log "OK — ${object} es restaurable"
    cat "${tmp}/x/MANIFEST.json"
}

cmd_restore() {
    local object=${1:-} dest=${2:-}
    [[ -n "$object" && -n "$dest" ]] || die "Uso: $0 restore <objeto> <directorio-destino>"
    par_load
    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-restore.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT
    os_get "$object" "${tmp}/o.tar.gz" || die "No se pudo bajar ${object}"
    # Verificar ANTES de escribir en el destino: restaurar basura encima de datos
    # buenos es peor que no restaurar.
    verify_tarball "${tmp}/o.tar.gz" "${tmp}/x" || die "${object} NO es restaurable — no se tocó ${dest}"
    mkdir -p "$dest"
    cp -R "${tmp}/x/data/." "${dest}/"
    log "OK — ${object} restaurado en ${dest}"
    log "Recordá que storage_state.json NO viaja en el respaldo: rehacé el login con"
    log "  docker compose -f ${COMPOSE_FILE} run --rm amalia python -m src.auth.login --manual"
}

cmd_list() {
    par_load
    os_list "${1:-}"
    echo ""
}

cmd_status() {
    if [[ -f "$STAMP_FILE" ]]; then
        cat "$STAMP_FILE"
    else
        echo "Sin sello local: nunca se completó un respaldo verificado en esta VM."
        return 1
    fi
}

cmd_alert() {
    local subject=${1:-"Respaldo de AMALIA falló"} body=${2:-}
    [[ -n "$body" ]] || body="El respaldo diario falló. Diagnóstico: journalctl -u amalia-backup.service -n 100"
    warn "$subject"
    notify "$subject" "$body"
}

case "${1:-run}" in
    run)      cmd_run ;;
    verify)   shift; cmd_verify "$@" ;;
    restore)  shift; cmd_restore "$@" ;;
    list)     shift; cmd_list "$@" ;;
    status)   cmd_status ;;
    alert)    shift; cmd_alert "$@" ;;
    *)        die "Comando desconocido: $1 (run|verify|restore|list|status|alert)" ;;
esac
