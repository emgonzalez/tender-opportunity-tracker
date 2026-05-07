#!/usr/bin/env bash
# start.sh — Arranque completo de tender-opportunity-tracker
# Uso: ./start.sh
set -euo pipefail

# ── Colores ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

APP="tender-opportunity-tracker"

# Siempre ejecutar desde el directorio del script
cd "$(dirname "${BASH_SOURCE[0]}")"

step() { echo -e "\n${BLUE}${BOLD}▶ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
die()  { echo -e "  ${RED}✗${NC}  $*" >&2; exit 1; }

# ── 1. Docker ─────────────────────────────────────────────────────────────────
step "Verificando Docker..."
docker info &>/dev/null || die "Docker no está corriendo. Inícialo antes de continuar."
ok "Docker disponible"

# ── 2. Variables de entorno ───────────────────────────────────────────────────
step "Verificando configuración (.env)..."
if [ ! -f .env ]; then
    cp .env.example .env
    warn ".env no encontrado — se creó desde .env.example"
    warn "Edita .env con tus credenciales y vuelve a ejecutar: ./start.sh"
    exit 1
fi
ok ".env encontrado"

# Cargar .env manualmente para manejar valores con espacios sin comillas
# (ej: Gmail App Passwords como "oouy gmdo azsi tkjl")
while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue   # comentarios
    [[ -z "${line// }" ]] && continue              # líneas vacías
    if [[ "$line" =~ ^([^=]+)=(.*)$ ]]; then
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        # Quitar comillas envolventes si las tiene
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        export "$key=$value"
    fi
done < .env

# Validar variables requeridas
missing=()
for var in COMPRASAL_EMAIL COMPRASAL_PASSWORD ANTHROPIC_API_KEY SMTP_USER SMTP_PASSWORD; do
    [[ -z "${!var:-}" ]] && missing+=("$var")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    for v in "${missing[@]}"; do warn "Falta en .env: $v"; done
    die "Completa las variables indicadas y vuelve a ejecutar: ./start.sh"
fi
ok "Variables de entorno válidas"

# ── 3. Perfiles de clientes ───────────────────────────────────────────────────
step "Verificando perfiles de clientes..."
active_count=0
if [[ -d clients ]]; then
    active_count=$(grep -rl "active: true" clients/ 2>/dev/null | wc -l | tr -d ' ')
fi
if [[ "$active_count" -eq 0 ]]; then
    warn "No hay clientes activos en clients/"
    warn "Copia clients/multicomputos.yaml, ajusta tu cliente y vuelve a ejecutar."
else
    ok "$active_count cliente(s) activo(s) encontrado(s)"
fi

# ── 4. Imagen Docker ──────────────────────────────────────────────────────────
step "Construyendo imagen Docker..."
docker compose build --quiet
ok "Imagen lista"

# ── 5. Sesión de COMPRASAL ────────────────────────────────────────────────────
step "Verificando sesión de COMPRASAL..."

_do_login() {
    if [[ -n "${COMPRASAL_TOTP_SECRET:-}" ]]; then
        echo "  TOTP automático configurado — generando OTP..."
        docker compose run -T --rm "$APP" python -m src.auth.login --manual \
            || die "Login fallido. Revisa data/login_debug.log para detalles."
    else
        echo ""
        echo "  ┌──────────────────────────────────────────────────────┐"
        echo "  │  Abre tu app de autenticación (Google Auth, Aegis)   │"
        echo "  │  y anota el código OTP actual para COMPRASAL.        │"
        echo "  └──────────────────────────────────────────────────────┘"
        echo ""
        read -rp "  Ingresa el código OTP (6 dígitos): " otp_code
        echo ""
        echo "$otp_code" | docker compose run -T --rm "$APP" python -m src.auth.login --manual \
            || die "Login fallido. Revisa data/login_debug.log para detalles."
    fi
    ok "Sesión guardada correctamente"
}

if [[ ! -f data/storage_state.json ]]; then
    echo "  No hay sesión guardada. Se realizará el login en COMPRASAL."
    _do_login
else
    echo "  Validando sesión contra COMPRASAL..."
    if docker compose run -T --rm "$APP" python -c "
import asyncio, sys
from src.config import get_config
from src.auth.session import is_session_valid
sys.exit(0 if asyncio.run(is_session_valid(get_config())) else 1)
" &>/dev/null; then
        ok "Sesión válida"
    else
        warn "Sesión expirada — se realizará un nuevo login"
        _do_login
    fi
fi

# ── 6. Levantar el agente ─────────────────────────────────────────────────────
step "Iniciando $APP..."
docker compose up -d
ok "$APP corriendo en segundo plano"

# ── 7. Estado y logs ──────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Comandos útiles:${NC}"
echo "    Logs en vivo:  docker compose logs -f $APP"
echo "    Detener:       docker compose down"
echo "    Estado:        docker compose ps"
echo ""
echo "  Siguiendo logs en tiempo real (Ctrl+C no detiene el agente)..."
echo ""
docker compose logs -f "$APP"
