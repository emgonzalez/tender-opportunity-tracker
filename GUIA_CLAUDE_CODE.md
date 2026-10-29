# Guía de Claude Code — tender-opportunity-tracker

Esta guía documenta cómo trabajar eficientemente en este proyecto con Claude Code.

---

## 1. Antes de Empezar

### Setup Local (Una sola vez)

```bash
# 1. Clonar repo
git clone <repo-url>
cd tender-opportunity-tracker

# 2. Copiar y configurar .env
cp .env.example .env
# Editar .env con:
# - COMPRASAL_EMAIL / PASSWORD
# - ANTHROPIC_API_KEY
# - SMTP_USER / SMTP_PASSWORD
# - COMPRASAL_TOTP_SECRET (opcional, pero recomendado)

# 3. Instalar Docker Desktop (si no lo tienes)
# https://www.docker.com/products/docker-desktop

# 4. Listo para trabajar
```

### Requisitos Previos

| Herramienta | Versión | Propósito |
|-------------|---------|----------|
| Python | 3.11+ | Runtime |
| Docker Desktop | Latest | Containerización |
| Git | 2.30+ | Control de versiones |
| GitHub CLI (`gh`) | 2.0+ | Operaciones GitHub desde terminal |
| Claude Code | Latest | Este asistente |

---

## 2. Flujo Diario

### A. Iniciar Sesión y Cargar Contexto

```
Tu acción: Abre Claude Code en la raíz del proyecto
Claude code: Lee automáticamente CLAUDE.md y entiende arquitectura/patrones
```

No hay comando de login especial: solo necesitas que Claude Code esté abierto en la raíz del proyecto.

### B. Levantar Entorno Local

Opción 1: Docker Compose directo
```bash
docker compose up -d
```

Opción 2: Con el script helper
```bash
./start.sh
```

Verifica que todo está corriendo:
```bash
docker compose ps           # Ver estado de servicios
docker compose logs -f      # Ver logs en vivo
```

### C. Trabajar en una Tarea

Ejemplo: Agregar soporte para un nuevo cliente

```bash
# 1. Crear branch
git checkout -b feature/nuevo-cliente

# 2. Editar cliente YAML
# - clients/nuevo_cliente.yaml (copiar desde multicomputos_sv.yaml)
# - Personalizar categorías, marcas, tags, reglas

# 3. Testear localmente
docker compose exec amalia python -m src.main

# 4. Ver logs
docker compose logs -f amalia

# 5. Verificar email recibido (o en logs)

# 6. Hacer commit
git add clients/nuevo_cliente.yaml
git commit -m "feat(clients): agregar nuevo cliente"

# 7. Hacer push
git push origin feature/nuevo-cliente

# 8. Crear PR en GitHub (opcional)
gh pr create --title "feat(clients): agregar nuevo cliente"
```

### D. Al Terminar la Sesión

```bash
docker compose down          # Detener todo
git status                   # Verificar cambios
git push                     # Push final
```

---

## 3. Tareas Comunes

### 3A. Agregar un Nuevo Cliente

```bash
# 1. Copiar plantilla
cp clients/multicomputos_sv.yaml clients/mi_cliente.yaml

# 2. Editar el YAML: cambiar ID, nombre, categorías, marcas, tags, email
nano clients/mi_cliente.yaml

# 3. Validar YAML (Python)
python -c "import yaml; yaml.safe_load(open('clients/mi_cliente.yaml'))"

# 4. Testear
docker compose up -d
docker compose exec amalia python -m src.main

# 5. Verificar logs
docker compose logs -f

# 6. Commit y push
git add clients/mi_cliente.yaml
git commit -m "feat(clients): agregar cliente [nombre]"
git push
```

### 3B. Cambiar Credenciales SMTP o Anthropic

```bash
# 1. Editar .env (NUNCA commitear .env, solo .env.example)
nano .env

# 2. Testear credenciales (ejecuta el clasificador)
docker compose run -T --rm amalia python -c \
  "from src.config import Config; c = Config(); print('OK')"

# 3. Si hay error, verificar .env
```

**Importante:** `.env` está en `.gitignore`. Nunca commitees credenciales.

### 3C. Arreglar Problema de Sesión Expirada

Si recibes un email diciendo "Sesión expirada":

```bash
# Opción 1: Si tienes TOTP configurado
# El agent automáticamente reintenta 3 veces — no necesitas hacer nada

# Opción 2: Si NO tienes TOTP, login manual
docker compose run -T --rm amalia \
  python -m src.auth.login --manual
# Te pedirá que ingreses el OTP de tu 2FA manualmente

# Opción 3: Si aún falla
docker compose logs -f amalia  # Ver qué pasó
# Luego reportar el error con el log completo
```

### 3D. Ejecutar una Pasada Manual (Debugging)

```bash
# Ejecutar el agente una sola vez (útil para testing)
docker compose up -d                 # Levanta servicios
docker compose run -T --rm amalia python -m src.main
docker compose logs -f               # Ver output

# O si quieres ver output en vivo
docker compose exec amalia python -m src.main
```

### 3E. Cambiar la Hora del Timer en OCI

Si quieres que se ejecute a las 8:00 AM en lugar de 7:00 AM:

```bash
# 1. Editar oci/tenants.yaml
nano oci/tenants.yaml

# 2. Cambiar schedule_time
# emiliogonzalez:
#   ...
#   instance:
#     schedule_time: "08:00"     # ← Cambiar aquí

# 3. Re-desplegar (O esperar al próximo deploy)
# ./oci/deploy.sh emiliogonzalez   # Vuelve a provisionar
```

**Nota:** Si el VM ya está en OCI, necesitas acceder por SSH y editar el systemd timer manualmente.

### 3F. Agregar un Nuevo Tag (Etiqueta) a un Cliente

```bash
# 1. Editar clients/[cliente].yaml

# Antes:
# catalog:
#   tag_taxonomy:
#     - Servidores
#     - Almacenamiento

# Después:
# catalog:
#   tag_taxonomy:
#     - Servidores
#     - Almacenamiento
#     - Nuevos_Tags_Aqui    # ← Agregar aquí

# 2. El clasificador usará automáticamente el nuevo tag en la próxima ejecución

# 3. Commit
git add clients/[cliente].yaml
git commit -m "feat(clients): agregar tag [nombre] a cliente [cliente]"
git push
```

### 3G. Probar el Clasificador Localmente (sin Email)

```bash
# 1. Editar temporalmente src/main.py si quieres skipear email
# O simplemente ejecutar el clasificador directamente:

docker compose run -T --rm amalia python -c "
from datetime import datetime
import dataclasses, json

from src.config import get_config
from src.classifier.engine import classify_procesos
from src.classifier.client_profile import load_all_active_profiles
from src.scraper.models import Proceso

config = get_config()

# Proceso de prueba (campos reales de la dataclass: no existe 'uuid' ni 'titulo')
procesos = [
    Proceso(
        institucion='Ministerio de Salud',
        descripcion='Adquisición de servidor rack con 2 procesadores Intel Xeon',
        fecha_publicacion=datetime.now(),
        url_directa='https://www.comprasal.gob.sv/pac-procesos/etapas/0',
        numero_referencia='TEST-0001',
    ),
]

# Se clasifica POR CLIENTE: classify_procesos(procesos, config, client_profile)
for profile in load_all_active_profiles(config.clients_dir):
    clasificados = classify_procesos(procesos, config, profile)
    print(f'--- {profile.client_id} ---')
    print(json.dumps([dataclasses.asdict(p) for p in clasificados],
                     indent=2, ensure_ascii=False, default=str))
"
```

---

## 4. Debugging

### Logs principales

```bash
# Logs en tiempo real
docker compose logs -f amalia

# Últimas 100 líneas de logs
docker compose logs --tail=100

# Logs desde hace 10 minutos
docker compose logs --since 10m
```

### Problemas comunes y soluciones

| Problema | Síntoma | Solución |
|----------|---------|----------|
| **Sesión expirada** | Email: "Sesión de COMPRASAL expirada" | Ver sección 3C |
| **SMTP auth falló** | `SMTPAuthenticationError` en logs | Verificar SMTP_PASSWORD en .env (app password, no password principal) |
| **API Anthropic falla** | `RateLimitError` o `AuthenticationError` | Verificar ANTHROPIC_API_KEY válida |
| **Browser timeout** | `TimeoutError: waiting for locator` | COMPRASAL está lento o layout cambió — revisar browser.py |
| **Email no llega** | Sin error en logs, pero email no recibido | Revisar carpeta Spam, verificar email en config cliente |
| **Proceso sin fecha** | `fecha_publicacion=null` — no crashea pero procesos incompletos | Esperado — algunos procesos en COMPRASAL no tienen fecha |

### Acceder al Contenedor para Debugging

```bash
# Abrir shell dentro del contenedor
docker compose exec amalia bash

# Ahora puedes:
python -c "from src.config import Config; print(Config().anthropic_api_key)"
python -m src.scraper.browser                     # Test login
python -m src.main                                # Ejecutar agente
python -c "import src.history; ..."               # Debuguear history
cat /app/data/last_run.json                       # Ver estado
```

---

## 5. Convenciones del Proyecto

### Naming

| Entidad | Convención | Ejemplo |
|---------|-----------|---------|
| Python files | `snake_case` | `browser.py`, `email_sender.py` |
| Python classes | `PascalCase` | `Proceso`, `ClientProfile`, `Config` |
| Python functions | `snake_case` | `perform_login()`, `send_report()` |
| YAML client files | `snake_case` | `multicomputos_sv.yaml`, `nuevo_cliente.yaml` |
| Git branches | `type/description` | `feature/e2-micro-fallback`, `fix/session-expired` |
| Commits | `type(scope): message` | `feat(oci): ...`, `fix(scraper): ...` |

### Tipos de Commits

```
feat:   Nueva característica (cliente, tag, funcionalidad)
fix:    Corrección de bug
chore:  Cambios sin impact funcional (deps, config)
docs:   Documentación
refactor: Reescritura sin cambio de comportamiento
perf:   Optimización de performance
test:   Tests (cuando haya suite)
```

### Estructura de Directorios

**No modificar:**
- `src/` — Lógica principal (cambios deben ser muy cuidadosos)
- `oci/` — Provisioning (solo si entiendes infraestructura)
- `docker-compose*.yml` — Config de contenedores

**Seguro modificar:**
- `clients/` — Agregar/editar perfiles de clientes
- `.env` — Credenciales locales (nunca commitear)
- `templates/` — Plantillas de email
- `data/` — Estado persistente (generado automáticamente)
- `CONTRIBUTING.md`, `README.md` — Docs

---

## 6. Flujo de Git

### Crear una rama de feature

```bash
git checkout -b feature/tu-feature-aqui
# Edita, testea, etc.
git add [archivos]
git commit -m "feat(scope): descripción"
git push origin feature/tu-feature-aqui
```

### Abrir un Pull Request

```bash
gh pr create --title "feat(scope): descripción" \
  --body "Detalles de qué cambió y por qué"
```

### Mergear a develop/main

```bash
# Localmente, si todo pasó QA:
git checkout develop
git pull origin develop
git merge --no-ff feature/tu-feature  # Preserva historial
git push origin develop

# O abrir PR y mergear desde GitHub UI
```

---

## 7. Monitoreo de OCI (Si usas)

### Verificar que el timer está corriendo

```bash
# SSH a la VM en OCI
ssh -i ~/.oci/tu-key.key ubuntu@INSTANCE_IP

# Dentro de la VM:
systemctl status amalia.timer
journalctl -u amalia.timer -n 50
journalctl -u amalia.service --since "2 hours ago"
```

### Ver logs de Docker en OCI

```bash
ssh -i ~/.oci/tu-key.key ubuntu@INSTANCE_IP
docker ps -a
docker logs [container-id] --tail=100
```

### Si el timer no se ejecutó

```bash
# En la VM:
systemctl enable amalia.timer
systemctl start amalia.timer

# Forzar una ejecución manual:
systemctl start amalia.service
```

---

## 8. Referencia Rápida

### Comandos Docker

```bash
docker compose up -d                    # Levanta todo en background
docker compose down                     # Detiene todo
docker compose logs -f                  # Logs en vivo
docker compose ps                       # Ver estado
docker compose exec [service] bash      # Shell dentro del contenedor
docker compose run -T --rm [service] [comando]  # Ejecutar comando
```

### Comandos Git

```bash
git status                              # Ver cambios
git checkout -b feature/[desc]          # Crear rama
git add [archivos]                      # Stage cambios
git commit -m "[type][scope]: msg"      # Commitear
git push origin [rama]                  # Push
gh pr create                            # Abrir PR
```

### Comandos Útiles para Debugging

```bash
# Ver último run
cat data/last_run.json

# Ver estado de procesos vistos
cat data/storage_state.json

# Ejecutar prueba de login
docker compose run -T --rm amalia \
  python -m src.scraper.browser

# Testear SMTP
docker compose run -T --rm amalia \
  python -c "from src.notifier.email_sender import _send; ..."

# Testear Claude API
docker compose run -T --rm amalia \
  python -c "from src.config import Config; print(Config().anthropic_api_key)"
```

---

## 9. Tips y Mejores Prácticas

1. **Siempre testea localmente antes de push**
   ```bash
   docker compose up -d
   docker compose run -T --rm amalia python -m src.main
   docker compose logs -f
   ```

2. **Nunca commitees `.env`** — usa `.env.example` como template

3. **Lee los logs primero cuando algo falla**
   ```bash
   docker compose logs -f amalia | tail -50
   ```

4. **Usa branches feature, no trabajes en main**
   ```bash
   git checkout develop      # Siempre branching desde develop
   git checkout -b feature/tu-cosa
   ```

5. **Para cambios en clientes, solo edita `clients/` YAML**
   — No necesitas recompilar nada, cambios se aplican en la próxima ejecución

6. **Si cambias código en `src/`, testea mucho**
   — La lógica del scraper/classifier es crítica

---

## 10. Resumen de Responsabilidades

| Si necesitas... | Haz esto... |
|-----------------|-----------|
| Agregar cliente | Edita `clients/mi_cliente.yaml` |
| Cambiar credenciales | Edita `.env` (nunca commitear) |
| Arreglar un bug | Crea branch `fix/[desc]`, edita código, testea, push, PR |
| Mejorar clasificador | Edita `src/classifier/engine.py` (prompts en `rules.py`), testea batch |
| Cambiar hora timer en OCI | Edita `oci/tenants.yaml` `schedule_time`, re-despliega |
| Ver por qué falló una ejecución | `docker compose logs -f` + busca `ERROR` |
| Forzar una ejecución manual | `docker compose run -T --rm amalia python -m src.main` |
