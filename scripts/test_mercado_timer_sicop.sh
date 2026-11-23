#!/usr/bin/env bash
# test_mercado_timer_sicop.sh — banco del paso SICOP del mercado semanal.
#
# Qué cubre (sin red, sin OCI, sin Docker):
#   - oci/lib/mercado/amalia-mercado.sh: ENABLE_MERCADO_SICOP apaga/enciende el
#     paso, va DESPUÉS de participados+resolver, usa `timeout $STEP_TIMEOUT_S`,
#     no corre el mercado en firme salvo ENABLE_MERCADO_SICOP_FIRME en la primera
#     semana del mes, y un fallo suyo sale 1 con un mensaje que nombra el WAF
#     (BloqueoAcceso), el journal y que COMPRASAL sí quedó.
#   - oci/lib/04-app.sh: el mapeo features.enable_mercado_sicop → .env, default false.
#   - oci/lib/09-mercado.sh: avisos de combinación inválida (no bloquean).
#   - oci/lib/mercado/amalia-mercado.service: TimeoutStartSec cubre el peor caso.
#   - oci/tenants.yaml: multicomputos_dev lo tiene APAGADO.
#
# Mismo enfoque que scripts/test_pull_deploy.sh: stubs de docker/systemctl/logger/
# timeout/sleep delante del PATH y un APP_DIR de mentira.
#
# Uso: bash scripts/test_mercado_timer_sicop.sh
set -uo pipefail

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MERCADO="${RAIZ_REPO}/oci/lib/mercado/amalia-mercado.sh"
SERVICE="${RAIZ_REPO}/oci/lib/mercado/amalia-mercado.service"
APP_SH="${RAIZ_REPO}/oci/lib/04-app.sh"
MERCADO_SH="${RAIZ_REPO}/oci/lib/09-mercado.sh"

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

RAICES=()
trap 'rm -rf "${RAICES[@]:-}"' EXIT

# ── amalia-mercado.sh con stubs ──────────────────────────────────────────────
escenario() {
    RAIZ=$(mktemp -d); RAICES+=("$RAIZ")
    BIN="$RAIZ/bin"
    export AMALIA_APP_DIR="$RAIZ/app"
    export LLAMADAS="$RAIZ/llamadas.log"
    export AMALIA_COMPOSE_FILE=docker-compose.e2.yml
    export AMALIA_MERCADO_CLIENTE=multicomputos_sv
    export AMALIA_MERCADO_WAIT_MAX_S=90 AMALIA_MERCADO_WAIT_STEP_S=30
    export AMALIA_MERCADO_STEP_TIMEOUT_S=5400
    export AMALIA_MERCADO_HOY_DIA=15
    mkdir -p "$AMALIA_APP_DIR/data" "$BIN"
    : > "$LLAMADAS"
    export ACTIVAS="" PARTICIPADOS_RC=0 RESOLVER_RC=0 SICOP_RC=0
    cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
    case " ${ACTIVAS:-} " in *" $arg "*) exit 0 ;; esac
done
exit 3
STUB
    cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$LLAMADAS"
case "$*" in
    *"src.mercado participados"*)        exit "${PARTICIPADOS_RC:-0}" ;;
    *"src.mercado resolver"*)            exit "${RESOLVER_RC:-0}" ;;
    *"src.mercado --portal sicop_cr"*)   exit "${SICOP_RC:-0}" ;;
esac
exit 0
STUB
    cat > "$BIN/logger" <<'STUB'
#!/usr/bin/env bash
echo "logger $*" >> "$LLAMADAS"
STUB
    # timeout <s> cmd... → anota el tope y ejecuta cmd (macOS no trae timeout).
    cat > "$BIN/timeout" <<'STUB'
#!/usr/bin/env bash
echo "timeout $1" >> "$LLAMADAS"
shift; exec "$@"
STUB
    cat > "$BIN/sleep" <<'STUB'
#!/usr/bin/env bash
echo "sleep $*" >> "$LLAMADAS"
STUB
    chmod +x "$BIN"/*
    cat > "$AMALIA_APP_DIR/.env" <<EOF
SMTP_USER=alguien@example.com
ENABLE_MERCADO_CRON=true
ENABLE_MERCADO_SICOP=true
SICOP_HEADLESS=false
EOF
    echo "▶ $1"
}
env_set() {  # env_set VAR valor  (reemplaza o agrega)
    grep -v "^$1=" "$AMALIA_APP_DIR/.env" > "$AMALIA_APP_DIR/.env.n"
    echo "$1=$2" >> "$AMALIA_APP_DIR/.env.n"
    mv "$AMALIA_APP_DIR/.env.n" "$AMALIA_APP_DIR/.env"
}
correr() { PATH="$BIN:$PATH" bash "$MERCADO" > "$RAIZ/salida" 2>&1; echo $?; }
salida() { cat "$RAIZ/salida"; }
pasos() {
    grep -oE 'src\.mercado (participados|resolver|--portal sicop_cr)' "$LLAMADAS" \
        | sed -e 's/src\.mercado //' -e 's/--portal sicop_cr/sicop/' | tr '\n' ' '
}
linea_sicop() { grep -- '--portal sicop_cr' "$LLAMADAS" | grep -v ops_alert; }

escenario "ENABLE_MERCADO_SICOP=false → solo COMPRASAL, sale 0 y lo dice"
env_set ENABLE_MERCADO_SICOP false
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(pasos)" == "participados resolver " ]] && ok "pasos: participados → resolver, sin SICOP" || bad "pasos: '$(pasos)'"
salida | grep -q "paso omitido" && ok "el journal dice que SICOP se omitió" || bad "no dejó rastro del omitido"

escenario "ENABLE_MERCADO_SICOP ausente del .env → igual que false (default apagado)"
grep -v '^ENABLE_MERCADO_SICOP=' "$AMALIA_APP_DIR/.env" > "$RAIZ/e" && mv "$RAIZ/e" "$AMALIA_APP_DIR/.env"
rc=$(correr)
[[ $rc == 0 && "$(pasos)" == "participados resolver " ]] && ok "sin la variable no corre SICOP" || bad "rc=$rc pasos='$(pasos)'"

escenario "ENABLE_MERCADO_CRON=false → no corre nada, tampoco SICOP"
env_set ENABLE_MERCADO_CRON false
rc=$(correr)
[[ $rc == 0 && -z "$(pasos)" ]] && ok "la segunda llave del timer también apaga SICOP" || bad "rc=$rc pasos='$(pasos)'"

escenario "encendido → SICOP AL FINAL, con la invocación exacta y el timeout por paso"
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(pasos)" == "participados resolver sicop " ]] && ok "orden: participados → resolver → sicop" || bad "pasos: '$(pasos)'"
linea_sicop | grep -qx 'docker compose -f docker-compose.e2.yml run --rm amalia python -m src.mercado --portal sicop_cr --aplicar --dias 60' \
    && ok "invocación: compose del tenant, --portal sicop_cr --aplicar --dias 60" || bad "invocación: '$(linea_sicop)'"
linea_sicop | grep -q -- '--mercado' && bad "corrió el mercado en firme sin pedirlo" || ok "sin --mercado (pesado: queda manual/mensual)"
[[ $(grep -c '^timeout 5400$' "$LLAMADAS") -eq 3 ]] \
    && ok "los 3 pasos (también SICOP) van bajo timeout \$STEP_TIMEOUT_S" || bad "timeouts: $(grep '^timeout' "$LLAMADAS" | tr '\n' ' ')"
n_t=$(grep -n '^timeout' "$LLAMADAS" | tail -1 | cut -d: -f1); n_s=$(grep -n -- '--portal sicop_cr' "$LLAMADAS" | head -1 | cut -d: -f1)
[[ -n "$n_t" && -n "$n_s" && $n_t -lt $n_s ]] && ok "el timeout envuelve la llamada de SICOP" || bad "timeout sin envolver SICOP"
salida | grep -q "OK: sicop_cr" && ok "el journal cierra con OK: sicop_cr" || bad "sin OK de SICOP: $(salida)"
grep -q 'ops_alert' "$LLAMADAS" && bad "alertó en el camino feliz" || ok "sin alertas en el camino feliz"

escenario "AMALIA_MERCADO_STEP_TIMEOUT_S cambia el tope también para SICOP"
export AMALIA_MERCADO_STEP_TIMEOUT_S=123
rc=$(correr)
[[ $(grep -c '^timeout 123$' "$LLAMADAS") -eq 3 ]] && ok "SICOP usa el mismo STEP_TIMEOUT_S" || bad "timeouts: $(grep '^timeout' "$LLAMADAS" | tr '\n' ' ')"

escenario "SICOP falla (WAF) → sale 1, RUIDOSO, y COMPRASAL ya había corrido"
SICOP_RC=1
rc=$(correr)
[[ $rc == 1 ]] && ok "sale 1 → la unit queda 'failed' y dispara OnFailure" || bad "rc=$rc (debía ser 1)"
[[ "$(pasos)" == "participados resolver sicop " ]] && ok "participados y resolver corrieron ANTES (lo de COMPRASAL quedó)" || bad "pasos: '$(pasos)'"
salida | grep -q "FALLO: SICOP" && ok "el log usa el estilo de die (FALLO: ...)" || bad "mensaje: $(salida)"
salida | grep -q "BloqueoAcceso" && salida | grep -q "WAF" && ok "el mensaje nombra WAF/BloqueoAcceso" || bad "no nombra el WAF: $(salida)"
salida | grep -q "journalctl -u amalia-mercado.service" && ok "el mensaje dice cómo leer el journal" || bad "sin journalctl en el mensaje"
salida | grep -q "COMPRASAL (participados + resolver) SÍ quedó" && ok "el mensaje aclara que COMPRASAL quedó" || bad "no aclara COMPRASAL"
grep -q 'logger .*user.err' "$LLAMADAS" && ok "deja rastro en el journal (logger user.err)" || bad "no usó logger"
grep -q 'src.notifier.ops_alert' "$LLAMADAS" && ok "manda correo a los system admins (ops_alert)" || bad "no avisó por correo"

escenario "participados falla → sale 1 y SICOP NO corre (va al final)"
PARTICIPADOS_RC=1
rc=$(correr)
[[ $rc == 1 && "$(pasos)" == "participados " ]] && ok "SICOP no corre si COMPRASAL falló antes" || bad "rc=$rc pasos='$(pasos)'"

escenario "resolver falla → sale 1 y SICOP NO corre"
RESOLVER_RC=1
rc=$(correr)
[[ $rc == 1 && "$(pasos)" == "participados resolver " ]] && ok "SICOP no corre si resolver falló" || bad "rc=$rc pasos='$(pasos)'"

escenario "SICOP_HEADLESS != false → falla ANTES de tocar SICOP (no golpea al WAF)"
env_set SICOP_HEADLESS true
rc=$(correr)
[[ $rc == 1 ]] && ok "sale 1" || bad "rc=$rc"
[[ "$(pasos)" == "participados resolver " ]] && ok "no llamó a SICOP" || bad "pasos: '$(pasos)'"
salida | grep -q "SICOP_HEADLESS" && salida | grep -q "BloqueoAcceso" && ok "el mensaje explica headless/WAF" || bad "mensaje: $(salida)"

escenario "ENABLE_MERCADO_SICOP_FIRME=true, día 3 del mes → agrega el mercado en firme"
env_set ENABLE_MERCADO_SICOP_FIRME true
export AMALIA_MERCADO_HOY_DIA=03
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
linea_sicop | grep -q -- '--portal sicop_cr --aplicar --dias 60 --mercado --dias-mercado 31' \
    && ok "primera semana del mes: --mercado --dias-mercado 31" || bad "invocación: '$(linea_sicop)'"
linea_sicop | grep -q -- '--max-paginas 250' && ok "con --max-paginas 250 (un mes ~221 páginas, el tope 100 truncaría)" || bad "sin --max-paginas"

escenario "ENABLE_MERCADO_SICOP_FIRME=true, día 08 → sin mercado en firme (08 no es octal)"
env_set ENABLE_MERCADO_SICOP_FIRME true
export AMALIA_MERCADO_HOY_DIA=08
rc=$(correr)
[[ $rc == 0 ]] && ! linea_sicop | grep -q -- '--mercado' && ok "fuera de la primera semana: solo lo propio" || bad "rc=$rc — $(linea_sicop) $(salida)"

escenario "ENABLE_MERCADO_SICOP_FIRME=false, día 01 → sin mercado en firme"
export AMALIA_MERCADO_HOY_DIA=01
rc=$(correr)
[[ $rc == 0 ]] && ! linea_sicop | grep -q -- '--mercado' && ok "sin el flag no hay barrido en firme" || bad "rc=$rc — $(linea_sicop)"

# ── 04-app.sh: mapeo al .env ─────────────────────────────────────────────────
echo "── 04-app.sh: mapeo de las variables"
grep -qE '"ENABLE_MERCADO_SICOP +features\.enable_mercado_sicop false"' "$APP_SH" \
    && ok "ENABLE_MERCADO_SICOP ← features.enable_mercado_sicop, default false" || bad "falta el mapeo de ENABLE_MERCADO_SICOP"
grep -qE '"ENABLE_MERCADO_SICOP_FIRME +features\.enable_mercado_sicop_firme false"' "$APP_SH" \
    && ok "ENABLE_MERCADO_SICOP_FIRME ← features.enable_mercado_sicop_firme, default false" || bad "falta el mapeo de ENABLE_MERCADO_SICOP_FIRME"
grep -qE '"SICOP_HEADLESS +features\.sicop_headless +true"' "$APP_SH" \
    && ok "SICOP_HEADLESS sigue mapeado (lo lee el paso)" || bad "SICOP_HEADLESS no está en tenant_env"

# ── service: TimeoutStartSec ─────────────────────────────────────────────────
echo "── amalia-mercado.service: TimeoutStartSec"
a_seg() {  # "5h30min" / "4h" / "300min" → segundos
    local v=$1 total=0 n u
    while [[ $v =~ ^([0-9]+)(h|min|s)?(.*)$ ]]; do
        n=${BASH_REMATCH[1]}; u=${BASH_REMATCH[2]:-s}; v=${BASH_REMATCH[3]}
        case $u in h) total=$((total + n * 3600)) ;; min) total=$((total + n * 60)) ;; s) total=$((total + n)) ;; esac
        [[ -z "$v" ]] && break
    done
    echo "$total"
}
tss=$(sed -n 's/^TimeoutStartSec=//p' "$SERVICE")
peor=$(( 1800 + 3 * 5400 + 300 ))   # espera + 3 pasos + correo de fallo (defaults del script)
grep -q 'WAIT_MAX_S:-1800' "$MERCADO" && grep -q 'STEP_TIMEOUT_S:-5400' "$MERCADO" \
    && ok "defaults del script: espera 1800 s, 5400 s por paso" || bad "cambiaron los defaults del script: recalcular el peor caso"
(( $(a_seg "$tss") >= peor )) && ok "TimeoutStartSec=${tss} ≥ peor caso ${peor} s (systemd no corta antes que el timeout del script)" \
    || bad "TimeoutStartSec=${tss} ($(a_seg "$tss") s) < peor caso ${peor} s"

# ── 09-mercado.sh: avisos de combinación ────────────────────────────────────
echo "── 09-mercado.sh: avisos (no bloquean)"
avisos() {
    OCI_DIR="${RAIZ_REPO}/oci" CRON="$1" SICOP="$2" HEADLESS="$3" bash -c "
        set -euo pipefail
        source '$MERCADO_SH'
        tenant_get_or() {
            case \"\$2\" in
                features.enable_mercado_cron)  echo \"\$CRON\" ;;
                features.enable_mercado_sicop) echo \"\$SICOP\" ;;
                features.sicop_headless)       echo \"\$HEADLESS\" ;;
                *)                             echo \"\$3\" ;;
            esac
        }
        _mercado_sicop_avisos t" 2>&1
}
s=$(avisos False True False); rc=$?
[[ $rc -eq 0 ]] && grep -q 'SICOP NO va a correr' <<<"$s" && ok "SICOP sin enable_mercado_cron: avisa y no rompe" || bad "rc=$rc — $s"
s=$(avisos True True True)
grep -q 'sicop_headless: false' <<<"$s" && ok "SICOP con headless: avisa que el WAF lo va a rechazar" || bad "sin aviso: $s"
s=$(avisos True True False)
[[ -z "$s" ]] && ok "combinación correcta: sin avisos" || bad "avisó de más: $s"
s=$(avisos True False True)
[[ -z "$s" ]] && ok "SICOP apagado: sin avisos" || bad "avisó de más: $s"

# ── tenants.yaml: apagado en DEV ─────────────────────────────────────────────
if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' 2>/dev/null; then
    echo "── tenants.yaml"
    for clave in enable_mercado_sicop enable_mercado_sicop_firme; do
        v=$(OCI_DIR="${RAIZ_REPO}/oci" bash -c "source '${RAIZ_REPO}/oci/lib/common.sh'; tenant_get_or multicomputos_dev features.${clave} False" 2>/dev/null)
        [[ "$v" == "False" ]] && ok "multicomputos_dev.features.${clave} APAGADO" \
            || bad "${clave} resolvió a '${v}' — encenderlo es decisión del operador"
    done
fi

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mTodo OK\033[0m\n'
else
    printf '\033[0;31m%d verificación(es) fallaron\033[0m\n' "$fallos"
fi
exit $((fallos > 0))
