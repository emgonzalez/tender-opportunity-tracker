# STATUS.md — tender-opportunity-tracker

> Última actualización: 2026-09-30 — redeploy de `multicomputos_dev` (imagen al día con
> `develop`, esquema v2 "switch" completo) + watchdog de costo arreglado de punta a punta.
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
| **Imagen desplegada** | `ghcr.io/emgonzalez/tender-opportunity-tracker:sha-b0262e78…-oci`, al día con `develop` (redeploy 2026-09-30) |
| **Feature flags activos** | `ENABLE_DRILLDOWN=true` · `ENABLE_MEMORIA=true` · `ENABLE_ESQUEMA_V2=true` · `LLM_PROVIDER=oci` (`OCI_FAMILIA=nativo`) |
| **Esquema de BD** | v2 completo: expand → dual-write → backfill → **switch** (los 6 lectores migrados, PR #47-53). v1 y v2 verificados idénticos (37/37/127, 2026-09-30). Fase "Contract" (desmontar v1) no empezada. |
| **Watchdog de costo** | Activo y verificado: `threshold_usd=$50` acotado al compartment `AMALIA_DEV` (no toda la tenancy), corriendo limpio cada 30 min desde el 2026-09-29 noche |
| **Notificación** | ⚠️ **MODO VALIDACIÓN activo** — todo el correo se redirige a `emiliogonzalez777@gmail.com`; los 4 destinatarios reales de `clients/multicomputos.yaml` no reciben nada |
| **Timer diario** | `amalia.timer`, activo, próxima corrida 2026-10-01 07:00 El Salvador |
| **Testing** | `pytest -m "not integration and not oracle_local"` → 905 passed (2026-09-30); + suite de integración contra Oracle 23ai local, corrida por CI en cada push |
| **CI** | `ruff` + pytest (3.11/3.12) + bancos de bash + SQL contra Oracle 23ai + imagen multi-arch a GHCR — en cada push, `.github/workflows/ci.yml` |

---

## Qué falta — pendientes reales conocidos

| Pendiente | Por qué importa | Bloquea algo |
|---|---|---|
| Apagar MODO VALIDACIÓN | Los 4 destinatarios reales de Multicómputos no están recibiendo el reporte diario | No — es una decisión del operador, no un bug |
| Fase "Contract" del esquema v2 | Desmontar `licitaciones_metadata`/`licitaciones_chunks`/`licitaciones_analisis` (v1) una vez que v2 demuestre ser confiable con tráfico real | No — deliberadamente diferida, ver `db/schema_v2.sql:8-14` |
| Paridad v1/v2 manual sobre tráfico real (no solo conteos agregados) | `buscar_precedentes_v2` es más estricto que v1 (exige fila en `cliente_proceso`); falta confirmar que ningún cliente con historial real pierde precedentes por un hueco del backfill | No |
| El "plan del portal AMALIA" (citado en comentarios como `§3`, `§3.7 punto 4`, etc.) no existe como documento accesible | El código cita secciones de un plan que se perdió — probablemente se discutió en una conversación que no se guardó. Solo quedan las citas dispersas en los commits | No, pero es deuda de documentación real |
| `docs/planes/fase8-evaluacion.md` (Terraform + guardas de dialecto) | Material de **decisión**, nada implementado. El propio documento dice: decidir conscientemente antes de que exista `AMALIA_PROD`, no por omisión | No — opcional |

---

## Historial reciente relevante (2026-09)

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

---

## Componentes

| Componente | Archivos | Notas |
|---|---|---|
| Scraper | `src/scraper/` | Playwright headless, DOM + API REST, TOTP |
| Classifier | `src/classifier/` | Claude Haiku 4.5 (o el proveedor configurado), batch, scoring/tags/marcas |
| Drill-down | `src/drilldown/` | Tras `ENABLE_DRILLDOWN` |
| Memoria institucional | `src/memoria/` | Tras `ENABLE_MEMORIA`; lectores v1 Y v2 coexisten (switch activo) |
| LLM (proveedor) | `src/llm/` | Claude ⇄ OCI Generative AI, libro de consumo en `data/llm_consumo.jsonl` |
| Feedback (Relevante/Irrelevante) | `src/feedback/`, `src/web/` | Enlaces firmados HMAC por destinatario |
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
