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
        "sudo systemctl stop amalia.timer amalia-scrape.timer 2>/dev/null; \
         sudo systemctl stop amalia.service amalia-scrape.service amalia-analyze.service 2>/dev/null; true" \
        >/dev/null 2>&1 || true
    log_ok "Timer detenido (se reactiva al terminar)"

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

    # Qué imagen levanta el compose. Va en el .env y no en el YAML porque el
    # compose es igual para todos los tenants y el SHA cambia en cada despliegue.
    # docker compose lee este mismo archivo para interpolar ${AMALIA_IMAGE}.
    if [[ "$deploy_mode" == pull ]]; then
        grep -v '^[[:space:]]*AMALIA_IMAGE[[:space:]]*=' "$env_tmp" > "${env_tmp}.n" && mv "${env_tmp}.n" "$env_tmp"
        printf 'AMALIA_IMAGE=%s\n' "$image_ref" >> "$env_tmp"
    fi
    chmod 600 "$env_tmp"

    scp "${ssh_opts[@]}" "$env_tmp" "${remote}:${app_dir}/.env" >/dev/null
    if [[ -n "$override_to" ]]; then
        log_ok ".env copiado — MODO VALIDACIÓN: todo el correo se redirige a ${override_to}"
    else
        log_ok ".env copiado — correo a los destinatarios reales de clients/*.yaml (producción)"
    fi
    log_info "  flags del tenant: ENABLE_DRILLDOWN=$(tenant_get_or "$tenant" features.enable_drilldown false | tr 'TF' 'tf') · ENABLE_MEMORIA=$(tenant_get_or "$tenant" features.enable_memoria false | tr 'TF' 'tf')"

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

    # ── Sesión COMPRASAL inicial ──────────────────────────────────────────────
    log_warn "Si es el primer despliegue, corré el login UNA vez para generar storage_state.json:"
    log_info "  ssh -i $ssh_key $remote"
    log_info "  cd $app_dir && docker compose -f ${compose_file} run --rm amalia python -m src.auth.login --manual"
    log_info ""
    log_info "Para disparar una corrida manual ahora (sin esperar al timer):"
    log_info "  ssh -i $ssh_key $remote 'sudo systemctl start amalia.service'"
}
