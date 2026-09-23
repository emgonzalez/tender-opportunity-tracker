#!/usr/bin/env bash
# 04-app.sh — Despliega la app en la VM remota
#
# Asume que la VM ya está RUNNING y que cloud-init terminó (instaló docker).
# Pasos:
#   1. Espera a que SSH esté listo
#   2. rsync del repo (excluye .git, data/, secretos)
#   3. Copia el .env local
#   4. Obtiene la imagen: la baja de GHCR (modo pull) o la construye en la VM
#
# Modo pull vs build: lo decide el propio compose file, no una key de config.
# Si declara `build:`, la VM construye; si no, baja la imagen que CI publicó.
# Hoy eso significa: x86 (docker-compose.e2.yml) baja, ARM (docker-compose.oci.yml)
# construye, porque CI todavía no publica imagen ARM. Cuando la publique, alcanza
# con quitarle el `build:` al compose ARM.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

# ── Lectura de ~/.oci/config ──────────────────────────────────────────────────
# Uso: oci_config_get <perfil> <clave>   → el valor, o vacío si no está.
# Por qué a mano y no con el CLI: `oci setup` no expone "dame el `user` de este
# perfil" y el archivo es un INI trivial. Vacío no es un error acá — quien llama
# decide qué hacer con la ausencia, igual que `state_get`.
oci_config_get() {
    local perfil=$1 clave=$2 archivo="${OCI_CLI_CONFIG_FILE:-${HOME}/.oci/config}"
    [[ -f "$archivo" ]] || return 0
    awk -F= -v p="[${perfil}]" -v k="$clave" '
        /^[[:space:]]*\[/ {
            s = $0; gsub(/[[:space:]]/, "", s); dentro = (s == p); next
        }
        dentro {
            nombre = $1; gsub(/[[:space:]]/, "", nombre)
            if (nombre == k) {
                valor = substr($0, index($0, "=") + 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", valor)
                print valor; exit
            }
        }
    ' "$archivo"
}

app_deploy() {
    local tenant=$1
    local public_ip ssh_key compose_file
    public_ip=$(state_get "$tenant" public_ip)
    ssh_key="${OCI_DIR}/state/${tenant}_id_ed25519"
    compose_file=$(tenant_get_or "$tenant" docker.compose_file "docker-compose.oci.yml")
    [[ -n "$public_ip" ]] || log_die "public_ip no en state"
    [[ -f "$ssh_key" ]] || log_die "SSH key no encontrada en $ssh_key"
    [[ -f "${REPO_ROOT}/${compose_file}" ]] || log_die "Compose file '${compose_file}' no existe en el repo"

    # ── Modo de despliegue e imagen a desplegar ───────────────────────────────
    local deploy_mode=pull image_repo image_tag image_ref="" expected_sha=""
    local channel="" pinned=false
    if grep -qE '^[[:space:]]+build:' "${REPO_ROOT}/${compose_file}"; then
        deploy_mode=build
    fi

    if [[ "$deploy_mode" == pull ]]; then
        image_repo=$(tenant_get_or "$tenant" docker.image "ghcr.io/emgonzalez/tender-opportunity-tracker")
        # Por defecto se despliega el commit que está checkouteado. El tag lo
        # produce CI con `type=sha,format=long`, de ahí el prefijo `sha-`.
        # AMALIA_IMAGE_TAG permite forzar otro (rollback a un SHA anterior, o
        # una etiqueta móvil como `develop` para una prueba puntual).
        if [[ -n "${AMALIA_IMAGE_TAG:-}" ]]; then
            image_tag="$AMALIA_IMAGE_TAG"
        else
            image_tag="sha-$(git -C "$REPO_ROOT" rev-parse HEAD)"
        fi
        image_ref="${image_repo}:${image_tag}"
        # Solo se puede verificar el sello contra un valor esperado si la etiqueta
        # ES un SHA. Con una etiqueta móvil se reporta lo que vino, sin exigir.
        if [[ "$image_tag" =~ ^sha-([0-9a-f]{40})$ ]]; then
            expected_sha="${BASH_REMATCH[1]}"
        fi

        # La imagen sale de un commit, no del árbol de trabajo: lo que no esté
        # commiteado y empujado no viaja. Avisar es más útil que fallar, porque
        # a veces lo sucio es irrelevante (notas, .env).
        if [[ -z "${AMALIA_IMAGE_TAG:-}" ]] && ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
            log_warn "El árbol de trabajo tiene cambios sin commitear: NO están en la imagen ${image_tag}"
        fi

        # ── Canal de auto-actualización (opt-in por tenant) ────────────────────
        # Vacío = comportamiento histórico: la VM se queda en el SHA que se le
        # dejó y solo cambia cuando alguien vuelve a desplegar. Con un canal, la
        # VM se actualiza sola a lo que apunte esa etiqueta móvil (ver
        # oci/lib/amalia-pull-deploy.sh y oci/README.md).
        channel=$(tenant_get_or "$tenant" docker.channel "")
        # Un despliegue con AMALIA_IMAGE_TAG es deliberado —típicamente un
        # rollback— y el auto-update lo pisaría a la mañana siguiente. El clavo
        # dura hasta el próximo despliegue normal, que lo levanta solo.
        if [[ -n "${AMALIA_IMAGE_TAG:-}" ]]; then
            pinned=true
        fi
    fi

    local ssh_opts=(-i "$ssh_key" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${OCI_DIR}/state/known_hosts" -o LogLevel=ERROR)
    local remote="ubuntu@${public_ip}"
    local app_dir=/opt/tender-opportunity-tracker

    # ── Espera SSH ────────────────────────────────────────────────────────────
    log_step "Esperando que SSH esté listo en $public_ip..."
    local i=0
    until ssh "${ssh_opts[@]}" -o ConnectTimeout=5 "$remote" "echo ok" &>/dev/null; do
        ((i++))
        ((i > 60)) && log_die "SSH no respondió en 5 minutos"
        sleep 5
    done
    log_ok "SSH conectado"

    # ── Espera cloud-init ─────────────────────────────────────────────────────
    log_step "Esperando que cloud-init termine (puede tardar 2-3 min)..."
    ssh "${ssh_opts[@]}" "$remote" "cloud-init status --wait" >/dev/null \
        || log_die "cloud-init falló — revisá /var/log/cloud-init-output.log en la VM"
    log_ok "cloud-init completo"

    # ── Congelar el scheduler durante el deploy ───────────────────────────────
    # Sin esto, una corrida puede dispararse a mitad del despliegue y ejecutarse
    # con código a medio sincronizar o con el .env ANTERIOR (el contenedor toma
    # las variables al arrancar). Eso ya provocó un envío con la política de
    # correo vieja tras arrancar una VM detenida. Se reactiva al final.
    log_step "Deteniendo timer durante el despliegue..."
    ssh "${ssh_opts[@]}" "$remote" \
        "sudo systemctl stop amalia.timer amalia-scrape.timer amalia-pull-deploy.timer 2>/dev/null; \
         sudo systemctl stop amalia.service amalia-scrape.service amalia-analyze.service amalia-pull-deploy.service 2>/dev/null; true" \
        >/dev/null 2>&1 || true
    log_ok "Timers detenidos (se reactivan al terminar)"

    # ── Sync del repo ─────────────────────────────────────────────────────────
    log_step "Sincronizando código a la VM..."
    rsync -az --delete \
        --exclude '.git' \
        --exclude 'data' \
        --exclude '.env' \
        --exclude 'oci/state' \
        --exclude '__pycache__' \
        --exclude '.pytest_cache' \
        -e "ssh ${ssh_opts[*]}" \
        "${REPO_ROOT}/" "${remote}:${app_dir}/" >/dev/null
    log_ok "Código sincronizado"

    # ── Permisos de data/ ─────────────────────────────────────────────────────
    # `data/` está excluido del rsync (es estado de runtime), pero sus permisos
    # SÍ son responsabilidad del despliegue, y hay dos escritores sobre el mismo
    # directorio: el contenedor —que corre como `botuser`, uid 997— y
    # `amalia-pull-deploy.service`, que corre como `ubuntu` (uid 1000) y escribe
    # `pull-deploy.log`. Un archivo 644 creado por uno deja al otro afuera.
    #
    # Por qué acá y no en el Dockerfile. `Dockerfile:53-55` hace `chmod -R 777
    # data` con el comentario "needed for volume mounts", y eso NO alcanza: el
    # bind mount REEMPLAZA /app/data por el directorio del host, así que los
    # permisos horneados en la imagen nunca se aplican. Es un arreglo que parece
    # estar puesto y no hace nada.
    #
    # Incidente que lo motiva (2026-09-14 → 2026-09-19, tres días de corridas
    # fallidas en multicomputos_dev): al recrear la instancia, `last_run.json` y
    # `storage_state.json` quedaron 644 de `ubuntu`. El directorio era 777, así
    # que el contenedor podía CREAR archivos nuevos pero no SOBRESCRIBIR esos
    # dos. Consecuencias encadenadas: no podía guardar la sesión de COMPRASAL
    # —así que el login "fallaba" todos los días sin poder recuperarse—, el
    # volcado de depuración reventaba ocultando el motivo real, y `_save_last_run`
    # tiraba PermissionError dejando al servicio en exit 1 hasta que systemd
    # agotó su StartLimitBurst y dejó de intentar.
    log_step "Asegurando permisos de data/..."
    ssh "${ssh_opts[@]}" "$remote" \
        "sudo mkdir -p ${app_dir}/data && sudo chmod -R a+rwX ${app_dir}/data" \
        || log_die "No se pudieron ajustar los permisos de ${app_dir}/data"
    log_ok "data/ escribible por el contenedor (uid 997) y por el host"

    # ── .env ──────────────────────────────────────────────────────────────────
    # La política de correo la decide el TENANT DESTINO, no el .env local: se
    # reescribe NOTIFICATION_OVERRIDE_TO con el valor de tenants.yaml antes de
    # copiar. Así, desplegar a un tenant de validación redirige todo el correo a
    # una casilla de pruebas, y desplegar a producción restaura los destinatarios
    # reales — sin tocar nada a mano y sin que un override local se filtre a prod.
    log_step "Copiando .env..."
    [[ -f "${REPO_ROOT}/.env" ]] || log_die "Falta ${REPO_ROOT}/.env"

    local env_tmp override_to
    env_tmp=$(mktemp "${TMPDIR:-/tmp}/amalia-env.XXXXXX") || log_die "No se pudo crear temporal"
    chmod 600 "$env_tmp"
    # shellcheck disable=SC2064  # se expande ahora a propósito
    trap "rm -f '$env_tmp'" RETURN

    # Variables gobernadas por el tenant: <VAR_ENTORNO> <ruta.en.tenants.yaml> <default>
    local -a tenant_env=(
        "NOTIFICATION_OVERRIDE_TO notification.override_to ''"
        "ENABLE_DRILLDOWN         features.enable_drilldown false"
        "ENABLE_MEMORIA           features.enable_memoria   false"
        "LLM_PROVIDER             features.llm_provider     anthropic"
        # Perilla ortogonal a la anterior: qué familia de modelos se usa cuando
        # el proveedor es `oci`. Default `nativo` — la única que mantiene los
        # pliegos dentro de OCI (ver tenants.yaml y PERMISOS-IAM.md § GenAI).
        "OCI_FAMILIA              features.oci_familia      nativo"
    )

    # Se eliminan del .env local y se reescriben con el valor del tenant, así el
    # destino del despliegue manda siempre y un residuo local no puede filtrarse.
    cp "${REPO_ROOT}/.env" "$env_tmp"
    local entry var path def val
    for entry in "${tenant_env[@]}"; do
        read -r var path def <<<"$entry"
        [[ "$def" == "''" ]] && def=""
        val=$(tenant_get_or "$tenant" "$path" "$def")
        # normalizar booleanos de YAML (True/False de Python) a minúsculas
        case "$val" in True) val=true ;; False) val=false ;; esac
        grep -v "^[[:space:]]*${var}[[:space:]]*=" "$env_tmp" > "${env_tmp}.n" && mv "${env_tmp}.n" "$env_tmp"
        printf '%s=%s\n' "$var" "$val" >> "$env_tmp"
        [[ "$var" == "NOTIFICATION_OVERRIDE_TO" ]] && override_to="$val"
    done
    # ── Credenciales de la ADB ────────────────────────────────────────────────
    # Salen del state del tenant y de su archivo de contraseña, NO del .env local.
    # La ADB pertenece al tenant: si estas variables vivieran en el .env de la
    # laptop, desplegar otro tenant le copiaría la base del anterior — el mismo
    # problema que NOTIFICATION_OVERRIDE_TO ya resuelve para el correo.
    local adb_dsn adb_pw_file
    adb_dsn=$(state_get "$tenant" adb_dsn)
    adb_pw_file="${OCI_DIR}/state/${tenant}_adb_amalia.pw"
    if [[ -n "$adb_dsn" && -f "$adb_pw_file" ]]; then
        local var
        for var in ADB_USER ADB_PASSWORD ADB_DSN; do
            grep -v "^[[:space:]]*${var}[[:space:]]*=" "$env_tmp" > "${env_tmp}.n" && mv "${env_tmp}.n" "$env_tmp"
        done
        printf 'ADB_USER=%s\n' "AMALIA" >> "$env_tmp"
        printf 'ADB_PASSWORD=%s\n' "$(cat "$adb_pw_file")" >> "$env_tmp"
        printf 'ADB_DSN=%s\n' "$adb_dsn" >> "$env_tmp"
        log_ok "Credenciales de ADB inyectadas desde el state del tenant"
    elif [[ "$(tenant_get_or "$tenant" features.enable_memoria false)" == "True" ]]; then
        log_warn "ENABLE_MEMORIA=true pero no hay adb_dsn en el state ni ${tenant}_adb_amalia.pw"
        log_warn "  La corrida va a fallar al usar la memoria — corré adb_setup y scripts/adb_bootstrap.py"
    fi

    # ── Credenciales de OCI Generative AI ─────────────────────────────────────
    # Espejo del bloque de la ADB de arriba, y por las mismas razones: la
    # credencial pertenece al TENANT, así que sale de oci/state/ y del perfil del
    # tenant, nunca del .env de la laptop.
    #
    # Por qué van en el .env y no en un archivo montado en /etc/amalia/oci/: el
    # uid no es el mismo en las dos imágenes. `botuser` es usuario de SISTEMA en
    # Dockerfile:9 (`useradd -r` → uid < 1000) y usuario normal en
    # Dockerfile.oci:27 (`useradd -m` → uid 1000); un archivo 0600 con dueño fijo
    # sería legible en una imagen y no en la otra — un bug latente por variante de
    # imagen. El .env, en cambio, ya se monta entero con `env_file:`, ya se
    # escribe con chmod 600 y ya transporta ADB_PASSWORD. El comentario del token
    # de GHCR más abajo (*"a propósito NO va en el .env"*) no aplica: AMALIA no
    # tiene razón para leer el token del registro, pero sí para leer su propia
    # credencial de inferencia.
    local llm_provider genai_key_file genai_region genai_compartment
    llm_provider=$(tenant_get_or "$tenant" features.llm_provider anthropic)
    genai_key_file="${OCI_DIR}/state/${tenant}_genai.pem"
    # ⚠️ NO es la región del tenant. GenAI no sirve inferencia en us-ashburn-1
    # —donde viven las VMs—: devuelve `404 Entity with key <modelo> not found`,
    # que se lee como "ID de modelo mal escrito" y manda el diagnóstico a
    # cualquier lado. La inferencia sale cross-region por HTTPS público.
    genai_region=us-chicago-1
    # El mismo compartment del tenant: es donde se verificó el permiso
    # `use generative-ai-family` y donde se factura el consumo.
    genai_compartment=$(tenant_get_or "$tenant" compartment_ocid "")
    # Se borran SIEMPRE, aunque este tenant no use OCI — mismo criterio que
    # AMALIA_CHANNEL más abajo, y acá pesa más: OCI_KEY_CONTENT es una clave
    # privada, y un residuo en el .env de la laptop la mandaría a una VM que no
    # tiene ninguna razón para tenerla.
    local genai_var
    for genai_var in OCI_USER_OCID OCI_TENANCY_OCID OCI_FINGERPRINT OCI_REGION \
                     OCI_KEY_CONTENT OCI_GENAI_COMPARTMENT_OCID; do
        grep -v "^[[:space:]]*${genai_var}[[:space:]]*=" "$env_tmp" > "${env_tmp}.n" && mv "${env_tmp}.n" "$env_tmp"
    done
    if [[ "$llm_provider" == "oci" && -f "$genai_key_file" ]]; then
        local genai_user genai_fingerprint genai_tenancy genai_key
        genai_user=$(oci_config_get "${OCI_PROFILE:-DEFAULT}" user)
        genai_fingerprint=$(oci_config_get "${OCI_PROFILE:-DEFAULT}" fingerprint)
        genai_tenancy=$(oci_config_get "${OCI_PROFILE:-DEFAULT}" tenancy)
        [[ -n "$genai_tenancy" ]] || genai_tenancy=$(tenant_get_or "$tenant" tenancy_ocid "")
        # El PEM es multilínea y el .env es una variable por línea: se escapan
        # los saltos como la secuencia literal \n. Se deja SIN COMILLAS a
        # propósito — así el valor llega a Python byte por byte, sin depender de
        # si esta versión de python-dotenv o de docker compose decide
        # interpretar escapes dentro de comillas. Quien consume (el adaptador de
        # OCI) deshace el escape con un replace y se lo pasa al SDK en
        # `private_key_content`, que espera saltos de línea de verdad. Un PEM no
        # contiene `$` ni `#`, así que la interpolación del compose no lo toca.
        genai_key=$(awk 'BEGIN { ORS = "" } { print $0 "\\n" }' "$genai_key_file")
        printf 'OCI_USER_OCID=%s\n'              "$genai_user" >> "$env_tmp"
        printf 'OCI_TENANCY_OCID=%s\n'           "$genai_tenancy" >> "$env_tmp"
        printf 'OCI_FINGERPRINT=%s\n'            "$genai_fingerprint" >> "$env_tmp"
        printf 'OCI_REGION=%s\n'                 "$genai_region" >> "$env_tmp"
        printf 'OCI_KEY_CONTENT=%s\n'            "$genai_key" >> "$env_tmp"
        printf 'OCI_GENAI_COMPARTMENT_OCID=%s\n' "$genai_compartment" >> "$env_tmp"
        log_ok "Credenciales de OCI GenAI inyectadas (perfil ${OCI_PROFILE:-DEFAULT}, región ${genai_region})"
        [[ -n "$genai_user" && -n "$genai_fingerprint" ]] \
            || log_warn "  El perfil '${OCI_PROFILE:-DEFAULT}' no tiene 'user' y/o 'fingerprint' en ~/.oci/config — la firma va a fallar"
    elif [[ "$llm_provider" == "oci" ]]; then
        log_warn "llm_provider=oci pero falta ${genai_key_file}"
        log_warn "  La corrida va a fallar al llamar al modelo — copiá ahí la clave privada del perfil ${OCI_PROFILE:-DEFAULT} (chmod 600)"
    fi

    # ── Qué imagen levanta el compose, y si la VM puede cambiarla sola ────────
    # Van en el .env y no en el YAML porque el compose es igual para todos los
    # tenants y estos valores cambian en cada despliegue; docker compose lee este
    # mismo archivo para interpolar ${AMALIA_IMAGE}.
    # Se borran SIEMPRE antes de reescribirlas, incluso si este tenant construye
    # la imagen: así un residuo del .env local no puede encender la
    # auto-actualización en la VM (el mismo criterio que NOTIFICATION_OVERRIDE_TO).
    local v
    for v in AMALIA_IMAGE AMALIA_CHANNEL AMALIA_PINNED; do
        grep -v "^[[:space:]]*${v}[[:space:]]*=" "$env_tmp" > "${env_tmp}.n" && mv "${env_tmp}.n" "$env_tmp"
    done
    if [[ "$deploy_mode" == pull ]]; then
        printf 'AMALIA_IMAGE=%s\n' "$image_ref" >> "$env_tmp"
        # El canal vive en el .env y no en el cloud-init porque cloud-init corre
        # una sola vez en la vida de la VM: si estuviera ahí, activarlo obligaría
        # a recrear la instancia en vez de editar tenants.yaml y redesplegar.
        printf 'AMALIA_CHANNEL=%s\n' "$channel" >> "$env_tmp"
        printf 'AMALIA_PINNED=%s\n' "$pinned" >> "$env_tmp"
    fi
    chmod 600 "$env_tmp"

    scp "${ssh_opts[@]}" "$env_tmp" "${remote}:${app_dir}/.env" >/dev/null
    if [[ -n "$override_to" ]]; then
        log_ok ".env copiado — MODO VALIDACIÓN: todo el correo se redirige a ${override_to}"
    else
        log_ok ".env copiado — correo a los destinatarios reales de clients/*.yaml (producción)"
    fi
    # LLM_PROVIDER se dice en voz alta acá porque es la única señal de qué
    # proveedor —y qué factura— queda corriendo en la VM tras el despliegue.
    local oci_familia
    oci_familia=$(tenant_get_or "$tenant" features.oci_familia nativo)
    log_info "  flags del tenant: ENABLE_DRILLDOWN=$(tenant_get_or "$tenant" features.enable_drilldown false | tr 'TF' 'tf') · ENABLE_MEMORIA=$(tenant_get_or "$tenant" features.enable_memoria false | tr 'TF' 'tf') · LLM_PROVIDER=${llm_provider} · OCI_FAMILIA=${oci_familia}"

    # ── Gobernanza del dato: familias que sacan los pliegos de OCI ────────────
    # `xai` y `google` NO corren dentro de la infraestructura de Oracle: OCI las
    # expone, pero la inferencia sale por llamadas externas a servidores del
    # tercero. La app también lo avisa al arrancar, pero ese WARNING termina
    # sepultado en `journalctl` de una corrida de las 07:00 que nadie mira. Acá,
    # en cambio, hay un humano mirando la pantalla: es el único momento en que
    # este aviso llega a tiempo para cancelar el despliegue.
    case "$oci_familia" in
        xai|google)
            local tercero
            [[ "$oci_familia" == xai ]] && tercero="xAI" || tercero="Google"
            log_warn "OCI_FAMILIA=${oci_familia} — la inferencia NO se queda dentro de OCI"
            log_warn "  El contenido de los pliegos de los clientes sale a servidores de ${tercero}."
            log_warn "  Ningún contrato vigente habilita a compartirlo. Confirmá que es lo querido."
            if [[ "$llm_provider" != "oci" ]]; then
                log_warn "  (Hoy es inerte: LLM_PROVIDER=${llm_provider}. Se activa al pasarlo a 'oci'.)"
            fi
            ;;
    esac

    # ── Obtener la imagen (modelo ephemeral: no se levanta daemon) ────────────
    if [[ "$deploy_mode" == build ]]; then
        log_step "Construyendo imagen Docker en la VM (${compose_file})..."
        log_info "Puede tardar varios minutos (depende del shape); mostrando últimos pasos:"
        ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f ${compose_file} build 2>&1 | tail -20"
        log_ok "Imagen construida"
    else
        # ── Login en GHCR ─────────────────────────────────────────────────────
        # El paquete es privado (el repo lo es), así que hace falta un PAT con
        # scope read:packages. Vive en oci/state/, junto a las llaves SSH: ese
        # directorio ya está en .gitignore y excluido del rsync. A propósito NO
        # va en el .env, que se monta entero dentro del contenedor de la app:
        # AMALIA no tiene ninguna razón para poder leer el token del registro.
        local token_file="${OCI_DIR}/state/ghcr_token" ghcr_user
        ghcr_user=$(tenant_get_or "$tenant" docker.registry_user "emgonzalez")
        if [[ ! -f "$token_file" ]]; then
            log_die "Falta ${token_file}
     Es un PAT de GitHub con scope 'read:packages' para bajar la imagen privada.
     Crear en https://github.com/settings/tokens y guardarlo así:
       printf '%s' '<token>' > ${token_file} && chmod 600 ${token_file}"
        fi

        log_step "Autenticando contra GHCR como ${ghcr_user}..."
        # El token viaja por stdin del ssh y de ahí al stdin de docker login:
        # nunca aparece en la línea de comandos, así que no queda en el `ps` de
        # la VM ni en el historial.
        if ! ssh "${ssh_opts[@]}" "$remote" \
                "docker login ghcr.io -u '${ghcr_user}' --password-stdin" \
                < "$token_file" >/dev/null 2>&1; then
            log_die "docker login en GHCR falló — ¿el token expiró o le falta read:packages?"
        fi
        log_ok "Autenticado en GHCR"

        # ── Pull ──────────────────────────────────────────────────────────────
        log_step "Bajando imagen ${image_ref}..."
        if ! ssh "${ssh_opts[@]}" "$remote" \
                "cd $app_dir && docker compose -f ${compose_file} pull 2>&1 | tail -10"; then
            log_die "No se pudo bajar ${image_ref}
     Lo más probable: CI todavía no publicó ese commit. El job 'image' solo
     publica desde main, develop y tags; desde una rama de trabajo construye y
     verifica pero no sube nada.
     Salidas: mergear a develop, o desplegar otro SHA con
       AMALIA_IMAGE_TAG=sha-<otro> ./oci/deploy.sh ${tenant}"
        fi
        log_ok "Imagen bajada"

        # ── Verificar qué se bajó ─────────────────────────────────────────────
        # El sello de Fase 2 sirve justamente para esto: sin él, "el pull
        # funcionó" y "la VM tiene el código que pedí" no se distinguen, porque
        # una imagen vieja también corre sin errores.
        log_step "Verificando el sello de versión de la imagen..."
        local got
        got=$(ssh "${ssh_opts[@]}" "$remote" \
              "cd $app_dir && docker compose -f ${compose_file} run --rm amalia python -m src.main --version 2>/dev/null" \
              | tr -d '\r' | head -1)
        if [[ -n "$expected_sha" ]]; then
            [[ "$got" == "$expected_sha" ]] || log_die "La imagen desplegada NO es la pedida.
     esperado: ${expected_sha}
     obtenido: ${got:-<vacío>}"
            log_ok "La VM corre ${got}"
        else
            log_warn "Etiqueta móvil '${image_tag}': no hay SHA esperado contra el cual comparar"
            log_ok "La VM corre ${got:-<desconocido>}"
        fi

        # ── Limpieza de imágenes viejas ───────────────────────────────────────
        # Cada despliegue deja atrás ~2.7 GB. En un boot volume de 50 GB eso se
        # nota en pocos despliegues, y el disco lleno rompe la corrida diaria de
        # una forma nada obvia. Se borran solo las de ESTE repo que no son la
        # actual, así que nada más de la VM está en riesgo.
        ssh "${ssh_opts[@]}" "$remote" \
            "docker image ls '${image_repo}' --format '{{.Repository}}:{{.Tag}}' \
             | grep -vxF '${image_ref}' | xargs -r docker rmi 2>/dev/null; true" \
            >/dev/null 2>&1 || true
    fi

    # ── Estado del systemd timer ──────────────────────────────────────────────
    log_step "Verificando systemd timer..."
    # Reactivar el scheduler congelado al inicio, ya con el código y el .env
    # definitivos en su lugar. `start` además de `enable` porque un `enable` sin
    # `start` deja el timer inactivo hasta el próximo reinicio — exactamente el
    # fallo de 2026-05-14 documentado en STATUS.md.
    ssh "${ssh_opts[@]}" "$remote" \
        "sudo systemctl enable --now amalia.timer 2>/dev/null; \
         sudo systemctl is-active amalia.timer >/dev/null || sudo systemctl start amalia.timer; true" \
        >/dev/null 2>&1 || true
    if ssh "${ssh_opts[@]}" "$remote" "systemctl is-enabled amalia.timer" &>/dev/null; then
        log_ok "Timer activo — próximo disparo:"
        ssh "${ssh_opts[@]}" "$remote" \
            "systemctl list-timers amalia.timer --no-pager 2>&1 | sed -n '2p'"
    else
        log_warn "Timer no habilitado — la VM no fue creada con ephemeral_container=true"
    fi

    # ── Auto-actualización ────────────────────────────────────────────────────
    # El timer se reactiva siempre (sin canal el script sale sin hacer nada), y
    # se reporta el estado de la última corrida: un PAT de GHCR vencido deja la
    # unit en 'failed' y este es el momento en que un humano está mirando. Sin
    # esto, la VM se quedaría congelada en una imagen vieja en silencio.
    if ssh "${ssh_opts[@]}" "$remote" \
            "sudo systemctl enable --now amalia-pull-deploy.timer" &>/dev/null; then
        if [[ -n "$channel" ]]; then
            log_ok "Auto-actualización ACTIVA — canal '${channel}'"
            [[ "$pinned" == true ]] && log_warn "  ...pero este despliegue quedó clavado (AMALIA_IMAGE_TAG): no se auto-actualiza hasta el próximo deploy sin esa variable"
            ssh "${ssh_opts[@]}" "$remote" \
                "systemctl list-timers amalia-pull-deploy.timer --no-pager 2>&1 | sed -n '2p'"
        else
            log_info "Auto-actualización desactivada (sin docker.channel) — la VM se queda en ${image_ref:-la imagen que construyó}"
        fi
        if ssh "${ssh_opts[@]}" "$remote" "systemctl is-failed --quiet amalia-pull-deploy.service"; then
            log_warn "La última auto-actualización FALLÓ. Causa más probable: el PAT de GHCR expiró."
            ssh "${ssh_opts[@]}" "$remote" \
                "journalctl -u amalia-pull-deploy.service -n 5 --no-pager -o cat 2>&1" || true
            log_info "  Renovar el token y redesplegar rehace el 'docker login' de la VM."
        fi
    else
        log_info "Esta VM no tiene el timer de auto-actualización (se creó antes de Fase 4)."
        log_info "  Se instala recreando la instancia; el despliegue manual sigue funcionando igual."
    fi

    # ── Sesión COMPRASAL inicial ──────────────────────────────────────────────
    log_warn "Si es el primer despliegue, corré el login UNA vez para generar storage_state.json:"
    log_info "  ssh -i $ssh_key $remote"
    log_info "  cd $app_dir && docker compose -f ${compose_file} run --rm amalia python -m src.auth.login --manual"
    log_info ""
    log_info "Para disparar una corrida manual ahora (sin esperar al timer):"
    log_info "  ssh -i $ssh_key $remote 'sudo systemctl start amalia.service'"
}
