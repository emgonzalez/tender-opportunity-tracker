# Changelog

Todas las versiones notables de **tender-opportunity-tracker (AMALIA)** se documentan en este archivo.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto se adhiere a [Versionado Semántico](https://semver.org/lang/es/):
`vMAYOR.MENOR.PARCHE` — **MAYOR** = cambios incompatibles, **MENOR** = features
compatibles, **PARCHE** = correcciones.

## [Sin publicar]

Cambios mergeados a `develop` después de `v0.3.0` (PR #25–#74, 2026-09-24 → 2026-10-22).
Todo corre en `multicomputos_dev`; sigue sin existir `AMALIA_PROD`.

### Added

- **Feedback Relevante/Irrelevante desde el correo**, de punta a punta. Enlaces firmados
  (HMAC) por destinatario, con motivos para «Irrelevante» (`src/feedback/`, #27); servicio
  web residente FastAPI (`src/web/`); y HTTPS en DEV: IP reservada, registro DNS en la zona
  `dev.amalia.multicomputos.com` y Caddy con TLS automático, que solo reenvía `/f/*` y
  `/healthz` (`oci/lib/08-web.sh`, #60). `ENABLE_FEEDBACK` queda encendido en
  `multicomputos_dev` (#62). Ver [ADR 0005](docs/decisiones/0005-servicio-web-residente-para-el-feedback.md).
- **Correo diario en formato digest** (#64–#72): liviano, sin precedentes ni matriz completa
  (P0); «Para decidir» en tarjetas, «No competibles» y «Baja prioridad» en filas, ordenado por
  cierre (P5); KPIs, chips de urgencia, categoría, método y marcas, y botones sólidos
  compatibles con Outlook (#72). Guardia de peso: si el HTML supera 90 KB, tarjetas pasan a
  filas (Gmail recorta a ~102 KB).
- **Señal estructurada `competible` / `motivo_bloqueo`** en el drill-down, y triaje
  consultable (#68).
- **Auto-actualización de la VM de DEV**: `docker.channel: develop` en
  `multicomputos_dev` (#73). `amalia-pull-deploy.timer` corre a las 05:30 y sigue la etiqueta
  `develop-oci`; lo que queda en el `.env` es siempre un `sha-<40hex>`.
- **Esquema v2 multi-tenant/multi-país** (`db/schema_v2.sql`): fases «Expandir» (#38),
  doble escritura (#39–#42), «Backfill» (`src/memoria/backfill_v2.py`, #43) y «switch» de los
  seis lectores (#47–#53, con `retention_v2.sql` e `index_ivf_v2.sql`).
- **Portal AMALIA, vista de país** (KPIs 1–6): capa de datos `src/analytics/pais.py` (#57) y
  ruta `/portal` tras `ENABLE_PORTAL`, sin autenticación hasta su Fase 3 (#58). Plan en
  [`docs/planes/portal-amalia.md`](docs/planes/portal-amalia.md) (#56).
- **Módulo de mercado** (`src/mercado/`): adjudicaciones del mercado completo y resultado
  propio, para entrenar a AMALIA (#35). Corre manual; no está programado.
- `src/memoria/reimportar.py`: recuperación ante desastre JSONL → ADB, sin LLM (#37).
  Espejo del libro del LLM en la ADB (`llm_llamada`) y circuit breaker real (#36).
- Imagen **multi-arquitectura** (amd64 + arm64) y tenant A1 en sombra (#29, #30).
- El drill-down extrae `monto_estimado` del pliego (#33); el scraper trae `tipo_actividad`
  real y `fecha_limite` por proceso (#34).

### Changed

- La alerta de presupuesto nace al **80 %** del tope, no al 1 %, y su mensaje deja de
  prometer un autoapagado que no ocurre a ese nivel (#74). La regla viva de DEV se ajustó a
  mano el 2026-10-22; el script solo crea la regla cuando no existe ninguna.
- `threshold_usd` del bloque A1 de 0.01 a 50 (inerte) (#74).
- El correo de validación de `multicomputos_dev` también llega a la casilla corporativa de
  Emilio (#71).
- `buscar_precedentes` aísla por `cliente_id` (#31).

### Fixed

- Un backslash suelto del pliego ya no tira la respuesta entera del LLM (#25); se decodifican
  escapes truncados y se normaliza el mojibake del modelo (#67); cuando el análisis no se
  puede leer se guarda la respuesta cruda (#26).
- Un deploy cortado ya no deja la VM sin timer (#28).
- El drill-down registra **todo** desenlace en la ADB, no solo `ANALIZADO` (#32).
- Watchdog de costo: override de nombre del dynamic group, suscripción del topic, y medía
  toda la tenancy en vez del compartment y le faltaba `oci-cli` (#44–#46).
- `ENABLE_ESQUEMA_V2` no estaba gobernado por tenant y cualquier redeploy lo pisaba (#54).
- `web_reserve_ip` separaba mal los campos de la IP pública (#61).
- `version` admite el SHA completo del commit (`VARCHAR2` 32 → 64) (#63).
- `fecha_limite` se guarda en el historial y `resend` la recupera (#65); se descartan marcas
  recomendadas fuera del portafolio del cliente (#66).
- Delta del KPI 1 del portal contra el mismo tramo del mes anterior (#59).

## [0.3.0] — 2026-09-24

Release de la **Fase 9**: el proveedor de LLM deja de estar cableado. `multicomputos_dev`
pasa a hacer inferencia en OCI Generative AI, con un libro contable que mide cada llamada.

### Added

- **El proveedor de LLM es configurable: Claude vía API u OCI Generative AI.**
  Una variable, `LLM_PROVIDER=anthropic|oci`, cambia de golpe los cinco sitios
  que hablan con un modelo. Dentro de OCI, una segunda perilla ortogonal
  —`OCI_FAMILIA=nativo|xai|google`— elige la familia: Meta Llama 4, Grok o
  Gemini. Los `MODEL_*` / `OCI_MODEL_*` por etapa siguen pisando la familia, así
  que se puede mezclar (triaje en Llama, análisis en Gemini). Ver
  [ADR 0004](docs/decisiones/0004-abstraccion-de-proveedor-llm.md).
- **Libro de consumo append-only** (`src/llm/registro.py`, JSONL en `data/`): una
  línea por llamada con proveedor, familia, modelo, etapa, tokens de entrada y
  salida, caracteres desglosados (texto · sistema · base64), latencia,
  truncamiento, clamp del techo, error y costo estimado. Registra **tamaños,
  nunca contenido**: un pliego de licitación no termina en un archivo de log.
  Encendido por default — el dato solo sirve si ya estaba ahí cuando se quiso
  comparar.
- `Respuesta.uso_crudo` transporta el `usage` **sin normalizar** del proveedor.
  Sin él, `razonamiento_oculto` valía 0 por construcción: el dato que lo revela
  (`total_tokens`) no sobrevive a `Uso.desde_oci`, que lo consume. Auditar una
  normalización contra su propio resultado no es auditarla.
- `scripts/comparar_llm.py` — lee el libro y arma la comparación por etapa entre
  proveedores: costo, tokens, latencia p50/p95, tasa de error y truncamiento.
- `scripts/smoke_oci_genai.py` — prueba de humo contra OCI real que responde las
  incógnitas que ninguna documentación contesta: auth/IAM/región, techo de
  salida, si el modelo lee imágenes base64, RSS del SDK y calibración de precio.
  El **gate 6** (`--key-file`) ejercita el camino de credencial de **la VM**
  —variables de entorno con el PEM en una línea— y no el de la laptop: los otros
  cinco pasaron en los tres despliegues que fallaron el 2026-09-23.
- El SDK de OCI entra como **extra opcional** (`pip install '.[oci]'`, build-arg
  `INSTALL_OCI`). Cuesta 405 MB de imagen, medido. CI publica las dos variantes
  del mismo commit en los refs de los que se despliega: `sha-<sha>` sin el SDK y
  `sha-<sha>-oci` con él.
- **`main` y `develop` exigen seis checks de CI antes de mergear.** Hasta ahora
  los rulesets solo bloqueaban borrado y force-push: el CI en verde era
  informativo, no una puerta. Detalle en
  [`CONTRIBUTING.md`](CONTRIBUTING.md#checks-obligatorios).

### Changed

- **`multicomputos_dev` hace inferencia en OCI** (`llm_provider: oci`,
  `oci_familia: nativo`), con el libro encendido. `nativo` es la única familia
  que mantiene los pliegos **dentro** de OCI; `xai` y `google` los mandarían a
  terceros. Volver a Claude es una línea en `tenants.yaml` y redesplegar.
- **La tarifa de OCI pasa de supuesta a medida: $0,00081 por 10k transacciones**,
  contra los $0,0018 de lista que la guardia de gasto venía asumiendo. 2,2× más
  barato; con el factor de seguridad encima, la guardia apretaba ~3,3× de más y
  podía frenar pliegos que sí entraban. Llama 4 queda en $0,6682/MTok contra
  $1,00 de entrada de Haiku 4.5.
- **`engine.py` entra en el libro mayor de gasto**, que hasta ahora solo cubría
  el drill-down. Solo `cobrar()`, nunca `cabe()`: el drill-down se frena por
  presupuesto, el clasificador solo se contabiliza — frenarlo es apagar el
  sistema, y eso lo decide una persona. `trainer.py` queda fuera (CLI manual).
- **La clasificación pasa a streamear.** El adaptador elige `create` vs `stream`
  por `max_tokens >= 8192`, y `engine.py` pide 16.384. La decisión está forzada:
  como el clasificador pide más que el análisis (16.384 > 16.000), ningún umbral
  puede dejar uno streameando y el otro no.
- El lote del clasificador se **deriva** del techo de salida del modelo en vez de
  ser una constante: 11 procesos con Llama (4.096 ÷ 350), 40 con Gemini o Grok.
- `OCI_ANALISIS_RASTER_PAGES` baja de 20 a **10**, junto con el espejo del
  adaptador y el de la guardia de gasto. Es el techo de imágenes de Llama 4;
  pedir más no mandaba más, mandaba un 400.
- `scripts/validate_tracka_e2e.py` acepta `--proveedor` y `--familia`, y resuelve
  los modelos con la misma tabla que usa el pipeline. Antes fijaba IDs literales
  que **diferían de los defaults de `config.py`**: certificaba una combinación
  que producción no usaba.
- `start.sh` pide `ANTHROPIC_API_KEY` **o** `OCI_GENAI_COMPARTMENT_OCID` según el
  proveedor activo. Antes exigía la de Anthropic siempre, e impedía arrancar
  localmente con OCI.

### Fixed

- **La imagen desplegada no traía el SDK que el tenant iba a usar.** El deploy
  terminaba en verde, el `.env` quedaba perfecto y la corrida de las 07:00 moría
  con `ModuleNotFoundError: No module named 'oci'` — doce horas después y lejos
  de la causa. La etiqueta de imagen ahora se deriva del **mismo flag** que
  elige el proveedor: dos perillas que tienen que coincidir terminan no
  coincidiendo.
- **`oci.Signer` exige `private_key_file_location`** aunque la clave viaje por
  `private_key_content`. Solo rompía en el camino de la VM. El test que debía
  atraparlo era `def _signer(**kw)`: un doble que acepta cualquier cosa no prueba
  nada sobre una función que no la acepta.
- **El cliente de OCI valida el config aunque reciba un signer.** Solo se saltea
  esa validación para tres clases de signer, y `oci.Signer` no es ninguna: con un
  config pelado responde `InvalidConfig: {'user': 'missing', …}`, que suena a
  credencial ausente cuando está toda presente.
- **OCI acepta 10 imágenes por request, no 20.** El 75% de las llamadas de la
  etapa `analisis` moría con `400 Too many images`, cada una dejando un pliego en
  `ERROR_LLM`. Tabla de techos por modelo y recorte con aviso, igual que el techo
  de salida.
- **El gasto de las familias que razonan se contabilizaba ~25× por debajo.** Grok
  y Gemini consumen tokens de razonamiento que facturan pero **no reportan** en
  `completion_tokens` (medido: 18 reportados sobre 514 reales). `Uso.desde_oci`
  usa `total - prompt` cuando la resta es mayor.
- **Un GO se convertía en NO-GO en silencio con Gemini.** Esos mismos tokens
  invisibles cuentan contra `max_tokens`, y el triaje pide 512 —calibrado para
  Haiku—: Gemini gastaba 508 pensando, emitía 21, el JSON quedaba cortado y el
  parseo fallaba devolviendo `NO-GO` con confianza 0.0. Es el peor modo de falla
  del sistema, porque descarta el pliego sin alertar, y era **intermitente**.
  `PISO_SALIDA_CON_RAZONAMIENTO = 2048` le da aire al presupuesto.
- `analysis.py` dejó de leer el PDF completo a base64 de forma ansiosa: el
  contrato transporta la **ruta**. Son ~43 MB menos de RSS en el pico, en una VM
  con `mem_limit: 800m`.
- **El despliegue fija los permisos de `data/` en el host.** El `chmod` del
  `Dockerfile` era inútil: el bind mount reemplaza el directorio de la imagen.
  Tres días de corridas fallidas en dev con `PermissionError` sobre
  `last_run.json`.
- El job `rutas` de CI necesita `pull-requests: read` en eventos de PR.

### Removed

- **`sql-oracle.yml` se disuelve dentro de `ci.yml`** como el job `dialecto`,
  condicionado por rutas con `dorny/paths-filter`. Vivía aparte con el argumento
  de que cuadruplicaría la espera del CI; **la medición lo desmintió**: el job
  tarda 89-125 s y `ci.yml` tarda 170-243 s (6 corridas cada uno), así que es el
  más corto de los dos — y como los jobs corren en paralelo, no agrega un segundo
  de reloj. Lo que se gana es poder marcarlo como check **obligatorio**,
  imposible antes porque un workflow con filtro `paths` no reporta ningún check
  en las PRs que no tocan esas rutas.
- **`oci/tenants.yaml` define un solo tenant: `multicomputos_dev`.** Se eliminaron
  `emiliogonzalez` y `emiliogonzalez_e2` —cuya infraestructura se destruyó el mismo
  día, incluida la ADB `AMALIAE2`— y `multicomputos`, que no tenía infraestructura
  desde el 2026-09-08 y se conservaba como plantilla de producción. Mantener una
  plantilla que nadie despliega tiene un costo: `deploy.sh` la acepta como destino
  válido y un archivo que describe recursos inexistentes se lee como si existieran.
  Cuando haga falta `AMALIA_PROD`, la plantilla es `multicomputos_dev`.

## [0.2.1] — 2026-09-14

_Solo correcciones. Ninguna cambia el comportamiento de la aplicación: las tres
primeras son de infraestructura y la cuarta, de CI._

### Fixed

- **El `cloud-init` superaba el límite de metadata de OCI y ninguna instancia
  nueva podía crearse.** `amalia-backup.sh` (31 kB) y `amalia-pull-deploy.sh`
  (15 kB) viajaban embebidos en base64; el total llegó a 80 428 bytes contra un
  techo de 32 000. Habría bloqueado el aprovisionamiento de AMALIA_PROD por
  completo. Embeberlos era además redundante —ambos operan sobre
  `/opt/tender-opportunity-tracker`—, así que las units pasan a apuntar a la copia
  del repo que `app_deploy` ya sincroniza: 80 428 → 19 460 bytes, y 8 660 con el
  `gzip` que se agrega como segunda red, más una guardia de tamaño.
- **Un error de request se reportaba como falta de capacidad.**
  `oci/lib/02-compute.sh` recorría los tres availability domains ante cualquier
  fallo y concluía "todos sin capacidad, reintentá en unas horas" — falso, y
  además inútil, porque un request inválido falla igual en todos. Ahora aborta de
  inmediato con el error de OCI y las causas frecuentes.
- **El ACL de la ADB quedaba obsoleto al recrear la instancia.** La condición solo
  comprobaba la VCN; al recrear, la VCN sigue estando y la IP pública cambia, así
  que informaba "ACL ya incluye la VCN" sin actualizar nada y Track B quedaba roto
  con `ORA-12506` en silencio. La VCN no alcanza porque la VM sale por el Internet
  Gateway al endpoint público de la ADB. El bloque pasa a `adb_acl_sync`, que
  `deploy.sh` invoca en cada despliegue.
- **El CI no corría en tags**, porque `branches: ["**"]` no los alcanza: un tag no
  es una rama. La regla `type=semver` del job de imagen era código muerto desde que
  se escribió, y `v0.2.0` no llegó a publicar una imagen por versión hasta que se
  recreó el tag.

## [0.2.0] — 2026-09-13

_Los feature flags de la Fase 2 siguen naciendo en `false`: para cualquier
despliegue que no los active, el comportamiento es idéntico a `v0.1.0`. En
`multicomputos_dev` están en `true` y el ambiente corre con Track A y Track B._

### Added

- **Fase 2 · Track A — Drill-down de pliegos** (flag `ENABLE_DRILLDOWN`):
  localiza y descarga el PDF del pliego de cada licitación relevante (streaming,
  con guardia de tamaño), lo inspecciona con PyMuPDF (`fitz.open(path)` vía mmap),
  aplica un pre-filtro RegEx contra las señales del cliente, hace **triaje GO/NO-GO
  con Claude Haiku 4.5** y, solo para los GO, un **análisis profundo con Claude
  Sonnet 5** que extrae una **matriz de riesgos** (financieros, técnicos, legales y
  de plazos) con severidad y cita textual de la cláusula.
- **Fase 2 · Track B — Memoria institucional** (flag `ENABLE_MEMORIA`):
  **Oracle Autonomous Database 23ai** con *AI Vector Search* y **embeddings
  generados dentro de la base de datos** (ONNX `multilingual-e5-small`, 384 dims, 100 idiomas) —
  sin API externa de embeddings. Incluye chunking con solape, vectorización
  idempotente, RAG intra-documento y **precedentes inter-documento**: pliegos
  históricos similares que responden *"¿ya vimos algo así antes?"*.
- **Dos etapas efímeras** (`python -m src.main --stage scrape|analyze`): separa la
  fase con navegador de la de análisis para que los picos de RAM de Chromium y del
  LLM nunca se sumen en la VM de 1 GB. Handoff durable (manifiesto JSON + PDFs).
- **Guardia de cuota de tokens** (`data/token_budget.json`) con reserva atómica y
  reset diario; si se excede, el pliego pasa a `REQUIERE_REVISION_MANUAL`.
- **Máquina de estados del pliego** idempotente y reanudable, persistida en
  `data/history/{cliente}.analisis.jsonl` y (con Track B) en ADB.
- Scripts SQL en `db/`: esquema idempotente, carga del modelo ONNX, índice
  vectorial IVF opt-in y política de retención.
- Provisioning **opt-in** en `oci/`: ADB Always Free + wallet en Vault + IAM
  mínimo (`05-adb.sh`), systemd de dos etapas (`instance.two_stage`) y red de
  mínimo privilegio (`network.ssh_cidr`, `network.restrict_egress`).
- Suite de pruebas: **64 tests unitarios** (33 de Track A + 31 de Track B) más un
  test de integración contra ADB real, saltado por defecto sin credenciales.
- Documentación de arquitectura v2 en `docs/`: arquitectura,
  inventario con DDL y políticas de reintento, diagramas HLD/LLD en Mermaid, la
  auditoría (`docs/decisiones/0001-rearquitectura-v2.md`) y los planes de ambos tracks.
- **Fase 5 — Respaldo verificado del historial** (opt-in por tenant,
  `backup.enabled` en `tenants.yaml`): timer diario en la VM que empaqueta
  `last_run.json`, `gasto_diario.json` y `data/history/`, los sube a un bucket de
  Object Storage con un **PAR de escritura** (la VM no puede usar Instance
  Principal: `dynamic-groups` está denegado por IAM) y **verifica el respaldo
  bajándolo y restaurándolo** contra el `MANIFEST.sha256` que viaja adentro.
  Retención del lado servidor (30 d diarios / 365 d mensuales / 7 d versiones),
  camino de restauración que **no necesita que la VM exista**
  (`backup_restore <tenant>` desde la laptop) y alerta por correo cuando falla,
  cuando el PAR está por vencer o cuando el mecanismo estuvo caído
  (`src/notifier/ops_alert.py`). `data/storage_state.json` **queda fuera a
  propósito**: es una credencial de sesión y caduca en ~30 min, así que
  respaldarla suma riesgo sin sumar capacidad de restaurar.
- **Fase 6 — Vigía externo de la ADB** (`.github/workflows/adb-keepalive.yml`):
  workflow diario que consulta el estado de la ADB de AMALIA_DEV por el **plano de
  control de OCI** y la arranca si la encuentra detenida — la única capa que no
  depende de que la VM esté encendida. Falla a propósito cuando la encuentra
  detenida, porque eso prueba que el timer de la VM lleva una semana muerto y la
  falla dispara el correo de GitHub. **No conecta a la base**: la ACL de red solo
  admite la VCN y la IP de la VM, y un runner sale por ~5.400 rangos rotativos de
  Azure; por eso **no evita el auto-stop a los 7 días** —eso sigue siendo del
  keepalive de la VM— pero sí cierra el riesgo de que la base se reclame a los 90
  días. Análisis completo en
  [`docs/decisiones/0003-keepalive-adb-fuera-de-la-vm.md`](docs/decisiones/0003-keepalive-adb-fuera-de-la-vm.md).
- **Umbral en el keepalive interno de la ADB** (`src/memoria/keepalive.py`): el
  conteo de fallos consecutivos se persiste en `data/adb_keepalive.json` y se
  alerta **al segundo fallo seguido**, no al primer parpadeo; un éxito resetea la
  racha. `service.ping_con_estado()` devuelve la decisión ya resuelta.

- **Fase 7 — El puerto 22 deja de estar abierto a internet.**
  `oci/scripts/ssh-allow.sh <tenant>` restringe el SSH al `/32` del operador y lo
  refresca cuando su IP residencial dinámica cambia, sin necesidad de redesplegar.
  Es **idempotente** (si el `/32` correcto ya está puesto no hace ninguna llamada
  de escritura) y **falla cerrado**: aborta sin escribir si los servicios de eco no
  coinciden entre sí —el síntoma de un proxy que intercepta TLS y contesta con
  *su* salida—, si contesta uno solo, si la security list tiene una forma
  inesperada o si el resultado calculado no pasa los guardias; nunca deja
  `0.0.0.0/0` ni una regla a medias. Después de escribir **verifica** que el 22
  responda con una conexión TCP nueva y **revierte** si no. Detecta además que la
  security list sea compartida (`emiliogonzalez` y `emiliogonzalez_e2` son el
  mismo OCID) y pide confirmación. El OCID sale de `oci/state/<tenant>.json`,
  **no está incrustado** en el script. Banco de pruebas con stubs del CLI de
  `oci`: `scripts/test_ssh_allow.sh`.

### Changed

- **`ssh_ingress_cidr` pasa de `0.0.0.0/0` a `auto` en los cuatro tenants.**
  `auto` significa "el `/32` de quien está desplegando, resuelto en el momento",
  y se resuelve en un único lugar (`oci/lib/ssh-cidr.sh`) que comparten el deploy
  (`01-network.sh`) y `ssh-allow.sh`. Un `/32` literal en `tenants.yaml` sería
  peor: nace vencido —la IP es dinámica—, con lo cual un redeploy pisaría con esa
  IP vieja el `/32` correcto y el paso siguiente del propio deploy, que entra por
  SSH, se colgaría contra su propia regla; y además versionaría la IP de casa de
  una persona. **El valor no cambia nada por sí solo:** surte efecto en el próximo
  `deploy.sh` o al correr `ssh-allow.sh`.
- **La guardia de gasto del drill-down se lleva en dólares, no en tokens**
  (`DAILY_TOKEN_BUDGET` → `DAILY_BUDGET_USD`; `data/token_budget.json` →
  `data/gasto_diario.json`, con desglose por modelo). El triaje corre en Haiku
  ($1/MTok) y el análisis en Sonnet ($3): un contador único de tokens no se puede
  traducir a dinero. Además, **estimar y cobrar se separaron** — se estima para
  decidir si arrancar, pero al libro se anota el `usage` **real** que devuelve la
  API. Motivos y alternativas descartadas en
  [`docs/decisiones/0002-presupuesto-en-dolares.md`](docs/decisiones/0002-presupuesto-en-dolares.md).
- `send_report()` acepta `resultados_analisis` (opcional): el reporte HTML suma
  una sección de **matriz de riesgos** y otra de **precedentes**, ambas email-safe
  y omitidas si no hay datos (retrocompatible).
- `ResultadoAnalisis` incorpora `precedentes`; `Proceso`/`ProcesoClasificado` no cambian.
- La lectura del estado del pliego pasa por una fachada (`src/memoria/service.py`)
  que consulta ADB con **fallback automático a JSONL**.

### Fixed

- **El triaje se enviaba a 72 DPI.** El tope del rasterizado (`min(1.0, 1568/lado)`)
  medía el lado en **puntos PDF**, no en píxeles: una carta son 612×792 pt
  independientemente de a qué resolución se escaneó, así que en toda página de
  tamaño estándar el zoom quedaba clavado en 1,0. Medido sobre pliegos reales de
  COMPRASAL: escaneos de 155–200 DPI llegaban al modelo a 72 DPI, ilegibles para
  texto legal denso — no era solo un problema de costo, sino de fiabilidad del
  GO/NO-GO. El tope ahora se calcula contra los píxeles del ráster embebido, con
  guarda para que un logo pequeño no reduzca la página entera a una miniatura.
- **El gasto del triaje no se contabilizaba.** La guardia estimaba antes del
  triaje pero solo reservaba tras un veredicto `GO`, así que todo lo que costaban
  los `NO-GO` quedaba fuera del presupuesto — el mayor consumidor del pipeline en
  un corpus mayoritariamente escaneado. Ahora se cobra por etapa.

### Security

- Wallet mTLS de ADB traído de **OCI Vault con Instance Principal** hacia `tmpfs`
  (`/dev/shm`), nunca a disco persistente, y borrado al terminar el proceso.
- Todo el SQL usa *bind variables*; el nombre del modelo de embeddings —único valor
  que debe interpolarse por ser identificador— se valida con allowlist estricta.
- Los PDFs descargados se borran tras analizar; el bucket de Object Storage lleva
  regla de ciclo de vida a 7 días (higiene de costo y de PII).

## [0.1.0] — 2026-06-26

Primera versión etiquetada del POC, verificada end-to-end en producción.

### Added
- **Agente AMALIA**: scraper Playwright con login + 2FA/TOTP automático (`pyotp`),
  extracción de procesos de COMPRASAL y parseo DOM → dataclass `Proceso`.
- **Clasificador** Claude Sonnet en modo batch: scoring multidimensional, tags
  dinámicos y recomendaciones de marca por cliente.
- **Notifier** de email HTML (Jinja2) con diseño card-based, match score unificado,
  chips de urgencia de cierre y responsive < 480px.
- **Despliegue OCI multi-tenant Free Tier** (`oci/deploy.sh`): VCN, subnet, gateway,
  route table, security list, instancia, systemd timer @ 07:00 (TZ El Salvador),
  cap de costo y auto-stop.
- Tenant **E2.1.Micro** como fallback opt-in con modelo de contenedor efímero y
  capacidades anti-OOM (zram + earlyoom + swap).
- `network.share_from` para compartir VCN entre tenants.
- Separación de **reportes diarios** (`email_to`) y **alertas de admin**
  (`system_admins_email`).
- Documentación de autonomía para Claude Code (CLAUDE.md, GUIA, STATUS, spec).

### Changed
- Consolidación **multi-tenant** en `develop`: selección de `docker-compose.yml`
  (ARM A1.Flex) vs `docker-compose.e2.yml` (x86 E2.1.Micro) por tenant en `tenants.yaml`.
- Redesign del reporte diario (formato V1 Hero) + extracción de `fecha_limite`.

### Fixed
- **Despliegue `multicomputos`**: agregados `ephemeral_container`/`schedule_time`
  (sin ellos cloud-init no generaba el systemd timer); `budget.enabled: false` como
  workaround por permisos IAM `in tenancy` faltantes.
- Auth/sesión: `wait_until=domcontentloaded` (en vez de `networkidle`), timeouts de
  validación y login aumentados, screenshot timeout.
- Docker: permisos de escritura de `botuser` en el volumen `data/` montado.
- Scraper: tolera `fecha_publicacion=null` en respuestas de COMPRASAL.
- Varios fixes de `deploy.sh` descubiertos al ejecutar en macOS (paths CLI de budgets,
  flags no soportados, output del build, etc.).

[Sin publicar]: https://github.com/emgonzalez/tender-opportunity-tracker/compare/v0.3.0...develop
[0.3.0]: https://github.com/emgonzalez/tender-opportunity-tracker/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/emgonzalez/tender-opportunity-tracker/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/emgonzalez/tender-opportunity-tracker/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/emgonzalez/tender-opportunity-tracker/releases/tag/v0.1.0
