#!/usr/bin/env bash
# test_deploy_timers.sh — banco de pruebas de la red de seguridad de oci/lib/04-app.sh
#
# Por qué existe: `app_deploy` detiene los timers al empezar y los reactivaba solo
# al final. Un deploy cortado a la mitad dejaba `amalia.timer` inactivo y la
# corrida de las 07:00 sin ocurrir, en silencio (pasó el 2026-09-24). El arreglo es
# un `trap`, y un trap es exactamente el tipo de código que se rompe sin que nadie
# lo note hasta el día que hace falta. Este banco lo ejercita de verdad: sale por
# `exit`, por señal, y por el camino feliz.
#
# Cómo: un `ssh` de mentira delante del PATH registra lo que se le pidió. No
# verifica que systemd sepa arrancar un timer — eso lo prueba el despliegue.
#
# Uso: bash scripts/test_deploy_timers.sh
set -uo pipefail

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SH="${RAIZ_REPO}/oci/lib/04-app.sh"
MERCADO_SH="${RAIZ_REPO}/oci/lib/09-mercado.sh"

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; LLAMADAS="$TMP/llamadas"
mkdir -p "$BIN"

# ssh de mentira: anota los argumentos y contesta según SSH_RC. Una llamada = una
# línea (el comando remoto es multilínea): así se cuentan llamadas contando líneas.
#   SSH_OUT: lo que "contesta" el host (vacío por defecto).
#   Si el comando remoto es `bash -s`, el script que llega por stdin se guarda
#   en $LLAMADAS.stdin (aparte, para no alterar el conteo de llamadas).
cat > "$BIN/ssh" <<'STUB'
#!/usr/bin/env bash
args="$*"
echo "${args//$'\n'/ }" >> "$LLAMADAS"
[[ "$args" == *"bash -s"* ]] && cat >> "${LLAMADAS}.stdin"
[[ -n "${SSH_OUT:-}" ]] && echo "$SSH_OUT"
exit "${SSH_RC:-0}"
STUB
# scp de mentira: anota la llamada y copia los archivos locales a $SCP_DIR para
# poder inspeccionar las units ya renderizadas.
cat > "$BIN/scp" <<'STUB'
#!/usr/bin/env bash
echo "scp $*" >> "$LLAMADAS"
mkdir -p "$SCP_DIR"
for a in "$@"; do [[ -f "$a" ]] && cp "$a" "$SCP_DIR/"; done
exit "${SCP_RC:-0}"
STUB
chmod +x "$BIN/ssh" "$BIN/scp"

# Corre un escenario en un bash aparte (así `set -e`, los traps y las señales se
# comportan como en deploy.sh) y deja stdout+stderr en $TMP/salida.
#   $1 = cuerpo del escenario, ejecutado después de cargar 04-app.sh
correr() {
    : > "$LLAMADAS"
    OCI_DIR="${RAIZ_REPO}/oci" PATH="$BIN:$PATH" LLAMADAS="$LLAMADAS" SSH_RC="${SSH_RC:-0}" \
        bash -c "set -euo pipefail; source '$APP_SH'; $1" >"$TMP/salida" 2>&1
    return $?
}
llamadas() { wc -l < "$LLAMADAS" | tr -d ' '; }

echo "── Salida anormal: el trap reactiva los timers"

correr '_timers_armar_restauracion -i clave ubuntu@1.2.3.4; log_die "boom"'; rc=$?
[[ $rc -eq 1 ]] && ok "log_die: el código de salida original (1) se conserva" || bad "log_die: salió con $rc, esperaba 1"
[[ $(llamadas) -eq 1 ]] && ok "log_die: se llamó a ssh una vez para restaurar" || bad "log_die: $(llamadas) llamadas a ssh"
grep -q 'ubuntu@1.2.3.4' "$LLAMADAS" && ok "restaura contra el host del despliegue" || bad "no usó el host del despliegue"
grep -q 'ConnectTimeout=10' "$LLAMADAS" && ok "con ConnectTimeout: un SSH caído no cuelga la salida" || bad "sin ConnectTimeout"
for t in amalia.timer amalia-scrape.timer amalia-pull-deploy.timer amalia-mercado.timer; do
    grep -q "$t" "$LLAMADAS" && ok "considera $t" || bad "no considera $t"
done
grep -q 'is-enabled' "$LLAMADAS" && ok "solo arranca lo que está habilitado (is-enabled)" || bad "no filtra por is-enabled"
grep -q 'A MEDIAS' "$TMP/salida" && ok "avisa que el despliegue pudo quedar a medias" || bad "no avisa del despliegue a medias"

correr '_timers_armar_restauracion ubuntu@h; false'; rc=$?
[[ $rc -ne 0 && $(llamadas) -eq 1 ]] && ok "fallo bajo set -e: también restaura" || bad "set -e: rc=$rc llamadas=$(llamadas)"

correr '_timers_armar_restauracion ubuntu@h; exit 7'; rc=$?
[[ $rc -eq 7 ]] && ok "exit 7: se conserva el 7" || bad "exit 7 salió con $rc"

echo "── Señales: Ctrl-C, TERM y HUP también restauran"
for sig in INT TERM HUP; do
    : > "$LLAMADAS"
    OCI_DIR="${RAIZ_REPO}/oci" PATH="$BIN:$PATH" LLAMADAS="$LLAMADAS" \
        bash -c "set -euo pipefail; source '$APP_SH'; _timers_armar_restauracion ubuntu@h; kill -$sig \$\$; sleep 5" \
        >"$TMP/salida" 2>&1
    [[ $(llamadas) -eq 1 ]] && ok "SIG$sig: restauró" || bad "SIG$sig: $(llamadas) llamadas a ssh"
done

echo "── Camino feliz: desarmado, no toca nada"
correr '_timers_armar_restauracion ubuntu@h; _timers_desarmar; exit 0'; rc=$?
[[ $rc -eq 0 && $(llamadas) -eq 0 ]] && ok "desarmado: no llama a ssh y sale 0" || bad "desarmado: rc=$rc llamadas=$(llamadas)"
correr 'true'; rc=$?
[[ $rc -eq 0 && $(llamadas) -eq 0 ]] && ok "sin armar: no llama a ssh" || bad "sin armar: rc=$rc llamadas=$(llamadas)"

echo "── Robustez"
SSH_RC=255 correr '_timers_armar_restauracion ubuntu@h; log_die "boom"'; rc=$?
[[ $rc -eq 1 ]] && ok "restauración fallida: aun así sale con el código original" || bad "restauración fallida: rc=$rc"
grep -q 'SIGUE SIN CORRIDA DIARIA' "$TMP/salida" && ok "restauración fallida: lo dice fuerte" || bad "restauración fallida: no avisa"
grep -q 'systemctl start amalia.timer' "$TMP/salida" && ok "restauración fallida: da el comando manual" || bad "no da el comando manual"

correr '_timers_armar_restauracion ubuntu@h; exit 3'
[[ $(grep -c 'is-enabled' "$LLAMADAS") -eq 1 ]] && ok "restaura UNA sola vez, no dos" || bad "restauró más de una vez"

echo "── Cableado en app_deploy (que nadie lo mueva sin querer)"
n_armar=$(grep -n '_timers_armar_restauracion "' "$APP_SH" | head -1 | cut -d: -f1)
n_parar=$(grep -n 'systemctl stop amalia.timer' "$APP_SH" | head -1 | cut -d: -f1)
n_desarmar=$(grep -n '^        _timers_desarmar' "$APP_SH" | head -1 | cut -d: -f1)
n_reactivar=$(grep -n 'systemctl enable --now amalia.timer' "$APP_SH" | head -1 | cut -d: -f1)
[[ -n "$n_armar" && -n "$n_parar" && $n_armar -lt $n_parar ]] \
    && ok "se arma ANTES de detener los timers (línea $n_armar < $n_parar)" \
    || bad "el trap no se arma antes de detener los timers"
[[ -n "$n_desarmar" && -n "$n_reactivar" && $n_desarmar -gt $n_reactivar ]] \
    && ok "se desarma DESPUÉS de reactivar (línea $n_desarmar > $n_reactivar)" \
    || bad "el trap se desarma antes de reactivar los timers"
# -A, no -B: con la rama de hold_timers (Fase M) delante, la que YA
# desarmaba (la `elif` original) quedó más lejos del `-B3` que medía desde
# `_timers_desarmar` hacia atrás. Mirar HACIA ADELANTE desde el propio
# `is-active` es robusto sin importar cuántas ramas la precedan.
grep -A4 'is-active --quiet amalia.timer' "$APP_SH" | grep -q '_timers_desarmar' \
    && ok "solo se desarma con amalia.timer verificado ACTIVO (salvo hold_timers)" \
    || bad "se desarma sin verificar is-active"

echo "── Mercado semanal: cableado en app_deploy"
linea_stop=$(grep -n 'systemctl stop amalia.timer' "$APP_SH" | head -1 | cut -d: -f1)
sed -n "${linea_stop},$((linea_stop + 1))p" "$APP_SH" > "$TMP/stop"
grep -q 'amalia-mercado.timer' "$TMP/stop" && ok "el deploy detiene amalia-mercado.timer con los demás" || bad "el deploy no detiene amalia-mercado.timer"
grep -q 'amalia-mercado.service' "$TMP/stop" && ok "el deploy detiene una corrida de mercado en curso" || bad "el deploy no detiene amalia-mercado.service"
grep -qE '"ENABLE_MERCADO_CRON +features\.enable_mercado_cron false"' "$APP_SH" \
    && ok "ENABLE_MERCADO_CRON está en tenant_env con default false (un redeploy no lo pisa)" \
    || bad "ENABLE_MERCADO_CRON falta en tenant_env o su default no es false"
n_sync=$(grep -n 'mercado_cron_sync "\$tenant"' "$APP_SH" | head -1 | cut -d: -f1)
[[ -n "$n_sync" && -n "$n_reactivar" && $n_sync -gt $n_reactivar ]] \
    && ok "mercado_cron_sync corre DESPUÉS de reactivar amalia.timer (línea $n_sync > $n_reactivar)" \
    || bad "mercado_cron_sync no está después de reactivar amalia.timer"
grep -qF 'source "${OCI_DIR}/lib/09-mercado.sh"' "${RAIZ_REPO}/oci/deploy.sh" \
    && ok "deploy.sh carga 09-mercado.sh" || bad "deploy.sh no carga 09-mercado.sh"

echo "── Mercado semanal: mercado_cron_sync (flag por tenant)"
# tenant_get_or de mentira: los valores salen de variables, así cada escenario
# fija el flag sin depender de tenants.yaml. Se define DESPUÉS del source.
STUB_TENANT='tenant_get_or() {
    case "$2" in
        features.enable_mercado_cron) echo "${FLAG:-False}" ;;
        instance.hold_timers)         echo "${HOLD:-False}" ;;
        mercado.schedule_day)         echo "${DIA:-$3}" ;;
        mercado.schedule_time)        echo "${HORA:-$3}" ;;
        mercado.cliente)              echo "${CLIENTE:-$3}" ;;
        docker.compose_file)          echo docker-compose.e2.yml ;;
        *)                            echo "$3" ;;
    esac
}'
correr_mercado() {
    : > "$LLAMADAS"; rm -f "${LLAMADAS}.stdin"; rm -rf "$TMP/scp"
    OCI_DIR="${RAIZ_REPO}/oci" PATH="$BIN:$PATH" LLAMADAS="$LLAMADAS" SCP_DIR="$TMP/scp" \
        SSH_RC="${SSH_RC:-0}" SSH_OUT="${SSH_OUT:-}" SCP_RC="${SCP_RC:-0}" \
        FLAG="${FLAG:-False}" HOLD="${HOLD:-False}" DIA="${DIA:-}" HORA="${HORA:-}" CLIENTE="${CLIENTE:-}" \
        bash -c "set -euo pipefail; source '$MERCADO_SH'; $STUB_TENANT; mercado_cron_sync t ubuntu@h -i clave" \
        >"$TMP/salida" 2>&1
}

FLAG=False correr_mercado; rc=$?
[[ $rc -eq 0 ]] && ok "flag apagado: sale 0" || bad "flag apagado: rc=$rc"
[[ $(llamadas) -eq 1 ]] && ok "flag apagado: UNA sola consulta por SSH" || bad "flag apagado: $(llamadas) llamadas"
grep -q 'scp' "$LLAMADAS" && bad "flag apagado: copió units" || ok "flag apagado: no copia ninguna unit"
grep -q 'enable --now' "$LLAMADAS" && bad "flag apagado: habilitó algo" || ok "flag apagado: no habilita nada"
grep -qF '[ -f /etc/systemd/system/amalia-mercado.timer ]' "$LLAMADAS" \
    && ok "flag apagado: solo toca la VM si la unit ya existía" || bad "flag apagado: actúa sin mirar si la unit existe"
grep -q 'DESHABILITADO' "$TMP/salida" && bad "flag apagado en VM sin unit: avisó de un apagado que no hubo" \
    || ok "flag apagado en VM sin unit: silencio (comportamiento de siempre)"

FLAG=False SSH_OUT=apagado correr_mercado
grep -q 'DESHABILITADO' "$TMP/salida" && ok "flag apagado en VM que lo tenía: lo deshabilita y lo dice" \
    || bad "no avisó al deshabilitar"

FLAG=True HOLD=True correr_mercado
grep -q 'scp' "$LLAMADAS" && bad "hold_timers: instaló igual" || ok "hold_timers (tenant en sombra): no se instala aunque el flag esté encendido"

FLAG=True DIA=Domingo correr_mercado; rc=$?
[[ $rc -eq 0 ]] && ! grep -q 'scp' "$LLAMADAS" && grep -q 'inválidos' "$TMP/salida" \
    && ok "día inválido: no instala y lo avisa (sin romper el deploy)" || bad "día inválido: rc=$rc"

FLAG=True correr_mercado; rc=$?
[[ $rc -eq 0 ]] && ok "flag encendido: sale 0" || bad "flag encendido: rc=$rc — $(cat "$TMP/salida")"
grep -q '^scp ' "$LLAMADAS" && ok "flag encendido: copia las units" || bad "flag encendido: no copió nada"
grep -qxF 'OnCalendar=Sun *-*-* 11:00:00' "$TMP/scp/amalia-mercado.timer" 2>/dev/null \
    && ok "timer renderizado: domingo 11:00 por defecto" || bad "timer mal renderizado: $(grep OnCalendar "$TMP/scp/amalia-mercado.timer" 2>/dev/null)"
grep -q '__' "$TMP/scp/amalia-mercado.timer" "$TMP/scp/amalia-mercado.service" 2>/dev/null \
    && bad "quedaron marcadores sin reemplazar" || ok "sin marcadores __X__ sin reemplazar"
grep -qx 'Environment=AMALIA_COMPOSE_FILE=docker-compose.e2.yml' "$TMP/scp/amalia-mercado.service" 2>/dev/null \
    && ok "service: compose del tenant" || bad "service: compose mal renderizado"
grep -qx 'Environment=AMALIA_MERCADO_CLIENTE=multicomputos_sv' "$TMP/scp/amalia-mercado.service" 2>/dev/null \
    && ok "service: cliente multicomputos_sv por defecto" || bad "service: cliente mal renderizado"
grep -qx 'OnFailure=amalia-failure.service' "$TMP/scp/amalia-mercado.service" 2>/dev/null \
    && ok "service: falla ruidosa vía OnFailure=amalia-failure.service" || bad "service: sin OnFailure"
grep -q '^Conflicts=' "$TMP/scp/amalia-mercado.service" 2>/dev/null \
    && bad "service: Conflicts= mataría una corrida diaria en curso" || ok "service: sin Conflicts= (la exclusión la hace el script, esperando)"
grep -q 'systemctl enable --now amalia-mercado.timer' "${LLAMADAS}.stdin" 2>/dev/null \
    && ok "flag encendido: habilita el timer con enable --now" || bad "no habilita el timer"
grep -q 'is-active --quiet amalia-mercado.timer || sudo systemctl start' "${LLAMADAS}.stdin" 2>/dev/null \
    && ok "flag encendido: confirma is-active y fuerza start (RUN-001)" || bad "no verifica is-active"
grep -q 'systemctl start amalia-mercado.service' "$LLAMADAS" "${LLAMADAS}.stdin" 2>/dev/null \
    && bad "dispara una corrida durante el deploy" || ok "no dispara una corrida durante el deploy (Chromium en 1 GB)"

FLAG=True SCP_RC=1 correr_mercado; rc=$?
[[ $rc -eq 0 ]] && grep -q 'NO quedó programado' "$TMP/salida" \
    && ok "scp fallido: avisa fuerte pero no rompe el deploy" || bad "scp fallido: rc=$rc"

# El trap RETURN de app_deploy borra el .env temporal con secretos: esta función
# corre adentro y no puede pisarlo.
grep -qE '^[[:space:]]*trap .*RETURN' "$MERCADO_SH" && bad "09-mercado.sh pone un trap RETURN (pisaría el de app_deploy)" \
    || ok "09-mercado.sh no pisa el trap RETURN de app_deploy"

if command -v python3 >/dev/null 2>&1 && [[ -f "${RAIZ_REPO}/oci/tenants.yaml" ]]; then
    valor_real=$(OCI_DIR="${RAIZ_REPO}/oci" bash -c '
        source "'"${RAIZ_REPO}"'/oci/lib/common.sh"
        tenant_get_or multicomputos_dev features.enable_mercado_cron false
    ' 2>/dev/null)
    [[ "$valor_real" == "False" ]] \
        && ok "tenants.yaml#multicomputos_dev.features.enable_mercado_cron sigue APAGADO" \
        || bad "enable_mercado_cron resolvió a '${valor_real}' — encenderlo es decisión del operador, no de un PR"
fi

echo "── hold_timers (Fase M: tenant en sombra, sin timer propio) ─────────────"
# tenants.yaml serializa True/False con mayúscula (str(bool) de Python, lo que
# devuelve tenant_get_or) — NO "true"/"false" en minúscula. Comparar contra el
# casing equivocado es un bug real que ya se cometió una vez acá mismo: el
# valor nunca hacía match y la rama de hold_timers jamás se activaba en la
# práctica, en silencio.
grep -q '"\$hold_timers" == "true"' "$APP_SH" \
    && bad "compara contra \"true\" en minúscula — tenant_get_or nunca devuelve eso" \
    || ok "compara contra el casing real (\"True\") que devuelve tenant_get_or"
grep -c '"\$hold_timers" == "True"' "$APP_SH" | grep -qx 2 \
    && ok "las DOS ramas (encender el timer / desarmar el trap) miran hold_timers" \
    || bad "falta alguna de las dos ramas que deberían mirar hold_timers"
# El valor real, contra el tenants.yaml de este repo (no un stub): si algún día
# alguien renombra la clave o cambia su ruta, esto lo nota sin depender de que
# nadie se acuerde de correr un deploy de prueba contra la A1 para notarlo.
if command -v python3 >/dev/null 2>&1 && [[ -f "${RAIZ_REPO}/oci/tenants.yaml" ]]; then
    valor_real=$(OCI_DIR="${RAIZ_REPO}/oci" bash -c '
        source "'"${RAIZ_REPO}"'/oci/lib/common.sh"
        tenant_get_or multicomputos_dev_a1 instance.hold_timers false
    ' 2>/dev/null)
    [[ "$valor_real" == "True" ]] \
        && ok "tenants.yaml#multicomputos_dev_a1.instance.hold_timers resuelve a \"True\"" \
        || bad "resolvió a '${valor_real}', no 'True' — revisá tenants.yaml o common.sh"
fi

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mTodo verde.\033[0m\n'; exit 0
fi
printf '\033[0;31m%d fallo(s).\033[0m\n' "$fallos"; exit 1
