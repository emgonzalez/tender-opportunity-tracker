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

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mTodo OK\033[0m\n'
else
    printf '\033[0;31m%d verificación(es) fallaron\033[0m\n' "$fallos"
fi
exit $((fallos > 0))
