#!/usr/bin/env bash
# test_pull_deploy.sh — banco de pruebas de oci/lib/amalia-pull-deploy.sh
#
# Por qué existe: ese script solo corre en una VM, con docker y systemd al lado,
# y su forma de fallar más cara es silenciosa (dejar la VM congelada en una
# imagen vieja, o peor, cambiarla a mitad de la corrida diaria). Probarlo
# "desplegando y esperando a mañana" no es un ciclo de verificación aceptable.
#
# Cómo: se le pone delante un PATH con stubs de `docker`, `systemctl` y `logger`
# que registran lo que se les pidió, y un APP_DIR temporal con un .env de
# mentira. Lo que se verifica es la MÁQUINA DE DECISIÓN (cuándo actualiza,
# cuándo se abstiene, cuándo grita), que es donde están los errores caros. No
# verifica que docker sepa bajar la imagen — eso ya lo prueba el despliegue.
#
# Uso: bash scripts/test_pull_deploy.sh
set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/oci/lib/amalia-pull-deploy.sh"
REPO=ghcr.io/emgonzalez/tender-opportunity-tracker
SHA_VIEJO=1111111111111111111111111111111111111111
SHA_NUEVO=2222222222222222222222222222222222222222

fallos=0
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

# ── Stubs ────────────────────────────────────────────────────────────────────
crear_stubs() {
    mkdir -p "$BIN"

    # Unidades "activas" para systemctl: las que estén en $ACTIVAS.
    cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
    case " ${ACTIVAS:-} " in *" $arg "*) exit 0 ;; esac
done
exit 3
STUB

    # docker: registra la invocación y responde según PULL_RC / SHA_IMAGEN.
    cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LLAMADAS"
case "$1" in
    pull)  exit "${PULL_RC:-0}" ;;
    run)   echo "${SHA_IMAGEN}" ;;
    tag)   exit 0 ;;
    image) printf '%s\n' ${IMAGENES:-} ;;
    rmi)   exit 0 ;;
esac
exit 0
STUB

    cat > "$BIN/logger" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LLAMADAS"
STUB

    chmod +x "$BIN"/systemctl "$BIN"/docker "$BIN"/logger
}

# escenario <nombre> — prepara un APP_DIR limpio con .env
escenario() {
    RAIZ=$(mktemp -d)
    BIN="$RAIZ/bin"
    export AMALIA_APP_DIR="$RAIZ/app"
    export LLAMADAS="$RAIZ/llamadas.log"
    mkdir -p "$AMALIA_APP_DIR/data"
    : > "$LLAMADAS"
    crear_stubs
    # Valores por defecto de cada escenario; cada test los pisa antes de correr.
    export ACTIVAS="amalia.timer"
    export PULL_RC=0
    export SHA_IMAGEN="$SHA_NUEVO"
    export IMAGENES="${REPO}:sha-${SHA_VIEJO} ${REPO}:sha-${SHA_NUEVO} ${REPO}:develop"
    cat > "$AMALIA_APP_DIR/.env" <<EOF
SMTP_USER=alguien@example.com
AMALIA_IMAGE=${REPO}:sha-${SHA_VIEJO}
AMALIA_CHANNEL=develop
AMALIA_PINNED=false
EOF
    echo "▶ $1"
}

correr() { PATH="$BIN:$PATH" bash "$SCRIPT" > "$RAIZ/salida" 2>&1; echo $?; }

img_actual() { sed -n 's/^AMALIA_IMAGE=//p' "$AMALIA_APP_DIR/.env"; }
salida()     { cat "$RAIZ/salida"; }

# ── 1. Sin canal: el default no debe tocar nada ──────────────────────────────
escenario "sin canal configurado → no hace nada (default de todos los tenants de hoy)"
sed -i.bak 's/^AMALIA_CHANNEL=.*/AMALIA_CHANNEL=/' "$AMALIA_APP_DIR/.env"
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "SKIP: sin canal" \
    && ok "sale 0 sin tocar la imagen" || bad "rc=$rc — $(salida)"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_VIEJO}" ]] || bad "cambió la imagen: $(img_actual)"
grep -q '^pull' "$LLAMADAS" && bad "hizo pull sin canal" || ok "ni siquiera habló con el registro"

# ── 2. Despliegue clavado a mano (rollback) ──────────────────────────────────
escenario "AMALIA_PINNED=true → respeta el rollback manual"
sed -i.bak 's/^AMALIA_PINNED=.*/AMALIA_PINNED=true/' "$AMALIA_APP_DIR/.env"
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "clavado a mano" \
    && ok "no pisa el SHA elegido por el operador" || bad "rc=$rc — $(salida)"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_VIEJO}" ]] || bad "cambió la imagen: $(img_actual)"

# ── 3. Corrida diaria en curso ───────────────────────────────────────────────
escenario "amalia.service activo → no cambia la imagen bajo los pies de la corrida"
ACTIVAS="amalia.timer amalia.service"
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "corrida diaria está en curso" \
    && ok "se abstiene" || bad "rc=$rc — $(salida)"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_VIEJO}" ]] || bad "cambió la imagen: $(img_actual)"

# ── 4. Despliegue manual en curso (04-app.sh congela los timers) ─────────────
escenario "timers detenidos → asume deploy manual en curso y se abstiene"
ACTIVAS=""
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "scheduler está detenido" \
    && ok "no se pelea con el deploy manual" || bad "rc=$rc — $(salida)"

# ── 5. Camino feliz ──────────────────────────────────────────────────────────
escenario "canal movido → actualiza y deja un sha-<40hex> en el .env"
rc=$(correr)
[[ $rc == 0 ]] || bad "rc=$rc — $(salida)"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_NUEVO}" ]] \
    && ok "el .env apunta al SHA exacto, no a la etiqueta móvil" \
    || bad "quedó $(img_actual)"
grep -q "^tag ${REPO}:develop ${REPO}:sha-${SHA_NUEVO}$" "$LLAMADAS" \
    && ok "reetiqueta local en vez de bajar la imagen dos veces" || bad "no reetiquetó"
grep -q "^rmi .*sha-${SHA_VIEJO}" "$LLAMADAS" \
    && ok "borra la imagen vieja (el disco de 50 GB no aguanta acumular ~2.7 GB por update)" \
    || bad "no limpió: $(grep '^rmi' "$LLAMADAS" || echo '(ningún rmi)')"
grep -q "^rmi .*sha-${SHA_NUEVO}" "$LLAMADAS" && bad "borró la imagen que acaba de activar" \
    || ok "conserva la nueva y la del canal"
# El resto del .env tiene que sobrevivir intacto: si se perdiera, la corrida
# siguiente arrancaría sin credenciales.
grep -q '^SMTP_USER=alguien@example.com$' "$AMALIA_APP_DIR/.env" \
    && ok "no pisa el resto del .env" || bad "se comió variables del .env"
[[ "$(stat -c '%a' "$AMALIA_APP_DIR/.env" 2>/dev/null || stat -f '%Lp' "$AMALIA_APP_DIR/.env")" == 600 ]] \
    && ok ".env queda en 0600" || bad "permisos del .env: no son 600"

# ── 6. El canal no se movió ──────────────────────────────────────────────────
escenario "el canal apunta al SHA que ya corre → idempotente"
SHA_IMAGEN="$SHA_VIEJO"
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "el canal no se movió" \
    && ok "no reescribe el .env por gusto" || bad "rc=$rc — $(salida)"

# ── 7. PAT vencido ───────────────────────────────────────────────────────────
escenario "docker pull falla (PAT vencido) → RUIDOSO y sin tocar la imagen"
PULL_RC=1
rc=$(correr)
[[ $rc == 1 ]] && ok "sale != 0 → la unit queda en 'failed'" || bad "rc=$rc (debía ser 1)"
salida | grep -q "PAT de GHCR" && ok "el mensaje nombra la causa probable" || bad "mensaje poco útil"
grep -q "auto-update:" "$LLAMADAS" && ok "deja rastro en el journal (logger user.err)" || bad "no usó logger"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_VIEJO}" ]] \
    && ok "la VM se queda en la imagen que funcionaba" || bad "rompió la imagen actual"

# ── 8. Imagen sin sello de versión ───────────────────────────────────────────
escenario "imagen sin sello (--version no da un SHA) → no se despliega"
SHA_IMAGEN="dev"
rc=$(correr)
[[ $rc == 1 ]] && salida | grep -q "sin sello no hay trazabilidad" \
    && ok "prefiere no actualizar antes que perder la trazabilidad" || bad "rc=$rc — $(salida)"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_VIEJO}" ]] || bad "cambió la imagen: $(img_actual)"

# ── 9. Tenant con SDK de OCI (Fase M): el canal no puede perder el sufijo ────
# Bug real, encontrado en la auditoría de migración a A1: `repo` se calcula
# quitando TODA la etiqueta de AMALIA_IMAGE, así que si esa etiqueta terminaba
# en `-oci` (LLM_PROVIDER=oci), el sufijo se perdía y el canal bajaba la
# variante SIN el SDK — la corrida de las 07:00 moría con
# `ModuleNotFoundError: No module named 'oci'`, silenciosa hasta esa hora.
escenario "tenant con -oci → el canal y el SHA final conservan el sufijo"
sed -i.bak "s#^AMALIA_IMAGE=.*#AMALIA_IMAGE=${REPO}:sha-${SHA_VIEJO}-oci#" "$AMALIA_APP_DIR/.env"
IMAGENES="${REPO}:sha-${SHA_VIEJO}-oci ${REPO}:sha-${SHA_NUEVO}-oci ${REPO}:develop-oci"
rc=$(correr)
[[ $rc == 0 ]] || bad "rc=$rc — $(salida)"
grep -q "^pull .*${REPO}:develop-oci\$" "$LLAMADAS" \
    && ok "bajó el canal CON el sufijo (${REPO}:develop-oci)" \
    || bad "bajó el canal sin el sufijo: $(grep '^pull' "$LLAMADAS")"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_NUEVO}-oci" ]] \
    && ok "el .env quedó apuntando a sha-${SHA_NUEVO}-oci, no a la variante sin SDK" \
    || bad "quedó $(img_actual) — perdió el SDK de OCI"
grep -q "^tag ${REPO}:develop-oci ${REPO}:sha-${SHA_NUEVO}-oci\$" "$LLAMADAS" \
    && ok "reetiqueta preservando el sufijo" || bad "reetiquetó sin el sufijo"

# ── 10. Mercado semanal en curso ─────────────────────────────────────────────
# amalia-mercado.service encadena dos `docker compose run` (participados y
# resolver): cambiar la imagen entre uno y otro los correría con código distinto.
escenario "amalia-mercado.service activo → no cambia la imagen a mitad del mercado semanal"
ACTIVAS="amalia.timer amalia-mercado.service"
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "amalia-mercado.service" \
    && ok "se abstiene mientras corre el mercado semanal" || bad "rc=$rc — $(salida)"
[[ "$(img_actual)" == "${REPO}:sha-${SHA_VIEJO}" ]] || bad "cambió la imagen: $(img_actual)"

escenario "amalia-mercado.timer activo NO cuenta como scheduler diario activo"
ACTIVAS="amalia-mercado.timer"
rc=$(correr)
[[ $rc == 0 ]] && salida | grep -q "scheduler está detenido" \
    && ok "con amalia.timer pausado se sigue absteniendo" || bad "rc=$rc — $(salida)"

# ════════════════════════════════════════════════════════════════════════════
# oci/lib/mercado/amalia-mercado.sh — la corrida semanal de src.mercado
# ════════════════════════════════════════════════════════════════════════════
# Mismo enfoque: stubs de docker/systemctl/logger/timeout/sleep y un APP_DIR de
# mentira. Se verifica la máquina de decisión (cuándo corre, en qué orden,
# cuándo se abstiene, cuándo grita), no que Playwright sepa loguearse.
MERCADO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/oci/lib/mercado/amalia-mercado.sh"

escenario_mercado() {
    RAIZ=$(mktemp -d)
    BIN="$RAIZ/bin"
    export AMALIA_APP_DIR="$RAIZ/app"
    export LLAMADAS="$RAIZ/llamadas.log"
    export AMALIA_COMPOSE_FILE=docker-compose.e2.yml
    export AMALIA_MERCADO_CLIENTE=multicomputos
    export AMALIA_MERCADO_WAIT_MAX_S=90 AMALIA_MERCADO_WAIT_STEP_S=30
    mkdir -p "$AMALIA_APP_DIR/data" "$BIN"
    : > "$LLAMADAS"
    export ACTIVAS="" PARTICIPADOS_RC=0 RESOLVER_RC=0
    cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
    case " ${ACTIVAS:-} " in *" $arg "*) exit 0 ;; esac
done
exit 3
STUB
    # docker: anota y falla según el subcomando de src.mercado pedido.
    cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$LLAMADAS"
case "$*" in
    *"src.mercado participados"*) exit "${PARTICIPADOS_RC:-0}" ;;
    *"src.mercado resolver"*)     exit "${RESOLVER_RC:-0}" ;;
esac
exit 0
STUB
    cat > "$BIN/logger" <<'STUB'
#!/usr/bin/env bash
echo "logger $*" >> "$LLAMADAS"
STUB
    # timeout <s> cmd... → cmd... (macOS no trae timeout; acá no se mide tiempo)
    cat > "$BIN/timeout" <<'STUB'
#!/usr/bin/env bash
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
EOF
    echo "▶ $1"
}
correr_mercado() { PATH="$BIN:$PATH" bash "$MERCADO" > "$RAIZ/salida" 2>&1; echo $?; }
pasos() { grep -o 'src\.mercado [a-z]*' "$LLAMADAS" | awk '{print $2}' | tr '\n' ' '; }

escenario_mercado "ENABLE_MERCADO_CRON=false en el .env → no hace nada (segunda llave)"
sed -i.bak 's/^ENABLE_MERCADO_CRON=.*/ENABLE_MERCADO_CRON=false/' "$AMALIA_APP_DIR/.env"
rc=$(correr_mercado)
[[ $rc == 0 ]] && salida | grep -q "SKIP: ENABLE_MERCADO_CRON" \
    && ok "sale 0 sin correr nada" || bad "rc=$rc — $(salida)"
grep -q '^docker' "$LLAMADAS" && bad "llamó a docker con el flag apagado" || ok "ni siquiera levanta un contenedor"

escenario_mercado "camino feliz → participados y LUEGO resolver, con el cliente y el compose del tenant"
rc=$(correr_mercado)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(pasos)" == "participados resolver " ]] && ok "orden: participados → resolver" || bad "pasos: '$(pasos)'"
grep -q 'compose -f docker-compose.e2.yml run --rm amalia python -m src.mercado participados --cliente multicomputos' "$LLAMADAS" \
    && ok "participados con --cliente multicomputos y el compose del tenant" || bad "invocación inesperada: $(grep participados "$LLAMADAS")"
grep -q 'src.mercado mercado' "$LLAMADAS" && bad "corrió el barrido completo (debía quedar manual)" \
    || ok "NO corre el barrido completo del registro (queda manual)"
grep -q 'ops_alert' "$LLAMADAS" && bad "mandó una alerta en el camino feliz" || ok "sin alertas en el camino feliz"

escenario_mercado "participados falla (sesión expirada) → RUIDOSO y resolver NO corre"
PARTICIPADOS_RC=1
rc=$(correr_mercado)
[[ $rc == 1 ]] && ok "sale 1 → la unit queda en 'failed' y dispara OnFailure" || bad "rc=$rc (debía ser 1)"
[[ "$(pasos)" == "participados " ]] && ok "resolver no corrió" || bad "pasos: '$(pasos)'"
salida | grep -q "sesión de COMPRASAL" && ok "el mensaje nombra la causa probable (sesión)" || bad "mensaje poco útil: $(salida)"
grep -q 'logger .*user.err' "$LLAMADAS" && ok "deja rastro en el journal (logger user.err)" || bad "no usó logger"
grep -q 'src.notifier.ops_alert' "$LLAMADAS" && ok "manda correo a los system admins (ops_alert)" || bad "no avisó por correo"

escenario_mercado "resolver falla → RUIDOSO"
RESOLVER_RC=1
rc=$(correr_mercado)
[[ $rc == 1 ]] && salida | grep -q "'resolver' falló" && ok "sale 1 y lo dice" || bad "rc=$rc — $(salida)"
grep -q 'src.notifier.ops_alert' "$LLAMADAS" && ok "avisa por correo" || bad "no avisó por correo"

escenario_mercado "corrida diaria activa todo el rato → espera, y se rinde ruidoso sin levantar Chromium"
ACTIVAS="amalia.service"
rc=$(correr_mercado)
[[ $rc == 1 ]] && salida | grep -q "amalia.service sigue activa" && ok "se rinde con mensaje claro" || bad "rc=$rc — $(salida)"
[[ -z "$(pasos)" ]] && ok "no corrió ningún paso encima de la corrida diaria" || bad "pasos: '$(pasos)'"
[[ $(grep -c '^sleep' "$LLAMADAS") -eq 3 ]] && ok "esperó (3 × 30 s hasta el tope de 90 s)" || bad "esperas: $(grep -c '^sleep' "$LLAMADAS")"

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mTodo OK\033[0m\n'
else
    printf '\033[0;31m%d verificación(es) fallaron\033[0m\n' "$fallos"
fi
exit $((fallos > 0))
