#!/usr/bin/env bash
# 08-web.sh — servicio web residente (ADR 0005, Fase 2): IP reservada, registro
# DNS, secreto de los enlaces y arranque de `web` + `caddy`.
#
# Todo es inerte salvo que el tenant declare `web.enabled: true` en tenants.yaml.
# El 80/443 de la Security List NO se arma acá sino en 01-network.sh: la API de
# OCI reemplaza la lista entera y tiene que ir en el mismo update que el 22.
#
# Orden en deploy.sh:  compute_create → web_reserve_ip → budget → adb_acl_sync →
#                      app_deploy → web_dns_record → web_up
# `web_reserve_ip` va ANTES de `adb_acl_sync` porque reservar cambia la IP pública
# de la VM, y la ACL de la ADB admite solo esa IP.
set -euo pipefail
source "${OCI_DIR}/lib/common.sh"

web_enabled() {
    [[ "$(tenant_get_or "$1" web.enabled false)" == "True" ]]
}

# ── IP pública reservada ──────────────────────────────────────────────────────
# Una IP efímera cambia si se recrea la VM, y con ella el registro A: los enlaces
# ya enviados en correos quedarían apuntando a ninguna parte.
# Convertir la efímera actual en reservada NO es posible: hay que soltar la
# efímera y pedir una reservada nueva, así que la IP cambia UNA vez. Por eso pide
# confirmación explícita y avisa de lo que arrastra (ACL de la ADB, SSH).
web_reserve_ip() {
    local tenant=$1
    web_enabled "$tenant" || return 0

    log_step "IP pública reservada..."
    # Tres respuestas (common.sh): sin red se ABORTA; nunca se toma por «no hay
    # IP reservada» (crearía otra y soltaría la efímera de la VM en el camino).
    local rid="" pendiente=""
    if oci_resolver "$tenant" reserved_ip_id "IP reservada" network public-ip get --public-ip-id @ID@; then
        rid=$(state_get "$tenant" reserved_ip_id)
        if [[ -n "$(printf '%s' "$OCI_SALIDA" | jq -r '.data."private-ip-id" // empty' 2>/dev/null)" ]]; then
            log_ok "IP reservada ya existe: $(state_get "$tenant" public_ip)"
            return 0
        fi
        # Creada pero sin asignar: un deploy anterior se cortó entre crear y asignar.
        log_warn "La IP reservada ${rid} existe pero NO está asignada a la VM (un deploy anterior se cortó): se retoma."
        pendiente=1
    fi

    local compartment vnic_id private_ip_id actual="" lifetime actual_id actual_ip rc_pub=0
    compartment=$(tenant_get "$tenant" compartment_ocid)
    vnic_id=$(state_get "$tenant" vnic_id)
    [[ -n "$vnic_id" ]] || log_die "vnic_id no está en el state — corré compute_create primero"
    private_ip_id=$(ociq network private-ip list --vnic-id "$vnic_id" \
        --query 'data[?"is-primary"] | [0].id' --raw-output)
    [[ -n "$private_ip_id" && "$private_ip_id" != "null" ]] || log_die "No se encontró la IP privada primaria de la VNIC $vnic_id"

    # ¿Qué IP pública tiene hoy la VM? 404 = ninguna; sin red = abortar (antes un
    # fallo se leía como «ninguna» y el flujo seguía a ciegas).
    oci_existe "IP pública actual de la VM" network public-ip get --private-ip-id "$private_ip_id" \
        --query "join(' ', [data.id, data.lifetime, data.\"ip-address\"])" --raw-output >/dev/null || rc_pub=$?
    case $rc_pub in
        0) actual=$OCI_SALIDA ;;
        1) actual="" ;;
        *) log_die "No pude confirmar qué IP pública tiene la VM (¿sin red?). No se toca nada: volvé a correr cuando haya conexión." ;;
    esac
    read -r actual_id lifetime actual_ip <<<"$actual" || true

    if [[ "${lifetime:-}" == "RESERVED" ]]; then
        # Alguien la reservó a mano: se adopta tal cual, no se toca.
        state_set "$tenant" reserved_ip_id "$actual_id"
        state_set "$tenant" public_ip "$actual_ip"
        log_ok "La VM ya tenía una IP reservada ($actual_ip): adoptada"
        return 0
    fi

    log_warn "La VM tiene una IP EFÍMERA (${actual_ip:-sin IP}). Para reservarla hay que soltarla:"
    log_warn "  · la IP pública CAMBIA una vez; la sesión SSH actual y los accesos por IP vieja dejan de servir"
    log_warn "  · adb_acl_sync (siguiente paso del deploy) actualiza la ACL de la ADB a la IP nueva"
    if [[ "${WEB_CONFIRM:-}" != "yes" ]]; then
        read -rp "  ¿Reservar la IP ahora? [y/N] " r
        [[ "$r" =~ ^[yY]$ ]] || log_die "Cancelado: sin IP reservada no se despliega el servicio web. Poné web.enabled: false o reintentá."
    fi

    # Orden: 1) crear la reservada SIN asignar y confirmarla, 2) recién entonces
    # soltar la efímera, 3) asignar. Antes se soltaba la efímera primero: si el
    # `create` fallaba (red, cupo), la VM quedaba sin IP pública. El id se guarda
    # en el state apenas existe, así un corte entre pasos se retoma (ver arriba).
    if [[ -z "$pendiente" ]]; then
        tenant_tag_args "$tenant"
        rid=$(ociq network public-ip create \
            --compartment-id "$compartment" \
            --lifetime RESERVED \
            --display-name "tot-${tenant}-ip" \
            "${OCI_TAG_ARGS[@]}" \
            --wait-for-state AVAILABLE \
            --query 'data.id' --raw-output)
        [[ -n "$rid" && "$rid" != "null" ]] || log_die "OCI no devolvió el id de la IP reservada; la efímera NO se soltó."
        state_set "$tenant" reserved_ip_id "$rid"
    fi
    local rc_res=0
    oci_existe "IP reservada ${rid}" network public-ip get --public-ip-id "$rid" \
        --query 'data.lifetime' --raw-output >/dev/null || rc_res=$?
    [[ $rc_res -eq 0 && "$OCI_SALIDA" == "RESERVED" ]] \
        || log_die "No pude confirmar la IP reservada ${rid}; la efímera NO se soltó. Volvé a correr el deploy (retoma desde acá)."
    log_ok "IP reservada lista: ${rid}"

    if [[ -n "${actual_id:-}" && "$actual_id" != "null" ]]; then
        ociq network public-ip delete --public-ip-id "$actual_id" --force >/dev/null
        log_ok "IP efímera liberada"
    fi

    ociq network public-ip update \
        --public-ip-id "$rid" \
        --private-ip-id "$private_ip_id" \
        --wait-for-state ASSIGNED >/dev/null \
        || log_die "La IP reservada ${rid} NO quedó asignada a la VM (que ahora no tiene IP pública). Volvé a correr el deploy: retoma la asignación."
    local nueva; nueva=$(ociq network public-ip get --public-ip-id "$rid" --query 'data."ip-address"' --raw-output)
    state_set "$tenant" public_ip "$nueva"
    log_ok "IP reservada asignada: $nueva (antes: ${actual_ip:-ninguna})"
}

# ── Registro A en OCI DNS ─────────────────────────────────────────────────────
# Falla suave: sin la zona (o sin permiso) el deploy de la app sigue; solo que el
# hostname no resuelve y Caddy no puede sacar el certificado todavía.
web_dns_record() {
    local tenant=$1
    web_enabled "$tenant" || return 0

    log_step "Registro DNS..."
    local zone host ip compartment
    zone=$(tenant_get "$tenant" web.dns_zone)
    host=$(tenant_get "$tenant" web.hostname)
    ip=$(state_get "$tenant" public_ip)
    compartment=$(tenant_get "$tenant" compartment_ocid)
    [[ -n "$ip" ]] || log_die "public_ip no está en el state"

    if ociq dns record rrset update --zone-name-or-id "$zone" --scope GLOBAL \
        --compartment-id "$compartment" --domain "$host" --rtype A --force \
        --items "[{\"domain\":\"${host}\",\"rdata\":\"${ip}\",\"rtype\":\"A\",\"ttl\":300}]" >/dev/null 2>&1; then
        state_set "$tenant" web_dns_zone "$zone"
        state_set "$tenant" web_hostname "$host"
        log_ok "${host}  A  ${ip}"
    else
        log_warn "No se pudo escribir ${host} en la zona '${zone}' (¿la zona no existe o falta permiso DNS?)."
        log_warn "  El servicio queda sin nombre: créalo a mano  →  ${host}  A  ${ip}  (TTL 300)"
    fi
}

# ── Secreto de los enlaces firmados ───────────────────────────────────────────
# Uno por tenant y estable entre despliegues: si cambiara en cada uno, todos los
# enlaces ya enviados en correos dejarían de validar. Vive en el state (que se
# respalda al bucket) y se genera la primera vez. DEV y PROD NO lo comparten.
web_hmac_secret() {
    local tenant=$1 f="${OCI_DIR}/state/${1}_feedback_hmac.secret"
    if [[ ! -s "$f" ]]; then
        require_cmd openssl
        (umask 077; openssl rand -hex 32 > "$f")
        log_ok "Secreto HMAC del feedback generado: $f" >&2
    fi
    cat "$f"
}

# ── Secreto de la identidad del portal ────────────────────────────────────────
# `IDENTIDAD_SECRET` firma los códigos de acceso y CIFRA el secreto TOTP de cada
# persona (src/identidad/mfa.py): si cambiara entre despliegues, los TOTP ya
# enrolados dejarían de descifrar. Por eso vive en el state, igual que el del
# feedback, y cada despliegue lo reescribe en el .env: un valor puesto a mano en
# la VM no sobrevive a `deploy.sh`, que reconstruye ese archivo. DEV y PROD NO lo
# comparten. NO rotarlo sin reenrolar el TOTP de admin_plataforma y admin_organizacion.
web_identidad_secret() {
    local tenant=$1 f="${OCI_DIR}/state/${1}_identidad.secret"
    if [[ ! -s "$f" ]]; then
        require_cmd openssl
        (umask 077; openssl rand -hex 32 > "$f")
        log_ok "Secreto de identidad generado: $f" >&2
    fi
    cat "$f"
}

# ── Arranque de web + caddy en la VM ──────────────────────────────────────────
# Va DESPUÉS de app_deploy: necesita el .env, el compose y la imagen ya en la VM.
web_up() {
    local tenant=$1
    web_enabled "$tenant" || return 0

    local ip host compose_file ssh_key
    ip=$(state_get "$tenant" public_ip)
    host=$(tenant_get "$tenant" web.hostname)
    compose_file=$(tenant_get "$tenant" docker.compose_file)
    ssh_key="${OCI_DIR}/state/${tenant}_id_ed25519"
    local ssh_opts=(-i "$ssh_key" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${OCI_DIR}/state/known_hosts" -o LogLevel=ERROR)
    local remote="ubuntu@${ip}" app_dir=/opt/tender-opportunity-tracker

    log_step "Levantando web + caddy..."
    ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f ${compose_file} up -d web caddy 2>&1 | tail -8" \
        || log_die "docker compose up -d web caddy falló"

    # El Caddyfile llega por rsync, que REEMPLAZA el archivo (otro inodo): el bind mount de un
    # solo archivo sigue viendo el viejo y `up -d` no recrea nada porque el compose no cambió.
    # Pasó el 2026-10-26 al publicar el portal: Caddy siguió sirviendo la lista anterior y todo
    # daba 404. Se compara el contenido y, si difiere, se recrea SOLO Caddy (los certificados
    # viven en el volumen caddy_data, no se vuelven a pedir).
    local host_sum cont_sum
    host_sum=$(ssh "${ssh_opts[@]}" "$remote" "md5sum ${app_dir}/oci/caddy/Caddyfile | cut -d' ' -f1" 2>/dev/null || true)
    cont_sum=$(ssh "${ssh_opts[@]}" "$remote" "docker exec amalia-caddy md5sum /etc/caddy/Caddyfile 2>/dev/null | cut -d' ' -f1" 2>/dev/null || true)
    if [[ -n "$host_sum" && "$host_sum" != "$cont_sum" ]]; then
        log_step "El Caddyfile cambió: recreando Caddy..."
        ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f ${compose_file} up -d --force-recreate caddy 2>&1 | tail -4" \
            || log_die "No se pudo recrear Caddy con el Caddyfile nuevo"
    fi

    # El servicio responde por dentro de la red de compose (no publica puertos).
    local i
    for i in 1 2 3 4 5 6; do
        if ssh "${ssh_opts[@]}" "$remote" "cd $app_dir && docker compose -f ${compose_file} exec -T web \
            python -c \"import urllib.request as u;print(u.urlopen('http://127.0.0.1:8080/healthz',timeout=3).read().decode())\"" 2>/dev/null | grep -q ok; then
            log_ok "web responde /healthz por dentro"
            break
        fi
        [[ $i -eq 6 ]] && log_die "El contenedor web no respondió /healthz — revisá: docker compose -f ${compose_file} logs web"
        sleep 5
    done

    # Prueba desde afuera: es la que importa (DNS + 80/443 + certificado). Falla
    # suave porque depende de que el administrador del dominio ya haya delegado.
    log_step "Verificando https://${host}/healthz desde afuera..."
    for i in 1 2 3 4 5 6 7 8; do
        if [[ "$(curl -fsS --max-time 15 "https://${host}/healthz" 2>/dev/null || true)" == "ok" ]]; then
            log_ok "https://${host}/healthz responde ok (certificado válido)"
            return 0
        fi
        sleep 15
    done
    log_warn "https://${host}/healthz NO responde todavía desde afuera. Causas probables, en orden:"
    log_warn "  1. la delegación DNS aún no propagó:  dig +short ${host}"
    log_warn "  2. Caddy no pudo sacar el certificado:  docker compose -f ${compose_file} logs caddy"
    log_warn "  3. 80/443 cerrados en la Security List o en el firewall de la VM"
    log_warn "  NO actives ENABLE_FEEDBACK hasta que esto responda."
}
