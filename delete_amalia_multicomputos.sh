#!/bin/bash

# Script de eliminación completa de AMALIA en tenant multicomputos
# ADVERTENCIA: Esta operación es destructiva e irreversible
# Elimina toda la infraestructura de AMALIA en OCI

set -e

# Colores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuración
INSTANCE_ID="ocid1.instance.oc1.iad.anuwcljtoja5e3qcrhjxfhaxyzs4j3b6togg7vt26mnzw52agakkvr4thmdq"
COMPARTMENT_ID="ocid1.compartment.oc1..aaaaaaaaijzf7ypkrfgg3h2g5n2tetu5bgjpdufaebnloccv6oprxcn5tnba"
PROFILE="MULTICOMPUTOS"
REGION="us-ashburn-1"

echo -e "${RED}╔════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║         ELIMINACIÓN COMPLETA DE AMALIA - TENANT MULTICOMPUTOS       ║${NC}"
echo -e "${RED}║                    ⚠️  OPERACIÓN IRREVERSIBLE ⚠️                     ║${NC}"
echo -e "${RED}╚════════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Confirmación
read -p "¿Confirmas la eliminación COMPLETA de AMALIA en multicomputos? (escribe 'sí' para continuar): " confirmation
if [ "$confirmation" != "sí" ]; then
    echo "Operación cancelada."
    exit 0
fi

echo ""
echo -e "${YELLOW}Iniciando eliminación...${NC}"
echo ""

# Función auxiliar para ejecutar comandos OCI con manejo de errores
run_oci_cmd() {
    local description=$1
    shift
    echo -n "  $description... "
    if output=$(oci --profile "$PROFILE" "$@" 2>&1); then
        echo -e "${GREEN}✅${NC}"
        echo "$output"
        return 0
    else
        echo -e "${RED}❌${NC}"
        echo "  Error: $output"
        return 1
    fi
}

# PASO 1: Detener instancia
echo -e "${YELLOW}[1/5] Deteniendo instancia OCI...${NC}"
run_oci_cmd "  Deteniendo tender-opportunity-tracker-prod" \
    compute instance action \
    --instance-id "$INSTANCE_ID" \
    --action STOP \
    --region "$REGION" \
    --wait-for-state STOPPED \
    --max-wait-seconds 600 \
    --query 'data."lifecycle-state"' || echo "  ⚠️  Instancia ya detenida o error menor"

echo ""

# PASO 2: Obtener VCN y volúmenes
echo -e "${YELLOW}[2/5] Obteniendo OCIDs de volúmenes y VCN...${NC}"

VCN_ID=$(oci --profile "$PROFILE" network vcn list \
    --compartment-id "$COMPARTMENT_ID" \
    --region "$REGION" \
    --query 'data[0].id' \
    --raw-output 2>/dev/null || echo "")

if [ -z "$VCN_ID" ]; then
    echo "  ⚠️  No se encontró VCN"
else
    echo -e "${GREEN}✅${NC} VCN encontrada: $VCN_ID"
fi

echo ""

# PASO 3: Eliminar instancia
echo -e "${YELLOW}[3/5] Eliminando instancia OCI y volúmenes...${NC}"
run_oci_cmd "  Terminando instancia" \
    compute instance terminate \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" \
    --force \
    --wait-for-state TERMINATED \
    --max-wait-seconds 600 || echo "  ⚠️  Instancia puede haber sido eliminada previamente"

echo ""

# PASO 4: Eliminar VCN y componentes
echo -e "${YELLOW}[4/5] Eliminando VCN y componentes de red...${NC}"

if [ -n "$VCN_ID" ]; then
    # Obtener subnets
    echo "  Obteniendo subnets..."
    SUBNETS=$(oci --profile "$PROFILE" network subnet list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --region "$REGION" \
        --query 'data[].id' \
        --raw-output 2>/dev/null || echo "")

    # Eliminar subnets
    if [ -n "$SUBNETS" ]; then
        while IFS= read -r SUBNET_ID; do
            if [ -n "$SUBNET_ID" ]; then
                echo "  Eliminando subnet: $SUBNET_ID"
                oci --profile "$PROFILE" network subnet delete \
                    --subnet-id "$SUBNET_ID" \
                    --region "$REGION" \
                    --force \
                    --wait-for-state DELETED \
                    --max-wait-seconds 300 2>&1 | grep -E "Action completed|error" || true
            fi
        done <<< "$SUBNETS"
    fi

    # Obtener internet gateway
    echo "  Obteniendo internet gateway..."
    IGW=$(oci --profile "$PROFILE" network internet-gateway list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --region "$REGION" \
        --query 'data[0].id' \
        --raw-output 2>/dev/null || echo "")

    # Eliminar internet gateway
    if [ -n "$IGW" ] && [ "$IGW" != "None" ]; then
        echo "  Eliminando internet gateway: $IGW"
        oci --profile "$PROFILE" network internet-gateway delete \
            --ig-id "$IGW" \
            --region "$REGION" \
            --force \
            --wait-for-state DELETED \
            --max-wait-seconds 300 2>&1 | grep -E "Action completed|error" || true
    fi

    # Obtener route tables
    echo "  Obteniendo route tables..."
    ROUTE_TABLES=$(oci --profile "$PROFILE" network route-table list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --region "$REGION" \
        --query 'data[].id' \
        --raw-output 2>/dev/null || echo "")

    # Eliminar route tables (excepto el default)
    if [ -n "$ROUTE_TABLES" ]; then
        while IFS= read -r RT_ID; do
            if [ -n "$RT_ID" ] && [[ ! "$RT_ID" =~ "default" ]]; then
                echo "  Eliminando route table: $RT_ID"
                oci --profile "$PROFILE" network route-table delete \
                    --rt-id "$RT_ID" \
                    --region "$REGION" \
                    --force \
                    --wait-for-state DELETED \
                    --max-wait-seconds 300 2>&1 | grep -E "Action completed|error" || true
            fi
        done <<< "$ROUTE_TABLES"
    fi

    # Obtener security lists
    echo "  Obteniendo security lists..."
    SEC_LISTS=$(oci --profile "$PROFILE" network security-list list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --region "$REGION" \
        --query 'data[].id' \
        --raw-output 2>/dev/null || echo "")

    # Eliminar security lists (excepto el default)
    if [ -n "$SEC_LISTS" ]; then
        while IFS= read -r SL_ID; do
            if [ -n "$SL_ID" ] && [[ ! "$SL_ID" =~ "default" ]]; then
                echo "  Eliminando security list: $SL_ID"
                oci --profile "$PROFILE" network security-list delete \
                    --security-list-id "$SL_ID" \
                    --region "$REGION" \
                    --force \
                    --wait-for-state DELETED \
                    --max-wait-seconds 300 2>&1 | grep -E "Action completed|error" || true
            fi
        done <<< "$SEC_LISTS"
    fi

    # Eliminar VCN
    echo "  Eliminando VCN: $VCN_ID"
    oci --profile "$PROFILE" network vcn delete \
        --vcn-id "$VCN_ID" \
        --region "$REGION" \
        --force \
        --wait-for-state DELETED \
        --max-wait-seconds 600 2>&1 | grep -E "Action completed|error" || true
fi

echo ""

# PASO 5: Verificación final
echo -e "${YELLOW}[5/5] Verificando limpieza completa...${NC}"

# Verificar instancia eliminada
INSTANCE_STATE=$(oci --profile "$PROFILE" compute instance list \
    --compartment-id "$COMPARTMENT_ID" \
    --region "$REGION" \
    --query "data[] | length(@)" \
    --raw-output 2>/dev/null || echo "0")

if [ "$INSTANCE_STATE" -eq "0" ]; then
    echo -e "${GREEN}✅${NC} Instancia eliminada completamente"
else
    echo -e "${YELLOW}⚠️${NC} Puede quedar una instancia sin terminar completamente"
fi

# Verificar VCN eliminada
if [ -n "$VCN_ID" ]; then
    VCN_STATE=$(oci --profile "$PROFILE" network vcn list \
        --compartment-id "$COMPARTMENT_ID" \
        --region "$REGION" \
        --query "data[] | length(@)" \
        --raw-output 2>/dev/null || echo "0")

    if [ "$VCN_STATE" -eq "0" ]; then
        echo -e "${GREEN}✅${NC} VCN eliminada completamente"
    else
        echo -e "${YELLOW}⚠️${NC} Puede quedar una VCN sin eliminar"
    fi
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                  ELIMINACIÓN COMPLETADA                           ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "✅ AMALIA ha sido completamente removido del tenant multicomputos"
echo "✅ Todos los recursos de OCI han sido liberados"
echo ""
echo "Próximos pasos:"
echo "  1. Verifica en OCI Console que no queden recursos residuales"
echo "  2. Los datos descargados se encuentran en: data_multicomputos_backup/"
echo ""
