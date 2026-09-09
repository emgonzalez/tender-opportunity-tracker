#!/usr/bin/env bash
# 07-backup.sh — Bucket + retención + PAR de escritura para el respaldo del
#                historial. Corre desde la LAPTOP del operador (necesita la CLI
#                de OCI); lo que corre en la VM es oci/lib/backup/amalia-backup.sh.
#
# Fase 5 del plan de adopción: "que perder la VM deje de significar perder el
# historial". El riesgo más serio del sistema — 74 días de datos con una sola
# copia, en el boot volume de una VM Always Free.
#
# Opt-in puro: solo actúa si `backup.enabled: true` en tenants.yaml#<tenant>.
# Con el bloque `backup:` ausente o en false esta función es un no-op.
#
# ⚠️ Este script NO está cableado en deploy.sh, igual que 05-adb.sh: crea
#    recursos en OCI y toca una VM viva, así que se invoca a mano y a conciencia.
#
# Uso manual:
#   export OCI_DIR="$(cd "$(dirname "$0")/.." && pwd)"
#   export REPO_ROOT="$(cd "$OCI_DIR/.." && pwd)"
#   export OCI_PROFILE="$(python3 -c "import yaml;print(yaml.safe_load(open('$OCI_DIR/tenants.yaml'))['tenants']['<tenant>']['profile'])")"
#   source "$OCI_DIR/lib/common.sh"
#   source "$OCI_DIR/lib/07-backup.sh"
#   backup_setup <tenant>        # crea/actualiza bucket + retención + PAR e instala en la VM
#   backup_status <tenant>       # ¿hay respaldos? ¿de cuándo es el último?
#   backup_restore <tenant> [objeto]   # baja, VERIFICA y extrae en oci/state/
#
# ── Por qué un PAR y no Instance Principal ────────────────────────────────────
# En el tenant corporativo el permiso `dynamic-groups` está denegado por IAM, y
# sin dynamic group la VM no puede autenticarse por Instance Principal contra
# Object Storage. El patrón que sí funciona ya está en uso en 06-onnx-stage.sh
# (PAR de LECTURA para que la ADB baje el modelo ONNX); esto es su simétrico:
# un PAR de ESCRITURA para que la VM suba.
#
# El PAR es de tipo AnyObjectReadWrite y no de solo escritura. La razón es la
# palabra que manda en esta fase: *verificado*. Comprobar que un respaldo sirve
# exige BAJARLO y restaurarlo; con un PAR de solo escritura la VM podría subir y
# nunca saber si lo subido se puede leer. El costo de esa decisión —la VM puede
# leer y sobrescribir respaldos— se acota así:
#   · un PAR NO puede borrar objetos (Object Storage no expone DELETE por PAR);
#   · el bucket tiene versionado, así que una sobrescritura no destruye nada;
#   · el bucket es exclusivo del tenant y solo contiene respaldos;
#   · la URL vive en /etc/amalia/backup.par (0600, ubuntu), FUERA del árbol que
#     sincroniza el rsync y FUERA de los volúmenes del contenedor — igual que el
#     token de GHCR, y por el mismo motivo: la app no tiene por qué poder leerla.
#
# ── Retención ─────────────────────────────────────────────────────────────────
# Del lado del SERVIDOR, con lifecycle rules, no del lado de la VM. Es la única
# forma coherente con lo anterior: si la VM no puede borrar, la retención no
# puede depender de ella. Y evita el otro modo de falla — el Always Free de
# Object Storage son 20 GB para TODA la tenancy, compartidos con el bucket del
# modelo ONNX; sin caducidad, el respaldo se come el cupo de los demás.
#   daily/    → 30 días   (un objeto por día)
#   monthly/  → 365 días  (el objeto del día 1 de cada mes)
#   versiones anteriores → 7 días
# Con un historial de ~10 MB comprimido eso son ~400 MB en régimen, no 3 GB.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

# Días de vida del PAR. Ni corto (rotar cada mes es una tarea que nadie hace) ni
# eterno (una URL firmada que no caduca es un secreto permanente). Seis meses, y
# la VM avisa desde 30 días antes; ver "Caducidad" en amalia-backup.sh.
BACKUP_PAR_DAYS="${BACKUP_PAR_DAYS:-180}"
BACKUP_DAILY_RETENTION_DAYS="${BACKUP_DAILY_RETENTION_DAYS:-30}"
BACKUP_MONTHLY_RETENTION_DAYS="${BACKUP_MONTHLY_RETENTION_DAYS:-365}"
BACKUP_VERSION_RETENTION_DAYS="${BACKUP_VERSION_RETENTION_DAYS:-7}"

# Nombre del bucket. Los nombres son únicos POR NAMESPACE, no por compartment:
# dos tenants de la misma tenancy (dev y prod) chocarían sin el sufijo. Los
# guiones bajos de tenants.yaml se normalizan a guiones para que el nombre se
# lea bien dentro de una URL.
_backup_bucket_name() { echo "amalia-backups-${1//_/-}"; }

_backup_enabled() {
    [[ "$(tenant_get_or "$1" backup.enabled false)" == "True" ]]
}

# ── 1) Bucket + versionado ────────────────────────────────────────────────────
_backup_bucket_ensure() {
    local tenant=$1 ns=$2 bucket=$3 compartment
    compartment=$(tenant_get "$tenant" compartment_ocid)

    if ociq os bucket get -ns "$ns" -bn "$bucket" &>/dev/null; then
        log_ok "Bucket '$bucket' ya existe"
    else
        log_step "Creando bucket '$bucket'..."
        # Versionado desde el nacimiento: habilitarlo después no protege lo ya
        # subido, y el momento en que hace falta es el que no se puede anticipar.
        ociq os bucket create -ns "$ns" --name "$bucket" \
            --compartment-id "$compartment" \
            --versioning Enabled >/dev/null \
            || log_die "No se pudo crear el bucket (¿falta 'manage object-family' en el compartment?)"
        log_ok "Bucket creado con versionado"
    fi
}

# ── 2) Retención (lifecycle rules) ────────────────────────────────────────────
_backup_lifecycle_ensure() {
    local ns=$1 bucket=$2 items err
    log_step "Aplicando política de retención..."
    items=$(cat <<JSON
{"items":[
 {"name":"expirar-diarios","action":"DELETE","is-enabled":true,
  "object-name-filter":{"inclusion-prefixes":["daily/"]},
  "time-amount":${BACKUP_DAILY_RETENTION_DAYS},"time-unit":"DAYS"},
 {"name":"expirar-mensuales","action":"DELETE","is-enabled":true,
  "object-name-filter":{"inclusion-prefixes":["monthly/"]},
  "time-amount":${BACKUP_MONTHLY_RETENTION_DAYS},"time-unit":"DAYS"},
 {"name":"expirar-versiones-viejas","action":"DELETE","is-enabled":true,
  "target":"previous-object-versions",
  "time-amount":${BACKUP_VERSION_RETENTION_DAYS},"time-unit":"DAYS"}
]}
JSON
)
    err=$(mktemp)
    if ociq os object-lifecycle-policy put -ns "$ns" -bn "$bucket" \
            --items "$(echo "$items" | jq -c '.items')" --force >/dev/null 2>"$err"; then
        log_ok "Retención: daily ${BACKUP_DAILY_RETENTION_DAYS}d · monthly ${BACKUP_MONTHLY_RETENTION_DAYS}d · versiones ${BACKUP_VERSION_RETENTION_DAYS}d"
    else
        # No es fatal: sin retención el respaldo igual protege el dato, solo
        # crece. Pero hay que enterarse, así que se imprime el comando exacto.
        log_warn "No se pudo aplicar la política de retención:"
        log_info "$(tail -3 "$err")"
        log_warn "Aplicala a mano (el respaldo funciona igual, pero CRECE SIN TECHO):"
        log_info "  oci --profile ${OCI_PROFILE} os object-lifecycle-policy put -ns ${ns} -bn ${bucket} --force --items '$(echo "$items" | jq -c '.items')'"
    fi
    rm -f "$err"
}

# ── 3) PAR de escritura+lectura ───────────────────────────────────────────────
_backup_par_create() {
    local tenant=$1 ns=$2 bucket=$3 par_name expira par_uri
    par_name="amalia-backup-rw-$(date -u +%Y%m%d%H%M%S)"
    expira=$(date -u -v+"${BACKUP_PAR_DAYS}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
             || date -u -d "+${BACKUP_PAR_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)
    log_step "Generando PAR de respaldo (expira $expira)..." >&2
    # --bucket-listing-action ListObjects: sin esto la VM no puede enumerar el
    # bucket, y enumerar es lo que permite `amalia-backup.sh list/status` y
    # encontrar el respaldo más reciente sin la CLI de OCI. Es poder que el PAR
    # ya tiene de hecho (puede leer cualquier objeto), explicitado.
    par_uri=$(ociq os preauth-request create \
        -ns "$ns" -bn "$bucket" --name "$par_name" \
        --access-type AnyObjectReadWrite \
        --bucket-listing-action ListObjects \
        --time-expires "$expira" \
        --query 'data."access-uri"' --raw-output) \
        || log_die "No se pudo crear el PAR"

    local region par_url
    region=$(tenant_get "$tenant" region)
    par_url="https://objectstorage.${region}.oraclecloud.com${par_uri}"
    # Al state va SOLO la fecha y el nombre, nunca la URL: el state file es el
    # lugar donde uno mira sin pensar, y esta URL es una credencial. La URL viaja
    # directo a la VM y queda además en oci/state/<tenant>_backup.par (gitignored).
    state_set "$tenant" backup_par_name "$par_name"
    state_set "$tenant" backup_par_expires "$expira"
    state_set "$tenant" backup_bucket "$bucket"

    # El umask va en un subshell: el archivo nace con 0600, sin ventana en la
    # que exista con permisos laxos, y sin cambiarle el umask al shell de quien
    # sourceó este archivo.
    local par_file="${OCI_DIR}/state/${tenant}_backup.par"
    ( umask 077 && printf 'url=%s\nexpires=%s\n' "${par_url}" "$expira" > "$par_file" )
    log_ok "PAR creado — copia local en ${par_file} (0600, gitignored)" >&2
    echo "$par_file"
}

# ── 4) Instalación en la VM ───────────────────────────────────────────────────
# Sirve para una VM que ya existe. En una VM nueva el script y las units ya vienen
# del cloud-init (render-cloud-init.sh los embebe desde estos MISMOS archivos de
# oci/lib/backup/, así que no hay dos versiones que puedan divergir); acá se
# reinstalan igual porque es idempotente y porque el PAR solo puede llegar así:
# el cloud-init es legible desde el metadata service de la instancia y una URL
# firmada no tiene nada que hacer ahí.
_backup_install_on_vm() {
    local tenant=$1 par_file=$2 public_ip schedule compose remote
    public_ip=$(state_get "$tenant" public_ip)
    [[ -n "$public_ip" ]] || log_die "No hay public_ip en el state de '${tenant}' — ¿corriste deploy.sh?"
    schedule=$(tenant_get_or "$tenant" backup.schedule_time "09:00")
    compose=$(tenant_get_or "$tenant" docker.compose_file "docker-compose.oci.yml")
    remote="ubuntu@${public_ip}"
    # Array literal y no una cadena que después se parte: OCI_DIR puede tener
    # espacios (un worktree bajo "Documents/…" ya los tiene cerca).
    local -a ssh_opts=(
        -i "${OCI_DIR}/state/${tenant}_id_ed25519"
        -o StrictHostKeyChecking=accept-new
        -o UserKnownHostsFile="${OCI_DIR}/state/known_hosts"
        -o LogLevel=ERROR
    )

    local src="${OCI_DIR}/lib/backup" tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-backup-units.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN
    cp "${src}/amalia-backup.sh" "${tmp}/"
    sed -e "s|__SCHEDULE__|${schedule}|g" "${src}/amalia-backup.timer"   > "${tmp}/amalia-backup.timer"
    sed -e "s|__COMPOSE__|${compose}|g"   "${src}/amalia-backup.service" > "${tmp}/amalia-backup.service"
    sed -e "s|__COMPOSE__|${compose}|g"   "${src}/amalia-backup-failure.service" > "${tmp}/amalia-backup-failure.service"

    log_step "Instalando el respaldo en ${public_ip}..."
    ssh "${ssh_opts[@]}" "$remote" "mkdir -p /tmp/amalia-backup-install" >/dev/null \
        || log_die "No se pudo conectar por SSH a ${public_ip}"
    scp "${ssh_opts[@]}" "${tmp}"/* "$par_file" "${remote}:/tmp/amalia-backup-install/" >/dev/null \
        || log_die "Falló la copia de los archivos a la VM"

    # El PAR queda fuera de /opt/tender-opportunity-tracker a propósito: ese
    # árbol lo sobrescribe el rsync de cada despliegue (con --delete) y además
    # se monta dentro del contenedor.
    ssh "${ssh_opts[@]}" "$remote" bash -s <<'REMOTE' || log_die "Falló la instalación en la VM"
set -euo pipefail
d=/tmp/amalia-backup-install
sudo install -m 0755 "$d/amalia-backup.sh" /usr/local/bin/amalia-backup.sh
sudo install -m 0644 "$d/amalia-backup.service" "$d/amalia-backup.timer" \
     "$d/amalia-backup-failure.service" /etc/systemd/system/
sudo install -d -m 0750 -o ubuntu -g ubuntu /etc/amalia /var/lib/amalia
sudo install -m 0600 -o ubuntu -g ubuntu "$d"/*_backup.par /etc/amalia/backup.par
sudo touch /var/log/amalia-backup.log && sudo chown ubuntu:ubuntu /var/log/amalia-backup.log
rm -rf "$d"
sudo systemctl daemon-reload
# `enable --now` puede dejar el timer inactivo por la carrera de systemd durante
# el primer boot (incidente RUN-001, STATUS.md). Confirmar y forzar.
sudo systemctl enable --now amalia-backup.timer
sudo systemctl is-active --quiet amalia-backup.timer || sudo systemctl start amalia-backup.timer
systemctl list-timers amalia-backup.timer --no-pager | sed -n '2p'
REMOTE
    log_ok "Script, units y PAR instalados; timer habilitado"

    # ── La comprobación que cierra la fase ───────────────────────────────────
    # No alcanza con instalar: hay que ver un respaldo completo subir, bajar y
    # restaurarse. Si esto falla, el despliegue no terminó.
    log_step "Ejecutando un respaldo de verdad y verificándolo..."
    if ssh "${ssh_opts[@]}" "$remote" "sudo systemctl start amalia-backup.service"; then
        ssh "${ssh_opts[@]}" "$remote" "sudo systemctl show -p Result --value amalia-backup.service" | grep -qx success \
            || log_die "El primer respaldo falló. Diagnóstico: ssh ${remote} 'journalctl -u amalia-backup.service -n 100'"
        log_ok "Primer respaldo verificado:"
        ssh "${ssh_opts[@]}" "$remote" "amalia-backup.sh status" | sed 's/^/     /'
    else
        log_die "No se pudo disparar amalia-backup.service"
    fi
}

# ── API pública ───────────────────────────────────────────────────────────────

backup_setup() {
    local tenant=$1 ns bucket par_file
    if ! _backup_enabled "$tenant"; then
        log_warn "backup.enabled no está en true para '${tenant}' — no se hace nada."
        return 0
    fi
    require_cmd oci; require_cmd jq; require_cmd ssh; require_cmd scp

    ns=$(ociq os ns get --raw-output --query data)
    bucket=$(_backup_bucket_name "$tenant")

    _backup_bucket_ensure   "$tenant" "$ns" "$bucket"
    _backup_lifecycle_ensure "$ns" "$bucket"
    par_file=$(_backup_par_create "$tenant" "$ns" "$bucket")
    _backup_install_on_vm   "$tenant" "$par_file"

    echo ""
    log_ok "backup_setup completo para '${tenant}'"
    log_info "  Bucket:     ${bucket} (namespace ${ns})"
    log_info "  Retención:  daily ${BACKUP_DAILY_RETENTION_DAYS}d · monthly ${BACKUP_MONTHLY_RETENTION_DAYS}d · versiones ${BACKUP_VERSION_RETENTION_DAYS}d"
    log_info "  PAR vence:  $(state_get "$tenant" backup_par_expires) — rotarlo es volver a correr esto"
    log_info "  Restaurar:  backup_restore ${tenant}"
}

# ¿Hay respaldos y de cuándo es el último? Se consulta desde la laptop, con la
# CLI, y NO desde la VM: el caso que importa es justamente el de la VM perdida.
backup_status() {
    local tenant=$1 ns bucket ultimo
    _backup_enabled "$tenant" || { log_warn "backup.enabled no está en true para '${tenant}'"; return 0; }
    ns=$(ociq os ns get --raw-output --query data)
    bucket=$(_backup_bucket_name "$tenant")

    log_step "Respaldos en ${bucket}"
    local listado
    listado=$(ociq os object list -ns "$ns" -bn "$bucket" --fields name,size,timeCreated --all 2>/dev/null) \
        || log_die "No se pudo listar ${bucket} — ¿existe? Corré backup_setup ${tenant}."
    echo "$listado" | jq -r '.data | sort_by(."time-created") | .[] | "  \(."time-created")  \(.size) B  \(.name)"' | tail -10

    ultimo=$(echo "$listado" | jq -r '[.data[]."time-created"] | sort | last // empty')
    [[ -n "$ultimo" ]] || log_die "El bucket está VACÍO: no hay ningún respaldo."
    local horas
    horas=$(python3 -c "
import sys
from datetime import datetime, timezone
t = datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00'))
print(f'{(datetime.now(timezone.utc) - t).total_seconds() / 3600:.1f}')
" "$ultimo")
    if awk -v h="$horas" 'BEGIN { exit !(h > 48) }'; then
        log_die "El último respaldo es de hace ${horas} h (${ultimo}). El mecanismo está CAÍDO."
    fi
    log_ok "Último respaldo: hace ${horas} h (${ultimo})"
}

# El camino de restauración que importa: no necesita que la VM exista.
# Baja el respaldo, lo VERIFICA contra su propio MANIFEST y lo deja extraído en
# oci/state/. Copiarlo a una VM nueva es un rsync, documentado en oci/README.md.
backup_restore() {
    local tenant=$1 object=${2:-} ns bucket dest tmp
    _backup_enabled "$tenant" || { log_warn "backup.enabled no está en true para '${tenant}'"; return 0; }
    require_cmd oci; require_cmd jq
    ns=$(ociq os ns get --raw-output --query data)
    bucket=$(_backup_bucket_name "$tenant")

    if [[ -z "$object" ]]; then
        log_step "Buscando el respaldo más reciente en ${bucket}..."
        object=$(ociq os object list -ns "$ns" -bn "$bucket" --fields name,timeCreated --all \
                 | jq -r '[.data[]] | sort_by(."time-created") | last | .name // empty')
        [[ -n "$object" ]] || log_die "No hay ningún respaldo en ${bucket}"
    fi
    log_ok "Respaldo elegido: ${object}"

    tmp=$(mktemp -d "${TMPDIR:-/tmp}/amalia-restore.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN
    log_step "Bajando ${object}..."
    ociq os object get -ns "$ns" -bn "$bucket" --name "$object" --file "${tmp}/o.tar.gz" >/dev/null \
        || log_die "No se pudo bajar ${object}"

    # Verificar ANTES de dejar nada en oci/state/: un respaldo corrupto que
    # parece bueno es peor que uno que falta.
    log_step "Verificando el respaldo (checksums del MANIFEST)..."
    mkdir -p "${tmp}/x"
    tar -xzf "${tmp}/o.tar.gz" -C "${tmp}/x" || log_die "El tarball no se puede descomprimir"
    [[ -f "${tmp}/x/MANIFEST.sha256" ]] || log_die "El tarball no trae MANIFEST.sha256"
    local sha=sha256sum
    command -v sha256sum >/dev/null 2>&1 || sha="shasum -a 256"
    ( cd "${tmp}/x" && $sha -c MANIFEST.sha256 >/dev/null ) \
        || log_die "Los checksums NO coinciden: ${object} está corrupto"
    log_ok "Verificado — el respaldo es restaurable"

    dest="${OCI_DIR}/state/restore-${tenant}-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$dest"
    cp -R "${tmp}/x/." "${dest}/"
    echo ""
    log_ok "Restaurado en ${dest}"
    sed 's/^/     /' "${dest}/MANIFEST.json"
    echo ""
    log_info "Para llevarlo a una VM (nueva o recuperada):"
    log_info "  rsync -az -e 'ssh -i ${OCI_DIR}/state/${tenant}_id_ed25519' \\"
    log_info "    ${dest}/data/ ubuntu@\$(jq -r .public_ip ${OCI_DIR}/state/${tenant}.json):/opt/tender-opportunity-tracker/data/"
    log_warn "storage_state.json NO está en el respaldo (es una credencial de sesión, no un dato):"
    log_info "  rehacé el login con 'python -m src.auth.login --manual' en la VM."
}
