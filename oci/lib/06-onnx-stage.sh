#!/usr/bin/env bash
# 06-onnx-stage.sh — Deja el modelo ONNX accesible para la ADB
#
# Una Autonomous Database serverless no expone filesystem: la única vía soportada
# para meterle un archivo es que ella misma lo baje por HTTPS. De ahí este paso:
# dejar el .onnx en un bucket del propio tenant y generar un PAR (Pre-Authenticated
# Request), que es una URL firmada. Después
# `DBMS_CLOUD.GET_OBJECT(credential_name => NULL, ...)` la usa para copiarlo a
# DATA_PUMP_DIR — sin credencial, porque el PAR ya autentica.
#
# ── De dónde sale el modelo ───────────────────────────────────────────────────
# El bucket del propio tenant es la fuente durable. La cadena de resolución baja
# de "ya está donde tiene que estar" a "hay que traerlo de afuera", y se detiene
# en el primer paso que da resultado:
#
#   1. El objeto ya está en el bucket destino          → no se descarga nada
#   2. Hay caché local en oci/state/                   → se sube
#   3. ONNX_SOURCE_URL apunta a una URL HTTPS          → se baja y se sube
#   4. ONNX_SOURCE_BUCKET=<ns>/<bucket> en OTRO tenant → se copia y se sube
#
# El paso 1 es el que hace que esto sea barato en el uso normal y el que permite
# que un tenant nuevo (AMALIA_PROD) arranque sin traer nada: los nombres de bucket
# son únicos por namespace, así que todos los compartments de la tenancy ven el
# mismo `amalia-onnx-staging`.
#
# NO hay una fuente por defecto en internet a propósito. `db/load_onnx.sql`
# documenta el origen de Oracle con la URL recortada —el token del PAR no está en
# el repo— y poner una inventada daría un 404 que parecería un problema de
# permisos. Si hace falta un arranque en frío, ver "Arranque en frío" abajo.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

ONNX_OBJETO="multilingual_e5_small.onnx"
ONNX_BUCKET="amalia-onnx-staging"
ONNX_BYTES_MIN=100000000   # ~117 MB reales; por debajo de esto la copia está trunca

_onnx_cache_ok() {
    local f=$1
    # GNU primero: en macOS `stat -c` falla sin ensuciar stdout, mientras que en
    # Linux `stat -f` imprime datos del filesystem ANTES de fallar y la
    # sustitución de comandos concatenaría esa salida con la del fallback.
    [[ -f "$f" ]] && (( $(stat -c%s "$f" 2>/dev/null || stat -f%z "$f") > ONNX_BYTES_MIN ))
}

_onnx_die_sin_fuente() {
    log_die "No hay de dónde sacar ${ONNX_OBJETO}.

     Arranque en frío — elegí una:

     a) Si ya lo tenés en disco:
          cp <ruta>/${ONNX_OBJETO} ${OCI_DIR}/state/${ONNX_OBJETO}

     b) Desde otro bucket de OCI al que tengas acceso con este mismo perfil:
          ONNX_SOURCE_BUCKET=<namespace>/<bucket> <comando>

     c) Bajándolo de Oracle (modelo 'multilingual_e5_small_augmented', OML
        pretrained models). La URL con su PAR NO está en el repo; se saca de la
        documentación de Oracle y se pasa así:
          ONNX_SOURCE_URL='https://...' <comando>"
}

onnx_stage() {
    local tenant=$1
    local compartment ns cache par_name par_uri expira
    compartment=$(tenant_get "$tenant" compartment_ocid)
    ns=$(ociq os ns get --raw-output --query data)
    cache="${OCI_DIR}/state/${ONNX_OBJETO}"

    # ── Bucket destino ────────────────────────────────────────────────────────
    if ociq os bucket get -ns "$ns" -bn "$ONNX_BUCKET" &>/dev/null; then
        log_ok "Bucket '$ONNX_BUCKET' ya existe"
    else
        log_step "Creando bucket '$ONNX_BUCKET'..."
        ociq os bucket create -ns "$ns" --name "$ONNX_BUCKET" \
            --compartment-id "$compartment" >/dev/null \
            || log_die "No se pudo crear el bucket (¿falta object-family en el compartment?)"
        log_ok "Bucket creado"
    fi

    # ── 1. ¿Ya está subido? ───────────────────────────────────────────────────
    if ociq os object head -ns "$ns" -bn "$ONNX_BUCKET" --name "$ONNX_OBJETO" &>/dev/null; then
        log_ok "El modelo ya está en el bucket del tenant — no se descarga nada"
    else
        # ── 2/3/4. Conseguir una copia local y subirla ────────────────────────
        if _onnx_cache_ok "$cache"; then
            log_ok "Usando caché local ($(( $(stat -c%s "$cache" 2>/dev/null || stat -f%z "$cache") / 1024 / 1024 )) MB)"
        elif [[ -n "${ONNX_SOURCE_URL:-}" ]]; then
            log_step "Bajando el modelo de ONNX_SOURCE_URL..."
            curl -fsSL "$ONNX_SOURCE_URL" -o "$cache" || log_die "Falló la descarga desde ONNX_SOURCE_URL"
            _onnx_cache_ok "$cache" || log_die "Lo descargado es más chico de lo esperado — ¿la URL devolvió un error en vez del archivo?"
            log_ok "Modelo descargado"
        elif [[ -n "${ONNX_SOURCE_BUCKET:-}" ]]; then
            local src_ns="${ONNX_SOURCE_BUCKET%%/*}" src_bn="${ONNX_SOURCE_BUCKET#*/}"
            log_step "Copiando desde ${src_bn} (namespace ${src_ns})..."
            ociq os object get -ns "$src_ns" -bn "$src_bn" \
                --name "$ONNX_OBJETO" --file "$cache" >/dev/null \
                || log_die "No se pudo leer ${ONNX_OBJETO} de ${ONNX_SOURCE_BUCKET}"
            _onnx_cache_ok "$cache" || log_die "La copia quedó trunca"
            log_ok "Modelo copiado"
        else
            _onnx_die_sin_fuente
        fi

        log_step "Subiendo el modelo al bucket del tenant (117 MB)..."
        ociq os object put -ns "$ns" -bn "$ONNX_BUCKET" \
            --file "$cache" --name "$ONNX_OBJETO" --no-overwrite >/dev/null \
            || log_die "Falló la subida"
        log_ok "Modelo subido"
    fi

    # ── PAR de lectura ────────────────────────────────────────────────────────
    # Vive poco a propósito: solo tiene que sobrevivir a la carga del modelo. Una
    # vez dentro de la BD el archivo ya no se necesita, y una URL firmada de larga
    # vida sería un secreto más que cuidar sin ninguna ganancia.
    par_name="amalia-onnx-read-$(date -u +%Y%m%d%H%M%S)"
    expira=$(date -u -v+2d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+2 days' +%Y-%m-%dT%H:%M:%SZ)
    log_step "Generando PAR de lectura (expira $expira)..."
    par_uri=$(ociq os preauth-request create \
        -ns "$ns" -bn "$ONNX_BUCKET" --name "$par_name" \
        --access-type ObjectRead --object-name "$ONNX_OBJETO" \
        --time-expires "$expira" \
        --query 'data."access-uri"' --raw-output) \
        || log_die "No se pudo crear el PAR"

    local par_url="https://objectstorage.$(tenant_get "$tenant" region).oraclecloud.com${par_uri}"
    state_set "$tenant" onnx_par_url "$par_url"
    log_ok "PAR creado y guardado en el state file"
    echo "$par_url"
}
