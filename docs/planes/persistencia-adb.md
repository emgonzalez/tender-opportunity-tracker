# Plan: persistencia en la ADB — la VM solo con lo estrictamente necesario

## Context
Hoy la VM guarda, además de lo que necesita para arrancar, configuración (`clients/*.yaml`) y estado mutable (`data/`: historial JSONL, `last_run.json`, `gasto_diario.json`, `llm_consumo.jsonl`, `storage_state.json`, diagnósticos). Eso ata el estado a un disco de 1 GB, obliga a que cada deploy vuelva a inyectar cosas, y recrear la VM pierde historial y la sesión de COMPRASAL. Meta: **la ADB es la fuente de verdad; la VM conserva solo lo irreducible y una caché mínima regenerable**, sin cambiar ningún comportamiento visible (reportes, portal, feedback, alertas, cuotas).

Decisiones ya tomadas por Emilio:
1. **Secretos se quedan en el `.env`** (SMTP, Anthropic, COMPRASAL/TOTP, PEM de GenAI, ADB_*, IDENTIDAD_SECRET, FEEDBACK_HMAC). Vault queda como fase futura (exige `manage dynamic-groups`, denegado hoy). **Única excepción: `storage_state.json` pasa a la ADB cifrado.**
2. **ADB caída a las 07:00 → la corrida sigue con una caché local** (perfiles de cliente + estado mínimo). Se conserva el contrato actual: un fallo de la ADB nunca impide que el reporte llegue.

## Qué se mueve y qué no (inventario verificado)
| Hoy en la VM | Destino | Notas |
|---|---|---|
| `clients/*.yaml` (rsync + mount `:ro`) | tabla `cliente_config` (ya existe, versionada, hoy **nadie la lee**) | Loader ADB-first + caché en `data/cache/` + YAML horneado en la imagen como último recurso. `trainer.save_intelligence` pasa a insertar versión nueva. |
| `history/{cid}.jsonl`, `.analisis.jsonl`, `.feedback.jsonl` | tablas `proceso`/`cliente_proceso`/`analisis`/`feedback`/`ejecucion` (ya existen) | JSONL pasa de «fuente» a **spool de salida** (solo se escribe si falla la ADB y se vacía en la siguiente corrida). |
| `last_run.json` | `ejecucion` (existe) | Escritura atómica en la caché; lectura ADB-first. |
| `gasto_diario.json` (cuota LLM) | tabla nueva `cuota_gasto` (DDL aditivo) con incremento atómico | **Nunca falla abierto**: si la ADB cae se usa la caché local. |
| `llm_consumo.jsonl` | `llm_llamada` (ya existe como espejo) | ADB primaria; JSONL solo como spool. `scripts/comparar_llm.py` lee la ADB. |
| `storage_state.json` | tabla nueva `sesion_portal_compras` (BLOB cifrado) | Cifrado con el cifrador de `src/identidad/mfa.py` (clave derivada de `IDENTIDAD_SECRET`). Se materializa en `/tmp` del contenedor (no en el volumen); si la ADB no responde, login nuevo con el TOTP del `.env`. |
| `screenshots/`, `debug/`, `login_debug.log`, `export/*.csv|json` | `login_debug.log` → stdout (journald); screenshots/debug → Object Storage (`objstore`, lifecycle 7 d ya sirve) o `/tmp` | Solo diagnóstico, sin impacto funcional. |
| `/tmp/amalia_pliegos`, `stage_handoff.json` | se queda local **solo si el tenant usa dos etapas** (verificar en Fase 0) | Handoff entre contenedores efímeros. |
| `templates/`, `static/`, `src/`, `clients/` copiados por rsync | **fuera de la VM**: ya van en la imagen | El rsync se reduce a compose + Caddyfile + scripts de `oci/lib` que corren en la VM. |
| `adb_keepalive.json` | **se queda** | Registra que la ADB está caída: no puede depender de ella. |

**Mínimo irreducible en la VM:** `.env` (secretos + ADB_DSN/USER/PASSWORD + flags), docker y `~/.docker/config.json` (login GHCR), un `docker-compose.<e2|a1>.yml`, `oci/caddy/Caddyfile` + volúmenes de certificados, units systemd (`amalia`, `pull-deploy`, `backup`, `failure`), scripts de backup/pull-deploy/watchdog, `data/` con la caché (`cache/clientes/`, `last_run.json`, `gasto_diario.json`, `adb_keepalive.json`, `spool/`) — todos pequeños y regenerables.

## Principios para no romper nada
- **Estrangulador, no big-bang.** Cada fase es un PR independiente, con **flag por fuente** (`CLIENTES_FUENTE`, `ESTADO_FUENTE`, `CUOTA_FUENTE`, `SESION_FUENTE` = `archivo|adb`), default `archivo` (= comportamiento actual). Revertir = cambiar el flag.
- **Modo sombra antes del corte:** leer de ambas fuentes, usar la vieja y **loguear cualquier diferencia**. El corte a ADB solo tras N corridas de DEV sin diferencias.
- **Nada se borra hasta la fase de limpieza**, y solo tras ≥7 corridas diarias estables en DEV y PROD.
- **Cada escritura a la ADB va tras `try/except` y no propaga** (patrón de `memoria/service.py`); un fallo cae al spool/caché. Reutilizar `estado_de`, `guardar_estado`, `registro._espejo_adb` como moldes.
- **DDL solo aditivo e idempotente** (`db/schema_v2.sql`, bloques `BEGIN…EXCEPTION -955`). Aplicarlo a la ADB real de DEV y luego PROD **solo con tu OK explícito** (regla vigente de DDL sobre infra real).
- **Sin dependencias nuevas** (`pyproject.toml` congelado): el cifrado ya existe en `src/identidad/mfa.py`.
- Territorio: `src/`, `tests/`, `db/`, `scripts/`. **`oci/` (Fase 6 y el rsync del deploy) y `CHANGELOG.md` requieren tu encargo explícito**; el CHANGELOG se escribe una vez al reconciliar. Las ramas se reconcilian de a una con `ruff check . && pytest -q`.

## Fases (cada una: rama → PR → CI → merge a develop → DEV → verificación → PROD fijado por sha)
**Fase 0 · Línea base (sin cambiar comportamiento).** Lectura por SSH a la VM de DEV (`ls/du` de `data/`, ¿`two_stage`?, qué sube `amalia-backup.sh`). Tests de caracterización de lo que hoy se lee/escribe (`history`, `quota`, `last_run`, `load_client_profile`). Captura de referencia de un `--run-now` en DEV (conteos y hash de los perfiles cargados).
**Fase 1 · Infraestructura común** (`src/persistencia/`): spool/outbox con vaciado idempotente, helper de caché atómica (`tmp + os.replace`), selector de fuente por flag, cifrado de blobs reutilizando `identidad/mfa.py`. Tests unitarios y de «ADB caída» (DSN inválido, **no** contraseña mala: evita bloquear la cuenta ORA-28000).
**Fase 2 · Perfiles de cliente.** Loader ADB-first sobre `cliente_config.contenido` con `_parse_profile` (ya puro), caché en `data/cache/clientes/`, fallback al YAML de la imagen. Script de sincronización repo→ADB (`scripts/semilla_esquema_v2.py` ya inserta v1) que sube versión solo si el contenido cambia (hash). Trainer escribe versión nueva. Modo sombra → corte. Cubre `portales/__init__.py`, `feedback/catalogo.py`, `web/portal.py`, `web/feedback.py` (leen YAML hoy).
**Fase 3 · Estado de corrida.** `last_run` e historial ADB-primarios; JSONL solo spool; `reimportar.py` pasa a vaciar el spool y a restaurar desde el **export lógico** (ver DR). `main._save_last_run` atómico (hoy no lo es).
**Fase 4 · Cuota y consumo LLM.** DDL `cuota_gasto`; `UPDATE … SET gasto = gasto + :x` atómico (hoy es lectura-suma-escritura sin lock); falla cerrado con la caché. Libro LLM: ADB primaria + spool.
**Fase 5 · `storage_state` cifrado.** DDL `sesion_portal_compras` (portal_id, contenido cifrado, versión, actualizado_en) con control de versión; materializar a `/tmp`, devolver a la ADB solo si cambió; fallback a login con TOTP. Resuelve de paso el pendiente de PROD (sesión de COMPRASAL en la VM nueva).
**Fase 6 · Adelgazar la VM** (`oci/`, con tu encargo): rsync reducido; quitar el mount `./clients`; `login_debug.log` a stdout (`auth/login.py:93` usa ruta fija); diagnósticos a Object Storage/`/tmp`; actualizar `oci/README.md`, `amalia-backup.sh` y `docs/arquitectura/*` (+ ADR nuevo). Tests de bash existentes + caso nuevo del rsync.
**Fase 7 · Limpieza** tras ≥7 días estables: retirar lectura/escritura de los archivos ya migrados y los flags de transición.
**Fase futura (no incluida):** parámetros de runtime en la ADB y secretos en Vault.

## Recuperación ante desastre (la ADB Always Free no tiene restauración ni PITR)
Hoy los JSONL son el respaldo (`reimportar.py`). Al volverlos spool, el respaldo pasa a ser un **export lógico diario de la ADB a Object Storage** (reutilizar la lógica de `scripts/copiar_adb.py --exportar`, ya probada DEV→PROD, y el PAR/bucket de `07-backup.sh`). **Prueba de restauración obligatoria** en el Oracle local (`db/docker-compose.oracle.yml`) antes de la Fase 7: sin ella no se borra ningún JSONL.

## Verificación de punta a punta
- Cada PR: `ruff check .`, `pytest -q`, `pytest -m oracle_local` (ampliar el filtro de `ci.yml` si hay SQL fuera de `src/memoria/`), 6 bancos de bash, `tests/test_documentacion.py` (árbol de CLAUDE.md al día).
- Pruebas nuevas por fase: round-trip ADB↔caché; ADB caída (DSN inválido) → la corrida termina y envía; spool se vacía en la corrida siguiente sin duplicar; cuota no falla abierta; `storage_state` cifrado ida y vuelta y con ADB caída → login nuevo.
- En DEV (`NOTIFICATION_OVERRIDE_TO` encendido): `--run-now` en modo sombra → 0 diferencias; luego con el flag en `adb`; correr con DSN inválido y comprobar que el reporte sale; revisión visual del portal (23 capturas del banco E14) idéntica; 3 corridas de las 07:00 consecutivas verdes.
- Antes de PROD: mismo sha ya validado en DEV, `AMALIA_IMAGE_TAG` fijado, DDL aplicado con tu OK, `/healthz` 200, `systemctl is-active amalia.timer`.
- Reversión: flag a `archivo`; los archivos siguen escribiéndose hasta la Fase 7.

## Riesgos y mitigación
| Riesgo | Mitigación |
|---|---|
| Sin ADB no hay perfiles y la corrida diría «no hay clientes activos» | Caché + YAML de la imagen; test dedicado |
| Cuota que se reinicia y gasta de más | Falla cerrado; incremento atómico |
| Reanalizar lo ya analizado (idempotencia hoy vive en `analisis.jsonl`) | `analisis_estado` ya lee ADB v2; test de «última línea gana» y de líneas corruptas |
| ACL de la ADB: solo la VM y la VCN llegan | Todo corre en la VM; los scripts siguen en dos pasos como `copiar_adb.py` |
| CI bloqueado (repo privado, Actions sin facturación) | Sin CI no se mergea ni hay imagen: resolver antes de la Fase 2 (decisión tuya) |
| Fase 6 toca `oci/` | Solo con tu encargo; probar con el banco de bash y un deploy no-op en DEV |

## Decisiones que quedan para ti
- Visto bueno para aplicar cada DDL (`cuota_gasto`, `sesion_portal_compras`) en DEV y luego PROD.
- Encargo explícito para tocar `oci/` (Fase 6).
- Cómo se desbloquea el CI (pagar Actions o repo público temporal) antes de empezar a mergear.
