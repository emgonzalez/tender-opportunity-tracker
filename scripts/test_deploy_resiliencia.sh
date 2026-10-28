#!/usr/bin/env bash
# test_deploy_resiliencia.sh — banco de la resiliencia del deploy (oci/lib/common.sh y 01-08)
#
# Por qué existe: el 2026-10-27 un deploy corrió sin red. Cada `get … || <crear>` tomó el
# timeout como «no existe», creó una VCN nueva y le pisó el `vcn_id` al state. La corrección
# (`oci_existe` con TRES respuestas, `oci_resolver`, `state_set` que no reemplaza ids, copia
# previa, lock y trap de salida) es justo el tipo de código que nadie ejercita hasta el día
# en que la red vuelve a caerse. Este banco lo ejercita siempre:
#
#   1. common.sh, función por función: clasificación de errores, oci_existe (0/1/2 y
#      reintentos), oci_resolver, state_set/state_validar/state_snapshot, lock y trap.
#   2. 03/05/06/07/08 con un `oci` falso que falla (ConnectTimeout o 404) en UNA llamada
#      elegida: debe abortar sin create/delete/update/put y sin cambiar el state.
#   3. 01-network/02-compute igual, con el state comparado byte a byte.
#
# Hermético: `oci` es un stub delante del PATH; todo vive en un tmpdir. Sin red, sin OCI.
#
# Uso: bash scripts/test_deploy_resiliencia.sh
set -uo pipefail

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="${RAIZ_REPO}/oci/lib"

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

command -v jq >/dev/null 2>&1 || { echo "Falta jq: el banco (y el deploy) lo necesitan." >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LLAMADAS="$TMP/llamadas"
export LLAMADAS

# ══════════════════════════════════════════════════════════════════════════════
# 1 · common.sh
# ══════════════════════════════════════════════════════════════════════════════
# `oci` de mentira con un guion por llamada: RESPUESTAS="timeout timeout ok" contesta
# timeout a la 1.ª y 2.ª llamada y éxito a la 3.ª (la última se repite si hay más).
BIN_U="$TMP/bin_unit"
mkdir -p "$BIN_U"
cat > "$BIN_U/oci" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LLAMADAS"
n=$(wc -l < "$LLAMADAS" | tr -d ' ')
guion=${RESPUESTAS:-ok}
r=$(echo "$guion" | tr ' ' '\n' | sed -n "${n}p")
[[ -n "$r" ]] || r=$(echo "$guion" | awk '{print $NF}')
case $r in
    ok)      echo "${SALIDA_OK:-respuesta-ok}"; exit 0 ;;
    timeout) echo "RequestException: ConnectTimeout(... timed out)" >&2; exit 1 ;;
    404)     echo "ServiceError: {'status': 404, 'code': 'NotAuthorizedOrNotFound'}" >&2; exit 1 ;;
    503)     echo "ServiceError: {'status': 503, 'code': 'InternalServerError'}" >&2; exit 1 ;;
    auth)    echo "ServiceError: {'status': 401, 'code': 'NotAuthenticated'}" >&2; exit 1 ;;
    *)       echo "algo que nadie previó" >&2; exit 1 ;;
esac
STUB
chmod +x "$BIN_U/oci"

U="$TMP/u"; mkdir -p "$U/oci/state"
ST="$U/oci/state/t.json"
COMMON="${LIB}/common.sh"
export COMMON

# correr_c '<cuerpo>' — bash aparte con common.sh cargado (set -e, traps y exit como en deploy.sh).
correr_c() {
    : > "$LLAMADAS"
    OCI_DIR="$U/oci" OCI_PROFILE=P PATH="$BIN_U:$PATH" DEPLOY_ESPERA=0 \
        bash -c "set -euo pipefail; source \"\$COMMON\"; $1" >"$TMP/salida" 2>&1
}
llamadas() { wc -l < "$LLAMADAS" | tr -d ' '; }
salida_tiene() { grep -q -- "$1" "$TMP/salida"; }

echo "── oci_clasificar_error"
clasifica() {   # clasifica <esperado> <texto>
    printf '%s\n' "$2" > "$TMP/err"
    local got
    got=$(OCI_DIR="$U/oci" bash -c 'source "$COMMON"; oci_clasificar_error "$1"' _ "$TMP/err" 2>/dev/null)
    [[ "$got" == "$1" ]] && ok "$1 ← ${2:0:60}" || bad "esperaba $1, dio '${got}' ← $2"
}
clasifica auth        "ServiceError: {'status': 401, 'code': 'NotAuthenticated'}"
clasifica auth        '{"status": 401} y además ConnectTimeout'
clasifica 404         "ServiceError: {'status': 404, 'code': 'NotAuthorizedOrNotFound'}"
clasifica 404         '{"code": "BucketNotFound", "status": 404}'
clasifica 404         "The object does not exist"
clasifica transitorio "RequestException: ConnectTimeout(... timed out)"
clasifica transitorio "Name or service not known"
clasifica transitorio "nodename nor servname provided"
clasifica transitorio "ServiceError: {'status': 503, 'code': 'InternalServerError'}"
clasifica transitorio "ServiceError: {'status': 429, 'code': 'TooManyRequests'}"
clasifica otro        "algo que nadie previó"

echo "── oci_existe: 0 existe · 1 no existe (404) · 2 indeterminado"
RESPUESTAS=ok SALIDA_OK=hola correr_c 'rc=0; oci_existe X network vcn get || rc=$?; echo "rc=$rc out=$OCI_SALIDA"'
salida_tiene "rc=0 out=hola" && ok "éxito → 0 y OCI_SALIDA con el stdout" || bad "éxito: $(cat "$TMP/salida")"
grep -q -- '--profile P --connection-timeout 20 --read-timeout 120' "$LLAMADAS" \
    && ok "ociq pasa --profile y timeouts explícitos" || bad "ociq sin timeouts: $(cat "$LLAMADAS")"

RESPUESTAS=404 correr_c 'rc=0; oci_existe X network vcn get || rc=$?; echo "rc=$rc"'
salida_tiene "rc=1" && [[ $(llamadas) -eq 1 ]] && ok "404 → 1, sin reintentar" || bad "404: $(cat "$TMP/salida") llamadas=$(llamadas)"

RESPUESTAS=timeout DEPLOY_REINTENTOS=3 correr_c 'rc=0; oci_existe X network vcn get || rc=$?; echo "rc=$rc"'
salida_tiene "rc=2" && [[ $(llamadas) -eq 3 ]] && ok "timeout persistente → 2 tras 3 intentos (nunca 1)" \
    || bad "timeout: $(tail -1 "$TMP/salida") llamadas=$(llamadas)"

RESPUESTAS="timeout 503 ok" DEPLOY_REINTENTOS=3 correr_c 'rc=0; oci_existe X network vcn get || rc=$?; echo "rc=$rc"'
salida_tiene "rc=0" && [[ $(llamadas) -eq 3 ]] && ok "timeout, 503, éxito → 0 al tercer intento" \
    || bad "reintento: $(tail -1 "$TMP/salida") llamadas=$(llamadas)"

RESPUESTAS="timeout 404" DEPLOY_REINTENTOS=3 correr_c 'rc=0; oci_existe X network vcn get || rc=$?; echo "rc=$rc"'
salida_tiene "rc=1" && [[ $(llamadas) -eq 2 ]] && ok "timeout y luego 404 → 1" || bad "timeout+404: $(tail -1 "$TMP/salida")"

RESPUESTAS=otro DEPLOY_REINTENTOS=2 correr_c 'rc=0; oci_existe X network vcn get || rc=$?; echo "rc=$rc"'
salida_tiene "rc=2" && ok "error desconocido → 2 (indeterminado, no «no existe»)" || bad "otro: $(tail -1 "$TMP/salida")"

RESPUESTAS=auth DEPLOY_REINTENTOS=3 correr_c 'oci_existe X network vcn get; echo SIGUIO'; rc=$?
[[ $rc -ne 0 ]] && ! salida_tiene SIGUIO && [[ $(llamadas) -eq 1 ]] && salida_tiene 401 \
    && ok "401 → aborta enseguida, sin reintentos" || bad "401: rc=$rc llamadas=$(llamadas)"

echo "── oci_listar: un error nunca es «lista vacía»"
RESPUESTAS=ok SALIDA_OK=uno correr_c 'oci_listar X iam policy list'; rc=$?
[[ $rc -eq 0 ]] && salida_tiene uno && ok "éxito → imprime la salida" || bad "listar ok: rc=$rc"
RESPUESTAS=timeout DEPLOY_REINTENTOS=2 correr_c 'v=$(oci_listar X iam policy list); echo "SIGUIO[$v]"'; rc=$?
[[ $rc -ne 0 ]] && ! salida_tiene SIGUIO && ok "timeout → aborta" || bad "listar timeout: rc=$rc $(tail -1 "$TMP/salida")"
RESPUESTAS=404 correr_c 'v=$(oci_listar X iam policy list); echo "SIGUIO[$v]"'; rc=$?
[[ $rc -ne 0 ]] && ! salida_tiene SIGUIO && ok "404 → aborta" || bad "listar 404: rc=$rc"

echo "── oci_resolver"
echo '{}' > "$ST"
RESPUESTAS=ok correr_c 'rc=0; oci_resolver t vcn_id VCN network vcn get --vcn-id @ID@ || rc=$?; echo "rc=$rc"'
salida_tiene "rc=1" && [[ $(llamadas) -eq 0 ]] && ok "sin id en el state → 1 (crear), sin llamar a OCI" || bad "sin id: $(cat "$TMP/salida")"

echo '{"vcn_id":"ocid.v"}' > "$ST"
RESPUESTAS=ok correr_c 'rc=0; oci_resolver t vcn_id VCN network vcn get --vcn-id @ID@ || rc=$?; echo "rc=$rc"'
salida_tiene "rc=0" && grep -q -- '--vcn-id ocid.v' "$LLAMADAS" && ok "id existe → 0, con @ID@ reemplazado" \
    || bad "existe: $(cat "$TMP/salida") $(cat "$LLAMADAS")"

RESPUESTAS=404 correr_c 'oci_resolver t vcn_id VCN network vcn get --vcn-id @ID@; echo SIGUIO'; rc=$?
[[ $rc -ne 0 ]] && ! salida_tiene SIGUIO && salida_tiene DEPLOY_RECREAR && ok "404 sobre un id conocido → aborta y nombra DEPLOY_RECREAR" \
    || bad "404 con id: rc=$rc $(tail -2 "$TMP/salida")"

RESPUESTAS=404 DEPLOY_RECREAR=1 correr_c 'rc=0; oci_resolver t vcn_id VCN network vcn get --vcn-id @ID@ || rc=$?; echo "rc=$rc"'
salida_tiene "rc=1" && ok "404 + DEPLOY_RECREAR=1 → 1 (recrear)" || bad "recrear: $(tail -1 "$TMP/salida")"

RESPUESTAS=timeout DEPLOY_REINTENTOS=2 DEPLOY_RECREAR=1 correr_c 'oci_resolver t vcn_id VCN network vcn get --vcn-id @ID@; echo SIGUIO'; rc=$?
[[ $rc -ne 0 ]] && ! salida_tiene SIGUIO && ok "timeout → aborta (ni con DEPLOY_RECREAR=1 se recrea a ciegas)" || bad "resolver timeout: rc=$rc"

echo "── state_validar"
rm -f "$ST"
correr_c 'state_validar t; echo VALIDO'
salida_tiene VALIDO && ok "sin archivo: válido (primer deploy)" || bad "sin archivo: $(cat "$TMP/salida")"
echo '{"vcn_id":"ocid.v"}' > "$ST"
correr_c 'state_validar t; echo VALIDO'
salida_tiene VALIDO && ok "objeto JSON: válido" || bad "objeto: $(cat "$TMP/salida")"
for caso in "vacío (0 bytes)|" "array|[]" "JSON roto|{\"vcn_id\":" "texto|hola"; do
    nombre=${caso%%|*}; contenido=${caso#*|}
    printf '%s' "$contenido" > "$ST"
    correr_c 'state_validar t; echo VALIDO'; rc=$?
    [[ $rc -ne 0 ]] && ! salida_tiene VALIDO && ok "${nombre}: aborta" || bad "${nombre}: no abortó"
done

echo "── state_set"
echo '{"vcn_id":"ocid.v","public_ip":"1.1.1.1"}' | jq . > "$ST"
correr_c 'state_set t subnet_id ocid.s'
[[ "$(jq -r .subnet_id "$ST")" == ocid.s && "$(jq -r .vcn_id "$ST")" == ocid.v ]] && ok "clave nueva: se agrega sin tocar las demás" || bad "clave nueva: $(cat "$ST")"
correr_c 'state_set t vcn_id ocid.v; echo IGUAL'
salida_tiene IGUAL && ok "mismo valor en un *_id: permitido" || bad "mismo valor: $(cat "$TMP/salida")"
cp "$ST" "$TMP/antes"
correr_c 'state_set t vcn_id ocid.OTRA; echo SIGUIO'; rc=$?
[[ $rc -ne 0 ]] && ! salida_tiene SIGUIO && cmp -s "$ST" "$TMP/antes" \
    && ok "*_id existente por otro distinto: se niega y el state queda byte a byte igual" || bad "reemplazó vcn_id: $(cat "$ST")"
echo '{"adb_ocid":"ocid.a"}' > "$ST"; cp "$ST" "$TMP/antes"
correr_c 'state_set t adb_ocid ocid.OTRA'; rc=$?
[[ $rc -ne 0 ]] && cmp -s "$ST" "$TMP/antes" && ok "*_ocid también está protegido" || bad "reemplazó adb_ocid"
echo '{"vcn_id":"ocid.v","public_ip":"1.1.1.1"}' > "$ST"
DEPLOY_RECREAR=1 correr_c 'state_set t vcn_id ocid.OTRA'
[[ "$(jq -r .vcn_id "$ST")" == ocid.OTRA ]] && ok "DEPLOY_RECREAR=1: reemplaza" || bad "recrear: $(cat "$ST")"
correr_c 'state_set t public_ip 2.2.2.2'
[[ "$(jq -r .public_ip "$ST")" == 2.2.2.2 ]] && ok "clave que no es id (public_ip): se actualiza libre" || bad "public_ip: $(cat "$ST")"
printf '{"vcn_id":' > "$ST"; cp "$ST" "$TMP/antes"
correr_c 'state_set t subnet_id ocid.s'; rc=$?
[[ $rc -ne 0 ]] && cmp -s "$ST" "$TMP/antes" && ok "state corrupto: no escribe encima" || bad "escribió sobre un state corrupto"
ls "$U/oci/state/" | grep -q 't.json\.' && bad "quedaron temporales: $(ls "$U/oci/state/")" || ok "sin temporales colgando (escritura atómica)"

echo "── state_snapshot"
rm -f "$U"/oci/state/t.json*
correr_c 'state_snapshot t; echo "snap=${DEPLOY_SNAPSHOT:-}"'
salida_tiene "snap=$" && ! ls "$U"/oci/state/t.json.pre-deploy-* >/dev/null 2>&1 && ok "sin state: no hace copia" || bad "sin state: $(cat "$TMP/salida")"
for i in 1 2 3 4 5 6 7; do
    echo "{\"viejo\":\"$i\"}" > "$ST.pre-deploy-2020010${i}-000000"
    touch -t "2020010${i}0000" "$ST.pre-deploy-2020010${i}-000000"
done
echo '{"vcn_id":"ocid.v"}' > "$ST"
correr_c 'state_snapshot t; echo "snap=${DEPLOY_SNAPSHOT:-}"'
snap=$(sed -n 's/^snap=//p' "$TMP/salida")
[[ -n "$snap" && -f "$snap" && "$snap" == "$ST".pre-deploy-* ]] && ok "crea ${snap##*/} y la deja en DEPLOY_SNAPSHOT" || bad "snapshot: $(cat "$TMP/salida")"
[[ -n "$snap" ]] && cmp -s "$snap" "$ST" && ok "la copia es idéntica al state" || bad "la copia difiere"
n=$(ls -1 "$ST".pre-deploy-* 2>/dev/null | wc -l | tr -d ' ')
[[ $n -eq 5 ]] && ok "conserva 5 copias" || bad "quedaron $n copias"
[[ ! -f "$ST.pre-deploy-20200101-000000" && -f "$ST.pre-deploy-20200107-000000" ]] \
    && ok "borra las más viejas, no las recientes" || bad "rotación: $(ls "$U/oci/state/")"

echo "── deploy_lock"
rm -rf "$ST.lock"
correr_c 'deploy_lock t; echo "pid=$(cat "$OCI_DIR/state/t.json.lock/pid") yo=$$"; deploy_unlock; [[ -d "$OCI_DIR/state/t.json.lock" ]] || echo LIBRE'
pid=$(sed -n 's/^pid=\([0-9]*\) .*/\1/p' "$TMP/salida"); yo=$(sed -n 's/.* yo=//p' "$TMP/salida")
[[ -n "$pid" && "$pid" == "$yo" ]] && ok "toma el lock con su PID" || bad "lock: $(cat "$TMP/salida")"
salida_tiene LIBRE && ok "deploy_unlock lo suelta" || bad "no soltó el lock"

correr_c 'deploy_lock t
    if OCI_DIR="$OCI_DIR" bash -c "set -euo pipefail; source \"\$COMMON\"; deploy_lock t" 2>&1; then echo SEGUNDO_ENTRO; else echo SEGUNDO_RECHAZADO; fi
    [[ "$(cat "$OCI_DIR/state/t.json.lock/pid")" == "$$" ]] && echo LOCK_INTACTO'
salida_tiene SEGUNDO_RECHAZADO && ok "segundo deploy con el primero vivo: rechazado" || bad "el segundo entró: $(cat "$TMP/salida")"
salida_tiene "en curso" && ok "dice que hay un deploy en curso" || bad "no explica el rechazo"
salida_tiene LOCK_INTACTO && ok "el rechazado no toca el lock ajeno" || bad "el rechazado pisó el lock"

rm -rf "$ST.lock"; mkdir "$ST.lock"
sleep 0 & muerto=$!; wait "$muerto" 2>/dev/null
echo "$muerto" > "$ST.lock/pid"
correr_c 'deploy_lock t; echo "pid=$(cat "$OCI_DIR/state/t.json.lock/pid") yo=$$"'; rc=$?
pid=$(sed -n 's/^pid=\([0-9]*\) .*/\1/p' "$TMP/salida"); yo=$(sed -n 's/.* yo=//p' "$TMP/salida")
[[ $rc -eq 0 && -n "$pid" && "$pid" == "$yo" ]] && salida_tiene "huérfano" \
    && ok "lock obsoleto (PID muerto): se recupera y lo avisa" || bad "lock obsoleto: rc=$rc $(cat "$TMP/salida")"
rm -rf "$ST.lock"; mkdir "$ST.lock"
correr_c 'deploy_lock t; echo TOMADO'
salida_tiene TOMADO && ok "lock sin pid (cortado al crearlo): se recupera" || bad "lock sin pid: $(cat "$TMP/salida")"
rm -rf "$ST.lock"

echo "── trap de salida (_deploy_salida)"
HANDLERS='h1() { echo "h1 rc=$DEPLOY_RC"; }; h2() { echo "h2 rc=$DEPLOY_RC"; false; }; h3() { echo "h3 rc=$DEPLOY_RC"; }
    deploy_registrar_salida h1; deploy_registrar_salida h2; deploy_registrar_salida h3; deploy_registrar_salida h1
    deploy_instalar_trap'
correr_c "$HANDLERS; exit 7"; rc=$?
[[ $rc -eq 7 ]] && ok "exit 7: se conserva el 7" || bad "exit 7 salió con $rc"
[[ "$(grep -c '^h1 ' "$TMP/salida")" -eq 1 ]] && ok "un handler registrado dos veces corre una sola" || bad "h1 corrió $(grep -c '^h1 ' "$TMP/salida") veces"
[[ "$(grep '^h' "$TMP/salida" | tr '\n' ' ')" == "h1 rc=7 h2 rc=7 h3 rc=7 " ]] \
    && ok "todos corren, en orden, con DEPLOY_RC=7 (aunque uno falle)" || bad "handlers: $(grep '^h' "$TMP/salida" | tr '\n' ' ')"
correr_c "$HANDLERS; log_die boom"; rc=$?
[[ $rc -eq 1 ]] && salida_tiene "h3 rc=1" && ok "log_die: DEPLOY_RC=1 y rc 1" || bad "log_die: rc=$rc $(cat "$TMP/salida")"
correr_c "$HANDLERS; false"; rc=$?
[[ $rc -eq 1 ]] && salida_tiene "h1 rc=1" && ok "fallo bajo set -e: corren los handlers" || bad "set -e: rc=$rc"
for par in "TERM 143" "INT 130" "HUP 129"; do
    sig=${par% *}; esperado=${par#* }
    correr_c "$HANDLERS; kill -$sig \$\$; sleep 5"; rc=$?
    [[ $rc -eq $esperado ]] && salida_tiene "h3 rc=$esperado" && ok "SIG${sig}: rc ${esperado} y handlers con DEPLOY_RC=${esperado}" \
        || bad "SIG${sig}: rc=$rc $(tr '\n' ' ' < "$TMP/salida")"
done
correr_c "$HANDLERS; exit 0"; rc=$?
[[ $rc -eq 0 ]] && salida_tiene "h1 rc=0" && ok "salida feliz: rc 0, handlers con DEPLOY_RC=0" || bad "feliz: rc=$rc"

echo '{"vcn_id":"ocid.v"}' > "$ST"
correr_c 'deploy_registrar_salida _deploy_informar_estado; deploy_instalar_trap
    deploy_lock t; DEPLOY_SNAPSHOT=/ruta/de/la/copia; exit 5'; rc=$?
[[ $rc -eq 5 ]] && salida_tiene "código 5" && salida_tiene /ruta/de/la/copia && salida_tiene find-orphans \
    && ok "_deploy_informar_estado: dice el código, la copia y find-orphans" || bad "informar: rc=$rc $(cat "$TMP/salida")"
[[ ! -d "$ST.lock" ]] && ok "al salir con error suelta el lock" || bad "el lock quedó tomado"
correr_c 'deploy_registrar_salida _deploy_informar_estado; deploy_instalar_trap; deploy_lock t; exit 0'; rc=$?
[[ $rc -eq 0 && ! -d "$ST.lock" ]] && ! salida_tiene "terminó con error" && ok "salida feliz: suelta el lock y no alarma" || bad "feliz: rc=$rc"
rm -rf "$ST.lock"

echo "── Cableado en deploy.sh (que nadie lo mueva sin querer)"
DEPLOY_SH="${RAIZ_REPO}/oci/deploy.sh"
linea() { grep -n "^$1" "$DEPLOY_SH" | head -1 | cut -d: -f1; }
n_tenancy=$(linea 'assert_tenancy'); n_trap=$(linea 'deploy_instalar_trap'); n_valid=$(linea 'state_validar')
n_lock=$(linea 'deploy_lock'); n_snap=$(linea 'state_snapshot'); n_red=$(linea 'network_create')
grep -q '^deploy_registrar_salida _deploy_informar_estado' "$DEPLOY_SH" && ok "registra _deploy_informar_estado" || bad "no registra _deploy_informar_estado"
[[ -n "$n_tenancy" && -n "$n_red" && $n_tenancy -lt $n_red ]] && ok "assert_tenancy antes de crear nada" || bad "assert_tenancy no precede a network_create"
[[ -n "$n_trap" && -n "$n_lock" && $n_trap -lt $n_lock ]] && ok "el trap se instala antes de tomar el lock" || bad "el trap no precede al lock"
[[ -n "$n_valid" && -n "$n_lock" && -n "$n_snap" && -n "$n_red" && $n_valid -lt $n_snap && $n_lock -lt $n_snap && $n_snap -lt $n_red ]] \
    && ok "state_validar, lock y copia previa antes de network_create" || bad "orden de validar/lock/snapshot/network_create roto"

# ══════════════════════════════════════════════════════════════════════════════
# 2 · 03-budget / 05-adb / 06-onnx-stage / 07-backup / 08-web
# ══════════════════════════════════════════════════════════════════════════════
# `oci` falso: contesta bien a todo, salvo a la llamada que contenga $PATRON, que falla
# según $MODO (timeout | 404).
BIN_O="$TMP/bin_oci"
mkdir -p "$BIN_O"
cat > "$BIN_O/oci" <<'STUB'
#!/usr/bin/env bash
a="$*"; echo "$a" >> "$LLAMADAS"
if [[ -n "$PATRON" && "$a" == *"$PATRON"* ]]; then
    case $MODO in
        timeout) echo "RequestException: ConnectTimeout(... timed out)" >&2; exit 1 ;;
        404) echo "ServiceError: {'status': 404, 'code': 'NotAuthorizedOrNotFound'}" >&2; exit 1 ;;
    esac
fi
case "$a" in
    *"region-subscription list"*) echo us-ashburn-1 ;;
    *"subscription list"*|*"alert-rule list"*) echo 1 ;;
    *"dynamic-group list"*) echo ocid1.dynamicgroup.fake ;;
    *"policy list"*) echo ocid1.policy.fake ;;
    *"autonomous-database get"*whitelisted*) echo '["ocid1.vcn.fake","1.1.1.1","9.9.9.9"]' ;;
    *"autonomous-database get"*lifecycle*) echo AVAILABLE ;;
    *"autonomous-database get"*mtls*) echo false ;;
    *"autonomous-database get"*connection-strings*) echo "dsn-fake" ;;
    *"autonomous-database list"*) echo ocid1.adb.byname ;;
    *"os ns get"*) echo nsfake ;;
    *"private-ip list"*) echo ocid1.privateip.fake ;;
    *"public-ip get --private-ip-id"*) echo "ocid1.eph EPHEMERAL 1.1.1.1" ;;
    *"public-ip get"*lifetime*) echo RESERVED ;;
    *"public-ip get"*ip-address*) echo 2.2.2.2 ;;
    *"public-ip get"*) echo "{\"data\":{\"private-ip-id\":${ASIGNADA:-null}}}" ;;
    *" create "*) echo ocid1.creado.fake ;;
    *) echo '{"data":{}}' ;;
esac
exit 0
STUB
chmod +x "$BIN_O/oci"

STATE_BASE='{
 "instance_id": "ocid1.instance.fake", "vnic_id": "ocid1.vnic.fake",
 "vcn_id": "ocid1.vcn.fake", "public_ip": "1.1.1.1",
 "topic_id": "ocid1.topic.fake", "budget_id": "ocid1.budget.fake",
 "adb_ocid": "ocid1.adb.fake", "reserved_ip_id": "ocid1.reserved.fake"
}'
O="$TMP/o"
mkdir -p "$O/oci/lib" "$O/oci/state"
for f in common 03-budget 05-adb 06-onnx-stage 07-backup 08-web; do cp "${LIB}/$f.sh" "$O/oci/lib/"; done
cat > "$O/oci/tenants.yaml" <<'EOF'
tenants:
  tp:
    profile: P
    region: us-ashburn-1
    tenancy_ocid: ocid1.tenancy.fake
    compartment_ocid: ocid1.compartment.fake
    budget: {enabled: true, threshold_usd: 1, alert_email: a@b.c}
    adb: {enabled: true, display_name: AMALIA}
    web: {enabled: true}
EOF
SO="$O/oci/state/tp.json"

# preparar <título> <ok|timeout|404> <PATRON> [filtro jq sobre el state base]
preparar() {
    export MODO=$2 PATRON=$3
    : > "$LLAMADAS"
    rm -f "$O"/oci/state/tp.json*
    echo "$STATE_BASE" | jq "${4:-.}" > "$SO"
    cp "$SO" "$TMP/state.antes"
    echo "  ▶ $1"
}
# correr_o <función> → deja el rc en $rc
correr_o() {
    OCI_DIR="$O/oci" OCI_PROFILE=P DEPLOY_ESPERA=0 DEPLOY_REINTENTOS=2 WEB_CONFIRM=yes PATH="$BIN_O:$PATH" \
        bash -c 'set -euo pipefail
        for f in common 03-budget 05-adb 06-onnx-stage 07-backup 08-web; do source "$OCI_DIR/lib/$f.sh"; done
        "$@"' _ "$@" > "$TMP/salida" 2>&1 < /dev/null
    rc=$?
}
escrituras() { grep -E ' (create|delete|update|put) ' "$LLAMADAS" || true; }
# sin_tocar [clave]: abortó, no escribió nada en OCI y el state quedó byte a byte igual.
# [clave]: una clave NUEVA que un paso anterior, ya confirmado, puede haber agregado
# (p. ej. el dynamic group que existía y se anotó). Quitándola, el resto debe ser idéntico.
sin_tocar() {
    [[ $rc -ne 0 ]] && ok "aborta (rc=$rc)" || bad "salió 0: $(tail -3 "$TMP/salida")"
    [[ -z "$(escrituras)" ]] && ok "cero create/delete/update/put" || bad "escribió: $(escrituras)"
    if [[ -z "${1:-}" ]]; then
        cmp -s "$SO" "$TMP/state.antes" && ok "state byte a byte igual" || bad "state cambió: $(cat "$SO")"
    else
        jq --arg k "$1" 'del(.[$k])' "$SO" | cmp -s - "$TMP/state.antes" \
            && ok "state byte a byte igual (más '$1', ya confirmado)" || bad "state cambió: $(cat "$SO")"
    fi
}

echo "── (a) Camino feliz: todo existe, no se crea nada"
unset DEPLOY_RECREAR
preparar "03 budget_setup" ok ""
correr_o budget_setup tp; [[ $rc -eq 0 ]] && ok "rc 0" || bad "rc=$rc $(tail -5 "$TMP/salida")"
[[ -z "$(escrituras)" ]] && ok "no crea nada" || bad "creó: $(escrituras)"

preparar "05 adb_setup — la ADB del state existe" ok ""
correr_o adb_setup tp; [[ $rc -eq 0 ]] && ok "rc 0" || bad "rc=$rc $(tail -5 "$TMP/salida")"
grep -q "autonomous-database create" "$LLAMADAS" && bad "creó otra ADB" || ok "no crea otra ADB"
grep -q "autonomous-database update" "$LLAMADAS" && bad "tocó la ACL sin necesidad" || ok "ACL al día: no la toca"

preparar "06 onnx_stage — bucket y objeto existen" ok ""
correr_o onnx_stage tp; [[ $rc -eq 0 ]] && ok "rc 0" || bad "rc=$rc $(tail -5 "$TMP/salida")"
grep -q "bucket create" "$LLAMADAS" && bad "creó bucket" || ok "no crea bucket"

preparar "07 state_backup" ok ""
correr_o state_backup tp; [[ $rc -eq 0 ]] && ok "rc 0" || bad "rc=$rc $(tail -5 "$TMP/salida")"
[[ $(grep -c "object put" "$LLAMADAS") -eq 2 ]] && ok "sube state y copia (2 put)" || bad "subidas: $(grep -c 'object put' "$LLAMADAS")"

preparar "08 web_reserve_ip — la reservada del state ya está asignada" ok ""
ASIGNADA='"ocid1.privateip.fake"' correr_o web_reserve_ip tp; [[ $rc -eq 0 ]] && ok "rc 0" || bad "rc=$rc"
[[ -z "$(escrituras)" ]] && ok "no toca nada" || bad "escribió: $(escrituras)"

preparar "08 web_reserve_ip — sin reservada: crear → confirmar → soltar efímera → asignar" ok "" 'del(.reserved_ip_id)'
correr_o web_reserve_ip tp; [[ $rc -eq 0 ]] && ok "rc 0" || bad "rc=$rc $(tail -5 "$TMP/salida")"
orden=$(grep -oE 'public-ip (create|delete|update)' "$LLAMADAS" | awk '{print $2}' | tr '\n' ' ')
[[ "$orden" == "create delete update " ]] && ok "orden: $orden" || bad "orden: $orden"
grep 'public-ip create' "$LLAMADAS" | grep -q -- '--private-ip-id' && bad "create con --private-ip-id" || ok "create sin asignar"
[[ "$(jq -r .reserved_ip_id "$SO")" == ocid1.creado.fake && "$(jq -r .public_ip "$SO")" == 2.2.2.2 ]] \
    && ok "state: reserved_ip_id + public_ip nuevos" || bad "state: $(cat "$SO")"

preparar "08 web_reserve_ip — la confirmación de la reservada falla → NO suelta la efímera" timeout "--query data.lifetime" 'del(.reserved_ip_id)'
correr_o web_reserve_ip tp
[[ $rc -ne 0 ]] && ok "aborta" || bad "siguió"
grep -q 'public-ip delete' "$LLAMADAS" && bad "soltó la efímera" || ok "la efímera sigue"
[[ "$(jq -r .reserved_ip_id "$SO")" == ocid1.creado.fake ]] && ok "la reservada quedó en el state (retomable)" || bad "se perdió el id"

echo "── (b) ConnectTimeout en cada consulta de existencia"
#   función|patrón que falla|clave nueva admitida (de un paso previo ya confirmado)
for caso in "budget_setup|ons topic get|" "budget_setup|subscription list|" "budget_setup|budget budget get|" \
            "budget_setup|alert-rule list|" "budget_setup|dynamic-group list|" "budget_setup|policy list|dynamic_group_id" \
            "adb_acl_sync|autonomous-database get|" \
            "adb_setup|autonomous-database get --autonomous-database-id ocid1.adb.fake|" \
            "onnx_stage|os bucket get|" "onnx_stage|object head|onnx_bucket" \
            "state_backup|os bucket get|" \
            "web_reserve_ip|public-ip get --public-ip-id|" "web_reserve_ip|public-ip get --private-ip-id|"; do
    fn=${caso%%|*}; resto=${caso#*|}; pat=${resto%|*}; nueva=${resto##*|}
    preparar "timeout en '$pat' → $fn" timeout "$pat"
    correr_o "$fn" tp; sin_tocar "$nueva"
done
preparar "timeout en el list por nombre de la ADB, sin adb_ocid" timeout "autonomous-database list" 'del(.adb_ocid)'
correr_o adb_setup tp; sin_tocar
# DG y policy de la ADB se consultan DESPUÉS de las escrituras legítimas (ACL): lo que
# importa es que no se cree IAM duplicado.
for pat in "dynamic-group list" "policy list"; do
    preparar "timeout en '$pat' → adb_setup" timeout "$pat"
    correr_o adb_setup tp
    [[ $rc -ne 0 ]] && ok "aborta" || bad "siguió"
    grep -qE '(dynamic-group|policy) create' "$LLAMADAS" && bad "creó IAM duplicado" || ok "no crea IAM"
done

echo "── (c) 404 sobre un id que el state ya conoce"
for caso in "budget_setup|ons topic get" "budget_setup|budget budget get" \
            "adb_setup|autonomous-database get --autonomous-database-id ocid1.adb.fake" \
            "adb_acl_sync|autonomous-database get" "web_reserve_ip|public-ip get --public-ip-id"; do
    fn=${caso%%|*}; pat=${caso#*|}
    preparar "404 en '$pat' con id en el state → $fn" 404 "$pat"
    correr_o "$fn" tp; sin_tocar
done
preparar "404 del bucket (no está en el state) → SÍ lo crea" 404 "os bucket get"
correr_o onnx_stage tp
grep -q "bucket create" "$LLAMADAS" && ok "crea el bucket" || bad "no lo creó: $(tail -3 "$TMP/salida")"
preparar "404 + DEPLOY_RECREAR=1 → recrea el topic" 404 "ons topic get"
DEPLOY_RECREAR=1 correr_o budget_setup tp
grep -q "ons topic create" "$LLAMADAS" && ok "recrea" || bad "no recreó: $(tail -3 "$TMP/salida")"

echo "── 07: un state vacío no pisa el respaldo"
preparar "state {} → no sube" ok ""
echo '{}' > "$SO"
correr_o state_backup tp
[[ $rc -ne 0 ]] && ok "rc != 0" || bad "rc 0"
grep -q "object put" "$LLAMADAS" && bad "subió un state vacío" || ok "cero subidas"
preparar "state de 0 bytes → no sube y no mata al llamador" ok ""
: > "$SO"
OCI_DIR="$O/oci" OCI_PROFILE=P DEPLOY_ESPERA=0 PATH="$BIN_O:$PATH" bash -c 'set -euo pipefail
    source "$OCI_DIR/lib/common.sh"; source "$OCI_DIR/lib/07-backup.sh"
    state_backup tp || echo SIGUE' > "$TMP/salida" 2>&1 < /dev/null
[[ "$(tail -1 "$TMP/salida")" == SIGUE ]] && ok "no fatal: el deploy sigue" || bad "mató al llamador: $(tail -1 "$TMP/salida")"
grep -q "object put" "$LLAMADAS" && bad "subió" || ok "cero subidas"

# ══════════════════════════════════════════════════════════════════════════════
# 3 · 01-network / 02-compute
# ══════════════════════════════════════════════════════════════════════════════
# `oci` falso por modo GLOBAL: ok | timeout (todas las llamadas) | 404 (solo las que
# nombran un id «ocid.viejo.*», o sea los que el state ya conocía).
BIN_R="$TMP/bin_red"
mkdir -p "$BIN_R"
cat > "$BIN_R/oci" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LLAMADAS"
a=" $* "
modo=${FAKE_MODE:-ok}
if [[ $modo == timeout ]]; then echo "RequestException: ConnectTimeout(Max retries exceeded)" >&2; exit 1; fi
viejo404() { if [[ $modo == 404 && "$a" == *ocid.viejo* ]]; then echo 'ServiceError: {"status": 404, "code": "NotAuthorizedOrNotFound"}' >&2; exit 1; fi; }
case "$a" in
  *" vcn create "*) echo ocid.nuevo.vcn ;;
  *" internet-gateway create "*) echo ocid.nuevo.igw ;;
  *" subnet create "*) echo ocid.nuevo.subnet ;;
  *" vcn get "*default-route-table-id*) viejo404; echo ocid.rt ;;
  *" vcn get "*default-security-list-id*) viejo404; echo ocid.sl ;;
  *" get "*|*get-volume-backup-policy-asset-assignment*) viejo404
     if [[ "$a" == *" instance get "* ]]; then echo "${FAKE_INST:-RUNNING}"
     elif [[ "$a" == *get-volume-backup-policy-asset-assignment* ]]; then
        if [[ -n "${FAKE_BP_FAIL:-}" ]]; then echo "ConnectTimeout" >&2; exit 1; fi; echo ""
     elif [[ "$a" == *" vnic get "* ]]; then echo 1.1.1.1
     else echo '{}'; fi ;;
  *" update "*) : ;;
  *" availability-domain list "*) echo '["AD-1"]' ;;
  *" image list "*) echo ocid.img ;;
  *" instance launch "*) echo ocid.nuevo.inst ;;
  *" instance action "*) echo '{}' ;;
  *" list-vnics "*) echo ocid.vnic ;;
  *" boot-volume-attachment list "*) echo ocid.bv ;;
  *) echo '{}' ;;
esac
STUB
chmod +x "$BIN_R/oci"

R="$TMP/r"
mkdir -p "$R/oci/lib" "$R/oci/state"
for f in common.sh ssh-cidr.sh 01-network.sh 02-compute.sh; do cp "${LIB}/$f" "$R/oci/lib/"; done
printf '#!/bin/sh\necho "#cloud-config"\n' > "$R/oci/lib/render-cloud-init.sh"; chmod +x "$R/oci/lib/render-cloud-init.sh"
cat > "$R/oci/tenants.yaml" <<'Y'
tenants:
  t:
    compartment_ocid: ocid.comp
    network: {vcn_cidr: 10.0.0.0/16, subnet_cidr: 10.0.1.0/24, ssh_ingress_cidr: 1.2.3.4/32}
    instance: {shape: VM.Standard.E2.1.Micro, boot_volume_size_gb: 50, os: Ubuntu, os_version: "22.04", display_name: vm}
Y
SR="$R/oci/state/t.json"
# correr_r <función> — variables FAKE_* / DEPLOY_RECREAR por entorno; deja el rc en $rc.
correr_r() {
    : > "$LLAMADAS"
    OCI_DIR="$R/oci" OCI_PROFILE=X DEPLOY_REINTENTOS=1 DEPLOY_ESPERA=0 PATH="$BIN_R:$PATH" \
        bash -c 'set -euo pipefail; source "$OCI_DIR/lib/01-network.sh"; source "$OCI_DIR/lib/02-compute.sh"; "$@"' _ "$@" \
        > "$TMP/salida" 2>&1 < /dev/null
    rc=$?
}
crea() { grep -qE " create |instance launch" "$LLAMADAS"; }
LLENO='{"vcn_id":"ocid.viejo.vcn","igw_id":"ocid.viejo.igw","subnet_id":"ocid.viejo.subnet","instance_id":"ocid.viejo.inst","availability_domain":"AD-1"}'

echo "── 01-network"
echo "$LLENO" > "$SR"; FAKE_MODE=ok correr_r network_create t
[[ $rc -eq 0 ]] && ! crea && grep -q "VCN ya existe: ocid.viejo.vcn" "$TMP/salida" \
    && ok "todo existe: reutiliza sin create" || bad "red feliz: rc=$rc $(tail -3 "$TMP/salida")"

echo "$LLENO" > "$SR"; cp "$SR" "$TMP/antes"; FAKE_MODE=timeout correr_r network_create t
[[ $rc -ne 0 ]] && ! crea && cmp -s "$SR" "$TMP/antes" \
    && ok "sin internet (el incidente del 2026-10-27): aborta, sin create, state byte a byte igual" || bad "red timeout: rc=$rc"

echo '{"vcn_id":"ocid.viejo.vcn"}' > "$SR"; cp "$SR" "$TMP/antes"; FAKE_MODE=timeout correr_r network_create t
[[ $rc -ne 0 ]] && ! crea && cmp -s "$SR" "$TMP/antes" && ok "sin internet y solo vcn_id: no crea IGW/subnet" || bad "red timeout parcial: rc=$rc"

echo "$LLENO" > "$SR"; cp "$SR" "$TMP/antes"; FAKE_MODE=404 correr_r network_create t
[[ $rc -ne 0 ]] && ! crea && cmp -s "$SR" "$TMP/antes" && grep -q 404 "$TMP/salida" \
    && ok "404 con id en el state: aborta sin create, state intacto" || bad "red 404: rc=$rc"

echo "$LLENO" > "$SR"; DEPLOY_RECREAR=1 FAKE_MODE=404 correr_r network_create t
[[ $rc -eq 0 && "$(jq -r .vcn_id "$SR")" == ocid.nuevo.vcn ]] && ok "404 + DEPLOY_RECREAR=1: recrea la VCN" || bad "red recrear: rc=$rc"

echo '{}' > "$SR"; FAKE_MODE=404 correr_r network_create t
[[ $rc -eq 0 && "$(jq -r .vcn_id "$SR")" == ocid.nuevo.vcn && "$(jq -r .subnet_id "$SR")" == ocid.nuevo.subnet ]] \
    && ok "state vacío: crea VCN/IGW/subnet" || bad "red vacío: rc=$rc"

echo "── 02-compute"
COMP='{"subnet_id":"ocid.s","instance_id":"ocid.viejo.inst","availability_domain":"AD-1"}'
echo "$COMP" > "$SR"; FAKE_MODE=ok FAKE_INST=RUNNING correr_r compute_create t
[[ $rc -eq 0 ]] && ! grep -q "instance launch" "$LLAMADAS" && grep -q "No había backup policy" "$TMP/salida" \
    && ok "RUNNING: reutiliza" || bad "RUNNING: rc=$rc"

echo "$COMP" > "$SR"; FAKE_MODE=ok FAKE_INST=STOPPED correr_r compute_create t
[[ $rc -eq 0 ]] && ! grep -q "instance launch" "$LLAMADAS" && grep -q "action --instance-id ocid.viejo.inst --action START" "$LLAMADAS" \
    && ok "STOPPED: START, sin launch" || bad "STOPPED: rc=$rc"

echo "$COMP" > "$SR"; cp "$SR" "$TMP/antes"; FAKE_MODE=ok FAKE_INST=TERMINATED correr_r compute_create t
[[ $rc -ne 0 ]] && ! grep -q "instance launch" "$LLAMADAS" && cmp -s "$SR" "$TMP/antes" \
    && ok "TERMINATED: aborta, state intacto" || bad "TERMINATED: rc=$rc"

echo "$COMP" > "$SR"; DEPLOY_RECREAR=1 FAKE_MODE=ok FAKE_INST=TERMINATED correr_r compute_create t
[[ $rc -eq 0 ]] && grep -q "instance launch" "$LLAMADAS" && [[ "$(jq -r .instance_id "$SR")" == ocid.nuevo.inst ]] \
    && ok "TERMINATED + DEPLOY_RECREAR=1: lanza otra" || bad "TERMINATED recrear: rc=$rc"

echo "$COMP" > "$SR"; cp "$SR" "$TMP/antes"; FAKE_MODE=timeout correr_r compute_create t
[[ $rc -ne 0 ]] && ! grep -q "instance launch" "$LLAMADAS" && cmp -s "$SR" "$TMP/antes" \
    && ok "sin internet: aborta sin launch, state byte a byte igual" || bad "compute timeout: rc=$rc"

echo "$COMP" > "$SR"; cp "$SR" "$TMP/antes"; FAKE_MODE=404 correr_r compute_create t
[[ $rc -ne 0 ]] && ! grep -q "instance launch" "$LLAMADAS" && cmp -s "$SR" "$TMP/antes" \
    && ok "404 con id: aborta sin launch" || bad "compute 404: rc=$rc"

echo "$COMP" > "$SR"; FAKE_MODE=ok FAKE_BP_FAIL=1 correr_r compute_create t
[[ $rc -eq 0 ]] && ! grep -q "No había backup policy" "$TMP/salida" && grep -q "No pude consultar la backup policy" "$TMP/salida" \
    && ok "backup policy con error de red: avisa, no dice «no había»" || bad "backup policy: rc=$rc"

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mTodo verde.\033[0m\n'; exit 0
fi
printf '\033[0;31m%d fallo(s).\033[0m\n' "$fallos"; exit 1
