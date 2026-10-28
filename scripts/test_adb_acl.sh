#!/usr/bin/env bash
# test_adb_acl.sh — banco de pruebas de adb_acl_sync (oci/lib/05-adb.sh)
#
# Por qué existe: hasta la Fase M, `adb_acl_sync` REEMPLAZABA la lista entera de
# IPs permitidas de la ADB por las dos entradas de ESTE tenant. Es seguro con un
# solo tenant, pero durante la migración a A1 dos tenants pueden compartir la
# MISMA ADB (a propósito, para no duplicar la memoria institucional): con
# reemplazo, el primero de los dos que redesplegara borraba en silencio la
# entrada del otro, que se quedaba sin acceso hasta un ORA-12506. Este banco
# defiende la propiedad que reemplaza a esa:
#
#     adb_acl_sync solo AGREGA. Nunca hace desaparecer una entrada que no es
#     la suya.
#
# Cómo: se stubea `oci` (no hace falta systemctl/docker acá) y se llama a la
# función DIRECTAMENTE (sourceada), no un binario completo — es una función
# bash, no un script con su propio main.
#
# Uso: bash scripts/test_adb_acl.sh
set -uo pipefail

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

# escenario <nombre> — árbol OCI_DIR temporal propio, ACL de partida en $1 (o "null")
escenario() {
    local acl_actual="${2:-null}"
    RAIZ=$(mktemp -d)
    BIN="$RAIZ/bin"
    export OCI_DIR="$RAIZ/oci"
    export LLAMADAS="$RAIZ/llamadas.log"
    export OCI_PROFILE="PERFIL_DE_PRUEBA"
    mkdir -p "$BIN" "$OCI_DIR/lib" "$OCI_DIR/state"
    cp "${RAIZ_REPO}/oci/lib/common.sh" "$OCI_DIR/lib/"
    cp "${RAIZ_REPO}/oci/lib/05-adb.sh"  "$OCI_DIR/lib/"
    : > "$LLAMADAS"

    export ACL_ACTUAL="$acl_actual"
    cat > "$BIN/oci" <<'STUB'
#!/usr/bin/env bash
args="$*"
echo "$args" >> "$LLAMADAS"
case "$args" in
    *"autonomous-database get"*)
        echo "${ACL_ACTUAL:-null}" ;;
esac
case "$args" in *"autonomous-database update"*) exit "${UPDATE_RC:-0}" ;; esac
exit 0
STUB
    chmod +x "$BIN/oci"

    cat > "$OCI_DIR/tenants.yaml" <<'EOF'
tenants:
  tprueba:
    profile: PERFIL_DE_PRUEBA
    network:
      share_from: tpadre
EOF
    cat > "$OCI_DIR/state/tprueba.json" <<EOF
{
  "adb_ocid": "ocid1.autonomousdatabase.oc1..adb",
  "vcn_id": "ocid1.vcn.oc1..vcndetprueba",
  "public_ip": "1.2.3.4"
}
EOF
    unset UPDATE_RC
    echo "▶ $1"
}

correr() {
    PATH="$BIN:$PATH" bash -c '
        set -euo pipefail
        source "'"$OCI_DIR"'/lib/common.sh"
        source "'"$OCI_DIR"'/lib/05-adb.sh"
        adb_acl_sync tprueba
    ' > "$RAIZ/salida" 2>&1
    echo $?
}
salida() { cat "$RAIZ/salida"; }
# El array exacto que se le pasó a --whitelisted-ips en la llamada de update.
acl_enviada() {
    grep 'autonomous-database update' "$LLAMADAS" | sed -n 's/.*--whitelisted-ips \(\[[^]]*\]\).*/\1/p'
}
tiene() { echo "$1" | jq -e --arg v "$2" 'index($v) != null' >/dev/null; }

# ── 1. ACL vacía (recién provisionada) → agrega la propia, sin más ──────────
escenario "ACL vacía → agrega VCN + IP de este tenant, nada más" "null"
rc=$(correr)
[[ $rc == 0 ]] || bad "rc=$rc — $(salida)"
enviada=$(acl_enviada)
tiene "$enviada" "ocid1.vcn.oc1..vcndetprueba" && ok "incluye la VCN propia" || bad "falta la VCN: $enviada"
tiene "$enviada" "1.2.3.4" && ok "incluye la IP propia" || bad "falta la IP: $enviada"
[[ "$(echo "$enviada" | jq 'length')" == 2 ]] && ok "exactamente 2 entradas (no inventa más)" || bad "trae de más: $enviada"

# ── 2. LA prueba: ya había entradas de OTRO tenant → se CONSERVAN ──────────
escenario "ACL con entradas de OTRO tenant → se agregan las propias, NO se pisan" \
    '["ocid1.vcn.oc1..otrotenant","9.9.9.9"]'
rc=$(correr)
[[ $rc == 0 ]] || bad "rc=$rc — $(salida)"
enviada=$(acl_enviada)
tiene "$enviada" "ocid1.vcn.oc1..otrotenant" && ok "conserva la VCN del otro tenant" || bad "BORRÓ la VCN del otro tenant: $enviada"
tiene "$enviada" "9.9.9.9" && ok "conserva la IP del otro tenant" || bad "BORRÓ la IP del otro tenant: $enviada"
tiene "$enviada" "ocid1.vcn.oc1..vcndetprueba" && ok "agrega la VCN propia" || bad "no agregó la propia: $enviada"
tiene "$enviada" "1.2.3.4" && ok "agrega la IP propia" || bad "no agregó la propia: $enviada"
[[ "$(echo "$enviada" | jq 'length')" == 4 ]] && ok "unión de las 4, sin duplicar" || bad "no es la unión: $enviada"

# ── 3. Ya está todo (idempotente) → CERO llamadas de escritura ─────────────
escenario "la ACL ya tiene todo lo necesario → no hay llamada de update" \
    '["ocid1.vcn.oc1..vcndetprueba","1.2.3.4","ocid1.vcn.oc1..otrotenant"]'
rc=$(correr)
[[ $rc == 0 ]] || bad "rc=$rc — $(salida)"
[[ -z "$(acl_enviada)" ]] && ok "no llamó a update (ya estaba al día)" || bad "reescribió sin necesidad: $(acl_enviada)"
grep -q "autonomous-database update" "$LLAMADAS" && bad "hizo una llamada de escritura de más" || ok "cero llamadas de escritura"

# ── 4. Sin adb_ocid → no habla con la ADB en absoluto ───────────────────────
escenario "tenant sin adb_ocid → adb_acl_sync es un no-op total" "null"
echo '{}' > "$OCI_DIR/state/tprueba.json"
rc=$(correr)
[[ $rc == 0 ]] || bad "rc=$rc — $(salida)"
[[ ! -s "$LLAMADAS" ]] && ok "ni siquiera llamó a 'oci'" || bad "llamó a oci sin tener adb_ocid: $(llamadas 2>/dev/null; cat "$LLAMADAS")"

# ── 5. Falla la actualización → sale != 0, no se queda callado ─────────────
escenario "el update de OCI falla → adb_acl_sync no se lo traga" \
    '["ocid1.vcn.oc1..otrotenant"]'
export UPDATE_RC=1
rc=$(correr)
[[ $rc != 0 ]] && ok "sale != 0 (no miente diciendo que sincronizó)" || bad "salió 0 con el update fallado"

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mbanco de adb_acl_sync: todo verde\033[0m\n'
else
    printf '\033[0;31m%d verificación(es) fallaron\033[0m\n' "$fallos"
fi
exit $((fallos > 0))
