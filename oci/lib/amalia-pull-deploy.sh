#!/usr/bin/env bash
# amalia-pull-deploy.sh — auto-actualización de la imagen EN la VM
#
# Corre en la VM (no en la laptop), disparado por amalia-pull-deploy.timer, y
# existe para cerrar el único agujero que dejaba Fase 4: hasta ahora actualizar
# la VM exigía que una persona la alcanzara por SSH desde su red.
#
# ── Por qué hace falta un "canal" y no alcanza con el SHA fijo ───────────────
# Desplegar a mano clava un SHA a propósito: es reproducible y el rollback es
# cambiar una cadena. Auto-actualizarse, en cambio, necesita un blanco móvil —
# alguien tiene que decidir "lo último de develop" sin intervención humana.
# Las dos cosas conviven así:
#   · sin AMALIA_CHANNEL en el .env  → este script no hace NADA (default)
#   · con AMALIA_CHANNEL=develop     → sigue esa etiqueta móvil
# El default es "no hacer nada" a propósito: ningún tenant existente cambia de
# comportamiento por el solo hecho de que este archivo esté instalado.
#
# ── La trazabilidad no se negocia ───────────────────────────────────────────
# Aunque el blanco sea móvil, lo que queda escrito en el .env es SIEMPRE un
# `sha-<40hex>`: se baja la etiqueta del canal, se le pregunta a la imagen qué
# commit la produjo (`--version`, el sello de Fase 2) y se reetiqueta localmente
# con ese SHA. Así "qué código produjo el reporte de esta mañana" se sigue
# respondiendo leyendo una línea del .env, igual que con el despliegue manual.
#
# ── Qué NO hace ─────────────────────────────────────────────────────────────
# No toca el código del repo en /opt (eso lo sincroniza el deploy manual): solo
# cambia la imagen. Basta porque la imagen trae la app entera adentro; lo que
# vive en el disco de la VM es el compose, el .env y data/.
set -euo pipefail

# AMALIA_APP_DIR solo lo usa el banco de pruebas (scripts/test_pull_deploy.sh):
# la unit de systemd no lo define, así que en la VM siempre es /opt/...
APP_DIR="${AMALIA_APP_DIR:-/opt/tender-opportunity-tracker}"
ENV_FILE="${APP_DIR}/.env"
# El log vive bajo data/ y no bajo /var/log porque este script corre como
# 'ubuntu' (sin sudo) y porque data/ está excluido del rsync del deploy: el
# historial de actualizaciones sobrevive a los despliegues manuales.
LOG_FILE="${APP_DIR}/data/pull-deploy.log"

# El log a disco es best-effort: si data/ no fuera escribible, perder la línea
# es preferible a abortar la actualización por eso. El journal siempre queda.
log()  { printf '%s  %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; echo "$*"; }
# Un "skip" NO es un fallo: es la respuesta correcta a "ahora no toca".
# Salir 0 evita que systemd deje la unit en 'failed' por una condición normal,
# que es justo lo que haría inútil el estado 'failed' cuando importa de verdad.
skip() { log "SKIP: $*"; exit 0; }
# Un fallo real tiene que ser RUIDOSO. El caso esperado es el PAT de GHCR
# vencido (se creó con vencimiento), y una auto-actualización que se rinde en
# silencio deja la VM congelada en una imagen vieja sin que nadie se entere.
# Tres rastros: journal con prioridad err, la unit queda en 'failed' (la ve
# `systemctl --failed` y la reporta el próximo deploy manual) y el log de data/.
die()  { log "FALLO: $*"; logger -t amalia -p user.err "auto-update: $*"; exit 1; }

mkdir -p "$(dirname "$LOG_FILE")"

# Lee una variable del .env sin sourcearlo: el archivo tiene secretos y valores
# con caracteres que un `source` interpretaría.
env_get() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" 2>/dev/null \
        | tail -1 | tr -d '\r'
}

# ── ¿Corresponde actualizar? ─────────────────────────────────────────────────
[[ -f "$ENV_FILE" ]] || skip "todavía no hay .env (la VM no fue desplegada aún)"

channel=$(env_get AMALIA_CHANNEL)
[[ -n "$channel" ]] || skip "sin canal configurado — modo SHA fijo (el default)"

# Un despliegue con AMALIA_IMAGE_TAG es una decisión deliberada del operador
# (típicamente un rollback). Si el auto-update lo pisara a la mañana siguiente,
# el rollback duraría un día y el operador no tendría forma de saber por qué.
# El clavo se levanta solo con el próximo deploy normal.
[[ "$(env_get AMALIA_PINNED)" == "true" ]] \
    && skip "despliegue clavado a mano (AMALIA_IMAGE_TAG) — se respeta hasta el próximo deploy normal"

current=$(env_get AMALIA_IMAGE)
[[ -n "$current" ]] || skip "sin AMALIA_IMAGE en el .env (este tenant construye la imagen, no la baja)"

# ── No pisarse con la corrida diaria ─────────────────────────────────────────
# Cambiar la imagen mientras corre el reporte significaría que el reporte de hoy
# se produjo con dos versiones distintas del código. Peor: en modo dos etapas,
# scrape y analyze correrían con imágenes diferentes.
for unit in amalia.service amalia-scrape.service amalia-analyze.service; do
    if systemctl is-active --quiet "$unit"; then
        skip "la corrida diaria está en curso (${unit})"
    fi
done

# ── No pisarse con un despliegue manual ──────────────────────────────────────
# `04-app.sh` detiene los timers al empezar y los reactiva al terminar, para que
# una corrida no se dispare con código a medio sincronizar. Reusamos esa misma
# señal: si el scheduler está congelado, o hay un deploy en curso o alguien lo
# pausó a propósito — en los dos casos la respuesta correcta es no tocar nada.
scheduler_activo=no
for unit in amalia.timer amalia-scrape.timer; do
    if systemctl is-active --quiet "$unit"; then
        scheduler_activo=si
    fi
done
[[ "$scheduler_activo" == "si" ]] \
    || skip "el scheduler está detenido (deploy manual en curso o pausa deliberada)"

# ── Resolver el repositorio a partir de la imagen actual ─────────────────────
# Sale del .env en vez de estar hardcodeado acá para que no haya dos lugares que
# puedan discrepar sobre de dónde sale la imagen. El corte por ':' se hace solo
# si el ':' está DESPUÉS del último '/', porque un registro con puerto
# (host:5000/repo) también lleva ':'.
if [[ "${current##*/}" == *:* ]]; then
    repo="${current%:*}"
else
    repo="$current"
fi

# ── Preservar el sufijo del SDK de OCI (-oci) ────────────────────────────────
# `repo` ya perdió toda la etiqueta, incluido el sufijo: `sha-<sha>-oci` y
# `sha-<sha>` son la MISMA imagen base con distinto contenido (con o sin el SDK
# de OCI, ver ci.yml), nunca dos repositorios. Si este tenant corre con
# LLM_PROVIDER=oci, `current` termina en `-oci` y el canal tiene que seguir la
# etiqueta CON sufijo (`develop-oci`, no `develop`) — CI la publica aparte,
# con el mismo `flavor: suffix=...` que arma las demás. Sin esto, la
# auto-actualización bajaba silenciosamente la imagen SIN el SDK y la corrida
# de las 07:00 moría con `ModuleNotFoundError: No module named 'oci'` (la
# misma falla observada el 2026-09-23, pero por esta vía en vez del despliegue
# manual).
current_tag="${current##*:}"
sufijo=""
[[ "$current_tag" == *-oci ]] && sufijo="-oci"

canal_ref="${repo}:${channel}${sufijo}"

# ── Bajar la etiqueta del canal ──────────────────────────────────────────────
log "canal '${channel}': bajando ${canal_ref} (actual: ${current})"
if ! docker pull --quiet "$canal_ref" >/dev/null 2>&1; then
    die "no se pudo bajar ${canal_ref} — lo más probable es que el PAT de GHCR haya expirado (docker login ghcr.io) o que CI no haya publicado nunca ese canal"
fi

# ── Preguntarle a la imagen qué commit la produjo ────────────────────────────
# Sin este paso, "el pull funcionó" y "sé qué código voy a correr" no son lo
# mismo. Se usa `docker run` pelado y no `docker compose run` a propósito: no
# hace falta montar data/ ni leer el .env para preguntar una versión, y así el
# chequeo no depende del compose file del tenant.
# El `|| sha=""` evita que `set -e` mate el script sin mensaje si el container
# no arranca: el chequeo de abajo diagnostica mejor que un exit silencioso.
sha=$(docker run --rm "$canal_ref" python -m src.main --version 2>/dev/null | tr -d '\r' | head -1) || sha=""
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    die "la imagen de '${canal_ref}' no trae sello de versión (--version devolvió '${sha:-<vacío>}') — sin sello no hay trazabilidad, así que no se despliega"
fi

nuevo_ref="${repo}:sha-${sha}${sufijo}"
[[ "$nuevo_ref" == "$current" ]] && skip "ya está corriendo ${sha} — el canal no se movió"

# Reetiquetado local: la imagen que se acaba de bajar YA es la del commit ${sha}
# (CI publica ambas etiquetas desde el mismo build, y el sello lo confirma). Se
# la nombra por su SHA para que el .env quede apuntando a una referencia
# inmutable en lugar de a una móvil — sin pagar una segunda descarga de ~975 MB.
docker tag "$canal_ref" "$nuevo_ref"

# Verificación final sobre la referencia EXACTA que va a quedar en el .env.
verificado=$(docker run --rm "$nuevo_ref" python -m src.main --version 2>/dev/null | tr -d '\r' | head -1) || verificado=""
[[ "$verificado" == "$sha" ]] \
    || die "la referencia ${nuevo_ref} no reporta ${sha} sino '${verificado:-<vacío>}'"

# ── Cambiar la imagen activa ─────────────────────────────────────────────────
# Se re-chequea acá y no solo al principio porque el pull puede haber tardado
# varios minutos: la corrida diaria pudo arrancar mientras tanto. La imagen ya
# bajada queda en disco, así que mañana el reintento es casi instantáneo.
for unit in amalia.service amalia-scrape.service amalia-analyze.service; do
    if systemctl is-active --quiet "$unit"; then
        skip "arrancó una corrida durante el pull (${unit}) — se reintenta en el próximo disparo"
    fi
done

# Escritura atómica (temporal en el MISMO directorio + mv) para que ningún
# lector vea un .env a medio escribir: el container toma las variables al
# arrancar y un archivo truncado se traduciría en una corrida sin credenciales.
env_tmp=$(mktemp "${ENV_FILE}.XXXXXX") || die "no se pudo crear el temporal del .env"
# El temporal se crea al lado del .env (mismo filesystem, para que el mv sea
# atómico), así que hay que asegurarse de no dejarlo tirado si algo falla antes.
trap 'rm -f "${env_tmp:-}"' EXIT
chmod 600 "$env_tmp"
grep -v '^[[:space:]]*AMALIA_IMAGE[[:space:]]*=' "$ENV_FILE" > "$env_tmp" || true
printf 'AMALIA_IMAGE=%s\n' "$nuevo_ref" >> "$env_tmp"
mv "$env_tmp" "$ENV_FILE"

# ── Limpieza ─────────────────────────────────────────────────────────────────
# Cada imagen pesa ~2.7 GB en disco. Sin esto, un boot volume de 50 GB se llena
# en pocas actualizaciones y la corrida diaria empieza a fallar de una forma
# nada obvia. Se borran solo las de ESTE repositorio que no son la nueva ni la
# del canal (que apunta a la misma imagen, así que conservarla no cuesta nada y
# hace incremental el pull siguiente).
docker image ls "$repo" --format '{{.Repository}}:{{.Tag}}' \
    | grep -vxF "$nuevo_ref" \
    | grep -vxF "$canal_ref" \
    | xargs -r docker rmi >/dev/null 2>&1 || true

log "OK: ${current} → ${nuevo_ref} (canal ${channel})"
