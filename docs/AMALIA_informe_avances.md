# AMALIA — Informe de avances

> **Fecha de corte:** 2026-10-22 · **Rama analizada:** `develop` (HEAD `a0b779b`, merge del PR #74)
> **Método:** lectura del repositorio (`git log`, código, `CHANGELOG.md`, `STATUS.md`, ADRs, planes), una corrida de la suite de pruebas y una **copia de solo lectura del libro de consumo y del historial de la VM de DEV** (`data/llm_consumo.jsonl`, `data/history/`). No se modificó la VM ni se versionó ninguno de esos archivos.
> **Regla de redacción:** cada dato lleva su evidencia (ruta, commit o comando). Lo que no se puede comprobar dice **"No verificable"**. No se incluyen secretos, OCIDs, IPs ni datos de clientes.
> **Sustituye** al informe con corte 2026-10-11. La diferencia principal: ahora hay **datos reales de uso y de costo** (§5.3, §6), que antes no existían en el repo.

---

## 1. Resumen ejecutivo

1. **AMALIA** es un agente interno que cada día a las 07:00 (hora de El Salvador) revisa el portal de compras públicas COMPRASAL, clasifica con IA las licitaciones nuevas según el catálogo del cliente y le envía un reporte por correo.
2. Además hace **análisis de pliegos** (triaje GO/NO-GO y matriz de riesgos con cita textual), mantiene una **memoria institucional** con búsqueda vectorial en Oracle Autonomous Database 23ai y, desde el 2026-10-22, un **correo en formato digest** con botones Relevante/Irrelevante que alimentan un historial de feedback.
3. Corre en OCI (VM + ADB 23ai + Object Storage + Vault) y hace la inferencia en **OCI Generative AI** (Llama 4) desde el 2026-09-23. La VM de DEV **se actualiza sola** desde la rama `develop` (desde el 2026-10-22).
4. Estado: **un solo ambiente desplegado, de desarrollo** (`multicomputos_dev`); `AMALIA_PROD` no existe y el correo sigue en **modo validación** (llega solo al desarrollador, no a los destinatarios reales).
5. **Dato nuevo:** en 42 corridas registradas (2026-09-10 → 2026-10-22) AMALIA revisó **1.849 procesos** y marcó **292 como relevantes (15,8 %)**; analizó **258 pliegos** y el costo de inferencia estimado fue de **≈ US$ 0,22 por día** (≈ US$ 0,02 por pliego). Sigue **sin medirse el ahorro de horas** ni la precisión frente a un criterio humano.

*(Evidencia: §2, §5.3 y §6; `STATUS.md`; `CHANGELOG.md` v0.3.0; `CLAUDE.md` Business Context.)*

---

## 2. Línea de tiempo

| Dato | Valor | Evidencia |
|---|---|---|
| Primer commit | 2026-05-07 `88c237a` "Initial commit: tender-opportunity-tracker POC" | `git log --reverse` |
| Último commit | 2026-10-22 `a0b779b` (merge del PR #74) | `git log -1` |
| Commits en `develop` | **257** (259 contando todas las ramas) | `git rev-list --count HEAD` / `--all` |
| Días con actividad | 38 días distintos en ~5,5 meses | `git log --format=%ad` |
| Merges / PRs | 61 merges; **74 PRs numerados** (73 fusionados en GitHub) | `git log --merges`; `gh pr list --state merged` |
| Tags | `v0.1.0` (2026-06-26) · `v0.2.0` (2026-09-13) · `v0.2.1` (2026-09-14) · `v0.3.0` (2026-09-24) | `git tag -l` |
| Tipo de commit | 79 feat · 54 fix · 27 docs · 13 chore · 11 test · 5 ci · 2 release · 1 refactor · 1 perf · 1 build | prefijos de `git log --format=%s` |

**Actividad por mes (commits):** mayo 36 · junio 3 · agosto 38 · septiembre 142 · octubre 38 (al corte).

**Hitos**

| Fecha | Hito | Evidencia |
|---|---|---|
| 2026-05-07/08 | POC inicial y despliegue multi-tenant en Free Tier con tope de costo y auto-stop | `88c237a`, `7a672ea` |
| 2026-06-26 | `v0.1.0`: POC verificado de punta a punta (scraper + clasificador + correo + timer en OCI) | `CHANGELOG.md` [0.1.0] |
| 2026-09-13 | `v0.2.0`: drill-down de pliegos (Track A) y memoria institucional en ADB 23ai (Track B), respaldo verificado, vigía de la ADB, SSH restringido | `CHANGELOG.md` [0.2.0] |
| 2026-09-23/24 | `v0.3.0`: proveedor de LLM configurable; inferencia en OCI Generative AI | `CHANGELOG.md` [0.3.0]; ADR 0004 |
| 2026-09-27/30 | Esquema multi-tenant v2 en la ADB real; lectores migrados; redeploy | `STATUS.md` "Historial reciente" |
| 2026-10-02 | Primera vista de país del portal (KPIs 1-6) | `4da602f` (#58) |
| 2026-10-22 | HTTPS del servicio de feedback en DEV (dominio `dev.amalia…`, IP reservada, Caddy) y `ENABLE_FEEDBACK` encendido | PR #60-#62 |
| 2026-10-22 | **Rediseño del correo** en seis PRs: liviano (P0), historial y fecha límite (P1), marcas del portafolio (P2), texto limpio del LLM (P3), señal "competible" (P4), digest en tres secciones (P5) y lenguaje visual (#72) | PR #64-#72 |
| 2026-10-22 | **Auto-actualización de la VM de DEV** (`channel: develop`) y alerta de presupuesto al 80 % | PR #73, #74 |

**Participación del autor.** Los 257 commits están a nombre de Emilio González: aparecen cuatro variantes de nombre/correo de la misma persona (151 + 72 + 17 + 17; `git shortlog -sne`). 178 commits llevan además el trailer `Co-Authored-By: Claude`, es decir, un desarrollo asistido por IA, no un equipo de varias personas. **No hay otros autores humanos en el historial.**

---

## 3. Funcionalidades implementadas

Criterio de estado: **En producción** = desplegado y activo en un ambiente vivo. Como `AMALIA_PROD` no existe, ninguna funcionalidad está en producción *de cara al cliente final*; "activo en DEV" significa que corre a diario en `multicomputos_dev`.

| # | Funcionalidad | Estado | Valor de negocio | Evidencia |
|---|---|---|---|---|
| 1 | Scraper autenticado de COMPRASAL (login + 2FA/TOTP, reintentos de sesión) | **Activo en DEV** (verificado e2e desde v0.1.0) | Cobertura de lo publicado en 24 h sin intervención humana | `src/scraper/`, `src/auth/`; CHANGELOG 0.1.0 |
| 2 | Clasificación semántica por cliente (score multidimensional, tags, marcas recomendadas) | **Activo en DEV** | Prioriza oportunidades según el catálogo real (44 tags, ~41 ítems de marca según `STATUS.md`) | `src/classifier/`, `clients/multicomputos_sv.yaml` |
| 3 | Reporte por correo **en formato digest**: "Para decidir" (tarjetas), "No competibles" y "Baja prioridad" (filas), KPIs, chips de urgencia y marcas, botones de feedback; guardia de peso (≤ 90 KB) | **Activo en DEV, en modo validación** (no llega a los destinatarios reales) | Lectura rápida para el comercial; señal de vida diaria | `src/notifier/`, `templates/report.html`; PR #64-#72 |
| 4 | Drill-down de pliegos: descarga, pre-filtro, triaje GO/NO-GO, matriz de riesgos con cita, señal estructurada "competible" | **Activo en DEV** (`ENABLE_DRILLDOWN=true`) | Reduce la lectura manual de pliegos; riesgo legal/técnico visible | `src/drilldown/`; `oci/tenants.yaml` |
| 5 | Memoria institucional: RAG y "precedentes" con búsqueda vectorial en ADB 23ai, embeddings ONNX *dentro* de la base | **Activo en DEV** (`ENABLE_MEMORIA=true`) | Respuesta a "¿ya vimos algo así?"; capacidad Oracle reutilizable | `src/memoria/`, `db/schema.sql`, `db/load_onnx.sql` |
| 6 | Proveedor de LLM configurable (Claude ⇄ OCI GenAI) con libro de consumo | **Activo en DEV** (`LLM_PROVIDER=oci`) | Permite negociar costo de inferencia dentro del contrato OCI; sin dependencia de un único proveedor | `src/llm/`; ADR 0004 |
| 7 | Guardia de gasto diaria en dólares y watchdog de costo a nivel compartment | **Activo en DEV** | Tope duro de gasto de IA y de nube | `src/drilldown/quota.py`; `oci/lib/cost-watchdog.sh` |
| 8 | Despliegue como código (VCN, VM, ADB, Vault, presupuesto, backup, systemd, IP reservada, DNS, Caddy) y CI con 6 checks obligatorios | **Funcional / activo** | Reproducibilidad del ambiente; repetible para otros clientes | `oci/deploy.sh`, `oci/lib/`, `.github/workflows/ci.yml` |
| 9 | **Auto-actualización de la VM** desde la etiqueta móvil `develop-oci` (timer 05:30; el `.env` siempre queda con un `sha-<40hex>`) | **Activo en DEV** desde 2026-10-22 | Cierra el agujero de depender de SSH para actualizar; el reporte de las 07:00 sale con el código nuevo | `oci/lib/amalia-pull-deploy.sh`; PR #73 |
| 10 | Respaldo verificado del historial + vigía externo de la ADB | **Funcional** (opt-in por tenant) | Continuidad y recuperación | CHANGELOG 0.2.0 Fases 5-6 |
| 11 | Esquema multi-tenant/multi-país v2 (switch de lectores hecho; fase "Contract" no empezada) | **Funcional en DEV** | Base para varios clientes y países | `db/schema_v2.sql`; `STATUS.md` |
| 12 | **Feedback Relevante/Irrelevante** desde el correo (enlaces firmados HMAC) + servicio web con HTTPS | **Activo en DEV** (`ENABLE_FEEDBACK=true`; `/healthz` verificado desde afuera). Solo 2 votos registrados, ambos de pruebas | Cierra el ciclo de aprendizaje con quien lee el reporte | `src/feedback/`, `src/web/`; ADR 0005; PR #60-#62 |
| 13 | Módulo de mercado (adjudicaciones, ganado/perdido) | **En desarrollo**: corre manual por CLI, no programado | Entrenar a AMALIA con resultados reales | `src/mercado/`; `docs/planes/portal-amalia.md` (KPI 14) |
| 14 | Portal AMALIA: vista de país (KPIs 1-6) | **En desarrollo** (`ENABLE_PORTAL`, sin autenticación hasta su Fase 3; el proxy público solo expone `/f/*` y `/healthz`) | Visibilidad ejecutiva | `src/web/portal.py`, `src/analytics/pais.py`; `oci/caddy/Caddyfile` |
| 15 | Portal con identidad/roles, pipeline comercial, SaaS externo | **Planificada** | Línea de producto | `docs/planes/portal-amalia.md` Fases 3-4 |
| 16 | Terraform + guardas de dialecto Oracle | **Planificada** (solo evaluación, nada implementado) | Entorno reproducible para `AMALIA_PROD` | `docs/planes/fase8-evaluacion.md` |

---

## 4. Arquitectura

### 4.1 Flujo

```
systemd timer 05:30  ─ amalia-pull-deploy: sigue develop-oci si el canal se movió
systemd timer 07:00 (TZ El Salvador)
  └─ Etapa A (contenedor efímero): Playwright/Chromium ─ login+TOTP ─ API REST COMPRASAL
        └─ handoff durable (manifiesto + PDFs)
  └─ Etapa B (contenedor efímero, sin navegador):
        clasificación (lote) ─ pre-filtro RegEx ─ guardia de gasto ─ triaje ─ análisis
        ─ persistencia en ADB 23ai (vector + relacional) ─ correo digest SMTP
Servicio web residente (FastAPI + Caddy/TLS): /f/{token}/relevante|irrelevante, /healthz
```

Las dos etapas existen para que los picos de RAM de Chromium y del LLM no se sumen en una VM de 1 GB (`src/main.py --stage`, `docs/arquitectura/vision-general.md` Parte II).

### 4.2 Servicios OCI

| Servicio | Uso | Evidencia |
|---|---|---|
| Compute (E2.1.Micro / A1.Flex Always Free) | Orquestador efímero; contenedor por corrida; web + Caddy residentes | `oci/lib/02-compute.sh`, `docker-compose.*.yml` |
| Autonomous Database 23ai | Metadatos, estado del pipeline, AI Vector Search, embeddings ONNX en la base, feedback | `db/`, `oci/lib/05-adb.sh`, `06-onnx-stage.sh` |
| Generative AI (inferencia) | Triaje, análisis, clasificación, perfilado | `src/llm/proveedor_oci.py`; ADR 0004 |
| Object Storage | Handoff de PDFs (ciclo de vida 7 d) y respaldos con PAR | CHANGELOG 0.2.0; `oci/lib/07-backup.sh` |
| Vault | Wallet/secretos de la ADB | CHANGELOG 0.2.0 "Security" |
| Budgets / Usage API | Presupuesto del compartment (US$ 50, alerta al 80 %) y tarifa medida | `oci/lib/03-budget.sh`, `cost-watchdog.sh`; `quota.py` |
| Networking, DNS, IP reservada, Caddy (TLS) | Servicio web de feedback en `dev.amalia…` | `oci/lib/01-network.sh`, `08-web.sh` |

### 4.3 Integraciones externas

COMPRASAL (scraping + API REST), SMTP (Gmail App Password), GHCR (imagen multi-arch, variante `-oci`), GitHub Actions (CI y vigía de la ADB), Anthropic API (proveedor alterno, hoy no usado por DEV).

### 4.4 Modelos de IA

| Etapa | OCI GenAI (activo en DEV, familia `nativo`) | Anthropic (alterno) | Evidencia |
|---|---|---|---|
| Triaje GO/NO-GO | Llama 4 Scout | Claude Haiku 4.5 | `src/llm/modelos.py` |
| Análisis / matriz de riesgos | Llama 4 Maverick | Claude Sonnet 5 | idem |
| Clasificación por lote | Llama 4 Scout | Claude Haiku 4.5 | idem |
| Perfilado (one-shot) | Llama 4 Scout | Claude Haiku 4.5 | idem |
| Embeddings | `multilingual-e5-small` (ONNX, 384 dim) **dentro de la ADB**, sin API externa | — | CHANGELOG 0.2.0 |

Las familias `xai` (Grok) y `google` (Gemini) existen como opción pero sacan los pliegos de OCI hacia un tercero; el default es `nativo` y hay una advertencia de gobernanza (ADR 0004, "Gobernanza del dato").

---

## 5. Optimización de costos de IA

### 5.1 Técnicas aplicadas (con evidencia)

| Técnica | Aplicada | Evidencia |
|---|---|---|
| **Modelo por tarea** (barato para triaje/clasificación, mayor para análisis) | Sí | `src/llm/modelos.py` `DEFAULTS`/`DEFAULTS_OCI` |
| **Cambio de proveedor por configuración** para llevar la inferencia al contrato OCI | Sí | ADR 0004; `oci/tenants.yaml` |
| **Pre-filtro sin LLM** (RegEx de señales de inclusión/exclusión del cliente) antes de gastar tokens | Sí | `src/drilldown/prefilter.py` |
| **Embeddings y búsqueda vectorial dentro de la base** (cero costo de API de embeddings) | Sí | CHANGELOG 0.2.0; ADR 0001 |
| **Batching**: una llamada por lote de hasta 40 procesos, con lote derivado del techo de salida del modelo y split-y-reintento ante truncamiento | Sí | `src/classifier/engine.py` |
| **Límites de tokens** por etapa y clamp por modelo | Sí | `triage.py`, `analysis.py`, `modelos.py` |
| **Tope de páginas enviadas**: triaje con ≤ 5 páginas rasterizadas y ≤ 15 de texto; análisis ≤ 10 imágenes en OCI | Sí | `src/config.py` (`TRIAGE_RASTER_PAGES`) |
| **Embudo en cascada**: el análisis caro solo corre para los `GO` del triaje | Sí | `src/drilldown/pipeline.py`; CHANGELOG 0.2.0 |
| **Guardia de gasto diaria en dólares** (default **US$ 5,00/día**): estima antes (`cabe`), cobra el `usage` real después (`cobrar`); al agotarse, el pliego pasa a revisión manual | Sí | `config.py`; `quota.py`; ADR 0002 |
| **Tarifa medida contra la factura real** (no la de lista) | Sí | `quota.py` |
| **Libro de consumo por llamada** (tokens, caracteres, latencia, truncamiento, error, costo estimado; sin contenido) | Sí | `src/llm/registro.py`; `scripts/comparar_llm.py` |
| **Reintentos controlados** (3 intentos de TOTP; un solo split de lote) | Sí | CLAUDE.md RB-006; `engine.py` |
| **Rasterizado a resolución real** (antes se mandaba a 72 DPI, ilegible, forzando reproceso) | Sí | CHANGELOG 0.2.0 "Fixed" |
| **Prompt caching** (`cache_control`) | **No** | `grep cache_control src` sin resultados |
| **Caché de respuestas / compresión de contexto** | **No** como tal. Hay idempotencia por pliego (estado persistido) que evita reprocesar un pliego ya analizado | `src/memoria/repo.py` MERGE idempotente |

### 5.2 Hallazgos de costo que el propio desarrollo documentó

- **Fuga de gasto corregida:** el triaje de los `NO-GO` no se contabilizaba y era "el mayor consumidor del pipeline en un corpus mayoritariamente escaneado"; hoy se cobra por etapa (CHANGELOG 0.2.0; ADR 0002). El libro real lo confirma: **66 % de los pliegos analizados son escaneados** (171 de 258).
- **Tarifa de OCI medida:** US$ 0,00081 por 10.000 transacciones, frente a US$ 0,0018 de lista (2,2× menos) — `quota.py`, CHANGELOG 0.3.0.
- **Gasto de modelos que razonan (Grok/Gemini) subestimado ~25×** si se usa `completion_tokens`; corregido en la contabilidad (CHANGELOG 0.3.0).
- **Las imágenes no se facturan por su base64.** El libro registra 210,4 millones de caracteres base64 enviados en el triaje contra solo 2,64 millones de tokens de entrada: de facturarse como texto serían ≳ 60 millones. Esto cierra la pregunta que el ADR 0004 dejó abierta.

### 5.3 Costo real por operación (**del libro de consumo de la VM**)

Fuente: `data/llm_consumo.jsonl` de `multicomputos_dev`, copiado el 2026-10-22. **665 llamadas** entre 2026-09-23 y 2026-10-22, en 27 días con actividad. Todas con `LLM_PROVIDER=oci` salvo una llamada de Anthropic que falló. El costo es el **estimado por la propia guardia** (con su factor de seguridad), no la factura.

| Etapa | Modelo | Llamadas ok | Tokens entrada | Tokens salida | Latencia p50 / p95 | Costo estimado |
|---|---|---:|---:|---:|---|---:|
| Triaje | Llama 4 Scout | 265 | 2.635.384 | 20.379 | 2,3 s / 2,9 s | US$ 1,93 |
| Análisis | Llama 4 Maverick | 222 | 4.605.998 | 113.574 | 11,7 s / 17,2 s | US$ 3,36 |
| Clasificación | Llama 4 Scout | 171 | 708.571 | 164.254 | 9,5 s / 11,3 s | US$ 0,66 |
| **Total** | | **658** | | | | **US$ 5,95** |

**Lectura**

| Métrica | Valor | Cálculo |
|---|---|---|
| Gasto estimado por día con actividad | **≈ US$ 0,22** (mediana US$ 0,21 sin el día atípico) | US$ 5,95 / 27 días |
| Día más caro | US$ 0,99 (2026-10-22, 108 llamadas: día de despliegues y reprocesos) | `data/gasto_diario.json` |
| Uso del tope diario | máximo ≈ 20 % del tope de US$ 5 | 0,99 / 5,00 |
| Gasto mensual estimado | septiembre US$ 1,57 (desde el 23) · octubre US$ 4,38 (hasta el 22) · **proyección ≈ US$ 6–7/mes** | libro × 30 días |
| Costo por pliego analizado | **≈ US$ 0,02** (triaje + análisis, incluye reprocesos) | (1,93 + 3,36) / 258 pliegos |
| Truncamientos | **0** de 658 respuestas | campo `truncada` |
| Errores | **7 de 665 (1,1 %)**: 4 por HTTP 400 de OCI, 2 de credenciales mal configuradas en las primeras pruebas y 1 HTTP 400 de Anthropic | campo `error` |

**Contraste con la factura.** El presupuesto de OCI para el compartment `AMALIA_DEV` marcaba **US$ 1,94** de gasto real en octubre al 2026-10-22, contra US$ 4,38 estimados por el libro para el mismo mes: la guardia **sobreestima ≈ 2,3×** (usa un factor de seguridad de ×1,5 sobre una tarifa ya medida). Es coherente con los US$ 0,79 que se midieron en septiembre contra US$ 1,57 estimados. La facturación de OCI puede ir con retraso, así que esa razón es indicativa, no exacta. **Conclusión prudente: el costo real está en el orden de la mitad del estimado.**

**Lo que sigue sin medirse.** La mitad Claude de la comparación OCI vs Claude: el libro no tiene corridas con Anthropic, así que la afirmación "OCI es más barato" descansa en la tarifa por MTok, no en una comparación de factura sobre el mismo tráfico.

---

## 6. Métricas disponibles

### 6.1 Uso real en DEV (historial de la VM)

Fuente: `data/history/multicomputos.jsonl` y `multicomputos.analisis.jsonl`, copiados el 2026-10-22.

| Métrica | Valor | Notas |
|---|---|---|
| Corridas registradas | **42**, entre 2026-09-10 y 2026-10-22, en 37 días distintos | incluye reejecuciones manuales |
| Procesos revisados | **1.849** (≈ 44 por corrida) | `total_revisados` |
| Procesos marcados relevantes | **292 (15,8 %)**; 25 de las 42 corridas tuvieron al menos uno | es la relevancia que decide AMALIA, **no** está validada por una persona |
| Pliegos con análisis | **258 únicos** | estado final por proceso |
| Estado final de esos 258 | 202 notificados · 40 descartados en triaje (NO-GO) · 6 pendientes de revisión manual · 3 descartados por regex · 7 con error del LLM (2,7 %) | |
| Formato de los pliegos | 171 escaneados (66 %) · 80 nativos | |
| Última corrida | 2026-10-22: 72 procesos revisados, éxito | `data/last_run.json` |
| Votos de feedback | **2**, ambos "Irrelevante" y del desarrollador al probar el flujo (con motivo y comentario) | demuestra que el circuito funciona de punta a punta; **no es señal estadística** |

### 6.2 Ingeniería

| Métrica | Valor | Cómo se obtuvo |
|---|---|---|
| Clientes / usuarios | **1 cliente** (`multicomputos`), 4 destinatarios definidos; **hoy ninguno recibe el correo** (modo validación) | `clients/`; `oci/tenants.yaml` |
| Suite de pruebas | **1.076 pasaron, 86 deseleccionadas** (integración/Oracle local), 30,5 s | `pytest -q -m "not integration and not oracle_local"` (2026-10-22) |
| Inventario de pruebas | 53 archivos `test_*.py`, 1.000 funciones `def test_` | `find` / `grep` estático |
| Cobertura de código | **No medida** (no hay `pytest-cov` ni configuración de cobertura) | `pyproject.toml`, `ci.yml` |
| CI | ruff, pytest 3.11/3.12, bancos de bash, SQL contra Oracle 23ai, imagen multi-arch; 6 checks obligatorios en `main`/`develop` | `.github/workflows/ci.yml` |

**Líneas de código** (`wc -l` sobre archivos versionados, incluye blancos y comentarios):

| Lenguaje | Líneas | Nota |
|---|---:|---|
| Python | 36.199 | `src/` 16.173 · `tests/` 16.373 · resto scripts/oci |
| Shell | 6.454 | aprovisionamiento, despliegue, bancos de prueba |
| Markdown | 7.509 | documentación |
| SQL | 1.636 | `db/` |
| YAML | 1.465 | incl. workflows y `tenants.yaml` |
| HTML | 711 | plantillas de correo y portal |
| JavaScript | 30 | hover de gráficos |

Relación pruebas/código en `src/`: ≈ 1,01 líneas de test por línea de producción.

---

## 7. Valor para Multicómputos

### 7.1 Horas y costos ahorrados

**Horas ahorradas: no verificable.** Sigue sin existir una línea base de cuántas horas dedicaba el equipo a revisar COMPRASAL ni a leer pliegos. Cualquier cifra de ahorro de horas hoy sería inventada. Lo que cambió respecto al informe anterior es que **ya está el dato (b)**:

| Dato necesario | Estado |
|---|---|
| (a) horas-persona/semana del monitoreo manual | **Falta** — lo aporta el negocio |
| (b) volumen: procesos y pliegos por día | **Disponible**: ≈ 44 procesos revisados por corrida y 258 pliegos analizados en ~6 semanas (§6.1) |
| (c) costo hora del recurso | **Falta** — lo aporta el negocio |

Con (a) y (c) el cálculo es directo: ahorro = horas de revisión evitadas × costo hora − costo de operación (≈ US$ 6–7/mes de inferencia, §5.3, más la infraestructura Always Free).

Lo que sí es verificable:
- **Costo de operación acotado:** orquestador en Always Free, inferencia ≈ US$ 0,22/día estimados (≈ la mitad según la factura) y un tope de US$ 5/día.
- **Cobertura de lo publicado en 24 h** en cada corrida (RB-003 en `CLAUDE.md`).

### 7.2 Capacidades reutilizables / comercializables

| Capacidad | Dónde | Reutilización |
|---|---|---|
| Capa de proveedor de LLM con guardia de gasto y libro de consumo | `src/llm/`, `quota.py` | Cualquier solución de IA sobre OCI GenAI con control de costo |
| RAG con embeddings ONNX *dentro* de ADB 23ai | `src/memoria/`, `db/` | Demostración y plantilla de AI Vector Search sin servicios externos |
| Aprovisionamiento como código de OCI (red, VM, ADB, Vault, presupuesto, backup, SSH restringido, DNS/TLS) y auto-actualización por canal | `oci/` | Despliegue repetible de otros agentes; hay que migrarlo a Terraform si se quiere venderlo (evaluado, no hecho) |
| Esquema multi-tenant/multi-país y aislamiento por cliente | `db/schema_v2.sql`, `docs/planes/portal-amalia.md` | Base para atender a más de un cliente; el SaaS externo está solo planificado |
| Ciclo de feedback del usuario (voto firmado por destinatario) | `src/feedback/`, `src/web/` | Datos para evaluar y reentrenar la relevancia |
| Vigía de costo y de disponibilidad (watchdog + keepalive) | `oci/lib/cost-watchdog.sh`, `adb-keepalive.yml` | Operación de soluciones Always Free sin sorpresas |

### 7.3 Relación con servicios Oracle

AMALIA ejercita en un mismo producto: **ADB 23ai (Vector Search, ONNX in-database), OCI Generative AI, Compute, Object Storage, Vault, Budgets/Usage API, IAM y DNS**. Es un caso de referencia propio de "IA + base de datos Oracle + OCI" con un costo de operación medido y acotado. El valor comercial de esa relación (oportunidades de venta o consumo generadas) **no es verificable en el repo**.

---

## 8. Pendientes, riesgos y próximos pasos

### Pendientes conocidos

| Pendiente | Fuente |
|---|---|
| **Apagar el modo validación**: los destinatarios reales no reciben el reporte | `oci/tenants.yaml` (`override_to`); `STATUS.md` |
| **`STATUS.md` desactualizado** (última actualización 2026-09-30): describe una imagen vieja, 905 pruebas y feedback apagado. Además `tests/test_documentacion.py::test_status_no_esta_abandonado` falla en CI al pasar 30 días, es decir **hacia el 2026-10-30** | `STATUS.md`; `tests/test_documentacion.py` |
| **`CHANGELOG.md` sin entradas desde `v0.3.0`**: la sección "Sin publicar" está vacía pese a ~6 semanas de cambios (feedback con HTTPS, rediseño del correo, auto-actualización). Se escribe una vez, al reconciliar | `CHANGELOG.md` |
| **`AMALIA_PROD` no existe**; hay que decidir Terraform y guardas de dialecto antes de crearlo | `STATUS.md`; `docs/planes/fase8-evaluacion.md` |
| Fase "Contract" del esquema v2 (retirar tablas v1) y paridad v1/v2 sobre tráfico real | `STATUS.md` |
| Verificar el correo nuevo en Outlook de escritorio, Gmail y Apple Mail reales (el PR #72 solo lo comprobó en Chrome) | PR #72 "No verificado" |
| Límite conocido: `amalia-pull-deploy.sh` no recrea el contenedor `web` tras una auto-actualización | `oci/lib/amalia-pull-deploy.sh` |
| Programar `src/mercado` en cron (KPI 14) | `portal-amalia.md` Fase 2 |
| Identidad, roles y autenticación del portal (hoy sin auth) | `portal-amalia.md` Fase 3 |
| Cobertura de pruebas sin medir | §6.2 |

### Riesgos

| Riesgo | Detalle | Evidencia |
|---|---|---|
| **Sin uso real por destinatarios** | Ningún destinatario final recibe el reporte; los 2 votos de feedback son de pruebas | §6.1 |
| **Auto-actualización sin compuerta humana** | Todo merge a `develop` llega solo a la VM de DEV a las 05:30 y entra en la corrida de las 07:00. Es lo deseado en DEV; **no** debe heredarse a PROD sin una decisión explícita | PR #73; `amalia-pull-deploy.sh` |
| **Dependencia de una sola persona** | 100 % de los commits son de un autor; áreas marcadas "Emilio (expert)" exigen revisión humana | `git shortlog`; `CLAUDE.md` |
| **Fragilidad del scraping** | Depende de la estructura/API de COMPRASAL y de sesión con TOTP | `CLAUDE.md` Troubleshooting |
| **Calidad de la IA no medida** | No hay evaluación versionada de precisión del GO/NO-GO frente a un criterio humano; los 292 "relevantes" son juicio del modelo. El A/B Sonnet vs Opus figura como plan | `docs/planes/fase2-track-a.md` |
| **Pliegos escaneados largos** con OCI pierden contenido (tope de 10 imágenes) | "pérdida real" según el propio ADR; 66 % del corpus es escaneado | ADR 0004; §6.1 |
| **7 de 258 pliegos terminaron en `ERROR_LLM`** (2,7 %) y 6 en revisión manual | Se reintentan o se revisan a mano; sin tasa objetivo definida | `multicomputos.analisis.jsonl` |
| **Fallo silencioso del GO→NO-GO** con modelos que razonan; mitigado con piso de 2.048 tokens | | CHANGELOG 0.3.0 |
| **Gobernanza del dato** si se usan Grok/Gemini | | ADR 0004 |
| **Tarifa con descuento** de la tenancy puede no ser la efectiva futura | | `quota.py` |
| **Disponibilidad de GenAI**: no sirve en la región donde viven las VMs; se usa otra región | | ADR 0004 |

### Próximos pasos recomendados (según lo anterior)

1. Apagar el modo validación y medir 2–4 semanas de uso real (lectura, relevancia, votos de feedback).
2. Conseguir (a) y (c) del §7.1 para poder afirmar un ahorro de horas.
3. Evaluar la precisión del triaje contra una muestra de pliegos revisada por una persona.
4. Actualizar `STATUS.md` y `CHANGELOG.md` antes del 2026-10-30.
5. Decidir `AMALIA_PROD` (y Terraform) antes de crearlo, y qué política de actualización llevará.

---

## 9. Cinco logros cuantificables

1. **257 commits, 4 versiones etiquetadas y 1.076 pruebas automatizadas** pasando en ~30 s, con CI obligatorio de 6 checks, en 5,5 meses de desarrollo.
2. **1.849 licitaciones revisadas y clasificadas automáticamente** en 42 corridas diarias (2026-09-10 → 2026-10-22), con 292 marcadas como relevantes (15,8 %), en ambiente de desarrollo y sin intervención humana.
3. **258 pliegos analizados** (66 % escaneados) con triaje GO/NO-GO y matriz de riesgos; 202 llegaron al reporte. Costo de inferencia estimado de **≈ US$ 0,02 por pliego** (la factura de OCI sugiere cerca de la mitad).
4. **Inferencia en OCI Generative AI a ≈ US$ 0,22 por día** estimados (≈ US$ 6–7/mes) frente a un tope configurado de US$ 5/día, con **0 respuestas truncadas, 1,1 % de errores** y latencias de 2,3 s (triaje) y 11,7 s (análisis) en p50.
5. **Operación autónoma de DEV:** infraestructura como código, HTTPS con dominio propio, ciclo de feedback por correo y la VM se actualiza sola desde `develop`, todo con costo de infraestructura objetivo US$ 0 (Always Free).

*(Nota para quien presente: los logros 2 a 4 son datos reales del ambiente DEV, pero el costo es estimado por la guardia y el "relevante" es criterio del modelo; el ahorro de horas y la precisión frente a una persona siguen sin medirse — ver §5.3 y §7.1.)*
