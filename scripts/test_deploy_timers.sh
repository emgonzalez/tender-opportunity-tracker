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

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; LLAMADAS="$TMP/llamadas"
mkdir -p "$BIN"

# ssh de mentira: anota los argumentos y contesta según SSH_RC. Una llamada = una
# línea (el comando remoto es multilínea): así se cuentan llamadas contando líneas.
cat > "$BIN/ssh" <<'STUB'
#!/usr/bin/env bash
args="$*"
echo "${args//$'\n'/ }" >> "$LLAMADAS"
exit "${SSH_RC:-0}"
STUB
chmod +x "$BIN/ssh"

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
for t in amalia.timer amalia-scrape.timer amalia-pull-deploy.timer; do
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
