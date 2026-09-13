#!/usr/bin/env bash
# ssh-cidr.sh — ÚNICO lugar donde se decide "cuál es el /32 que puede entrar por
# el 22". Lo consumen dos caminos que tienen que coincidir o el operador queda
# afuera:
#
#   · oci/lib/01-network.sh    — al desplegar, escribe la regla de ingress.
#   · oci/scripts/ssh-allow.sh — entre despliegues, la refresca cuando la IP
#                                residencial (dinámica) cambia.
#
# Si cada uno resolviera la IP a su manera, un `deploy.sh` podría pisar con un
# valor viejo el /32 que `ssh-allow.sh` acababa de dejar bien. Por eso hay una
# sola función.
#
# ── Por qué IPv4 y solo IPv4 ─────────────────────────────────────────────────
# Las VCN de este proyecto son IPv4 puras (`vcn_cidr: 10.x.0.0/16`, sin bloque
# IPv6) y la VM se alcanza por una IP pública v4. O sea: el SSH del operador
# SIEMPRE sale por IPv4, aunque su red tenga IPv6. Una regla `::/128` no serviría
# de nada —OCI ni siquiera la acepta sin IPv6 habilitado en la VCN—, así que la
# detección fuerza `curl -4`. Si la red del operador solo alcanza los servicios
# de eco por IPv6, eso es un problema del detector, no de la ruta a la VM, y hay
# que resolverlo con `--cidr` (ver el mensaje de error).
#
# ── Por qué se exige corroboración ───────────────────────────────────────────
# La red del operador tiene un proxy que intercepta TLS. Un proxy así puede
# contestar el "¿cuál es mi IP?" con SU salida y no con la del operador, y ese
# valor sería un /32 que no corresponde a nadie: puerto 22 cerrado para todos.
# Por eso no alcanza con que UN proveedor conteste: hacen falta al menos dos
# respuestas y que sean idénticas. Ante cualquier discrepancia se aborta.

set -euo pipefail

# Guard: se sourcea desde varios lugares — definir una sola vez.
[[ -n "${_TOT_SSH_CIDR_LOADED:-}" ]] && return 0
_TOT_SSH_CIDR_LOADED=1

# Logging: si quien sourcea ya cargó common.sh, se reutiliza; si no (por ejemplo
# un test que prueba esta unidad sola), se definen equivalentes mínimos.
if ! declare -F log_die >/dev/null 2>&1; then
    log_step() { echo "▶ $*"; }
    log_ok()   { echo "  ✓  $*"; }
    log_warn() { echo "  ⚠  $*"; }
    log_info() { echo "  ·  $*"; }
    log_die()  { echo "  ✗  $*" >&2; exit 1; }
fi

# Servicios de eco. Son cuatro a propósito: si uno está caído no se aborta, y
# tener varios independientes es justamente lo que permite corroborar.
SSH_CIDR_PROVIDERS=${SSH_CIDR_PROVIDERS:-"https://api.ipify.org https://checkip.amazonaws.com https://icanhazip.com https://ifconfig.me/ip"}
SSH_CIDR_CURL_TIMEOUT=${SSH_CIDR_CURL_TIMEOUT:-6}

# ── Validadores ──────────────────────────────────────────────────────────────
# Una IPv4 con octetos en rango. El regex solo no alcanza: "999.1.1.1" lo pasa.
ssh_cidr_is_ipv4() {
    local ip=$1
    [[ $ip =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    local o
    for o in "${BASH_REMATCH[@]:1:4}"; do
        ((10#$o <= 255)) || return 1
    done
    return 0
}

# CIDR IPv4 bien formado (dirección válida + prefijo 0..32).
ssh_cidr_is_ipv4_cidr() {
    local cidr=$1
    [[ $cidr == */* ]] || return 1
    local ip=${cidr%/*} pfx=${cidr#*/}
    [[ $pfx =~ ^[0-9]{1,2}$ ]] || return 1
    ((10#$pfx <= 32)) || return 1
    ssh_cidr_is_ipv4 "$ip"
}

# ── Detección ────────────────────────────────────────────────────────────────
# Escribe la IPv4 pública en stdout. Devuelve != 0 si NO puede afirmarla con
# certeza; nunca "adivina". El que llama debe abortar sin escribir.
ssh_cidr_detect_ipv4() {
    local url ans
    local -a votos=()
    for url in $SSH_CIDR_PROVIDERS; do
        ans=$(curl -4 -fsS --max-time "$SSH_CIDR_CURL_TIMEOUT" "$url" 2>/dev/null | tr -d '[:space:]') || ans=""
        # Se descarta lo que no sea una IPv4 limpia: un portal cautivo o un
        # proxy que devuelve HTML no vota.
        [[ -n "$ans" ]] && ssh_cidr_is_ipv4 "$ans" || continue
        votos+=("$ans")
        # `if` y no `&& break`: con `set -e`, un `((...)) && break` que da falso
        # devuelve 1 como último comando del cuerpo y aborta el script entero.
        if ((${#votos[@]} >= 3)); then break; fi
    done

    if ((${#votos[@]} < 2)); then
        echo "DETECCION_INSUFICIENTE ${#votos[@]}" >&2
        return 1
    fi

    local v
    for v in "${votos[@]}"; do
        if [[ "$v" != "${votos[0]}" ]]; then
            echo "DETECCION_DISCREPANTE ${votos[*]}" >&2
            return 2
        fi
    done

    printf '%s\n' "${votos[0]}"
}

# Diagnóstico para cuando la detección falla: ¿la red sale por IPv6?
# No cambia ninguna decisión, solo mejora el mensaje de error.
ssh_cidr_probable_ipv6() {
    local ans
    ans=$(curl -6 -fsS --max-time "$SSH_CIDR_CURL_TIMEOUT" "https://api64.ipify.org" 2>/dev/null | tr -d '[:space:]') || return 1
    [[ "$ans" == *:* ]] || return 1
    printf '%s\n' "$ans"
}

# ── Resolución del valor de tenants.yaml ─────────────────────────────────────
# ssh_cidr_resolve <valor-crudo>
#   auto        → detecta la IPv4 del operador y devuelve <ip>/32
#   <cidr IPv4> → lo valida y lo devuelve tal cual
#   0.0.0.0/0   → se acepta (compatibilidad) pero grita: es el agujero que la
#                 Fase 7 vino a cerrar.
# Cualquier otra cosa, o una detección dudosa, MATA el proceso: es preferible no
# desplegar a dejar una regla que no corresponde a nadie.
ssh_cidr_resolve() {
    local raw=$1 ip rc

    case "$raw" in
        auto|AUTO|Auto)
            set +e
            ip=$(ssh_cidr_detect_ipv4 2>/dev/null)
            rc=$?
            set -e
            if ((rc != 0)); then
                local v6=""
                v6=$(ssh_cidr_probable_ipv6 2>/dev/null) || true
                {
                    echo "No se pudo determinar con certeza la IP pública del operador."
                    if ((rc == 2)); then
                        echo "     Los servicios de eco NO coincidieron. Eso pasa con un proxy que"
                        echo "     intercepta TLS o con CGNAT: la IP que verías no es la que ve OCI."
                    else
                        echo "     Menos de dos servicios de eco contestaron por IPv4."
                    fi
                    if [[ -n "$v6" ]]; then
                        echo "     Tu red parece salir por IPv6 ($v6). No sirve: la VCN es IPv4 pura"
                        echo "     y la VM se alcanza por IP pública v4, así que el SSH sale por v4."
                    fi
                    echo "     Salida: averiguá la IPv4 que ve el server y pasala explícita:"
                    echo "       ssh <vm> 'echo \$SSH_CLIENT'      # la fuente REAL, la que ve sshd"
                    echo "       oci/scripts/ssh-allow.sh <tenant> --cidr <esa-ip>/32"
                } >&2
                log_die "detección de IP abortada — no se escribe nada"
            fi
            printf '%s/32\n' "$ip"
            ;;
        "")
            log_die "ssh_ingress_cidr vacío — poné 'auto' o un CIDR IPv4 explícito"
            ;;
        0.0.0.0/0)
            log_warn "ssh_ingress_cidr=0.0.0.0/0 — el puerto 22 queda abierto a internet."
            log_warn "  Usá 'auto' (o un /32) salvo que tengas una razón deliberada."
            printf '%s\n' "$raw"
            ;;
        *)
            ssh_cidr_is_ipv4_cidr "$raw" \
                || log_die "ssh_ingress_cidr='$raw' no es un CIDR IPv4 válido (¿IPv6? la VCN no tiene IPv6)"
            printf '%s\n' "$raw"
            ;;
    esac
}
