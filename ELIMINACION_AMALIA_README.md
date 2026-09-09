# Eliminación de AMALIA en Tenant Multicomputos

## Estado Actual

❌ **Problema:** Conectividad de red a OCI endpoints bloqueada ("Network is unreachable")

Este documento guía la eliminación completa de AMALIA en el tenant multicomputos de OCI cuando se restaure la conectividad.

## Problema de Conectividad Diagnosticado

```
Error: Network is unreachable a 140.91.12.32:443 (OCI endpoint)
```

### Posibles Causas

1. **Firewall corporativo** bloqueando acceso a OCI
2. **VPN activa** con restricciones de split-tunneling
3. **ISP** bloqueando rango de IPs de OCI
4. **Configuración local** de firewall/proxy

## Solución Paso a Paso

### Paso 1: Ejecutar Diagnóstico de Conectividad

```bash
cd /Users/emiliogonzalez/Documents/MULTICOMPUTOS/POCs/tender-opportunity-tracker

# Ejecutar diagnóstico
./network_diagnostics.sh
```

El script verificará:
- ✅ Conexión básica a Internet
- ✅ Resolución DNS
- ✅ Routing a servidores OCI
- ✅ Conectividad puerto 443
- ✅ Credenciales OCI CLI

**Resultado esperado:** Todos los checks en verde (✅)

### Paso 2: Resolver Problemas de Conectividad

Si el diagnóstico falla, intenta:

#### Opción A: Si Estás en VPN Corporativa
```bash
# Desconecta la VPN temporalmente
# Intenta el diagnóstico de nuevo
./network_diagnostics.sh

# Si funciona, solicita al admin acceso a OCI desde VPN
```

#### Opción B: Si Estás en macOS
```bash
# Verifica el firewall
System Preferences > Security & Privacy > Firewall

# Desactiva temporalmente y prueba:
./network_diagnostics.sh
```

#### Opción C: Si el Problema Persiste
- Contacta a tu ISP (puede estar bloqueando OCI)
- Intenta desde otra red (hotspot móvil, otra oficina)
- Verifica proxies en: `~/.oci/config`

### Paso 3: Ejecutar Eliminación de AMALIA

Una vez que el diagnóstico pase (todos los checks en verde):

```bash
# Ejecutar script de eliminación
./delete_amalia_multicomputos.sh
```

**El script eliminará:**
- ✅ Instancia OCI (tender-opportunity-tracker-prod)
- ✅ Boot volume
- ✅ VCN (Virtual Cloud Network)
- ✅ Subnets
- ✅ Internet Gateway
- ✅ Security Lists
- ✅ Route Tables
- ✅ Todos los datos asociados

**Tiempo estimado:** 10-15 minutos

### Paso 4: Verificar Completitud

Después de que el script termine:

```bash
# Verificar en OCI Console
# https://console.oracle.com

# O via CLI:
oci --profile MULTICOMPUTOS compute instance list \
  --compartment-id ocid1.compartment.oc1..aaaaaaaaijzf7ypkrfgg3h2g5n2tetu5bgjpdufaebnloccv6oprxcn5tnba \
  --region us-ashburn-1 \
  --query 'data | length(@)'

# Esperado: 0 (sin instancias)
```

## Datos Respaldados

Los datos de AMALIA (74 días de ejecución) han sido descargados a:

```
/Users/emiliogonzalez/Documents/MULTICOMPUTOS/POCs/tender-opportunity-tracker/data_multicomputos_backup/
```

Contenido:
- `last_run.json` - Estado de última ejecución
- `history/multicomputos.jsonl` - Historial de procesos
- `storage_state.json` - Estado de sesión Playwright
- `debug/` - Logs de login (74 días)
- `screenshots/` - Capturas de pantalla

## Referencias

- **OCIDs de referencia:**
  - Instancia: `ocid1.instance.oc1.iad.anuwcljtoja5e3qcrhjxfhaxyzs4j3b6togg7vt26mnzw52agakkvr4thmdq`
  - Compartment: `ocid1.compartment.oc1..aaaaaaaaijzf7ypkrfgg3h2g5n2tetu5bgjpdufaebnloccv6oprxcn5tnba`
  - Profile OCI CLI: `MULTICOMPUTOS`
  - Región: `us-ashburn-1`

- **Credenciales almacenadas en:**
  - `~/.oci/config` (profile MULTICOMPUTOS)
  - `~/.oci/multicomputos.pem` (clave privada)

## ⚠️ IMPORTANTE

- **Esta operación es IRREVERSIBLE** — no hay undo después de la eliminación
- Los datos ya han sido respaldados en `data_multicomputos_backup/`
- La instancia OCI tiene IP pública 193.122.188.14 (se liberará)
- El tenant multicomputos quedará limpio después de esto

## Soporte

Si encuentras errores durante la eliminación:

1. Ejecuta diagnóstico de nuevo: `./network_diagnostics.sh`
2. Revisa logs de OCI CLI: `oci --version` y verifica credenciales
3. Intenta desde otra red si sospecha firewall/ISP
4. Los scripts logs completos aparecen en terminal (copia la salida si necesitas reportar)

---

**Próximos pasos tras eliminación completa:**
1. Verificar en OCI Console que no queden recursos
2. Documentar que multicomputos ha sido completamente desmantelado
3. Actualizar STATUS.md del proyecto
4. Cerrar cualquier issue/PR relacionada a AMALIA en multicomputos
