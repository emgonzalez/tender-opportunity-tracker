# STATUS.md — tender-opportunity-tracker

> Última actualización: 2026-10-23 — vista operativa del portal (KPIs 11-13), `MERGE` de tags no
> destructivo, timer semanal de `src/mercado` (apagado) y planes de la Fase 3 y de la evaluación del triaje.
> Rama activa: `develop`
>
> **No hay producción viva.** El único ambiente desplegado es `multicomputos_dev`
> (compartment `AMALIA_DEV`). `AMALIA_PROD` no existe — ver "Tenant" abajo.
>
> Este archivo documenta el **estado actual**, no la historia: el historial de cambios
> por versión vive en [`CHANGELOG.md`](CHANGELOG.md); incidentes y diagnósticos ya
> cerrados, en `git log` y en los PRs correspondientes. Si un hecho de aquí tiene fecha
> de hace meses y nadie lo tocó desde entonces, no asumas que sigue siendo cierto —
> verificalo antes de citarlo.

---

## De un vistazo

| | |
|---|---|
| **Tenant** | `multicomputos_dev` (único; `multicomputos`/`emiliogonzalez`/`emiliogonzalez_e2` eliminados el 2026-09-08/20) |
| **Imagen desplegada** | `…:sha-ebb3817d…-oci` (redeploy 2026-10-22). Con `docker.channel: develop` la VM **se actualiza sola** a las 05:30 (`amalia-pull-deploy.timer`) y deja siempre un `sha-<40hex>` en el `.env`: lo que corre es lo que diga `--version`, no esta fila |
| **Feature flags activos** | `ENABLE_DRILLDOWN=true` · `ENABLE_MEMORIA=true` · `ENABLE_ESQUEMA_V2=true` · `ENABLE_FEEDBACK=true` · `LLM_PROVIDER=oci` (`OCI_FAMILIA=nativo`) |
| **Feedback (web)** | `https://dev.amalia.multicomputos.com/healthz` responde 200 desde afuera (IP reservada, DNS delegado, Caddy con TLS; solo expone `/f/*` y `/healthz`). Solo hay 2 votos, ambos de pruebas del desarrollador |
| **Esquema de BD** | v2 completo: expand → dual-write → backfill → **switch** (los 6 lectores migrados, PR #47-53). v1 y v2 verificados idénticos (37/37/127, 2026-09-30). Fase "Contract" (desmontar v1) no empezada. |
| **Costo** | Watchdog activo (`threshold_usd=$50`, acotado a `AMALIA_DEV`, cada 30 min). La alerta del presupuesto de OCI dispara al **80 %** (ajustada el 2026-10-22). Gasto de inferencia estimado ≈ US$ 0,22/día (la factura de OCI es cerca de la mitad) |
| **Notificación** | ⚠️ **MODO VALIDACIÓN activo** — todo el correo se redirige a `emiliogonzalez777@gmail.com`; los 4 destinatarios reales de `clients/multicomputos.yaml` no reciben nada |
| **Timer diario** | `amalia.timer` activo, 07:00 El Salvador; corrida de ayer (2026-10-22) con 72 procesos revisados |
| **Testing** | `pytest -m "not integration and not oracle_local"` → 1093 passed, 86 deselected (2026-10-23); + suite de integración contra Oracle 23ai local, corrida por CI en cada push |
| **CI** | `ruff` + pytest (3.11/3.12) + bancos de bash + SQL contra Oracle 23ai + imagen multi-arch a GHCR — en cada push, `.github/workflows/ci.yml` |

---

## Qué falta — pendientes reales conocidos

| Pendiente | Por qué importa | Bloquea algo |
|---|---|---|
| Apagar MODO VALIDACIÓN | Los 4 destinatarios reales de Multicómputos no están recibiendo el reporte diario | No — es una decisión del operador, no un bug |
| Fase "Contract" del esquema v2 | Desmontar `licitaciones_metadata`/`licitaciones_chunks`/`licitaciones_analisis` (v1) una vez que v2 demuestre ser confiable con tráfico real | No — deliberadamente diferida, ver `db/schema_v2.sql:8-14` |
| Paridad v1/v2 manual sobre tráfico real (no solo conteos agregados) | `buscar_precedentes_v2` es más estricto que v1 (exige fila en `cliente_proceso`); falta confirmar que ningún cliente con historial real pierde precedentes por un hueco del backfill | No |
| Correr `python -m src.memoria.backfill_v2 --dry-run` y luego sin `--dry-run` en la VM | Rellena tags/categoría de `cliente_proceso` con el JSONL; desbloquea los KPIs 5 y R4 (PR #76). No ejecutado | No |
| Activar `enable_mercado_cron` | Programa `participados` + `resolver` los domingos 11:00 (PR #78, KPI 14). Falta renovar la sesión de COMPRASAL (`storage_state`) o fallará cada semana, y correr el barrido `mercado` aparte | No — decisión del operador |
| Ver `/portal/operativo` con datos reales de la ADB | Las consultas de los KPIs 11-13 solo se probaron con una conexión simulada (`PERCENTILE_CONT`, latencias NULL, zona horaria) | No |
| Guardar el motivo del triaje (`anexos["triaje_motivo"]`) antes de evaluar su precisión | 26 de los 40 NO-GO tienen confianza exactamente 0,0, que es también la firma de `parse_error`; hoy no se pueden distinguir (ver [`docs/planes/evaluacion-precision-triaje.md`](docs/planes/evaluacion-precision-triaje.md), PR #79) | No |
| Verificar el correo nuevo en Outlook de escritorio, Gmail y Apple Mail reales | El PR #72 solo lo comprobó renderizado en Chrome (claro, oscuro, móvil) | No |
| `amalia-pull-deploy.sh` no recrea el contenedor `web` tras una auto-actualización | Si cambia el código del servicio web, el contenedor residente sigue con la imagen vieja hasta el próximo `deploy.sh` | No |
| `AMALIA_PROD` y la política de actualización que llevará | La auto-actualización sin compuerta humana es lo deseado en DEV; no debe heredarse a PROD por omisión | No — decisión previa a crearlo |
| Medir uso real: ahorro de horas y precisión del GO/NO-GO frente a una persona | No hay línea base ni evaluación; ver [`docs/AMALIA_informe_avances.md`](docs/AMALIA_informe_avances.md) | No |
| `docs/planes/fase8-evaluacion.md` (Terraform + guardas de dialecto) | Material de **decisión**, nada implementado. El propio documento dice: decidir conscientemente antes de que exista `AMALIA_PROD`, no por omisión | No — opcional |

---

## Historial reciente relevante (2026-09 y 2026-10)

Resumen de los cambios de las últimas semanas que definen el estado de arriba — el
detalle línea por línea está en `git log` y en los PRs citados, no acá:

- **2026-09-08/20** — Infraestructura reducida a un solo tenant: se eliminaron
  `multicomputos` (prod, sin infra desde el 09-08), `emiliogonzalez` y
  `emiliogonzalez_e2` (incluida su ADB `AMALIAE2`). `oci/tenants.yaml` define
  únicamente `multicomputos_dev`.
- **2026-09-23/24** — Fase 9: proveedor de LLM configurable (`src/llm/`,
  [ADR 0004](docs/decisiones/0004-abstraccion-de-proveedor-llm.md)). `multicomputos_dev`
  pasa a inferencia en OCI Generative AI (`LLM_PROVIDER=oci`). Release `v0.3.0`.
- **2026-09-27/28** — Esquema v2 "Expand" + "Backfill" aplicados contra la ADB real
  (PRs #38-43). `ENABLE_ESQUEMA_V2=true` se activó a mano en el `.env` de la VM —
  sin efecto real hasta el redeploy del 09-30 (ver abajo).
- **2026-09-29** — Cuatro bugs del watchdog de costo (`oci/lib/cost-watchdog.sh`)
  encontrados y arreglados en producción (PRs #44-46): CLI de OCI faltante, fecha sin
  truncar para `--granularity MONTHLY`, scope de tenancy completa en vez de solo
  `AMALIA_DEV`, y `jq` leyendo `computed-amount` como una resta.
- **2026-09-30** — Fase "switch" del esquema v2: los 6 lectores migrados a v2
  (PRs #47-53). Al verificar contra la ADB real se encontró que la VM corría una
  imagen **56 commits atrás de `develop`** (desde antes de todo el esquema v2) y que
  `ENABLE_ESQUEMA_V2` nunca estuvo gobernado por tenant (PR #54, lo arregla). Redeploy
  ejecutado; backfill re-corrido para cerrar el hueco de 3 días; v1==v2 verificado.
- **2026-10-02** — Primera vista de país del portal (`/portal`, KPIs 1-6, PR #56-#59), sin
  autenticación y sin exponer por el proxy público.
- **2026-10-23** — Vista operativa `/portal/operativo` (KPIs 11-13, #77); el `MERGE` de
  `cliente_proceso` ya no pisa tags con vacío (#76); timer semanal de `src/mercado` detrás de
  `enable_mercado_cron` (#78, apagado); planes de la Fase 3 del portal y de la evaluación del
  triaje (#79).
- **2026-10-22** — Feedback en DEV: HTTPS con dominio propio (PR #60-#61, IP reservada
  nueva) y `ENABLE_FEEDBACK=true` (#62). Correo rediseñado en seis PRs (#64-#72): liviano,
  digest en tres secciones y lenguaje visual nuevo. Auto-actualización de la VM
  (`channel: develop`, #73). El correo de validación llega también a la casilla corporativa
  (#71).
- **2026-10-22** — Alerta de presupuesto de DEV de 1 % a 80 % (#74); la regla viva se ajustó
  a mano. Informe de avances regenerado con datos reales de la VM.

---

## Componentes

| Componente | Archivos | Notas |
|---|---|---|
| Scraper | `src/scraper/` | Playwright headless, DOM + API REST, TOTP |
| Classifier | `src/classifier/` | Claude Haiku 4.5 (o el proveedor configurado), batch, scoring/tags/marcas |
| Drill-down | `src/drilldown/` | Tras `ENABLE_DRILLDOWN` |
| Memoria institucional | `src/memoria/` | Tras `ENABLE_MEMORIA`; lectores v1 Y v2 coexisten (switch activo) |
| LLM (proveedor) | `src/llm/` | Claude ⇄ OCI Generative AI, libro de consumo en `data/llm_consumo.jsonl` |
| Feedback (Relevante/Irrelevante) | `src/feedback/`, `src/web/` | Enlaces firmados HMAC por destinatario; activo en DEV |
| Mercado (adjudicaciones) | `src/mercado/` | Resultado propio + terceros, para entrenar a AMALIA |
| Notifier | `src/notifier/` | Jinja2 email-safe, alertas de sesión/circuit breaker |

Ver [`CLAUDE.md`](CLAUDE.md) § Project Structure para el árbol completo — ese árbol lo
verifica `tests/test_documentacion.py` en cada push, así que es la fuente más confiable
de "qué módulo existe y para qué".

---

## Cliente activo: MULTICOMPUTOS

| Propiedad | Valor |
|---|---|
| `client_id` | `multicomputos` |
| Destinatarios reales | 4 (`michael.albert`, `astrid.cruz`, `mauricio.salinas`, `emilio.gonzalez`@multicomputos.com) — **actualmente no reciben nada**, ver MODO VALIDACIÓN arriba |
| Tags / categorías | 44 tags, ~41 items de marca entre categorías — ver `clients/multicomputos.yaml` |

---

## How to Update This File

Se actualiza **cuando cambia algo que alguien necesitaría saber antes de tocar el
sistema**: un redeploy, un cambio de feature flag en producción, un tenant que se crea
o se destruye, una fase que cierra, un bug de infraestructura encontrado y arreglado.
No para commits menores (`chore`, `docs`, un fix de test).

Al actualizar: cambiar la fecha de "Última actualización" en el encabezado, y agregar
una línea al "Historial reciente relevante" — sin repetir el detalle que ya vive en
`CHANGELOG.md` o en el PR correspondiente (regla de oro: un hecho, un hogar).

**Enforcement mecánico:** `tests/test_documentacion.py::test_status_no_esta_abandonado`
falla en CI si pasaron más de 30 días desde la fecha declarada arriba — no garantiza que
el *contenido* siga siendo preciso, pero garantiza que alguien lo mire periódicamente.
Si el chequeo falla y el contenido sigue siendo cierto, alcanza con bump de la fecha —
pero leé el archivo primero: la vez que esto se abandonó de verdad (hasta el 2026-09-30),
la fecha declarada tenía solo 6 días, y aun así todo el contenido estaba desactualizado
por semanas. La fecha es una señal de "alguien lo revisó", no un sustituto de revisarlo.
