#!/bin/bash

# Script de diagnóstico de conectividad a OCI
# Resuelve problemas de "Network is unreachable"

set -e

echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  DIAGNÓSTICO DE CONECTIVIDAD A OCI - tender-opportunity-tracker  ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""

# Paso 1: Verificar conexión básica a internet
echo "📡 PASO 1: Verificando conectividad básica a internet..."
echo "---"
if ping -c 1 8.8.8.8 &>/dev/null; then
    echo "✅ Conexión a internet: OK"
else
    echo "❌ NO HAY CONEXIÓN A INTERNET"
    echo "   Solución: Conecta a una red con acceso a internet"
    exit 1
fi

# Paso 2: Verificar DNS
echo ""
echo "🔍 PASO 2: Verificando resolución DNS..."
echo "---"
if nslookup iaas.us-ashburn-1.oraclecloud.com &>/dev/null; then
    OCI_IP=$(nslookup iaas.us-ashburn-1.oraclecloud.com 2>/dev/null | grep "Address:" | grep -v "#53" | head -1 | awk '{print $2}')
    echo "✅ DNS resolviendo: iaas.us-ashburn-1.oraclecloud.com → $OCI_IP"
else
    echo "❌ DNS NO RESOLVIENDO"
    echo "   Solución: Verifica tu configuración de DNS"
    echo "   Prueba: sudo scutil -d -v -c setdnsservers Main 8.8.8.8 1.1.1.1"
    exit 1
fi

# Paso 3: Verificar routing
echo ""
echo "🛣️  PASO 3: Verificando routing a OCI..."
echo "---"
if traceroute -m 3 "$OCI_IP" &>/dev/null; then
    echo "✅ Routing OK (traceroute alcanza servidores)"
else
    echo "⚠️  Traceroute puede estar bloqueado por firewall (normal)"
fi

# Paso 4: Conectar a puerto 443
echo ""
echo "🔐 PASO 4: Verificando conectividad HTTPS (puerto 443)..."
echo "---"
if timeout 5 bash -c "echo > /dev/tcp/$OCI_IP/443" 2>/dev/null; then
    echo "✅ Puerto 443 abierto: Puedes conectar a OCI"
else
    echo "❌ Puerto 443 BLOQUEADO (Network is unreachable)"
    echo "   Posibles causas:"
    echo "     1. Firewall corporativo bloqueando OCI"
    echo "     2. ISP bloqueando ese rango de IPs"
    echo "     3. VPN activa con DNS split-tunneling"
    echo ""
    echo "   Soluciones a probar:"
    echo "     A) Si estás en VPN corporativa:"
    echo "        - Desconecta la VPN temporalmente"
    echo "        - O solicita al admin acceso a OCI"
    echo ""
    echo "     B) Si no estás en VPN:"
    echo "        - Verifica que no haya Firewall Mac bloqueando"
    echo "        - En macOS: System Preferences > Security & Privacy > Firewall"
    echo ""
    echo "     C) Contacta a tu ISP si persiste"
    exit 1
fi

# Paso 5: Verificar OCI CLI
echo ""
echo "🔑 PASO 5: Probando OCI CLI..."
echo "---"
if ! command -v oci &>/dev/null; then
    echo "❌ OCI CLI no instalado"
    echo "   Solución: brew install oci-cli"
    exit 1
fi

echo "✅ OCI CLI instalado"

# Paso 6: Probar autenticación
echo ""
echo "🔐 PASO 6: Probando credenciales MULTICOMPUTOS..."
echo "---"
if oci --profile MULTICOMPUTOS iam region-subscription list --region us-ashburn-1 &>/dev/null; then
    echo "✅ Autenticación con OCI: OK"
else
    echo "❌ Error de autenticación"
    echo "   Verifica ~/.oci/config y la clave privada"
    exit 1
fi

echo ""
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  ✅ TODOS LOS CHECKS PASARON - CONECTIVIDAD A OCI OK               ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""
echo "Ahora puedes proceder con la eliminación de AMALIA en multicomputos."
echo ""
echo "Comando para reanudar:"
echo "  cd /Users/emiliogonzalez/Documents/MULTICOMPUTOS/POCs/tender-opportunity-tracker"
echo "  git checkout -"
echo "  # El proceso de eliminación se reanudará automáticamente"
