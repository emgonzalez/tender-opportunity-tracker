#!/usr/bin/env bash
# destroy.sh — Da de baja TODOS los recursos de OCI de un tenant.
#
# Uso: ./oci/destroy.sh <tenant> [--dry-run] [--with-backups]
#
# Lee los OCIDs de oci/state/<tenant>.json y borra en orden inverso al deploy:
#   compute → boot volume → ADB → object storage → budget → identity → red
#
# ── Por qué este script se reescribió (Fase 8) ────────────────────────────────
# La versión anterior tenía una sola línea que lo explica todo:
#
#     del() { ociq "$@" --force >/dev/null 2>&1 || true; }
#
# Errores al vacío, código de salida ignorado. Y al final, siempre,
# `rm -f "$STATE"` seguido de «✓ Tenant limpio». Si un borrado fallaba —permiso
# denegado, recurso con dependencias, red caída— el recurso quedaba vivo Y el
# único registro de su OCID se acababa de borrar. Eso no es un bug de borde: es
# la receta exacta para fabricar un huérfano, y ya se pagó una vez (el
# `delete_amalia_multicomputos.sh` que vivía en la raíz del repo, 246 líneas con
# OCIDs escritos a mano para limpiar lo que este script no supo limpiar).
#
# Las tres reglas que gobiernan ahora:
#   1. Un borrado que falla SE VE: se imprime el error de OCI y se acumula.
#   2. Si algo falló, el state NO se borra. Es el mapa para volver a intentar.
#      Volver a correr el script es seguro: lo ya borrado se saltea solo.
#   3. "No existe" no es un fallo. Es el resultado que se buscaba.
#
# ── Qué NO borra, a propósito ────────────────────────────────────────────────
#   · El bucket del modelo ONNX (`amalia-onnx-staging`): los nombres de bucket
#     son únicos por NAMESPACE, no por compartment, así que lo comparten todos
#     los tenants de la tenancy. Borrarlo al destruir uno le rompería la carga
#     del modelo al otro. Se avisa con el comando exacto para borrarlo a mano.
#   · El bucket de respaldos (`amalia-backups-<tenant>`), salvo --with-backups.
#     Ese bucket es el "deshacer" de esta operación: contiene el historial de la
#     app y el inventario de OCI. Borrar la infraestructura y su copia de
#     seguridad en el mismo comando es justo lo que no se quiere poder hacer sin
#     escribirlo aparte.
#
# CUIDADO: irreversible. Pide confirmación escribiendo el nombre del tenant.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Igual que oci/scripts/ssh-allow.sh: se puede apuntar a otro árbol con OCI_DIR.
# Es lo que permite que scripts/test_destroy.sh corra ESTE archivo contra un
# tenants.yaml y un state de mentira, con stubs del CLI, sin tocar nada real.
export OCI_DIR="${OCI_DIR:-$SCRIPT_DIR}"
source "${OCI_DIR}/lib/common.sh"

# ── Args ──────────────────────────────────────────────────────────────────────
TENANT=""
DRY_RUN=0
WITH_BACKUPS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)       DRY_RUN=1 ;;
        --with-backups)  WITH_BACKUPS=1 ;;
        -h|--help)
            sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*) log_die "Opción desconocida: $1" ;;
        *)  [[ -z "$TENANT" ]] || log_die "Un solo tenant por corrida"; TENANT=$1 ;;
    esac
    shift
done
[[ -n "$TENANT" ]] || { echo "Uso: $0 <tenant> [--dry-run] [--with-backups]"; exit 1; }

export OCI_PROFILE
OCI_PROFILE=$(tenant_get "$TENANT" profile)
STATE=$(state_file "$TENANT")
[[ -f "$STATE" ]] || log_die "No hay state para $TENANT — ¿desplegaste alguna vez?
     Si el state se perdió, el inventario puede estar respaldado:
       source ${OCI_DIR}/lib/common.sh && source ${OCI_DIR}/lib/07-backup.sh
       state_restore ${TENANT}
     Y si tampoco está ahí, buscá por etiqueta lo que haya quedado vivo:
       ${OCI_DIR}/scripts/find-orphans.sh ${TENANT}"

g() { state_get "$TENANT" "$1"; }

# ── Contabilidad de fallos ────────────────────────────────────────────────────
# `del` NUNCA devuelve != 0: registra y sigue. Así el script recorre la lista
# entera (borrar 8 de 9 cosas es mucho mejor que abortar en la 2ª) sin pelearse
# con `set -e`, y al final decide con FALLOS en la mano.
FALLOS=()
BORRADOS=0

# ¿El error de OCI significa "ya no existe"? Entonces no es un fallo: es el
# estado que buscábamos. Cubre el 404 del API y los mensajes del CLI.
_es_inexistente() {
    grep -qiE 'NotAuthorizedOrNotFound|does not exist|status: 404|ObjectNotFound|BucketNotFound|not found|no objects' "$1"
}

# del <descripción> <args de oci...>
del() {
    local desc=$1; shift
    if [[ $DRY_RUN -eq 1 ]]; then
        log_info "[dry-run] ${desc}: oci --profile ${OCI_PROFILE} $*"
        return 0
    fi
    local err; err=$(mktemp)
    if ociq "$@" --force >"$err" 2>&1; then
        log_ok "$desc"
        BORRADOS=$((BORRADOS + 1))
    elif _es_inexistente "$err"; then
        log_info "${desc}: ya no existía"
    else
        log_warn "FALLÓ — ${desc}"
        # El error de OCI, visible. Esta es LA línea que la versión anterior
        # mandaba a /dev/null y por la que se perdían recursos.
        sed 's/^/        /' "$err" | tail -6 >&2
        FALLOS+=("$desc")
    fi
    rm -f "$err"
    return 0
}

# Como `del` pero para comandos que no aceptan --force.
del_sin_force() {
    local desc=$1; shift
    if [[ $DRY_RUN -eq 1 ]]; then
        log_info "[dry-run] ${desc}: oci --profile ${OCI_PROFILE} $*"
        return 0
    fi
    local err; err=$(mktemp)
    if ociq "$@" >"$err" 2>&1; then
        log_ok "$desc"
        BORRADOS=$((BORRADOS + 1))
    elif _es_inexistente "$err"; then
        log_info "${desc}: ya no existía"
    else
        log_warn "FALLÓ — ${desc}"
        sed 's/^/        /' "$err" | tail -6 >&2
        FALLOS+=("$desc")
    fi
    rm -f "$err"
    return 0
}

# ── Inventario y confirmación ─────────────────────────────────────────────────
echo -e "${C_RED}${C_BOLD}DESTROY $TENANT${C_NC} — esto va a eliminar:"
jq -r 'to_entries | .[] | "  - \(.key): \(.value)"' "$STATE"

ADB_ID=$(g adb_ocid)

# ── Guarda: no borrar una ADB que otro tenant también reclama ────────────────
# Fase M (migración a A1): el tenant nuevo comparte la VCN de multicomputos_dev
# (network.share_from) y, mientras dura la migración en sombra, TAMBIÉN puede
# compartir esta misma ADB — a propósito, para no duplicar la memoria
# institucional. Si otro tenant tiene este mismo adb_ocid en su propio state,
# destruirla desde acá se la rompe en silencio: el otro tenant seguiría
# creyendo que existe, hasta el próximo ORA-12154. Se recorren los state files
# en vez de una lista hardcodeada de nombres, así cubre cualquier tenant que se
# agregue después, no solo los de hoy.
if [[ -n "$ADB_ID" ]]; then
    OTROS_CON_ESTA_ADB=()
    for f in "${OCI_DIR}"/state/*.json; do
        [[ -f "$f" ]] || continue
        otro="$(basename "$f" .json)"
        [[ "$otro" == "$TENANT" ]] && continue
        [[ "$(jq -r '.adb_ocid // ""' "$f" 2>/dev/null)" == "$ADB_ID" ]] && OTROS_CON_ESTA_ADB+=("$otro")
    done
    if [[ ${#OTROS_CON_ESTA_ADB[@]} -gt 0 ]]; then
        log_die "La ADB ${ADB_ID} también está en el state de: ${OTROS_CON_ESTA_ADB[*]}.
     Destruirla desde '${TENANT}' se la rompería a esos tenants sin que lo sepan.
     Destruí primero esos tenants, o si de verdad ya no la necesitan, quitales
     'adb_ocid' del state a mano antes de volver a correr esto."
    fi
fi

# ── Guarda simétrica: no borrar la red si otro tenant la sigue compartiendo ──
# El aviso de "destruí el padre solo cuando ya no haya hijos" hoy vive nada más
# como comentario en la sección 7, y solo se ve al destruir al HIJO. Nada
# impedía destruir al PADRE mientras el hijo seguía vivo: el hijo se queda con
# un vcn_id en su propio state que ya no existe en OCI, y recién se entera en
# el próximo deploy o destroy, con un error que no dice por qué.
MI_VCN=$(g vcn_id)
if [[ -n "$MI_VCN" ]]; then
    OTROS_COMPARTIENDO_MI_RED=()
    for f in "${OCI_DIR}"/state/*.json; do
        [[ -f "$f" ]] || continue
        otro="$(basename "$f" .json)"
        [[ "$otro" == "$TENANT" ]] && continue
        [[ "$(jq -r '.network_shared_from // ""' "$f" 2>/dev/null)" == "$TENANT" ]] && OTROS_COMPARTIENDO_MI_RED+=("$otro")
    done
    if [[ ${#OTROS_COMPARTIENDO_MI_RED[@]} -gt 0 ]]; then
        log_die "La red de '${TENANT}' (VCN ${MI_VCN}) todavía la usan: ${OTROS_COMPARTIENDO_MI_RED[*]}.
     Destruí primero esos tenants — si no, se quedan con un vcn_id que ya no
     existe en OCI y el próximo deploy o destroy de ellos falla sin avisar
     por qué."
    fi
fi

BACKUP_BUCKET=$(g backup_bucket)
[[ -n "$BACKUP_BUCKET" ]] || BACKUP_BUCKET="amalia-backups-${TENANT//_/-}"

echo ""
if [[ -n "$ADB_ID" ]]; then
    echo -e "${C_RED}${C_BOLD}  ⚠ DATOS:${C_NC} la ADB ${ADB_ID} se TERMINA. La memoria institucional"
    echo    "    (procesos, chunks, embeddings) se pierde y no hay export previo."
fi
if [[ $WITH_BACKUPS -eq 1 ]]; then
    echo -e "${C_RED}${C_BOLD}  ⚠ DATOS:${C_NC} --with-backups → también se borra el bucket ${BACKUP_BUCKET}"
    echo    "    con TODO el historial respaldado y el inventario. No queda deshacer."
else
    echo -e "${C_GREEN}  ✓${C_NC} El bucket de respaldos ${BACKUP_BUCKET} se CONSERVA (sin --with-backups)."
fi
echo ""

if [[ $DRY_RUN -eq 1 ]]; then
    log_warn "--dry-run: no se va a borrar nada. Se imprime el plan."
else
    read -rp "Escribí el nombre del tenant para confirmar: " confirm
    [[ "$confirm" == "$TENANT" ]] || { log_warn "Cancelado"; exit 0; }
fi

# ── 1. Compute ────────────────────────────────────────────────────────────────
if [[ -n "$(g instance_id)" ]]; then
    log_step "Terminando instancia..."
    del "Instancia terminada" compute instance terminate \
        --instance-id "$(g instance_id)" \
        --preserve-boot-volume false --wait-for-state TERMINATED
fi

# ── 2. Boot volume ────────────────────────────────────────────────────────────
# Con --preserve-boot-volume false el volumen se va con la instancia, así que
# acá casi siempre no queda nada que hacer. "Casi siempre" es el problema: si el
# terminate falló, si la instancia se terminó a mano desde la consola (que sí
# ofrece conservarlo), o si alguien lo desprendió, el volumen queda vivo
# consumiendo los 200 GB de Always Free y sin aparecer en ninguna lista de
# instancias. Es el huérfano silencioso por excelencia.
BOOT_VOL=$(g boot_volume_id)
if [[ -n "$BOOT_VOL" ]]; then
    log_step "Verificando boot volume..."
    if [[ $DRY_RUN -eq 1 ]]; then
        del "Boot volume eliminado" bv boot-volume delete --boot-volume-id "$BOOT_VOL"
    else
        BV_STATE=$(ociq bv boot-volume get --boot-volume-id "$BOOT_VOL" \
            --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "")
        case "$BV_STATE" in
            ""|TERMINATED|TERMINATING)
                log_info "Boot volume ya no existe (se fue con la instancia)" ;;
            *)
                del "Boot volume eliminado" bv boot-volume delete --boot-volume-id "$BOOT_VOL" ;;
        esac
    fi
fi

# ── 3. Autonomous Database ────────────────────────────────────────────────────
if [[ -n "$ADB_ID" ]]; then
    log_step "Terminando Autonomous Database (tarda varios minutos)..."
    # OJO: acá --wait-for-state espera estados de WORK REQUEST
    # (ACCEPTED|IN_PROGRESS|FAILED|SUCCEEDED), no del ciclo de vida del recurso.
    # Pasarle TERMINATED —como en el resto de los deletes de este archivo— hace
    # que el CLI rechace el comando antes de llamar a la API.
    del "ADB terminada" db autonomous-database delete \
        --autonomous-database-id "$ADB_ID" \
        --wait-for-state SUCCEEDED --max-wait-seconds 1800
fi

# ── 4. Object Storage: PARs y buckets ─────────────────────────────────────────
# Los PAR son URLs firmadas. Mientras no venzan, cualquiera que tenga la URL
# sigue pudiendo leer (y en el caso del de respaldo, escribir) el bucket. Que la
# VM haya muerto no los invalida — viven en el bucket, no en la VM.
NS=""
if [[ $DRY_RUN -eq 0 ]]; then
    NS=$(ociq os ns get --raw-output --query data 2>/dev/null || echo "")
    [[ -n "$NS" ]] || log_warn "No se pudo resolver el namespace de Object Storage — se saltean PARs y buckets"
else
    NS="<namespace>"
fi

# par_borrar <bucket> <nombre-del-par>
par_borrar() {
    local bucket=$1 par_name=$2 par_id
    [[ -n "$bucket" && -n "$par_name" ]] || return 0
    if [[ $DRY_RUN -eq 1 ]]; then
        log_info "[dry-run] PAR '${par_name}' de ${bucket}: list + delete"
        return 0
    fi
    par_id=$(ociq os preauth-request list -ns "$NS" -bn "$bucket" --all \
        --query "data[?name=='${par_name}'].id | [0]" --raw-output 2>/dev/null || echo "")
    if [[ -z "$par_id" || "$par_id" == "null" ]]; then
        log_info "PAR '${par_name}': ya no existía"
        return 0
    fi
    del "PAR '${par_name}' revocado" os preauth-request delete \
        -ns "$NS" -bn "$bucket" --par-id "$par_id"
}

if [[ -n "$NS" ]]; then
    log_step "Revocando Pre-Authenticated Requests..."
    # El del modelo ONNX vive en un bucket compartido que NO se borra: revocarlo
    # es la única forma de que la URL firmada deje de servir.
    par_borrar "$(g onnx_bucket)" "$(g onnx_par_name)"
    par_borrar "$BACKUP_BUCKET"   "$(g backup_par_name)"

    if [[ $WITH_BACKUPS -eq 1 ]]; then
        log_step "Vaciando y eliminando el bucket de respaldos..."
        # Un bucket no se borra con objetos adentro, y este tiene versionado:
        # hay que barrer también las versiones anteriores o el delete falla con
        # BucketNotEmpty sin decir por qué.
        del "Objetos borrados"  os object bulk-delete          -ns "$NS" -bn "$BACKUP_BUCKET"
        del "Versiones borradas" os object bulk-delete-versions -ns "$NS" -bn "$BACKUP_BUCKET"
        del "Bucket ${BACKUP_BUCKET} eliminado" os bucket delete -ns "$NS" -bn "$BACKUP_BUCKET"
    fi
fi

# ── 5. Budget + alerts + topic ────────────────────────────────────────────────
[[ -n "$(g budget_id)" ]] && { log_step "Eliminando budget..."; del "Budget eliminado" budgets budget budget delete --budget-id "$(g budget_id)"; }
[[ -n "$(g topic_id)"  ]] && { log_step "Eliminando topic...";  del "Topic eliminado"  ons topic delete --topic-id "$(g topic_id)"; }

# ── 6. Identity ───────────────────────────────────────────────────────────────
# Cuatro recursos, no dos: 03-budget.sh crea el par del watchdog y 05-adb.sh
# crea OTRO par para el acceso a la ADB. El segundo no lo miraba nadie.
[[ -n "$(g policy_id)"            ]] && { log_step "Eliminando policy del watchdog...";     del "Policy eliminada"            iam policy delete        --policy-id "$(g policy_id)"; }
[[ -n "$(g adb_policy_id)"        ]] && { log_step "Eliminando policy de la ADB...";        del "Policy de ADB eliminada"     iam policy delete        --policy-id "$(g adb_policy_id)"; }
[[ -n "$(g dynamic_group_id)"     ]] && { log_step "Eliminando dynamic group del watchdog..."; del "Dynamic Group eliminado"  iam dynamic-group delete --dynamic-group-id "$(g dynamic_group_id)"; }
[[ -n "$(g adb_dynamic_group_id)" ]] && { log_step "Eliminando dynamic group de la ADB...";  del "Dynamic Group de ADB eliminado" iam dynamic-group delete --dynamic-group-id "$(g adb_dynamic_group_id)"; }

# ── 6b. Servicio web: registro DNS e IP reservada ─────────────────────────────
# La IP reservada NO muere con la instancia: queda suelta y facturable. El
# registro A tampoco: apuntaría a una IP que ya no es nuestra.
if [[ -n "$(g web_hostname)" && -n "$(g web_dns_zone)" ]]; then
    log_step "Eliminando registro DNS..."
    del "Registro A $(g web_hostname) eliminado" dns record rrset delete \
        --zone-name-or-id "$(g web_dns_zone)" --scope GLOBAL --domain "$(g web_hostname)" --rtype A
fi
[[ -n "$(g reserved_ip_id)" ]] && { log_step "Liberando IP reservada..."; del "IP reservada liberada" network public-ip delete --public-ip-id "$(g reserved_ip_id)"; }

# ── 7. Network ────────────────────────────────────────────────────────────────
SHARED_FROM=$(g network_shared_from)
[[ -n "$(g subnet_id)" ]] && { log_step "Eliminando subnet..."; del "Subnet eliminada" network subnet delete --subnet-id "$(g subnet_id)" --wait-for-state TERMINATED; }

if [[ -n "$SHARED_FROM" ]]; then
    log_warn "VCN/IGW/Route Table/Security List son del tenant '$SHARED_FROM' — no se eliminan acá"
    log_info "Para borrarlos: ./oci/destroy.sh $SHARED_FROM (cuando ya no haya otros tenants usándolos)"
else
    if [[ -n "$(g route_table_id)" ]]; then
        log_step "Limpiando route rules..."
        # Update, no delete: es la route table DEFAULT de la VCN, se va con
        # ella. Vaciar las reglas es lo que permite borrar el IGW después.
        del "Route rules limpiadas" network route-table update \
            --rt-id "$(g route_table_id)" --route-rules '[]' --wait-for-state AVAILABLE
    fi
    [[ -n "$(g igw_id)" ]] && { log_step "Eliminando IGW..."; del "IGW eliminado" network internet-gateway delete --ig-id "$(g igw_id)" --wait-for-state TERMINATED; }
    [[ -n "$(g vcn_id)" ]] && { log_step "Eliminando VCN..."; del "VCN eliminada" network vcn delete --vcn-id "$(g vcn_id)" --wait-for-state TERMINATED; }
fi

# ── 8. Cierre ─────────────────────────────────────────────────────────────────
if [[ $DRY_RUN -eq 1 ]]; then
    echo ""
    log_ok "--dry-run terminado: no se tocó nada. El state sigue intacto."
    exit 0
fi

if [[ ${#FALLOS[@]} -gt 0 ]]; then
    echo ""
    echo -e "${C_RED}${C_BOLD}✗ $TENANT NO quedó limpio — ${#FALLOS[@]} borrado(s) fallaron:${C_NC}"
    for f in "${FALLOS[@]}"; do echo "    · $f"; done
    echo ""
    log_warn "El state NO se borró: ${STATE}"
    log_info "Es el único mapa que queda de esos OCIDs. Arreglá la causa (permisos,"
    log_info "dependencias, un recurso que todavía referencia a otro) y volvé a correr:"
    log_info "  $0 $TENANT"
    log_info "Lo ya borrado se saltea solo. Para ver qué sigue vivo:"
    log_info "  ${OCI_DIR}/scripts/find-orphans.sh $TENANT"
    exit 1
fi

# Solo acá —con CERO fallos— se puede tirar el mapa.
log_step "Limpiando state local..."
if [[ -n "$NS" && $WITH_BACKUPS -eq 0 ]]; then
    # El bucket sobrevivió: su copia de `state/<tenant>.json` describe recursos
    # que ya no existen y sería una trampa para el próximo `state_restore`. Las
    # copias fechadas de state/history/ quedan (sirven de registro de qué hubo).
    ociq os object delete -ns "$NS" -bn "$BACKUP_BUCKET" \
        --name "state/${TENANT}.json" --force >/dev/null 2>&1 \
        && log_info "Inventario vigente retirado del bucket (queda state/history/)" || true
fi
rm -f "$STATE" "${OCI_DIR}/state/${TENANT}_id_ed25519" "${OCI_DIR}/state/${TENANT}_id_ed25519.pub" \
      "${OCI_DIR}/state/${TENANT}_backup.par" "${OCI_DIR}/state/${TENANT}_onnx.par"
log_ok "State eliminado (${BORRADOS} recurso(s) dados de baja)"

echo ""
echo -e "${C_GREEN}${C_BOLD}✓ Tenant $TENANT limpio${C_NC}"
echo ""
log_info "Lo que sigue en pie a propósito:"
[[ $WITH_BACKUPS -eq 0 ]] && log_info "  · Bucket de respaldos ${BACKUP_BUCKET} (historial + inventario). Para borrarlo:"
[[ $WITH_BACKUPS -eq 0 ]] && log_info "      oci --profile ${OCI_PROFILE} os object bulk-delete -ns ${NS:-<ns>} -bn ${BACKUP_BUCKET} --force"
[[ $WITH_BACKUPS -eq 0 ]] && log_info "      oci --profile ${OCI_PROFILE} os bucket delete -ns ${NS:-<ns>} -bn ${BACKUP_BUCKET} --force"
log_info "  · Bucket amalia-onnx-staging (modelo ONNX, COMPARTIDO entre tenants)."
log_info "    Borralo SOLO si ningún otro tenant de esta tenancy va a cargar el modelo:"
log_info "      oci --profile ${OCI_PROFILE} os object bulk-delete -ns ${NS:-<ns>} -bn amalia-onnx-staging --force"
log_info "      oci --profile ${OCI_PROFILE} os bucket delete -ns ${NS:-<ns>} -bn amalia-onnx-staging --force"
log_info "Verificá que no haya quedado nada con etiqueta de AMALIA:"
log_info "  ${OCI_DIR}/scripts/find-orphans.sh $TENANT"
