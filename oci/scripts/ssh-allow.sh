#!/usr/bin/env bash
# ssh-allow.sh — deja el puerto 22 abierto SOLO para el /32 del operador que
# corre este script, y lo refresca cuando su IP residencial dinámica cambia.
#
# Uso:
#   oci/scripts/ssh-allow.sh <tenant>
#   oci/scripts/ssh-allow.sh <tenant> --cidr 179.5.96.48/32
#   oci/scripts/ssh-allow.sh <tenant> --dry-run
#
#   --cidr <ipv4/32>   No detecta: usa este CIDR. Es la salida cuando la
#                      detección automática aborta (proxy que intercepta TLS).
#   --dry-run          Muestra qué haría y sale. NO escribe.
#   --no-verify        No comprueba el 22 después de escribir (ver abajo).
#   --yes              No pregunta cuando la security list es compartida.
#   --security-list-id Fuerza el OCID (por defecto sale del state del tenant).
#
# ── De dónde sale el OCID ────────────────────────────────────────────────────
# De `oci/state/<tenant>.json`, clave `security_list_id`, que es donde lo dejó
# el despliegue. NO está incrustado acá a propósito: un OCID hardcodeado en un
# script es la forma más fácil de escribirle a la red equivocada el día que se
# recrea la VCN. Dónde vive y cómo consultarlo: `oci/PERMISOS-IAM.md`.
#
# ── Garantías ────────────────────────────────────────────────────────────────
# 1. Idempotente. Si el /32 que corresponde ya es el que está puesto, no se
#    hace NINGUNA llamada de escritura a OCI. Correrlo dos veces seguidas
#    escribe una sola vez.
# 2. Falla cerrado. Ante cualquier duda —no se puede afirmar la IP, la security
#    list tiene una forma inesperada, el resultado calculado no pasa los
#    guardias— aborta SIN escribir. Nunca deja 0.0.0.0/0 ni una regla a medias.
# 3. Preserva el resto. La API de OCI reemplaza la lista COMPLETA de reglas, así
#    que se lee la security list entera, se cambia solo el `source` de la regla
#    TCP/22 y todo lo demás (otros ingress, todo el egress) se reescribe igual.
# 4. Verifica y revierte. Después de escribir prueba que el 22 responda desde
#    esta máquina; si no responde, restaura la regla anterior. Es lo único que
#    distingue "escribí un /32" de "escribí EL /32 correcto": un proxy puede
#    hacer que la IP detectada no sea la que ve el sshd de la VM.
#
# ── Por qué no podés quedarte afuera para siempre ────────────────────────────
# Esta regla se edita por la API de OCI, no por dentro de la VM. Aunque el /32
# quede mal y el SSH deje de funcionar, volver a correr este script (con
# --cidr) lo arregla. Lo único que no se puede perder es el acceso a la API.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OCI_DIR="${OCI_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
source "${OCI_DIR}/lib/common.sh"
source "${OCI_DIR}/lib/ssh-cidr.sh"

# ── Args ─────────────────────────────────────────────────────────────────────
TENANT=""; CIDR_ARG=""; DRY_RUN=false; VERIFY=true; ASSUME_YES=false
SL_ARG="${AMALIA_SECURITY_LIST_OCID:-}"

uso() {
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

while (($# > 0)); do
    case "$1" in
        --cidr)              CIDR_ARG="${2:-}"; shift 2 ;;
        --security-list-id)  SL_ARG="${2:-}";   shift 2 ;;
        --dry-run)           DRY_RUN=true;      shift ;;
        --no-verify)         VERIFY=false;      shift ;;
        --yes|-y)            ASSUME_YES=true;   shift ;;
        -h|--help)           uso 0 ;;
        -*)                  log_die "Opción desconocida: $1" ;;
        *)                   [[ -z "$TENANT" ]] || log_die "Sobra el argumento '$1'"; TENANT="$1"; shift ;;
    esac
done
[[ -n "$TENANT" ]] || uso 1

require_cmd oci
require_cmd jq
require_cmd curl
require_cmd python3
# `if` y no `$VERIFY && ...`: con set -e, un `&&` cuyo lado izquierdo es `false`
# devuelve 1 y aborta el script. (Mismo motivo en los otros `if` de una línea.)
if $VERIFY; then require_cmd nc; fi

# ── Perfil del tenant ────────────────────────────────────────────────────────
export OCI_PROFILE
OCI_PROFILE=$(tenant_get "$TENANT" profile)
[[ -n "$OCI_PROFILE" ]] || log_die "Tenant '$TENANT' no está en ${OCI_DIR}/tenants.yaml"

# ── OCID de la security list ─────────────────────────────────────────────────
SL_ID="$SL_ARG"
if [[ -z "$SL_ID" ]]; then
    SL_ID=$(state_get "$TENANT" security_list_id)
fi
[[ -n "$SL_ID" ]] || log_die "No hay security_list_id para '$TENANT'.
     Se lee de $(state_file "$TENANT") (lo escribe el deploy).
     Si desplegaste desde otra máquina, pasalo a mano:
       $0 $TENANT --security-list-id ocid1.securitylist.oc1...
     Ver oci/PERMISOS-IAM.md § Dónde vive el OCID de la security list."
[[ "$SL_ID" == ocid1.securitylist.* ]] || log_die "'$SL_ID' no parece un OCID de security list"

log_step "Tenant '$TENANT' (perfil $OCI_PROFILE)"
log_info "security list: $SL_ID"

# ── Quién más comparte esta security list ────────────────────────────────────
# `emiliogonzalez_e2` reusa la red de `emiliogonzalez` (network.share_from), así
# que ambos apuntan al MISMO OCID: escribir acá cambia el SSH de las dos VMs.
# Se avisa y se pide confirmación explícita, porque el operador podría estar
# pensando en una sola.
COMPARTEN=()
for _f in "${OCI_DIR}"/state/*.json; do
    [[ -e "$_f" ]] || continue
    _t=$(basename "$_f" .json)
    if [[ "$(jq -r '.security_list_id // ""' "$_f")" == "$SL_ID" ]]; then COMPARTEN+=("$_t"); fi
done
if ((${#COMPARTEN[@]} > 1)); then
    log_warn "Esta security list es COMPARTIDA: ${COMPARTEN[*]}"
    log_warn "  El cambio afecta el acceso SSH de todas esas VMs."
    if ! $DRY_RUN && ! $ASSUME_YES; then
        if [[ -t 0 ]]; then
            read -r -p "  ¿Seguir? (escribí 'si'): " _r
            [[ "$_r" == "si" ]] || log_die "cancelado por el operador — nada se escribió"
        else
            log_die "security list compartida y sin terminal para confirmar — repetí con --yes si es lo que querés"
        fi
    fi
fi

# ── CIDR objetivo ────────────────────────────────────────────────────────────
if [[ -n "$CIDR_ARG" ]]; then
    NEW_CIDR=$(ssh_cidr_resolve "$CIDR_ARG")
    log_ok "CIDR dado a mano: $NEW_CIDR"
else
    log_step "Detectando la IP pública del operador..."
    NEW_CIDR=$(ssh_cidr_resolve auto)
    log_ok "IP detectada (corroborada por ≥2 servicios): $NEW_CIDR"
fi

# Guardia dura #1: este script existe para CERRAR el 22. Que nunca sea la vía
# por la que se abre, ni por un --cidr distraído ni por un bug de detección.
[[ "$NEW_CIDR" == */32 ]] || log_die "guardia: '$NEW_CIDR' no es un /32 — este script solo escribe /32"
[[ "$NEW_CIDR" != "0.0.0.0/32" && "$NEW_CIDR" != "0.0.0.0/0" ]] || log_die "guardia: CIDR inaceptable ($NEW_CIDR)"

# ── Leer la security list COMPLETA ───────────────────────────────────────────
log_step "Leyendo la security list..."
if ! ACTUAL=$(ociq network security-list get --security-list-id "$SL_ID" 2>&1); then
    log_die "no se pudo leer la security list: $ACTUAL"
fi
echo "$ACTUAL" | jq -e '.data."ingress-security-rules"' >/dev/null 2>&1 \
    || log_die "respuesta de OCI con forma inesperada (sin .data.ingress-security-rules)"

# jq que selecciona LA regla de SSH. Protocolo 6 = TCP; el rango tiene que
# cubrir el 22 (no se asume min==max==22: una regla 1-1024 también lo abre).
JQ_ES_SSH='.protocol=="6" and (."tcp-options"."destination-port-range" != null)
           and (."tcp-options"."destination-port-range".min <= 22)
           and (."tcp-options"."destination-port-range".max >= 22)'

N_SSH=$(echo "$ACTUAL" | jq "[.data.\"ingress-security-rules\"[] | select($JQ_ES_SSH)] | length")
# Guardia dura #2: si hay 0 o >1 reglas que abren el 22, este script no sabe
# cuál es "la" regla y editar a ciegas podría abrir de más. Revisión manual.
((N_SSH == 1)) || log_die "se esperaba exactamente 1 regla de ingress que abra el 22, hay $N_SSH.
     Revisá la security list a mano: puede haber quedado una regla vieja o un
     rango amplio (ej. 1-1024). No se toca nada."

CIDR_ACTUAL=$(echo "$ACTUAL" | jq -r ".data.\"ingress-security-rules\"[] | select($JQ_ES_SSH) | .source")

# ── Idempotencia ─────────────────────────────────────────────────────────────
if [[ "$CIDR_ACTUAL" == "$NEW_CIDR" ]]; then
    log_ok "Sin cambios: el 22 ya está abierto solo para $NEW_CIDR"
    log_info "no se hizo ninguna llamada de escritura a OCI"
    exit 0
fi

log_info "actual:  $CIDR_ACTUAL"
log_info "nuevo:   $NEW_CIDR"

# ── Reconstruir el ingress ───────────────────────────────────────────────────
INGRESS_VIEJO=$(echo "$ACTUAL" | jq -c '.data."ingress-security-rules"')
EGRESS=$(echo "$ACTUAL"        | jq -c '.data."egress-security-rules"')
ETAG=$(echo "$ACTUAL"          | jq -r '.etag // ""')

INGRESS_NUEVO=$(echo "$ACTUAL" | jq -c --arg c "$NEW_CIDR" \
    "[ .data.\"ingress-security-rules\"[] | if ($JQ_ES_SSH) then .source = \$c else . end ]")

# ── Guardias sobre el resultado, ANTES de escribir ───────────────────────────
# #3: el número de reglas no puede cambiar (ni se pierde ni se agrega ninguna).
N_VIEJO=$(echo "$INGRESS_VIEJO" | jq 'length')
N_NUEVO=$(echo "$INGRESS_NUEVO" | jq 'length')
((N_VIEJO == N_NUEVO)) || log_die "guardia: la cantidad de reglas cambiaría ($N_VIEJO → $N_NUEVO)"

# #4: ninguna regla del resultado puede abrir el 22 a internet.
if echo "$INGRESS_NUEVO" | jq -e "[.[] | select($JQ_ES_SSH) | select(.source==\"0.0.0.0/0\")] | length > 0" >/dev/null; then
    log_die "guardia: el resultado dejaría 0.0.0.0/0 en el 22"
fi
# #5: y la regla del 22 tiene que quedar exactamente en el /32 pedido.
QUEDO=$(echo "$INGRESS_NUEVO" | jq -r "[.[] | select($JQ_ES_SSH) | .source] | join(\",\")")
[[ "$QUEDO" == "$NEW_CIDR" ]] || log_die "guardia: el 22 quedaría en '$QUEDO' y no en '$NEW_CIDR'"

# ── Traducir al dialecto que acepta el update ────────────────────────────────
# El CLI de OCI NO es simétrico: `get` devuelve kebab-case ("tcp-options") y
# `update` espera camelCase ("tcpOptions"), como se ve en
# `oci network security-list update --generate-param-json-input ...`.
# Devolverle tal cual lo que dio el `get` es apostar a que normalice solo; acá
# se traduce explícitamente. Se descartan además las claves en null que el `get`
# rellena (icmp-options, source-port-range): no aportan nada y son una fuente
# más de rechazos.
JQ_NORM='def camel: gsub("-(?<a>[a-z])"; .a|ascii_upcase);
         walk(if type=="object"
              then (with_entries(select(.value != null)) | with_entries(.key |= camel))
              else . end)'
INGRESS_API=$(echo "$INGRESS_NUEVO" | jq -c "$JQ_NORM")
INGRESS_VIEJO_API=$(echo "$INGRESS_VIEJO" | jq -c "$JQ_NORM")
EGRESS_API=$(echo "$EGRESS" | jq -c "$JQ_NORM")

# Guardia dura #6: la traducción también se revisa. Un bug acá escribiría un
# JSON válido pero con otra semántica — justo el error que nadie ve venir.
JQ_ES_SSH_API='.protocol=="6" and (.tcpOptions.destinationPortRange != null)
               and (.tcpOptions.destinationPortRange.min <= 22)
               and (.tcpOptions.destinationPortRange.max >= 22)'
QUEDO_API=$(echo "$INGRESS_API" | jq -r "[.[] | select($JQ_ES_SSH_API) | .source] | join(\",\")")
[[ "$QUEDO_API" == "$NEW_CIDR" ]] || log_die "guardia: tras traducir a camelCase el 22 quedaría en '$QUEDO_API'"
[[ "$(echo "$INGRESS_API" | jq 'length')" == "$N_NUEVO" ]] || log_die "guardia: la traducción perdió reglas"

if $DRY_RUN; then
    log_step "--dry-run: NO se escribe nada"
    log_info "se habría llamado:"
    log_info "  oci --profile $OCI_PROFILE network security-list update \\"
    log_info "      --security-list-id $SL_ID --force \\"
    log_info "      --ingress-security-rules '<$N_NUEVO reglas, el 22 en $NEW_CIDR>' \\"
    log_info "      --egress-security-rules '<sin cambios>'"
    echo "$INGRESS_API" | jq .
    exit 0
fi

# ── Escritura ────────────────────────────────────────────────────────────────
# --if-match con el etag que se leyó: si alguien más tocó la security list entre
# el get y el update, esto falla en vez de pisarlo.
log_step "Actualizando la regla de SSH..."
UPDATE_ARGS=(network security-list update --security-list-id "$SL_ID"
             --ingress-security-rules "$INGRESS_API"
             --egress-security-rules "$EGRESS_API" --force)
if [[ -n "$ETAG" ]]; then UPDATE_ARGS+=(--if-match "$ETAG"); fi

if ! SALIDA=$(ociq "${UPDATE_ARGS[@]}" 2>&1); then
    log_die "la actualización falló — la regla anterior sigue vigente: $SALIDA"
fi
log_ok "Escrito: 22/tcp ← $NEW_CIDR"

# ── Verificación + rollback ──────────────────────────────────────────────────
# Escribir bien y ser alcanzable NO son lo mismo: si un proxy contestó por vos,
# el /32 es de otro y el 22 quedó cerrado para todos. La única prueba real es
# abrir una conexión TCP nueva desde esta máquina.
if ! $VERIFY; then
    log_warn "--no-verify: no se comprobó que el 22 siga respondiendo desde acá"
    exit 0
fi

IP_VM=$(state_get "$TENANT" public_ip)
if [[ -z "$IP_VM" ]]; then
    log_warn "no hay public_ip en el state: no se puede verificar. Probá a mano:"
    log_warn "  nc -z -w 8 <ip-de-la-vm> 22"
    exit 0
fi

log_step "Verificando que el 22 responda en $IP_VM (las reglas tardan unos segundos)..."
ALCANZABLE=false
for _i in 1 2 3 4 5 6; do
    if nc -z -w 8 "$IP_VM" 22 >/dev/null 2>&1; then ALCANZABLE=true; break; fi
    sleep 5
done

if $ALCANZABLE; then
    log_ok "El 22 responde desde esta red. SSH restringido a $NEW_CIDR"
    exit 0
fi

log_warn "El 22 NO responde desde acá. Revirtiendo a la regla anterior ($CIDR_ACTUAL)..."
# Sin --if-match a propósito: el etag ya cambió por NUESTRA propia escritura de
# hace un momento, y acá lo que importa es volver a un estado alcanzable. Es el
# único lugar del script donde escribir sin verificación previa es lo correcto.
if ociq network security-list update --security-list-id "$SL_ID" \
        --ingress-security-rules "$INGRESS_VIEJO_API" \
        --egress-security-rules "$EGRESS_API" --force >/dev/null 2>&1; then
    log_die "revertido a $CIDR_ACTUAL. La IP $NEW_CIDR no es la que ve la VM —
     casi seguro contestó el proxy. Averiguá la fuente real y reintentá:
       ssh <vm> 'echo \$SSH_CLIENT'
       $0 $TENANT --cidr <esa-ip>/32"
else
    log_die "NO SE PUDO REVERTIR y el 22 no responde. Arreglalo por la API o la
     consola de OCI (no hace falta SSH): poné el source de la regla 22 de
     $SL_ID en un CIDR que te incluya."
fi
