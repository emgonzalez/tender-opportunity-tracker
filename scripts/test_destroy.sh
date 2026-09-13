#!/usr/bin/env bash
# test_destroy.sh — banco de pruebas de oci/destroy.sh
#
# Por qué existe: destroy.sh no se puede probar corriéndolo. Su único objeto de
# prueba real sería la infraestructura que uno no quiere perder, y su falla
# característica es silenciosa — borra el state file diciendo "tenant limpio"
# mientras un recurso sigue vivo y facturando. Esa es exactamente la falla que
# produjo `delete_amalia_multicomputos.sh`, 246 líneas de OCIDs escritos a mano
# para limpiar lo que el script no supo limpiar.
#
# Lo que se verifica es la MÁQUINA DE DECISIÓN, con un `oci` de mentira delante:
# qué borra, en qué orden, qué NO borra nunca, y —lo más importante— cuándo se
# permite borrar el state file. La regla que este banco existe para defender:
#
#     si algún borrado falló, el state NO se borra.
#
# La segunda mitad del banco cubre lo complementario: `state_backup` /
# `state_restore` de oci/lib/07-backup.sh. Es el mismo activo visto del otro
# lado — lo que respalda el inventario y lo que lo borra— y su falla también es
# silenciosa: subir una credencial a un bucket, o pisar un state bueno con uno
# viejo.
#
# Uso: bash scripts/test_destroy.sh
set -uo pipefail

RAIZ_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fallos=0
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[0;31m✗\033[0m %s\n' "$*"; fallos=$((fallos + 1)); }

# ── Stub de `oci` ────────────────────────────────────────────────────────────
# Registra cada invocación completa y decide su resultado según el escenario:
#   FALLAR_EN      — subcadena: esa llamada devuelve un error REAL de OCI (409)
#   INEXISTENTE_EN — subcadena: esa llamada devuelve el 404 de "ya no está"
# Las lecturas (ns get, boot-volume get, preauth-request list) contestan siempre,
# porque no son lo que se está probando.
crear_stubs() {
    mkdir -p "$BIN"
    cat > "$BIN/oci" <<'STUB'
#!/usr/bin/env bash
args="$*"
echo "$args" >> "$LLAMADAS"
case "$args" in
    *"os ns get"*)               echo "nstest"; exit 0 ;;
    *"bv boot-volume get"*)      echo "${BV_STATE_STUB:-AVAILABLE}"; exit 0 ;;
    *"os preauth-request list"*) echo "${PAR_ID_STUB:-ocid1.par.oc1..test}"; exit 0 ;;
    *"os bucket get"*)           exit "${BUCKET_EXISTE_RC:-0}" ;;
    *"os object-lifecycle-policy put"*) exit 0 ;;
    *"os object get"*)
        # Materializa el archivo que pidió --file, como hace el CLI real.
        prev=""; dest=""
        for a in "$@"; do [[ "$prev" == "--file" ]] && dest="$a"; prev="$a"; done
        [[ -n "$dest" ]] && echo '{"vcn_id":"ocid1.vcn.oc1..restaurada"}' > "$dest"
        exit 0 ;;
esac
if [[ -n "${INEXISTENTE_EN:-}" && "$args" == *"${INEXISTENTE_EN}"* ]]; then
    echo "ServiceError: {'status': 404, 'code': 'NotAuthorizedOrNotFound'}" >&2
    exit 1
fi
if [[ -n "${FALLAR_EN:-}" && "$args" == *"${FALLAR_EN}"* ]]; then
    echo "ServiceError: {'status': 409, 'code': 'Conflict', 'message': 'El recurso todavia tiene dependencias'}" >&2
    exit 1
fi
exit 0
STUB
    chmod +x "$BIN/oci"
}

# escenario <nombre> — árbol OCI_DIR temporal propio; no se lee ni se escribe
# NADA del repo real más allá de copiar los scripts que se prueban.
escenario() {
    RAIZ=$(mktemp -d)
    BIN="$RAIZ/bin"
    export OCI_DIR="$RAIZ/oci"
    export LLAMADAS="$RAIZ/llamadas.log"
    mkdir -p "$OCI_DIR/lib" "$OCI_DIR/scripts" "$OCI_DIR/state"
    cp "${RAIZ_REPO}/oci/lib/common.sh"   "$OCI_DIR/lib/"
    cp "${RAIZ_REPO}/oci/lib/07-backup.sh" "$OCI_DIR/lib/"
    cp "${RAIZ_REPO}/oci/destroy.sh"      "$OCI_DIR/"
    : > "$LLAMADAS"
    crear_stubs

    cat > "$OCI_DIR/tenants.yaml" <<'EOF'
tenants:
  tprueba:
    profile: PERFIL_DE_PRUEBA
    region: us-ashburn-1
    compartment_ocid: ocid1.compartment.oc1..prueba
    tags:
      project: tender-opportunity-tracker
      environment: prueba
EOF
    # Un state COMPLETO: todas las claves que algún lib/*.sh llega a escribir.
    # Si destroy.sh ignora una, este banco lo ve.
    cat > "$OCI_DIR/state/tprueba.json" <<'EOF'
{
  "instance_id": "ocid1.instance.oc1..inst",
  "boot_volume_id": "ocid1.bootvolume.oc1..boot",
  "vnic_id": "ocid1.vnic.oc1..vnic",
  "public_ip": "203.0.113.10",
  "availability_domain": "AD-1",
  "vcn_id": "ocid1.vcn.oc1..vcn",
  "igw_id": "ocid1.internetgateway.oc1..igw",
  "route_table_id": "ocid1.routetable.oc1..rt",
  "security_list_id": "ocid1.securitylist.oc1..sl",
  "subnet_id": "ocid1.subnet.oc1..subnet",
  "topic_id": "ocid1.onstopic.oc1..topic",
  "budget_id": "ocid1.budget.oc1..budget",
  "dynamic_group_id": "ocid1.dynamicgroup.oc1..dg",
  "policy_id": "ocid1.policy.oc1..pol",
  "adb_ocid": "ocid1.autonomousdatabase.oc1..adb",
  "adb_dynamic_group_id": "ocid1.dynamicgroup.oc1..adbdg",
  "adb_policy_id": "ocid1.policy.oc1..adbpol",
  "adb_dsn": "(description=(address=...))",
  "onnx_bucket": "amalia-onnx-staging",
  "onnx_par_name": "amalia-onnx-read-20260101000000",
  "backup_bucket": "amalia-backups-tprueba",
  "backup_par_name": "amalia-backup-rw-20260101000000"
}
EOF
    touch "$OCI_DIR/state/tprueba_id_ed25519" "$OCI_DIR/state/tprueba_id_ed25519.pub"

    unset FALLAR_EN INEXISTENTE_EN BV_STATE_STUB PAR_ID_STUB BUCKET_EXISTE_RC
    export FALLAR_EN="" INEXISTENTE_EN="" BV_STATE_STUB="AVAILABLE" \
           PAR_ID_STUB="ocid1.par.oc1..test" BUCKET_EXISTE_RC=0
    echo "▶ $1"
}

# correr [args...] — confirma escribiendo el nombre del tenant por stdin.
correr() {
    PATH="$BIN:$PATH" bash "$OCI_DIR/destroy.sh" tprueba "$@" <<< "tprueba" \
        > "$RAIZ/salida" 2>&1
    echo $?
}
correr_sin_confirmar() {
    PATH="$BIN:$PATH" bash "$OCI_DIR/destroy.sh" tprueba "$@" <<< "otra-cosa" \
        > "$RAIZ/salida" 2>&1
    echo $?
}
salida()    { cat "$RAIZ/salida"; }
llamadas()  { cat "$LLAMADAS"; }
# ¿Se intentó esta operación? (cuenta de llamadas que la contienen)
veces()     { grep -c -- "$1" "$LLAMADAS" || true; }
hay_state() { [[ -f "$OCI_DIR/state/tprueba.json" ]]; }

# ── 1. Camino feliz: borra todo y recién ahí tira el state ───────────────────
escenario "todo borra bien → se da de baja cada recurso y el state desaparece"
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
hay_state && bad "dejó el state con todo borrado" || ok "el state se borró"
salida | grep -q "Tenant tprueba limpio" && ok 'dice "limpio"' || bad "no lo dice"

# Los que la versión anterior ignoraba por completo.
[[ "$(veces 'autonomous-database delete')"  -ge 1 ]] && ok "termina la ADB"                || bad "no tocó la ADB"
[[ "$(veces 'boot-volume delete')"          -ge 1 ]] && ok "borra el boot volume"          || bad "no tocó el boot volume"
[[ "$(veces 'preauth-request delete')"      -ge 2 ]] && ok "revoca los DOS PAR"            || bad "revocó $(veces 'preauth-request delete') PAR"
[[ "$(veces 'policy delete')"               -ge 2 ]] && ok "borra las DOS policies"        || bad "borró $(veces 'policy delete') policies"
[[ "$(veces 'dynamic-group delete')"        -ge 2 ]] && ok "borra los DOS dynamic groups"  || bad "borró $(veces 'dynamic-group delete') dynamic groups"
# Y los que ya borraba, que deben seguir borrándose.
for r in "instance terminate" "budgets budget budget delete" "ons topic delete" \
         "subnet delete" "internet-gateway delete" "vcn delete"; do
    [[ "$(veces "$r")" -ge 1 ]] && ok "sigue borrando: $r" || bad "dejó de borrar: $r"
done

# ── 2. LA prueba: un borrado fallido NO borra el state ──────────────────────
# Es el bug que fabrica huérfanos: el recurso queda vivo y el único registro de
# su OCID se acaba de borrar.
escenario "falla el borrado de la VCN → el state SOBREVIVE y el script sale != 0"
export FALLAR_EN="vcn delete"
rc=$(correr)
[[ $rc != 0 ]] && ok "sale != 0" || bad "salió 0 con un borrado fallido"
hay_state && ok "EL STATE SIGUE AHÍ (es el mapa para reintentar)" || bad "BORRÓ EL STATE CON UN FALLO — es el bug de la Fase 8"
salida | grep -q "NO quedó limpio" && ok "lo dice sin ambigüedad" || bad "no avisa: $(salida)"
salida | grep -q "dependencias" && ok "muestra el error REAL de OCI (no lo traga)" || bad "se tragó el stderr de OCI"
salida | grep -q "VCN eliminada" && ok "nombra el recurso que falló" || bad "no dice cuál falló"

escenario "falla un borrado del MEDIO → sigue con los siguientes igual"
export FALLAR_EN="budgets budget budget delete"
rc=$(correr)
[[ $rc != 0 ]] && ok "sale != 0" || bad "salió 0"
[[ "$(veces 'vcn delete')" -ge 1 ]] && ok "llegó hasta la VCN pese al fallo previo" || bad "abortó en el primer fallo"
hay_state && ok "el state sobrevive" || bad "borró el state"

escenario "fallan VARIOS → los lista todos"
export FALLAR_EN="delete"
rc=$(correr)
[[ $rc != 0 ]] && ok "sale != 0" || bad "salió 0"
salida | grep -qE "[0-9]+ borrado\(s\) fallaron" && ok "cuenta cuántos fallaron" || bad "no los cuenta"
hay_state && ok "el state sobrevive" || bad "borró el state"

# ── 3. "Ya no existe" NO es un fallo ────────────────────────────────────────
# Sin esto, reintentar un destroy parcial sería imposible: la segunda corrida
# fallaría con 404 en todo lo que la primera ya borró.
escenario "todo devuelve 404 (ya borrado a mano) → se considera limpio"
export INEXISTENTE_EN="delete"
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — trató un 404 como fallo"
hay_state && bad "no borró el state" || ok "borra el state: no quedó nada vivo"
salida | grep -q "ya no existía" && ok "lo dice" || bad "no lo dice"

# ── 4. Los buckets: qué se borra y qué NO ───────────────────────────────────
escenario "sin --with-backups → el bucket de respaldos NO se toca"
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc"
[[ "$(veces 'bucket delete')" == 0 ]] && ok "no borró ningún bucket" || bad "borró un bucket sin pedírselo"
[[ "$(veces 'bulk-delete')" == 0 ]] && ok "no vació nada" || bad "vació un bucket sin pedírselo"
salida | grep -q "amalia-backups-tprueba" && ok "dice qué bucket quedó vivo y cómo borrarlo" || bad "no lo menciona"

escenario "--with-backups → vacía versiones y objetos, y recién ahí borra el bucket"
rc=$(correr --with-backups)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(veces 'bulk-delete -ns')" -ge 1 ]] && ok "borra los objetos"  || bad "no vació el bucket"
[[ "$(veces 'bulk-delete-versions')" -ge 1 ]] && ok "borra las versiones (si no, BucketNotEmpty)" || bad "olvidó las versiones"
[[ "$(veces 'bucket delete')" == 1 ]] && ok "borra UN bucket, uno solo" || bad "borró $(veces 'bucket delete') buckets"
llamadas | grep 'bucket delete' | grep -q 'amalia-backups-tprueba' && ok "y es el de respaldos" || bad "borró otro bucket"

escenario "el bucket del modelo ONNX es COMPARTIDO: no se borra ni con --with-backups"
rc=$(correr --with-backups)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc"
llamadas | grep 'bucket delete' | grep -q 'amalia-onnx-staging' \
    && bad "borró el bucket compartido: le rompe la carga del modelo a otro tenant" \
    || ok "no lo borró"
# Pero sí revoca su PAR: la URL firmada sigue sirviendo aunque la VM muera.
llamadas | grep -q 'preauth-request list.*amalia-onnx-staging' && ok "sí revoca el PAR del ONNX" || bad "dejó viva la URL firmada"

# ── 5. --dry-run no toca nada ───────────────────────────────────────────────
escenario "--dry-run → cero llamadas destructivas, state intacto, sin confirmación"
rc=$(PATH="$BIN:$PATH" bash "$OCI_DIR/destroy.sh" tprueba --dry-run </dev/null > "$RAIZ/salida" 2>&1; echo $?)
[[ $rc == 0 ]] && ok "sale 0 sin que nadie confirme nada" || bad "rc=$rc — $(salida)"
[[ "$(veces 'delete')" == 0 && "$(veces 'terminate')" == 0 ]] && ok "no llamó a ningún borrado" || bad "llamó a $(veces 'delete') deletes"
hay_state && ok "el state intacto" || bad "borró el state en dry-run"
salida | grep -q "dry-run" && ok "muestra el plan" || bad "no muestra nada"

# ── 6. La confirmación tiene que servir de algo ─────────────────────────────
escenario "confirmación equivocada → no borra nada"
rc=$(correr_sin_confirmar)
[[ $rc == 0 ]] && ok "sale 0 (cancelar no es un error)" || bad "rc=$rc"
[[ "$(veces 'delete')" == 0 && "$(veces 'terminate')" == 0 ]] && ok "cero borrados" || bad "borró igual"
hay_state && ok "el state intacto" || bad "borró el state"

# ── 7. Lo que no está en el state no se intenta ─────────────────────────────
escenario "state mínimo (solo VCN) → no inventa llamadas para lo que no hay"
cat > "$OCI_DIR/state/tprueba.json" <<'EOF'
{"vcn_id": "ocid1.vcn.oc1..vcn"}
EOF
rc=$(correr)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
[[ "$(veces 'instance terminate')" == 0 ]] && ok "no intenta terminar una instancia que no existe" || bad "inventó un terminate"
[[ "$(veces 'autonomous-database')" == 0 ]] && ok "no intenta borrar una ADB que no hay" || bad "inventó un delete de ADB"
[[ "$(veces 'vcn delete')" == 1 ]] && ok "borra lo único que había" || bad "no borró la VCN"

# ── 8. Sin state no se adivina: se manda a buscar por etiqueta ──────────────
escenario "no hay state → aborta y dice cómo recuperarlo o cómo buscar huérfanos"
rm -f "$OCI_DIR/state/tprueba.json"
rc=$(correr)
[[ $rc != 0 ]] && ok "sale != 0" || bad "siguió sin saber qué borrar"
salida | grep -q "state_restore" && ok "ofrece restaurar el inventario respaldado" || bad "no lo ofrece"
salida | grep -q "find-orphans" && ok "ofrece la búsqueda por etiqueta" || bad "no la ofrece"

# ════════════════════════════════════════════════════════════════════════════
# Segunda mitad: el respaldo del inventario (oci/lib/07-backup.sh)
# ════════════════════════════════════════════════════════════════════════════

# Corre una función de 07-backup.sh con los stubs delante.
correr_funcion() {
    PATH="$BIN:$PATH" OCI_DIR="$OCI_DIR" OCI_PROFILE="PERFIL_DE_PRUEBA" \
        bash -c 'source "$OCI_DIR/lib/common.sh"; source "$OCI_DIR/lib/07-backup.sh"; "$@"' \
        _ "$@" > "$RAIZ/salida" 2>&1
    echo $?
}
# El JSON que se le pasó al `object put` (el contenido del archivo temporal ya
# no existe cuando esto corre, así que se compara contra lo registrado).
objetos_subidos() { sed -n 's/.*--name \([^ ]*\).*/\1/p' "$LLAMADAS" | sort; }

# ── 9. Qué se sube, y con qué nombre ────────────────────────────────────────
escenario "state_backup → sube el vigente Y una copia fechada"
rc=$(correr_funcion state_backup tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
objetos_subidos | grep -qx "state/tprueba.json" && ok "sube state/tprueba.json (el vigente)" || bad "no subió el vigente: $(objetos_subidos)"
objetos_subidos | grep -q "state/history/tprueba-" && ok "sube una copia fechada en state/history/" || bad "no subió copia fechada"
[[ "$(veces 'os object put')" == 2 ]] && ok "dos subidas, ni una más" || bad "$(veces 'os object put') subidas"
llamadas | grep -q 'object-lifecycle-policy put' && ok "deja puesta la retención (si no, history/ crece sin techo)" || bad "no aplicó retención"

escenario "state_backup con el bucket sin crear → lo crea con versionado y etiquetas"
export BUCKET_EXISTE_RC=1
rc=$(correr_funcion state_backup tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
llamadas | grep 'bucket create' | grep -q 'versioning Enabled' && ok "versionado desde el nacimiento" || bad "creó el bucket sin versionado"
llamadas | grep 'bucket create' | grep -q 'freeform-tags' && ok "y etiquetado" || bad "creó un bucket sin etiquetas"

# ── 10. LA prueba del respaldo: no se sube una credencial ───────────────────
# El state se guarda en un bucket que la propia VM puede leer con su PAR. Una
# URL firmada ahí adentro sería una credencial publicada.
escenario "claves que parecen credenciales → NO se suben, y se avisa cuáles"
cat > "$OCI_DIR/state/tprueba.json" <<'EOF'
{
  "vcn_id": "ocid1.vcn.oc1..vcn",
  "backup_par_name": "amalia-backup-rw-20260101000000",
  "backup_par_expires": "2026-07-01T00:00:00Z",
  "onnx_par_url": "https://objectstorage.us-ashburn-1.oraclecloud.com/p/SECRETO/n/ns/b/bk/o/x",
  "adb_admin_password": "Tx-no-deberia-viajar-9!",
  "algun_token": "abc123",
  "url_firmada_con_otro_nombre": "https://objectstorage.us-ashburn-1.oraclecloud.com/p/OTRO/n/ns/b/bk/o/y"
}
EOF
rc=$(correr_funcion state_backup tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
salida | grep -q "onnx_par_url" && ok "declara que omitió onnx_par_url" || bad "no dijo nada de onnx_par_url"
salida | grep -q "adb_admin_password" && ok "…y la password de la ADB" || bad "no filtró la password"
salida | grep -q "algun_token" && ok "…y la clave con 'token'" || bad "no filtró el token"
salida | grep -q "url_firmada_con_otro_nombre" \
    && ok "…y una URL firmada aunque la clave no se llame '_par_url'" \
    || bad "solo mira el nombre de la clave, no el valor"
salida | grep -q "backup_par_name" && bad "descartó backup_par_name, que NO es un secreto y hace falta para revocar el PAR" || ok "conserva backup_par_name (no es secreto)"
# 3 claves quedan: vcn_id, backup_par_name, backup_par_expires
salida | grep -q "3 claves" && ok "sube exactamente las 3 claves inocuas" || bad "subió otra cantidad: $(salida | grep claves)"

escenario "state file ilegible → no se sube nada a medias"
echo 'esto no es json' > "$OCI_DIR/state/tprueba.json"
rc=$(correr_funcion state_backup tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "dio por bueno un JSON roto"
[[ "$(veces 'os object put')" == 0 ]] && ok "cero subidas" || bad "subió basura"

escenario "falla la subida → avisa y sale != 0 (no miente con un ✓)"
export FALLAR_EN="os object put"
rc=$(correr_funcion state_backup tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "salió 0 sin haber subido nada"
salida | grep -qi "SIN respaldar" && ok "lo dice" || bad "no avisa: $(salida)"

escenario "sin state local → no inventa un respaldo"
rm -f "$OCI_DIR/state/tprueba.json"
rc=$(correr_funcion state_backup tprueba)
[[ "$(veces 'os object put')" == 0 ]] && ok "no sube nada" || bad "subió algo sin state"

# ── 11. Restaurar: no pisar un inventario bueno por accidente ───────────────
# Dos inventarios distintos del mismo tenant es el escenario que produce
# recursos duplicados, porque deploy.sh adopta por OCID o crea.
escenario "state_restore con un state existente → se niega"
rc=$(correr_funcion state_restore tprueba)
[[ $rc != 0 ]] && ok "sale != 0" || bad "pisó el state sin preguntar"
[[ "$(veces 'os object get')" == 0 ]] && ok "ni siquiera lo bajó" || bad "lo bajó igual"
grep -q 'ocid1.vcn.oc1..vcn' "$OCI_DIR/state/tprueba.json" && ok "el state local quedó intacto" || bad "lo modificó"
salida | grep -q "STATE_RESTORE_FORCE" && ok "dice cómo forzarlo si es lo que se quiere" || bad "no lo dice"

escenario "state_restore sin state local → lo trae"
rm -f "$OCI_DIR/state/tprueba.json"
rc=$(correr_funcion state_restore tprueba)
[[ $rc == 0 ]] && ok "sale 0" || bad "rc=$rc — $(salida)"
grep -q 'ocid1.vcn.oc1..restaurada' "$OCI_DIR/state/tprueba.json" && ok "el inventario quedó en oci/state/" || bad "no escribió el archivo"
salida | grep -qi "llave SSH" && ok "avisa que la llave SSH NO viene en el respaldo" || bad "no lo aclara: se puede destruir, no entrar"

# ── Cierre ──────────────────────────────────────────────────────────────────
echo ""
if [[ $fallos -eq 0 ]]; then
    printf '\033[0;32m✓ banco de destroy.sh: todo verde\033[0m\n'
else
    printf '\033[0;31m✗ banco de destroy.sh: %d fallo(s)\033[0m\n' "$fallos"
fi
exit $(( fallos > 0 ? 1 : 0 ))
