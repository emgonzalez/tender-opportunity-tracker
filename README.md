# tender-opportunity-tracker

Agente automatizado que monitorea el portal de compras públicas de El Salvador
([COMPRASAL](https://www.comprasal.gob.sv)) para identificar licitaciones tecnológicas
relevantes y notificar a cada cliente con un reporte personalizado.

## Qué hace

1. Se autentica en COMPRASAL (email + contraseña + OTP, manual o automático)
2. Extrae los procesos de compra publicados en las últimas 24 horas
3. Para cada cliente activo en `clients/`:
   - **Clasifica semánticamente** los procesos con Claude Haiku 4.5 (Anthropic API)
   - **Scoring multidimensional**: evalúa match técnico, match de categoría y factibilidad comercial
   - **Tags dinámicos**: etiqueta cada proceso con sub-categorías granulares (ej. "Almacenamiento", "Flash", "HCI")
   - **Recomendación de marcas**: sugiere qué marca del portafolio usar para ofertar (ej. HPE, Fortinet, PureStorage)
   - Envía un reporte HTML personalizado por email a los destinatarios del cliente
4. **Auto-restaura la sesión** si expira (hasta 3 reintentos con TOTP); solo notifica si falla
5. Se ejecuta automáticamente a las 7:00 AM (hora El Salvador), todos los días
6. Ejecuta una pasada inmediata al arrancar el contenedor si la del día no se ha ejecutado

Es **multi-cliente**: una sola instancia extrae los procesos una vez y genera reportes
independientes para cada cliente configurado en `clients/*.yaml`.

## Requisitos previos

- [Docker](https://docs.docker.com/get-docker/) y [Docker Compose](https://docs.docker.com/compose/install/)
- Una cuenta activa en COMPRASAL con 2FA habilitado
- API Key de Anthropic ([console.anthropic.com](https://console.anthropic.com))
- Cuenta Gmail con [App Password](https://support.google.com/accounts/answer/185833) habilitado

## Setup y arranque

### 1. Clonar el repositorio

```bash
git clone <repo-url>
cd tender-opportunity-tracker
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
```

Editar `.env` con las credenciales reales:

```dotenv
COMPRASAL_EMAIL=tu_email@empresa.com
COMPRASAL_PASSWORD=tu_password

# TOTP automático (opcional — ver sección TOTP más abajo)
COMPRASAL_TOTP_SECRET=

ANTHROPIC_API_KEY=sk-ant-api03-...

SMTP_USER=tu_email@gmail.com
SMTP_PASSWORD=tu_app_password_de_gmail
NOTIFICATION_FROM=tender-opportunity-tracker <tu_email@gmail.com>
```

> Los destinatarios de cada cliente se configuran en `clients/{cliente}.yaml`, no en `.env`.

### 3. Configurar perfiles de cliente

Cada cliente se define en un archivo YAML dentro de `clients/`. Copiar el ejemplo incluido:

```bash
cp clients/multicomputos.yaml clients/mi_cliente.yaml
```

Editar el YAML con el catálogo, marcas, reglas y notificación del cliente:

```yaml
client:
  id: mi_cliente
  name: Mi Empresa S.A.
  description: Integrador de soluciones TI especializado en...
  active: true

catalog:
  categories:
    - id: infraestructura
      label: Infraestructura
      items:
        - Servidores rack (HPE ProLiant)
      brands:
        - id: hpe
          name: HPE
          products: [servidores ProLiant, almacenamiento]

  tag_taxonomy:
    - Servidores
    - Almacenamiento
    - Firewall

  inclusion_signals:
    - servidor, storage, firewall
  exclusion_signals:
    - alimentos, agrícola

rules:
  context_note: Notas sobre el mercado local.
  categories: infraestructura, ciberseguridad, otro

notification:
  email_to:
    - contacto@miempresa.com
  report_title: Reporte de Licitaciones
  subject_prefix: "[TOT | Mi Empresa]"
  greeting: Estimado equipo,
  intro_message: |
    A continuación las licitaciones detectadas en COMPRASAL.
```

Ver `clients/multicomputos.yaml` como referencia completa (18 marcas, 44 tags, señales de inclusión/exclusión, inteligencia histórica).

Para desactivar un cliente sin borrar su archivo: `client.active: false`.

### 4. Ejecutar el script de arranque

```bash
./start.sh
```

El script ejecuta en orden:
1. Verifica que Docker esté corriendo
2. Valida el `.env` y las variables requeridas
3. Detecta clientes activos en `clients/`
4. Construye la imagen Docker
5. Realiza el login en COMPRASAL si no hay sesión guardada:
   - Con TOTP configurado: genera el OTP automáticamente
   - Sin TOTP: solicita el código al operador por pantalla
6. Levanta el contenedor en segundo plano
7. Muestra los logs en tiempo real (Ctrl+C no detiene el agente)

**Comandos útiles:**

```bash
docker compose logs -f amalia   # logs en vivo
docker compose down                                  # detener el agente
docker compose ps                                    # ver estado
```

---

## Motor de clasificación inteligente

El clasificador utiliza Claude Haiku 4.5 en una sola llamada batch por ejecución y produce para cada proceso:

### Scoring multidimensional

Tres sub-scores entre 0.0 y 1.0:

| Score | Descripción |
|---|---|
| **match_tecnico** | Coincidencia entre requerimientos técnicos y marcas/productos del portafolio |
| **match_categoria** | Encaje de la categoría del proceso con las áreas de negocio |
| **factibilidad** | Viabilidad comercial general (combinación de los anteriores + capacidad competitiva) |

Umbrales de decisión (basados en factibilidad):
- 0.85–1.0: Claramente relevante
- 0.70–0.84: Probable relevante
- 0.50–0.69: Dudoso (revisión manual)
- 0.00–0.49: No relevante

### Tags dinámicos

Cada proceso recibe entre 1 y 4 tags de una taxonomía definida en el YAML del cliente (`catalog.tag_taxonomy`). Ejemplos:

- `Servidores`, `Almacenamiento`, `Flash` — para una licitación de data center
- `Firewall`, `SOC`, `IAM` — para una licitación de ciberseguridad
- `Cloud`, `OCI`, `Migración` — para un proyecto de nube

### Recomendación de marcas

Para cada proceso relevante, el clasificador sugiere qué marca(s) del portafolio son las más adecuadas para ofertar, con una justificación breve. Ejemplo:

- **HPE** — Requiere servidores rack de alto rendimiento
- **PureStorage** — Almacenamiento all-flash mencionado explícitamente

Las marcas disponibles se configuran por categoría en el YAML del cliente bajo `catalog.categories[].brands`.

### Portafolio de Multicómputos (configuración incluida)

El perfil `clients/multicomputos.yaml` incluye el portafolio real con 18 marcas en 8 categorías:

| Categoría | Marcas |
|---|---|
| Infraestructura | HPE, Nutanix, PureStorage, Supermicro |
| Cloud y Bases de Datos | Oracle, Oracle NetSuite |
| Virtualización y VDI | Citrix, Ncomputing |
| Networking | Aruba |
| Ciberseguridad | Fortinet, Imperva, Cyrebro, Okta, Onespan, Teramind |
| Monitoreo y Observabilidad | Dynatrace |
| Protección de Datos | Veritas, Exagrid |
| Servicios Profesionales TI | *(sin marca específica)* |

---

## TOTP automático

Si dispones del secret TOTP de la cuenta COMPRASAL, el agente puede generar los códigos OTP
automáticamente sin intervención humana:

1. Obtener el secret base32 de tu app de autenticación (Aegis en Android permite exportarlo)
2. Agregarlo al `.env`:
   ```dotenv
   COMPRASAL_TOTP_SECRET=JBSWY3DPEHPK3PXP   # (ejemplo)
   ```
3. Reiniciar el contenedor:
   ```bash
   docker compose restart amalia
   ```

Con esto activado, el login inicial y las renovaciones de sesión son completamente automáticos.
Si la sesión expira durante la operación normal, el agente intenta restaurarla automáticamente
hasta 3 veces (con 10 segundos entre intentos). Solo envía el email de alerta si los 3 intentos fallan.
Sin TOTP configurado, el agente no puede auto-restaurar y envía la alerta inmediatamente.

---

## Comandos manuales

### Ejecutar el pipeline manualmente

Ejecuta una pasada completa del pipeline (extracción + clasificación + envío de reportes) sin iniciar el scheduler:

```bash
# Desde Docker
docker compose run --rm amalia python -m src.main --run-now

# Desde el host (requiere dependencias Python instaladas)
python -m src.main --run-now
```

### Renovar sesión de COMPRASAL

Las sesiones de COMPRASAL expiran periódicamente. Con TOTP habilitado, el agente intenta
restaurar la sesión automáticamente (3 intentos, 10s entre cada uno) y solo envía alerta
por email si todos los intentos fallan. Sin TOTP, envía la alerta inmediatamente.
Para renovar manualmente:

```bash
# Con TOTP automático (no requiere intervención):
docker compose run --rm amalia python -m src.auth.login

# Sin TOTP (requiere código OTP manual):
echo "CODIGO_OTP" | docker compose run -T --rm amalia python -m src.auth.login --manual
```

El agente retoma su operación normal en la siguiente ejecución programada.

### Reenviar el último reporte

Reenvía el último reporte enviado sin volver a consultar COMPRASAL ni llamar a la API de clasificación.
El reporte se reconstruye desde el historial (`data/history/{client_id}.jsonl`) y conserva
el timestamp original de la ejecución.

```bash
# Todos los clientes activos
docker compose run --rm amalia python -m src.notifier.resend

# Cliente específico
docker compose run --rm amalia python -m src.notifier.resend --client multicomputos
```

### Generar perfil de inteligencia histórica (Trainer)

El Trainer analiza el historial de participación del cliente en COMPRASAL con Claude Haiku
y genera un perfil de inteligencia que mejora la precisión del clasificador.

**Se ejecuta una sola vez por cliente** (o cuando cambie significativamente su catálogo/portafolio).

```bash
# Ver cuántos procesos históricos existen sin generar nada (dry-run)
docker compose -f docker-compose.yml -f docker-compose.trainer.yml run --rm amalia \
  python -m src.classifier.trainer --client multicomputos --dry-run

# Generar el perfil de inteligencia y guardarlo en clients/multicomputos.yaml
docker compose -f docker-compose.yml -f docker-compose.trainer.yml run --rm amalia \
  python -m src.classifier.trainer --client multicomputos

# Limitar páginas extraídas (útil para pruebas)
docker compose -f docker-compose.yml -f docker-compose.trainer.yml run --rm amalia \
  python -m src.classifier.trainer --client multicomputos --max-pages 3
```

El perfil generado se guarda en `clients/{cliente}.yaml` bajo la clave `intelligence`
y es usado automáticamente en cada clasificación. No editar esta sección manualmente.

Costo estimado para 200 procesos históricos: < USD 0.05 (usa Claude Haiku).

### Exportar todos los procesos de COMPRASAL

Exporta TODOS los procesos publicados actualmente en COMPRASAL (sin filtro de fecha) a CSV, JSON o ambos.

```bash
# Exportar a CSV (default)
docker compose run --rm amalia python -m src.scraper.export

# Exportar a JSON
docker compose run --rm amalia python -m src.scraper.export --format json

# Exportar a ambos formatos
docker compose run --rm amalia python -m src.scraper.export --format both
```

Los archivos quedan en `data/export/` dentro del volumen del contenedor. Para copiarlos al host:

```bash
docker cp tender-opportunity-tracker:/app/data/export/. ./data/export/
```

### Ejecutar tests

```bash
# Desde el host (requiere dependencias Python instaladas)
python -m pytest tests/ -v

# Desde Docker
docker compose run --rm amalia python -m pytest tests/ -v
```

---

## Estructura del proyecto

```
tender-opportunity-tracker/
├── start.sh                     # Script de arranque completo (punto de entrada del admin)
├── docker-compose.yml           # Orquestación del contenedor principal
├── docker-compose.trainer.yml   # Override para ejecutar el Trainer
├── Dockerfile                   # Imagen con Playwright y Chromium
├── .env.example                 # Template de variables de entorno
├── pyproject.toml               # Dependencias, pytest y ruff
├── clients/                     # Un YAML por cliente (fuente de verdad)
│   └── multicomputos.yaml       # Perfil de Multicómputos (18 marcas, 44 tags)
├── src/
│   ├── main.py                  # Entry point: orquesta el pipeline multi-cliente
│   ├── config.py                # Configuración desde variables de entorno
│   ├── auth/
│   │   ├── login.py             # Login interactivo en COMPRASAL
│   │   ├── session.py           # Validación de sesión (storage_state.json)
│   │   └── totp.py              # Generación automática de OTP con pyotp
│   ├── scraper/
│   │   ├── extractor.py         # Extracción de procesos de COMPRASAL
│   │   ├── export.py            # Exportador masivo a CSV/JSON
│   │   └── models.py            # Dataclasses: Proceso, ProcesoClasificado, MarcaRecomendada
│   ├── classifier/
│   │   ├── client_profile.py    # Carga perfiles desde clients/*.yaml (Brand, Catalog, etc.)
│   │   ├── engine.py            # Clasificación batch con Claude Haiku 4.5 (scoring + tags + marcas)
│   │   ├── rules.py             # Construcción dinámica de prompts por perfil de cliente
│   │   └── trainer.py           # Generador de inteligencia histórica (Claude Haiku)
│   ├── history.py               # Persistencia de resultados por cliente (JSONL)
│   ├── notifier/
│   │   ├── email_sender.py      # Reportes HTML por SMTP (reporte, alertas)
│   │   └── resend.py            # CLI para reenviar el último reporte bajo demanda
│   ├── scheduler/
│   │   └── cron.py              # Scheduler interno + catch-up (en OCI manda el systemd timer)
│   ├── drilldown/               # [Fase 4 · flag ENABLE_DRILLDOWN] análisis del pliego
│   │   ├── locator.py           # Localiza la URL del PDF en la página del proceso
│   │   ├── downloader.py        # Descarga en streaming con guardia de tamaño
│   │   ├── pdf.py               # PyMuPDF memory-safe (mmap) + detección de escaneados
│   │   ├── prefilter.py         # Pre-filtro RegEx contra las señales del cliente
│   │   ├── triage.py            # Triaje GO/NO-GO (Claude Haiku 4.5)
│   │   ├── analysis.py          # Matriz de riesgos (Claude Sonnet 5)
│   │   ├── quota.py             # Guardia de presupuesto diario de tokens
│   │   └── pipeline.py          # Orquestación en dos fases (descarga / análisis)
│   └── memoria/                 # [Fase 4 · flag ENABLE_MEMORIA] memoria institucional
│       ├── db.py                # Conexión a Oracle ADB (thin) + wallet en tmpfs
│       ├── repo.py              # Metadatos, estado y cola durable (SKIP LOCKED)
│       ├── chunker.py           # Troceado con solape
│       ├── vectorizer.py        # Embeddings calculados DENTRO de la BD (ONNX)
│       ├── rag.py               # Búsqueda semántica + precedentes inter-documento
│       ├── service.py           # Fachada: no-op si el flag está off; degrada si ADB falla
│       └── objstore.py          # OCI Object Storage (handoff entre etapas)
├── db/                          # [Fase 4] schema.sql, load_onnx.sql, index_ivf.sql, retention.sql
├── templates/
│   └── report.html              # Template Jinja2 (score bars, tags, marcas, matriz, precedentes)
├── data/                        # [GENERADO]
│   ├── history/
│   │   └── {client_id}.jsonl    # Historial de ejecuciones por cliente
│   ├── export/                  # Exportaciones CSV/JSON de COMPRASAL
│   ├── screenshots/             # Capturas del login/scraping
│   ├── debug/                   # HTML de páginas capturadas
│   └── last_run.json            # Metadata de la última ejecución
└── tests/                       # Tests unitarios (ver `pytest tests/`)
```

---

## Perfil de cliente (YAML)

Cada archivo `clients/{id}.yaml` define la configuración completa de un cliente:

```yaml
client:
  id: mi_cliente            # Identificador único
  name: Mi Empresa S.A.     # Nombre para mostrar en reportes
  description: ...          # Descripción del negocio (inyectada en el prompt)
  active: true              # false para desactivar sin borrar

catalog:
  categories:               # Categorías del portafolio
    - id: infraestructura
      label: Infraestructura
      items:                 # Productos/servicios en esta categoría
        - Servidores rack (HPE)
      brands:                # Marcas asociadas a esta categoría
        - id: hpe
          name: HPE
          products: [servidores ProLiant, almacenamiento]

  tag_taxonomy:              # Tags válidos para clasificación granular
    - Servidores
    - Almacenamiento

  inclusion_signals:         # Palabras clave que indican relevancia
    - servidor, storage, HPE
  exclusion_signals:         # Palabras clave que indican no-relevancia
    - alimentos, agrícola

rules:
  context_note: ...          # Contexto del mercado local
  categories: ...            # Categorías válidas de respuesta (comma-separated)

classification:
  relevance_threshold: 0.6   # Umbral mínimo de factibilidad para reportar
  confidence_threshold: 0.7  # Umbral para sección "revisión manual"

notification:
  email_to: [...]            # Lista de destinatarios
  report_title: ...          # Título del reporte
  subject_prefix: ...        # Prefijo del asunto del email
  greeting: ...              # Saludo personalizado
  intro_message: ...         # Mensaje introductorio

intelligence:                # [AUTO-GENERADO por el Trainer]
  generated_at: ...
  processes_analyzed: 46
  profile: ...               # Perfil de inteligencia (~500 palabras)
```

---

## Variables de entorno

| Variable | Requerida | Descripción |
|---|---|---|
| `COMPRASAL_EMAIL` | Sí | Email de la cuenta COMPRASAL |
| `COMPRASAL_PASSWORD` | Sí | Contraseña de la cuenta COMPRASAL |
| `COMPRASAL_TOTP_SECRET` | No | Secret TOTP para OTP automático |
| `ANTHROPIC_API_KEY` | Sí | API Key de Anthropic |
| `SMTP_USER` | Sí | Email remitente (Gmail) |
| `SMTP_PASSWORD` | Sí | App Password de Gmail |
| `NOTIFICATION_FROM` | No | Nombre y email del remitente (default: `SMTP_USER`) |
| `SMTP_HOST` | No | Servidor SMTP (default: `smtp.gmail.com`) |
| `SMTP_PORT` | No | Puerto SMTP (default: `587`) |
| `SCHEDULE_MORNING` | No | Hora ejecución matutina UTC-6 (default: `07:00`) |
| `SCHEDULE_AFTERNOON` | No | ⚠️ **Sin efecto**: definida en `config.py` pero `scheduler/cron.py` solo programa `SCHEDULE_MORNING` |
| `DAYS_LOOKBACK` | No | Días hacia atrás a revisar (default: `1`) |
| `APP_NAME` | No | Nombre del sistema en emails/logs (default: `tender-opportunity-tracker`) |
| `APP_FULL_NAME` | No | Nombre completo del sistema (default: `Tender Opportunity Tracker`) |
| `LOG_LEVEL` | No | Nivel de logging (default: `INFO`) |
| `HEADLESS` | No | Chromium sin interfaz (default: `true`) |

---

## Troubleshooting

### "No se encontraron elementos" en el extractor

COMPRASAL es una SPA. Los selectores DOM pueden cambiar con actualizaciones del portal.

1. Revisar los archivos HTML en `data/debug/` (se guardan en cada primera ejecución de página)
2. Abrir el HTML en un navegador e inspeccionar la estructura de la tabla
3. Actualizar los selectores en `src/scraper/extractor.py`

### El contenedor crashea con "out of memory"

Chromium headless consume bastante RAM. Verificar:
- `docker stats tender-opportunity-tracker` para ver el uso de memoria
- El límite de 2GB en `docker-compose.yml` puede ajustarse con `mem_limit`
- `ipc: host` debe estar habilitado en docker-compose para evitar crashes de Chromium

### Errores de SMTP / "Authentication failed"

- Usar un **App Password** de Gmail (no la contraseña normal de la cuenta)
- La autenticación en 2 pasos debe estar habilitada en la cuenta Gmail
- Instrucciones: [Crear App Password de Google](https://support.google.com/accounts/answer/185833)

### Circuit breaker activado

Si el agente falla 3 veces consecutivas, envía un email de alerta a todos los clientes activos
y sigue intentando en las siguientes ejecuciones programadas. Revisar los logs para
identificar la causa raíz:

```bash
docker compose logs -f amalia
```

### No hay clientes activos

Si el log muestra `No hay clientes activos`, verificar que:
- Existe al menos un archivo `.yaml` en `clients/`
- El campo `client.active: true` está configurado en el YAML

---

*tender-opportunity-tracker es un proyecto interno de Multicómputos El Salvador.*
