#!/usr/bin/env bash
# test_ssh_allow.sh — banco de pruebas de oci/scripts/ssh-allow.sh
#
# Por qué existe: este script edita la regla que decide si el operador puede
# entrar a una VM viva. Sus dos formas de fallar son caras y asimétricas:
#   · escribir cuando no hacía falta (rompe la idempotencia, y cada escritura
#     es una oportunidad de dejar la regla mal), y
#   · escribir algo distinto de lo que se calculó (deja el 22 abierto a internet
#     o cerrado para todos).
# Probarlo "corriéndolo contra OCI a ver qué pasa" no es un ciclo aceptable:
# la única VM contra la que valdría la pena probarlo es la que no se puede
# perder.
#
# Cómo: se le pone delante un PATH con stubs de `oci`, `curl` y `nc`. El stub de
# `oci` registra cada invocación en un log y devuelve una security list de
# mentira; el de `curl` simula los servicios de eco (incluyendo un proxy que
# miente); el de `nc` simula que el 22 responde o no. Lo que se verifica es la
# MÁQUINA DE DECISIÓN: cuándo escribe, cuándo se abstiene, y qué escribe.
#
# Uso: bash scripts/test_ssh_allow.sh
set -uo pipefail

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${RAIZ_REPO}/oci/scripts/ssh-allow.sh"
SL=ocid1.securitylist.oc1.iad.aaaatest
IP_VM=203.0.113.10          # TEST-NET-3, nunca es una VM real
IP_OK=179.5.96.48
IP_PROXY=198.51.100.7       # TEST-NET-2, la "IP del proxy" del escenario mentiroso

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

# ── Stubs ────────────────────────────────────────────────────────────────────
crear_stubs() {
    mkdir -p "$BIN"

    # oci: registra la invocación completa y contesta el `get` con la security
    # list que arme el escenario ($SL_JSON). El `update` APLICA el cambio sobre
    # ese mismo archivo —no solo lo registra— para que dos corridas seguidas
    # vean el mundo que dejó la anterior: es la única forma de probar de verdad
    # la idempotencia y no una versión de juguete.
    cat > "$BIN/oci" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LLAMADAS"
for arg in "$@"; do
    case "$arg" in
        get) cat "$SL_JSON"; exit 0 ;;
        update)
            [[ "${UPDATE_RC:-0}" == 0 ]] || exit "${UPDATE_RC}"
            prev=""; ing=""; egr=""
            for a in "$@"; do
                case "$prev" in
                    --ingress-security-rules) ing="$a" ;;
                    --egress-security-rules)  egr="$a" ;;
                esac
                prev="$a"
            done
            if [[ -n "$ing" ]]; then
                # El CLI real NO es simétrico: acepta camelCase en el update y
                # devuelve kebab-case en el get. El stub reproduce esa asimetría
                # —guarda en kebab lo que recibió en camel— porque si no, la
                # prueba de idempotencia sería falsa: pasaría con un script que
                # en la vida real el API rechaza.
                jq --argjson i "$ing" --argjson e "${egr:-[]}" '
                    def kebab: gsub("(?<a>[A-Z])"; "-" + (.a|ascii_downcase));
                    def dekebab: walk(if type=="object" then with_entries(.key |= kebab) else . end);
                    .data."ingress-security-rules"=($i|dekebab)
                  | .data."egress-security-rules"=($e|dekebab)' \
                   "$SL_JSON" > "$SL_JSON.n" && mv "$SL_JSON.n" "$SL_JSON"
            fi
            exit 0 ;;
    esac
done
exit 0
STUB

    # curl: simula los servicios de eco. Distingue -4 de -6 igual que el real,
    # que es justo lo que importa acá: la red del operador tiene IPv6 y el
    # script tiene que quedarse con la familia que sirve para llegar a la VM.
    # RESPUESTAS es la lista de respuestas IPv4, una por proveedor y en orden
    # ("-" = ese proveedor falla); RESPUESTA_V6 es lo que contesta el sondeo -6.
    cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
    if [[ "$a" == "-6" ]]; then
        [[ -n "${RESPUESTA_V6:-}" ]] || exit 7
        echo "$RESPUESTA_V6"; exit 0
    fi
done
n=$(cat "$CONTADOR" 2>/dev/null || echo 0)
echo $((n + 1)) > "$CONTADOR"
read -r -a r <<< "${RESPUESTAS:-}"
v="${r[$n]:-}"
[[ -z "$v" || "$v" == "-" ]] && exit 7
echo "$v"
STUB

    # nc: ¿responde el 22? Lo decide el escenario.
    cat > "$BIN/nc" <<'STUB'
#!/usr/bin/env bash
exit "${NC_RC:-0}"
STUB

    chmod +x "$BIN"/oci "$BIN"/curl "$BIN"/nc
}

# sec_list <cidr-del-22> — arma la respuesta del `security-list get`, con la
# forma REAL del OCI CLI (kebab-case, protocol como string) y una segunda regla
# de ingress que este script no debe tocar nunca.
sec_list() {
    cat > "$SL_JSON" <<EOF
{
  "data": {
    "id": "${SL}",
    "egress-security-rules": [
      {"destination":"0.0.0.0/0","destination-type":"CIDR_BLOCK","is-stateless":false,"protocol":"all","tcp-options":null,"udp-options":null}
    ],
    "ingress-security-rules": [
      {"protocol":"6","source":"$1","source-type":"CIDR_BLOCK","is-stateless":false,
       "tcp-options":{"destination-port-range":{"max":22,"min":22},"source-port-range":null},"icmp-options":null,"udp-options":null},
      {"protocol":"6","source":"10.0.0.0/16","source-type":"CIDR_BLOCK","is-stateless":false,
       "tcp-options":{"destination-port-range":{"max":443,"min":443},"source-port-range":null},"icmp-options":null,"udp-options":null}
    ]
  },
  "etag": "abc123"
}
EOF
}

# escenario <nombre> — OCI_DIR temporal con tenants.yaml y state propios, para
# no leer ni escribir NADA del repo real.
escenario() {
    RAIZ=$(mktemp -d)
    BIN="$RAIZ/bin"
    export OCI_DIR="$RAIZ/oci"
    export LLAMADAS="$RAIZ/llamadas.log"
    export SL_JSON="$RAIZ/sl.json"
    export CONTADOR="$RAIZ/contador"
    mkdir -p "$OCI_DIR/lib" "$OCI_DIR/scripts" "$OCI_DIR/state"
    cp "${RAIZ_REPO}/oci/lib/common.sh" "${RAIZ_REPO}/oci/lib/ssh-cidr.sh" "$OCI_DIR/lib/"
    : > "$LLAMADAS"; echo 0 > "$CONTADOR"
    crear_stubs

    cat > "$OCI_DIR/tenants.yaml" <<'EOF'
tenants:
  tprueba:
    profile: PERFIL_DE_PRUEBA
    network:
      ssh_ingress_cidr: auto
EOF
    cat > "$OCI_DIR/state/tprueba.json" <<EOF
{"security_list_id":"${SL}","public_ip":"${IP_VM}"}
EOF

    # Defaults de cada escenario; cada test los pisa antes de correr.
    export RESPUESTAS="$IP_OK $IP_OK $IP_OK"
    export RESPUESTA_V6=""
    export NC_RC=0
    export UPDATE_RC=0
    sec_list "0.0.0.0/0"
    echo "▶ $1"
}

correr() { PATH="$BIN:$PATH" bash "$SCRIPT" "$@" > "$RAIZ/salida" 2>&1; echo $?; }
salida() { cat "$RAIZ/salida"; }
updates() { grep -c 'security-list update' "$LLAMADAS" || true; }
# El JSON que se le pasó al update. Se recorta en el flag siguiente: si no, el
# ingress "incluiría" el egress y cualquier aserción sobre 0.0.0.0/0 daría un
# falso positivo (el egress SÍ es 0.0.0.0/0 y así debe quedar).
ingress_escrito() { sed -n 's/.*--ingress-security-rules //p' "$LLAMADAS" | head -1 | sed 's/ --egress-security-rules.*//'; }
egress_escrito()  { sed -n 's/.*--egress-security-rules //p'  "$LLAMADAS" | head -1 | sed 's/ --force.*//'; }

# ── 1. Camino feliz ──────────────────────────────────────────────────────────
escenario "0.0.0.0/0 → /32 del operador: escribe una vez y verifica"
rc=$(correr tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(updates)" == 1 ]] && ok "exactamente una escritura" || bad "$(updates) escrituras"
ingress_escrito | grep -q "${IP_OK}/32" && ok "el 22 queda en ${IP_OK}/32" || bad "no escribió el /32: $(ingress_escrito)"
ingress_escrito | grep -q '"443"\|443' && ok "conserva la otra regla de ingress (443)" || bad "se comió la regla de 443"
ingress_escrito | grep -q '0\.0\.0\.0/0' && bad "dejó 0.0.0.0/0 en el ingress" || ok "ya no hay 0.0.0.0/0 en el ingress"
egress_escrito | grep -q '"protocol":"all"' && ok "el egress se reescribe idéntico" || bad "tocó el egress: $(egress_escrito)"
salida | grep -q 'responde desde esta red' && ok "verifica el 22 después de escribir" || bad "no verificó"

# ── 2. Idempotencia — el criterio de aceptación de la Fase 7 ─────────────────
escenario "dos corridas seguidas → la segunda no hace NINGUNA escritura"
rc=$(correr tprueba)
[[ $rc == 0 ]] && [[ "$(updates)" == 1 ]] && ok "1ª corrida: una escritura" || bad "1ª: rc=$rc, $(updates) escrituras"
: > "$LLAMADAS"; echo 0 > "$CONTADOR"   # los servicios de eco vuelven a contestar
rc=$(correr tprueba)
[[ $rc == 0 ]] && ok "2ª corrida: sale 0" || bad "2ª: rc=$rc — $(salida)"
[[ "$(updates)" == 0 ]] && ok "2ª corrida: CERO escrituras" || bad "2ª: escribió $(updates) vez/veces"

escenario "la regla ya es el /32 correcto → no escribe"
sec_list "${IP_OK}/32"
rc=$(correr tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(updates)" == 0 ]] && ok "CERO llamadas de escritura a OCI" || bad "escribió $(updates) vez/veces"
salida | grep -q 'Sin cambios' && ok "lo dice explícito" || bad "$(salida)"

# ── 3. Falla cerrado: los servicios de eco no coinciden (proxy TLS) ──────────
escenario "proveedores discrepantes → aborta SIN escribir"
RESPUESTAS="$IP_OK $IP_PROXY $IP_OK"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "salió 0 con detección dudosa"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió con una IP dudosa"
salida | grep -q 'proxy' && ok "el mensaje nombra la causa probable" || bad "mensaje poco útil: $(salida)"

# ── 4. Falla cerrado: ningún proveedor contesta ─────────────────────────────
escenario "sin señal de ningún servicio de eco → aborta SIN escribir"
RESPUESTAS="- - - -"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "salió 0 sin saber la IP"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió sin saber la IP"

# ── 5. Falla cerrado: un solo proveedor contesta (sin corroboración) ────────
escenario "un solo voto → no alcanza, aborta SIN escribir"
RESPUESTAS="$IP_OK - - -"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0 con un solo voto" || bad "se conformó con una sola fuente"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió con una sola fuente"

# ── 6. Falla cerrado: el operador sale por IPv6 ─────────────────────────────
# El caso real de esta red: tiene IPv6 y los servicios de eco no contestan por
# IPv4. Una regla /128 no arreglaría nada (la VCN no tiene bloque IPv6 y la VM
# se alcanza por IP pública v4), así que lo correcto es abortar y decir por qué.
escenario "la red solo alcanza los servicios por IPv6 → aborta y lo explica"
RESPUESTAS="- - - -"
RESPUESTA_V6="2800:b20:1234::1"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "aceptó una IPv6"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió una regla IPv6 inútil"
salida | grep -qi 'ipv6' && ok "el mensaje explica que la VCN es IPv4 pura" || bad "no explica el problema: $(salida)"

escenario "un proveedor devuelve IPv6 o basura → no cuenta como voto"
RESPUESTAS="$IP_OK 2800:b20::1 <html>error</html> -"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "un voto válido no alcanza: aborta" || bad "tomó basura por votos"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió"

# ── 7. --cidr no puede ser la puerta trasera para reabrir el 22 ─────────────
escenario "--cidr 0.0.0.0/0 → se rechaza"
rc=$(correr tprueba --cidr 0.0.0.0/0)
[[ $rc != 0 ]] && ok "sale != 0" || bad "aceptó reabrir el 22 a internet"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "abrió el 22"

escenario "--cidr con máscara amplia (/16) → se rechaza: este script solo escribe /32"
rc=$(correr tprueba --cidr 179.5.0.0/16)
[[ $rc != 0 ]] && ok "sale != 0" || bad "aceptó un /16"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió un /16"

escenario "--cidr basura → se rechaza"
rc=$(correr tprueba --cidr "no-es-una-ip")
[[ $rc != 0 ]] && ok "sale != 0" || bad "aceptó basura"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió basura"

escenario "--cidr válido → no consulta ningún servicio de eco"
rc=$(correr tprueba --cidr "${IP_OK}/32")
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(cat "$CONTADOR")" == 0 ]] && ok "no llamó a curl (la IP la dio el operador)" || bad "consultó igual"

# ── 8. Forma inesperada de la security list → no se asume nada ──────────────
escenario "dos reglas abren el 22 → aborta, revisión manual"
cat > "$SL_JSON" <<EOF
{"data":{"id":"${SL}","egress-security-rules":[],"ingress-security-rules":[
 {"protocol":"6","source":"0.0.0.0/0","tcp-options":{"destination-port-range":{"min":22,"max":22}}},
 {"protocol":"6","source":"1.2.3.4/32","tcp-options":{"destination-port-range":{"min":1,"max":1024}}}
]},"etag":"e"}
EOF
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "editó a ciegas una SL ambigua"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió sobre una SL ambigua"

escenario "ninguna regla abre el 22 → aborta (no inventa una)"
cat > "$SL_JSON" <<EOF
{"data":{"id":"${SL}","egress-security-rules":[],"ingress-security-rules":[
 {"protocol":"6","source":"1.2.3.4/32","tcp-options":{"destination-port-range":{"min":443,"max":443}}}
]},"etag":"e"}
EOF
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "agregó una regla que no existía"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió"

escenario "respuesta de OCI sin .data → aborta"
echo '{"algo":"otra cosa"}' > "$SL_JSON"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "siguió con una respuesta rota"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió"

# ── 9. Una regla que abre el 22 dentro de un rango amplio se detecta ────────
escenario "regla 1-1024 (incluye el 22) → la reconoce y la acota al /32"
cat > "$SL_JSON" <<EOF
{"data":{"id":"${SL}","egress-security-rules":[],"ingress-security-rules":[
 {"protocol":"6","source":"0.0.0.0/0","tcp-options":{"destination-port-range":{"min":1,"max":1024}}}
]},"etag":"e"}
EOF
rc=$(correr tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
ingress_escrito | grep -q "${IP_OK}/32" && ok "la acotó (no la ignoró por no ser min=max=22)" || bad "no la tocó: $(ingress_escrito)"

# ── 10. Verificación y rollback ─────────────────────────────────────────────
escenario "el 22 no responde tras escribir → revierte al CIDR anterior"
sec_list "1.2.3.4/32"
NC_RC=1
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0 (el operador se tiene que enterar)" || bad "salió 0 con el 22 caído"
[[ "$(updates)" == 2 ]] && ok "dos escrituras: la nueva y la vuelta atrás" || bad "$(updates) escrituras"
sed -n 's/.*--ingress-security-rules //p' "$LLAMADAS" | tail -1 | grep -q '1\.2\.3\.4/32' \
    && ok "la última escritura restaura 1.2.3.4/32" || bad "no revirtió"
salida | grep -q 'SSH_CLIENT' && ok "dice cómo averiguar la IP real" || bad "no orienta al operador"

escenario "--no-verify → escribe y no comprueba"
NC_RC=1
rc=$(correr tprueba --no-verify)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(updates)" == 1 ]] && ok "una sola escritura, sin rollback" || bad "$(updates) escrituras"

# ── 11. --dry-run ───────────────────────────────────────────────────────────
escenario "--dry-run → muestra el plan y no escribe"
rc=$(correr tprueba --dry-run)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(updates)" == 0 ]] && ok "CERO escrituras" || bad "escribió en dry-run"
salida | grep -q "${IP_OK}/32" && ok "dice qué habría escrito" || bad "no muestra el plan"

# ── 12. El update de OCI falla → la regla vieja sigue vigente ───────────────
escenario "OCI rechaza el update → sale != 0 y lo dice"
UPDATE_RC=1
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "ignoró el fallo de OCI"
salida | grep -q 'regla anterior sigue vigente' && ok "aclara que nada cambió" || bad "$(salida)"

# ── 13. Security list compartida (el caso emiliogonzalez / emiliogonzalez_e2) ─
escenario "SL compartida por dos tenants y sin terminal → aborta salvo --yes"
cp "$OCI_DIR/state/tprueba.json" "$OCI_DIR/state/otro.json"
rc=$(correr tprueba < /dev/null)
[[ $rc != 0 ]] && ok "no toca a ciegas una SL compartida" || bad "escribió sin confirmar"
[[ "$(updates)" == 0 ]] && ok "no escribió nada" || bad "escribió"
salida | grep -qi 'compartida' && ok "nombra a los tenants afectados" || bad "no avisa: $(salida)"

escenario "SL compartida + --yes → escribe, avisando a quiénes afecta"
cp "$OCI_DIR/state/tprueba.json" "$OCI_DIR/state/otro.json"
rc=$(correr tprueba --yes < /dev/null)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(updates)" == 1 ]] && ok "una escritura" || bad "$(updates) escrituras"
salida | grep -qi 'compartida' && ok "el aviso queda igual" || bad "escribió en silencio"

# ── 14. Sin OCID en el state → no adivina ───────────────────────────────────
escenario "state sin security_list_id → aborta con instrucciones"
echo '{"public_ip":"'"$IP_VM"'"}' > "$OCI_DIR/state/tprueba.json"
rc=$(correr tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "siguió sin saber a qué SL escribir"
salida | grep -q 'PERMISOS-IAM' && ok "apunta a dónde está documentado el OCID" || bad "no orienta"

# ── 15. El resolvedor que también usa el deploy ─────────────────────────────
# oci/lib/01-network.sh resuelve el CIDR con esta MISMA función. Si acá fallara,
# un `deploy.sh` escribiría la regla equivocada sobre una VM viva — y el paso
# siguiente del propio deploy entra por SSH. Se prueba la función directamente
# porque en el deploy no hay flag `--dry-run` que la aísle.
escenario "ssh_cidr_resolve: el mismo valor que verá 01-network.sh"
# Se captura en una variable en vez de encadenar `resolver ... | grep`: con
# `pipefail`, un rechazo (que sale != 0) haría fallar la tubería aunque grep
# hubiera encontrado el mensaje, y el test mentiría.
resolver() { PATH="$BIN:$PATH" bash -c '
    source "'"${RAIZ_REPO}"'/oci/lib/ssh-cidr.sh"
    ssh_cidr_resolve "$1"' _ "$1" 2>&1; }
dice() { local out; out=$(resolver "$1"); grep -qi "$2" <<<"$out"; }

[[ "$(resolver auto)" == "${IP_OK}/32" ]] && ok "auto → el /32 detectado" || bad "auto dio: $(resolver auto)"
echo 0 > "$CONTADOR"
[[ "$(resolver 1.2.3.4/32)" == "1.2.3.4/32" ]] && ok "un CIDR explícito pasa tal cual" || bad "no respetó el CIDR"
dice "2800:b20::1/128" 'no es un CIDR IPv4' && ok "un CIDR IPv6 se rechaza" || bad "aceptó IPv6"
dice "" 'vacío' && ok "vacío se rechaza" || bad "aceptó un valor vacío"
dice 0.0.0.0/0 'abierto a internet' && ok "0.0.0.0/0 se acepta pero grita (compatibilidad)" || bad "pasó en silencio"
[[ "$(resolver 0.0.0.0/0 | tail -1)" == "0.0.0.0/0" ]] && ok "...y no rompe a un tenant que lo tenga puesto" || bad "rompió la compatibilidad"
RESPUESTAS="- - - -"; echo 0 > "$CONTADOR"
dice auto 'no se escribe nada' && ok "auto sin detección MATA el deploy en vez de adivinar" || bad "el deploy habría seguido"

echo
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32mTodo OK\033[0m\n'
else
    printf '\033[0;31m%d verificación(es) fallaron\033[0m\n' "$fallos"
fi
exit $((fallos > 0))
