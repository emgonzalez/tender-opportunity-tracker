# Plan del Portal AMALIA — Analytics, KPIs y arquitectura de información

## Contexto

El "plan del portal AMALIA" existe como **cita, no como documento**: `db/schema_v2.sql`
y media docena de módulos citan secciones (`§3`, `§3.7 punto 4`, `§5.2`, `§5.4`, `§9
Fase 0`) de un plan que se discutió alguna vez y nunca se guardó — confirmado hoy
(2026-09-30, ver `STATUS.md` → "Qué falta") con una búsqueda exhaustiva en el repo, en
todo el historial de git y en los Artifacts de la cuenta: no existe en ningún lado
accesible. Este documento lo reemplaza con una fuente real, versionada, que vive en
`docs/planes/` (la convención que este mismo repo ya usa) para que no se vuelva a perder.

El pedido explícito es un plan de **analytics y UX/UI** — no solo "qué tablas faltan".
Sigue la metodología validada de la skill `dataviz` de este entorno (forma del dato →
color → specs de marca → interacción → accesibilidad) y los principios pedidos:
pirámide invertida, minimalismo, ningún número sin contexto, color estratégico, y el
tipo de gráfico correcto para cada dato — nunca al revés.

**Hallazgo que fija el alcance real:** el esquema v2 (`db/schema_v2.sql`) ya diseñó la
capa analítica completa (`oportunidad_estado`, `oportunidad_evento`, `ejecucion_etapa`,
`kpi_snapshot`, identidad/roles) — pero **de las 4 tablas analíticas, solo una tiene un
escritor real** (`oportunidad_estado`, vía `src/mercado/repo.py`, y ese módulo corre
manual por CLI, no programado). Las otras tres (`oportunidad_evento`, `ejecucion_etapa`,
`kpi_snapshot`) son esquema puro, sin una sola fila. Diseñar KPIs sobre una tabla vacía
es la forma más común de que un dashboard salga mal — así que este plan **fasea los
KPIs según qué dato ya es real hoy**, no según qué tabla ya existe.

## Decisión de arquitectura (ya tomada, ver el intercambio previo)

**Server-rendered: FastAPI + Jinja2 + routers, extendiendo `src/web/` (la semilla de
ADR 0005) + SVG inline con hover/tooltip en JS vanilla/HTMX.** No SPA. Motivo resumido:
la calidad visual la dan las specs (marca, color, espaciado), no el framework; HTMX
cubre filtros/drill-down sin el costo de un ecosistema JS aparte; el repo entero vive
bajo la premisa Always Free/minimalista y ADR 0005 ya construyó y probó esta semilla
(`crear_app`, routers montables, middlewares de seguridad, Jinja2 con autoescape).

---

## 1. Audiencia — quién lo usa, qué decide, y desde dónde

Confirmado directamente por Emilio (2026-09-30), más preciso que lo que el pitch deck
por sí solo permitía inferir:

| Rol | Alcance | Qué necesita decidir |
|---|---|---|
| **CEO** | Todos los países | Panorama ejecutivo del grupo completo |
| **Director de Estrategia** (también Gerente de País en algunos países) | Todos los países (+ el suyo con detalle de operador si aplica) | Estrategia regional; en los países donde además es gerente de país, también el día a día local |
| **Director Comercial** | Todos los países | Pipeline comercial consolidado — distinto de Gerente de País, es la cabeza de ventas a nivel grupo, no de un país |
| **Gerente de Marca Oracle** | Todos los países | Oportunidades con componente Oracle, consolidadas — es un rol de línea de producto, no de territorio |
| **Gerente Regional Comercial Oracle** | Todos los países | Pipeline Oracle a nivel regional |
| **Director de Servicios Oracle** | Todos los países | Oportunidades de servicios (no licencias) Oracle a nivel regional |
| **Gerente de País** (uno por país, lidera el equipo de ventas local) | Solo su país | ¿Hay oportunidades nuevas en mi país? ¿Dónde está comprando el gobierno local? |
| **Equipo de Ventas** | Su país | Contexto por proceso antes de una reunión — vista de detalle, no de agregados |
| **Operador/Admin** (técnico, no de negocio) | Según alcance de la plataforma | ¿Corrió bien hoy? ¿Cuánto gastamos? |

**Corrección sobre la versión anterior de este plan:** Gerente de Marca Oracle y
Director Comercial son **regionales**, no por país — y Director Comercial **no es**
el mismo rol que Gerente de País (son dos cabezas distintas: una de territorio, una
de producto/pipeline a nivel grupo). Son **6 roles regionales** (CEO, Director de
Estrategia, Director Comercial, Gerente de Marca Oracle, Gerente Regional Comercial
Oracle, Director de Servicios Oracle) contra **2 roles de país** (Gerente de País,
Equipo de Ventas) — la vista regional (§3, KPIs R1-R6 abajo) es, numéricamente, la
vista que más roles distintos consume.

**Regla de acceso, textual:** *"Los roles regionales o directivos deben tener acceso
a la información de todos los países, el resto, solo al país en el que operan."* —
esta es la regla de autorización que gobierna toda vista del portal, no una
preferencia de UI: cualquier consulta de KPI filtra primero por el `alcance` del rol
antes de aplicar cualquier otro filtro de usuario.

### 1.1 Países y el hueco real de datos

Multicómputos opera en 8 países: **República Dominicana, Puerto Rico, Panamá, Costa
Rica, Honduras, El Salvador, Guatemala y Ecuador.** Verificado hoy contra el código
(no es una suposición): **AMALIA solo escanea COMPRASAL (El Salvador)** —
`src/scraper/extractor.py` está íntegramente hardcodeado a esa API
(`COMPRASAL_BASE`, rutas `/pac-procesos/...`, sin ninguna capa de adaptador por
portal), `clients/` tiene un solo archivo (`multicomputos.yaml`), y el propio POC
histórico ya lo registraba como hueco conocido: *"No multi-region — Only monitors
COMPRASAL (El Salvador). Adding Guatemala/Honduras requires portal integration"*
(`docs/historico/spec-poc-v0.1.0.md:398`).

**Esto separa dos proyectos que no hay que confundir:**
1. **Este plan (portal/analytics):** diseña el modelo de ROLES Y ACCESO para los 8
   países desde ya — es barato hacerlo bien ahora porque nada lo lee todavía
   (`db/schema_v2.sql:524`: "sin lectores todavía").
2. **Proyecto aparte, no cubierto acá (scraper multi-portal):** construir el
   extractor de cada uno de los otros 7 portales de compras gubernamentales, uno por
   país, antes de que el portal tenga un solo dato real que mostrar fuera de El
   Salvador. Es trabajo de `src/scraper/`, de alcance comparable al que ya tomó
   construir el de COMPRASAL — no es "agregar una fila a una tabla".

**Consecuencia concreta para las Fases de §3:** las Fases 1-2 (datos reales) solo
tienen datos de **El Salvador**. El portal se construye multi-país desde el día uno
(el filtro de país y el modelo de acceso están siempre ahí), pero para los otros 7
países va a mostrar honestamente "sin datos — portal de compras no integrado todavía"
en vez de simularlo con ceros. Mostrar un cero donde en realidad no hay integración
es peor que no mostrar nada: un cero se lee como "no hay oportunidades", no como "no
estamos mirando este país todavía".

### 1.2 Ajuste de esquema necesario (barato ahora, caro después)

`cliente` hoy es 1:1 con "el YAML de un cliente" (`db/schema_v2.sql:116-118`) — para
que **un** `cliente_id` represente **un país** de Multicómputos (ej.
`multicomputos_sv`, `multicomputos_do`, `multicomputos_pa`...) esto ya encaja sin
tocar el DDL: la organización raíz sigue siendo `multicomputos` (el grupo), y cada
país es un `cliente` bajo ella — exactamente la relación 1→N que `organizacion →
cliente` ya modela.

Lo que **sí** exige un ajuste: `membresia` tiene PK `(usuario_id, cliente_id)`
(`db/schema_v2.sql:554`) — un rol regional/directivo con acceso a los 8 países
necesitaría 8 filas repetidas (una por país), lo cual funciona pero es frágil (dar de
alta un noveno país algún día obliga a recordar insertar la fila para cada CEO/Director
regional existente). Mejor: agregar una fila **a nivel organización**, no por cliente
— ej. una tabla `membresia_org(usuario_id, org_id, rol)` separada, o relajar
`cliente_id` a NULLABLE en `membresia` con la semántica "toda la organización" cuando
es NULL. Cualquiera de las dos es una decisión de Fase 3 (identidad, sin lectores
todavía, cero riesgo de migración) — se señala acá para que no se construya la UI de
roles sobre el supuesto equivocado de "siempre hay que elegir un país".

**Códigos de rol (`membresia.rol`, `db/schema_v2.sql:557-558`) — con los 8 roles de
negocio reales ya nombrados, los 9 códigos existentes de dos letras (`PA,AO,OP,EJ,DC,GM,VE,PV,EX`)
se quedan cortos.** Contando también los 3 roles técnicos (Admin de Plataforma, Admin
de Organización, Operador) son **11 roles reales para 9 códigos** — no vale la pena
forzar el mapeo viejo (que nunca tuvo glosario en ningún lado, era conjetura desde el
principio) sobre nombres de negocio ya confirmados. Propuesta: ampliar el `CHECK
constraint` a 11 códigos (**decisión 2026-10-24: palabras completas en minúscula, no siglas** — se deducen del rol sin glosario y se leen en logs y auditoría; la columna pasa a `VARCHAR2(32)`) (cambio de una línea, **sin lectores todavía** —
`db/schema_v2.sql:524` — cero riesgo de migración), con esta asignación:

| Código | Rol | Alcance |
|---|---|---|
| `ceo` | CEO | Regional |
| `director_estrategia` *(nuevo)* | Director de Estrategia | Regional |
| `director_comercial` | Director Comercial | Regional |
| `gerente_marca_oracle` | Gerente de Marca Oracle | Regional |
| `gerente_regional_comercial_oracle` | Gerente Regional Comercial Oracle | Regional |
| `director_servicios_oracle` | Director de Servicios Oracle | Regional |
| `gerente_pais` *(nuevo)* | Gerente de País | Su país |
| `equipo_ventas` | Equipo de Ventas | Su país |
| `admin_plataforma` | Admin de Plataforma (soporte técnico de AMALIA) | Según alcance de la plataforma |
| `admin_organizacion` | Admin de Organización | Regional |
| `operador` | Operador (pipeline/costo, no ventas) | Según alcance de la plataforma |

Esto es una propuesta técnica razonable a partir de nombres ya confirmados, no una
conjetura como la versión anterior — igual queda para que la confirmes antes de
tocar el DDL (Fase 3, no bloquea nada de las Fases 1-2).

## 2. Principios de diseño (aplicados, no solo citados)

- **Pirámide invertida, a nivel de página Y de KPI.** La respuesta primero: el número,
  después su contexto, después el detalle que lo explica. Sin scroll: 3-4 stat tiles.
  Con un scroll: los 2 gráficos de contexto. Detrás de un filtro/click: la tabla.
- **Ningún número solo** (contrato de `dataviz/references/marks-and-anatomy.md` §Figuras):
  todo KPI de Tier 1-2 es un **stat tile** = `valor` + `delta` (firmado, contra un
  período nombrado, color = dirección × si subir es bueno) + `trend` (sparkline de 12
  puntos). Un número sin esos tres elementos no es un KPI, es un dato suelto.
- **El tipo de gráfico lo decide el trabajo del dato, nunca la costumbre**
  (`dataviz/references/choosing-a-form.md`):

  | KPI | Trabajo del dato | Forma | Color |
  |---|---|---|---|
  | Oportunidades del mes | 1 valor + tendencia | Stat tile + sparkline | sequential (1 hue) |
  | Instituciones que más compran | comparar magnitud, ranking | Bar horizontal, top 10 | sequential (1 hue) |
  | Embudo DESCUBIERTO→NOTIFICADO | conversión por etapa ordenada | **Funnel** | 1 hue en rampa (ordinal) |
  | Gasto LLM vs presupuesto diario | 1 ratio contra un límite | **Meter** (misma rampa) | accent→warning→danger |
  | Gasto por modelo (varios modelos a la vez) | distinguir series | Bar agrupado o stacked | **categorical** |
  | Latencia / tasa de error en el tiempo | tendencia | Línea | sequential o 1 categorical |
  | Pipeline comercial (NUEVO→GANADO/PERDIDO) | conversión + estados terminales | Funnel + diverging para Ganado/Perdido | diverging (verde/rojo del status palette, NUNCA categorical) |
  | Motivos de descarte "Irrelevante" | distinguir categorías (9 motivos) | Bar horizontal (NO pie — son 9, tope de categórico son 8) | categorical, fold a "Otro" si excede |
  | % relevancia (clasificador vs. confirmado) | 1 ratio | Stat tile, nunca gauge/dona | sequential |

- **Minimalismo y espacio en blanco:** máx. 4 stat tiles por fila; cards con padding
  generoso; grid/ejes en gris recesivo de un paso fuera de superficie (`#e1e0d9` claro
  / `#2c2c2a` oscuro); texto nunca en el color de la serie (tokens de texto: primario
  `#0b0b0b`, secundario `#52514e`, muted `#898781`); leyenda solo si ≥2 series.
- **Color estratégico, no decorativo** — paleta validada de `dataviz/references/palette.md`
  (ya corre el validador, no hace falta re-certificarla a mano):
  - Secuencial por defecto (magnitud): azul `#2a78d6` → oscuro `#104281`.
  - Categórico (identidad, cuando la serie ES el punto): orden fijo de 8, nunca ciclado
    — azul, naranja, aqua, amarillo, magenta, verde, violeta, rojo. Tope de 3 en formas
    all-pairs (scatter/burbuja); tope de 8 en adyacentes (barras/líneas/stacks).
  - Diverging (Ganado/Perdido, por encima/debajo de meta): azul↔rojo, punto medio gris
    `#f0efec`.
  - Status (reservado, nunca para "serie 4"): good `#0ca30c`, warning `#fab219`, serious
    `#ec835a`, critical `#d03b3b` — siempre con ícono + etiqueta, nunca solo color.
  - Esta es la paleta **de referencia** (validada, lista para usar); el día que
    Multicómputos defina colores de marca, se sustituyen los valores de este bloque y
    se re-corre `validate_palette.js` — la metodología no cambia.
- **Interacción por defecto:** todo gráfico lleva tooltip/crosshair (línea/área) o hover
  por marca (barra/punto) — es la forma honesta de mostrar el valor exacto sin flotar un
  número en cada punto. Filtros (rango de fecha, cliente si aplica) en una sola fila
  arriba de los gráficos, nunca repetidos por widget.

## 3. Catálogo completo de KPIs — con el estado verificado contra la ADB real

**Regla de oro de este plan:** un KPI no se muestra hasta que su dato sea real. Mejor
una vista con 4 KPIs honestos que 10 con la mitad en cero permanente. Por eso cada
fila de abajo lleva una bandera verificada **hoy mismo** (2026-10-01, 04:40 El
Salvador) contra la ADB real de `multicomputos_dev` — no es una inferencia de "esta
tabla debería tener datos", es `SELECT COUNT(*)` corrido de verdad:

- 🟢 **Activo** — hay filas reales, el KPI se puede construir ya.
- 🟡 **Escritor activo, sin filas todavía** — el código que llena esta tabla ya está
  desplegado (`enable_memoria=true` y equivalentes), pero la corrida que la puebla por
  primera vez con el código nuevo (redeploy del 2026-09-30, posterior a la corrida de
  esa mañana) todavía no pasó — la de hoy, 07:00, es la primera candidata.
- 🔴 **Sin escritor** — nadie llena esta tabla todavía; mostrar este KPI hoy sería
  mostrar un cero permanente disfrazado de dato.

### Tabla maestra

| # | KPI | Rol principal | Fuente | Forma | Estado hoy (verificado) |
|---|---|---|---|---|---|
| 1 | Oportunidades relevantes del mes (+ delta vs. el mismo tramo del mes anterior) | Gerente de País | `cliente_proceso` (`es_relevante=1`/`triaje='GO'`) | Stat tile | 🟢 37 filas |
| 2 | **Aptos para decidir** = aptos / revisados, con apoyo «% marcados relevantes» = relevantes / revisados *(etiqueta honesta, no es precisión real — ver #9; definición desde E11.1, [aquí](#e111--relevancia-estricta-y-descartados))* | Gerente de País | `cliente_proceso` (aptos y relevantes) + `ejecucion` (revisados) | Stat tile | 🟢 37 filas |
| 3 | Instituciones que más publican (top 10) | Gerente de País | `proceso.institucion` **(crudo, no `institucion_norm` — ver hallazgo abajo)** | Bar horizontal | 🟢 29 instituciones distintas en 37 filas |
| 4 | Embudo del pipeline: DESCUBIERTO → … → NOTIFICADO | Gerente de País | `cliente_proceso.estado` | Funnel | 🟡 datos reales pero sin variación — ver hallazgo |
| 5 | Categorías/marcas más detectadas | Gerente de País, Gerente de Marca | `cliente_proceso.tags`/`categoria_detectada` | Bar horizontal | 🟡 campo vacío en 37/37; el escritor ya no pisa (PR #76), falta correr el backfill en la ADB — ver hallazgo |
| 6 | Severidad de riesgos detectados (alta/media/baja) | Gerente de País | `analisis.severidad_max` | Bar horizontal (ordinal) | 🟡 dato existe pero sin variación — ver hallazgo |
| 7 | Monto estimado total de oportunidades del mes | Gerente de País, Director Comercial | `proceso.monto_usd` | Stat tile | 🔴 NULL en 37/37 — COMPRASAL no lo publica o el extractor no lo captura |
| 8 | Licitaciones por vencer esta semana (urgencia) | Equipo de Ventas | `proceso.fecha_limite_utc` | Tabla ordenada, no chart | 🔴 NULL en 37/37 |
| 9 | % confirmado relevante por el cliente (precisión real) | Gerente de País | `feedback` v2 | Stat tile | 🔴 `ENABLE_FEEDBACK=false`, ADR 0005 Fase 2 sin desplegar |
| 10 | Motivos de "Irrelevante" (9 categorías del catálogo) | Gerente de País | `feedback.motivo` | Bar horizontal | 🟡 E11 lo muestra en el bloque de votos (conteos y motivos, nunca `destinatario`; §7). Con `ENABLE_FEEDBACK` apagado no hay votos que mostrar. No ajusta el clasificador |
| 11 | Estado de la última corrida (OK/ERROR, hace cuánto) | Operador | `ejecucion` (v1) | Stat tile + badge | 🟢 construido (`/portal/operativo`, PR #77); falta verlo con datos reales de la ADB |
| 12 | Gasto LLM hoy vs. presupuesto diario | Operador | `llm_llamada` (+ `drilldown/quota.py` para el local del día) | Meter | 🟢 construido (PR #77): `SUM(costo_estimado_usd)` de `llm_llamada` contra `daily_budget_usd` |
| 13 | Latencia y tasa de error del pipeline LLM (14 días) | Operador | `llm_llamada` | Línea | 🟢 construido (PR #77): p50/p95 y tasa de error por día, 14 días |
| 14 | Ganado / Perdido / Pendiente (automático, desde mercado) | Director Comercial | `oportunidad_estado` (`origen='AUTOMATICO_MERCADO'`) | Funnel + diverging | 🟡 timer semanal listo y apagado (`enable_mercado_cron`, PR #78); falta activarlo, renovar la sesión de COMPRASAL y correr el barrido `mercado` |
| 15 | Tasa de conversión detectado → ofertado → ganado | Director Comercial | `oportunidad_evento` | Funnel | 🟢 construido en E5 (`src/analytics/pais.py`, `conversion_comercial`); se conserva en el bloque de análisis de la vista de país (§7) |
| 16 | Tiempo promedio en cada etapa del pipeline comercial | Director Comercial | `oportunidad_evento.ts` | Bar horizontal (duración) | 🟢 construido en E5 (`src/analytics/pais.py`, `tiempo_por_etapa`); se conserva en el bloque de análisis de la vista de país (§7) |
| 17 | Vista ejecutiva (resumen, 1 pantalla, sin filtros) | CEO, Director de Estrategia | `kpi_snapshot` materializado | Stat tiles + 1 gráfico | 🔴 esquema EAV sin un solo `kpi_id` definido todavía |
| R1 | Oportunidades por país (comparación) | CEO, Director de Estrategia | `cliente_proceso` agrupado por `cliente_id`→país | Bar horizontal | 🟡 1 país con datos (SV, 37); 7 sin integrar — ver §1.1, no confundir con "sin escritor" |
| R2 | Gasto LLM consolidado por país | CEO, Director de Estrategia | `llm_llamada` agrupado por `cliente_id` | Bar agrupado (categorical) | 🔴 — además de faltar los 7 países, la tabla en sí está en 🟡 (ver #12) |
| R3 | Pipeline comercial consolidado, por país | Director Comercial | `oportunidad_estado` agrupado por `cliente_id` | Funnel + diverging | 🔴 — depende de #14 |
| R4 | Oportunidades con componente Oracle, por país | Gerente de Marca Oracle, Gerente Regional Comercial Oracle | `cliente_proceso.tags`/`marcas` filtrado a Oracle | Bar horizontal | 🟡 — depende de #5 (backfill de tags pendiente) |
| R5 | Oportunidades de **servicios** Oracle vs. licencias/producto, por país | Director de Servicios Oracle | *(sin columna hoy — ver hallazgo)* | Bar horizontal | 🔴 — no hay forma de distinguir "servicio" de "producto" en el esquema actual |
| R6 | Selector de país → cae a la vista de ese país | Todos los roles regionales | — | Navegación, no chart | 🟡 funciona para los 8 ya (7 mostrarán "sin datos — portal no integrado") |

**E14 (roles Oracle):** R4 queda cubierto por R1 filtrado a oportunidades Oracle, y R5 sigue 🔴.
La restricción por marca y sus cifras ocultas están en
[`portal-fase3-identidad.md`](portal-fase3-identidad.md) §1.2.

Esto responde los 3 pedidos textuales del pitch deck (Mejora 1, §5: volumen por mes,
ranking de instituciones, % de relevancia) con los KPIs 1-3, ambos 🟢 hoy mismo.

### Hallazgos de la verificación — no son del portal, pero lo bloquean

Encontrados corriendo `SELECT COUNT(*)`/`GROUP BY` reales contra `multicomputos_dev`,
no sospechados de antemano — hay que decidir qué hacer con cada uno antes de que su
KPI pueda ser 🟢 de verdad:

1. **`institucion_norm` nunca se pobló** (`proceso.institucion_norm`, 0 valores
   distintos en 37 filas) aunque el campo crudo `institucion` sí tiene 29 valores
   distintos. El KPI 3 usa el crudo mientras tanto — funciona hoy, pero sin
   normalizar dos instituciones que se escriban distinto (`"MINED"` vs. `"Ministerio
   de Educación"`) contarán como dos.
2. **`tags`/`categoria_detectada`/`marcas` están vacíos en las 37 filas de
   `cliente_proceso`**, pese a que `ProcesoClasificado` sí produce tags en el
   clasificador (`src/classifier/engine.py`) — el escritor v2
   (`repo.upsert_proceso_v2`/`set_estado_v2`) aparentemente no los está mapeando.
   Bloquea los KPIs 5 y R4 enteros. **Se arregla aparte, fuera de este
   plan.** Causa raíz (2026-10-23): `backfill_v2` armaba el proceso sin tags y el
   `MERGE` de `upsert_proceso_v2` pisaba con vacío; corregido en el PR #76 (el `MERGE` ya
   no es destructivo y el backfill reaplica el JSONL). Falta correr el backfill en la ADB real.
3. **`severidad_max` es `'alta'` en las 37 filas, sin ninguna variación** — o el
   corpus real hasta ahora es consistentemente de alto riesgo, o el cálculo de
   severidad (`repo._severidad_max`) tiene un sesgo. No se puede distinguir con 37
   filas; revisar cuando haya más volumen antes de confiar en el KPI 6.
4. **Ningún proceso tiene `monto_usd`/`monto_estimado`** — COMPRASAL no publica
   presupuesto en el listado (ver `src/scraper/models.py:97`, ya documentado como
   limitación conocida), así que el KPI 7 puede no ser viable nunca sin una fuente de
   monto distinta, no es un problema temporal.
5. **`fecha_limite_utc` es NULL en las 37 filas** — mismo tipo de hueco que el monto:
   si COMPRASAL no publica la fecha límite de forma estructurada en el endpoint que
   usa el extractor, el KPI 8 ("por vencer esta semana") puede ser inviable, no solo
   inmaduro. Confirmar contra el extractor antes de prometerlo en Fase 1.
6. **No hay columna que distinga "servicio" de "producto/licencia"** en ningún punto
   del esquema (ni v1 ni v2) — el KPI R5 (Director de Servicios Oracle) necesita que
   el clasificador o el catálogo de tags del cliente agregue esa dimensión antes de
   poder construirse; hoy no es un problema de datos faltantes sino de modelo
   faltante.
7. **`cliente_portal` tiene 0 filas** — ni siquiera el único país activo (El
   Salvador↔COMPRASAL) tiene su fila en la tabla que formalmente los vincula; el
   sistema funciona igual porque hoy todo pasa por `clients/multicomputos.yaml`, no
   por esta tabla — pero si el portal va a leer de acá para saber qué países/portales
   existen, hay que sembrarla primero (una fila, barato).
8. **`cliente_proceso.es_relevante`/`.triaje` están NULL en las 37 filas** —
   verificado en vivo (2026-10-01) contra `multicomputos_dev`, y **no es un hueco del
   backfill v2**: la v1 de origen (`licitaciones_metadata.triaje`) ya tenía ese mismo
   campo NULL en las 37 filas equivalentes. El campo simplemente casi nunca se puebla
   en producción real — `estado` sí es confiable (`ANALIZADO`=36, `DESCARTADO_TRIAJE`=1
   de 37) porque llegar a `ANALIZADO` implica haber pasado el filtro de relevancia del
   pipeline. `src/analytics/pais.py` (KPIs 1 y 2) usa
   `es_relevante=1 OR triaje='GO' OR estado IN ('ANALIZADO','NOTIFICADO')` — las tres
   condiciones conviven con OR, nunca se reemplazan, así que el día que
   `es_relevante`/`triaje` se empiecen a poblar de verdad no hay que tocar esta
   función. Sin este fallback los KPIs 1 y 2 muestran un cero permanente y falso en
   vez del dato real que sí existe, solo que bajo otra columna — confirmado
   corrigiendo ambas funciones y re-verificando en vivo: KPI 2 pasó de 0.0% a 97.3%
   (36/37), que es el número real. Los dos quedan 🟢 sin reservas.

### Fases de implementación (ligadas a la tabla maestra, no al revés)

- **Fase 1 — ya construible:** KPIs 1, 2, 3 (con la salvedad del hallazgo 1), 11-13
  (🟡, listos apenas corra la de las 07:00 de hoy). KPI 4 y 6 se muestran con una nota
  de "sin variación todavía" en vez de ocultarse — es información real, aunque poco
  interesante hoy.
- **Fase 2 — activar lo apagado:** KPI 9-10 (`ENABLE_FEEDBACK` + deploy del router,
  ADR 0005 Fase 2) y KPI 14 (programar `src/mercado` en un cron). Requiere además
  resolver el hallazgo 2 (tags vacíos) para que 5 y R4 pasen a 🟢 en esta misma fase,
  ya que es barato comparado con lo demás.
- **Fase 3 — construir identidad y pipeline comercial manual:** KPI 15-17, R1-R3
  completos, roles/sesión (`usuario`/`membresia`/...), UI de captura manual de
  `oportunidad_estado`, resolver hallazgos 4-6 si el negocio decide que valen la
  inversión (monto y fecha límite pueden requerir una fuente de datos externa a
  COMPRASAL; "servicio vs. producto" requiere decisión de producto sobre cómo
  clasificarlo).
- **Fase 4 — aspiracional** (Mejora 3 del pitch, SaaS externo a otras empresas, **no
  confundir con el multi-país de §1.1-1.2** que es la estructura real de
  Multicómputos): otra `organizacion` paga por AMALIA. No necesita KPI nuevo — la
  arquitectura ya aísla por cliente (la fuga que cerró la Fase 4 del esquema v2,
  2026-09-30); falta producto (facturación, onboarding), no analytics.

## 4. Arquitectura de información (layout)

**Primer fork, por alcance del rol (antes que cualquier otro filtro):** un rol
regional/directivo (CEO, Director de Estrategia, Director Comercial, Gerente de
Marca Oracle, Gerente Regional Comercial Oracle, Director de Servicios Oracle)
entra a la **vista regional** (KPIs R1-R6, comparación entre países); un rol de país
(Gerente de País, Equipo de Ventas) entra directo a la vista de **su** país — nunca
ve el selector de país porque no tiene otro país para elegir. Mismo principio de
pirámide invertida, aplicado a nivel de navegación: cada rol llega directo a la
pregunta que le toca, sin un paso intermedio de "elegí qué país mirar" para quien
solo tiene uno. Dentro de la vista regional, cada rol ve además solo los KPIs R que
le tocan (Director de Servicios Oracle no necesita ver R3, pipeline comercial
general) — mismo principio de pirámide invertida, ahora a nivel de qué card se
renderiza, no solo de layout.

La vista de país sigue el orden de la Entrega E11 (§7): primero qué hay que decidir
hoy, después el pipeline, la clasificación, los votos y, al final, el análisis.

```
┌─────────────────────────────────────────────────────────┐
│ Cabecera: frase con el veredicto del día · fecha ·        │  ← una frase, arriba de todo
│           última corrida                                  │
├─────────────────────────────────────────────────────────┤
│ Filtros: rango de fecha · país (solo si el rol es        │  ← una fila, bajo la cabecera
│          regional/directivo — el resto no lo ve)          │
├─────────────────────────────────────────────────────────┤
│ [Para decidir] [Pipeline en juego] [Relevancia] [Estancadas] │  ← 4 KPIs, sin scroll
├─────────────────────────────────────────────────────────┤
│ Calendario de cierres (14 días)  │  Requiere atención     │  ← qué vence y qué pide acción
├─────────────────────────────────────────────────────────┤
│ Tablero: Nuevo · Evaluando · Ofertado · Cerradas          │  ← el pipeline, por tarjetas
├─────────────────────────────────────────────────────────┤
│ Clasificación de AMALIA (grupos + Descartados → Recuperar)│  ← lo que AMALIA decidió
├─────────────────────────────────────────────────────────┤
│ Votos del equipo y desacuerdos con la clasificación       │  ← el equipo contra AMALIA
├─────────────────────────────────────────────────────────┤
│ Análisis: tendencia · marcas · embudo; instituciones ·    │  ← el "por qué", abajo
│           severidad · conversión · tiempos                │
└─────────────────────────────────────────────────────────┘
```

La vista regional y la operativa no cambian con E11. Vista operativa (rol Operador/Admin) es una página separada, no una sección de la
vista comercial — mezclar "¿ganamos licitaciones?" con "¿cuánto gastamos en LLM?" en
la misma pantalla diluye la pirámide invertida de ambos públicos. La vista regional
(R1-R6) es, por el mismo motivo, una tercera página separada — no una fila extra
arriba de la vista de país.

## 5. Arquitectura técnica — qué se reusa, qué se agrega

**Se reusa tal cual** (ADR 0005, ya construido y probado):
- `src/web/app.py::crear_app` — fábrica de app, el portal se monta como un router más.
- `src/web/seguridad.py` — `EncabezadosSeguros`, `LimiteCuerpo`.
- `src/web/plantillas.py` — entorno Jinja2 con autoescape.
- Patrón de capa de datos separada de HTTP (`src/feedback/service.py` como modelo).

**Se agrega:**
- `src/web/portal.py` (o `src/portal/` como paquete si crece) — nuevo router `/portal`,
  monta en `crear_app` junto al de feedback.
- `src/analytics/` (nuevo) — las consultas de los KPIs de la tabla §3, una función por
  KPI, cada una devolviendo el shape que el stat-tile/chart espera (valor, delta,
  serie). Separado de `src/web/` por el mismo motivo que `src/feedback/`: que el portal
  reuse la lógica sin arrastrar HTTP, y que sea testeable sin un cliente FastAPI.
- `templates/portal_*.html` — las vistas, Jinja2, siguiendo los specs de §2.
- JS vanilla (sin bundler) para hover/tooltip/crosshair de los SVG — un archivo por
  tipo de gráfico (`static/charts/bar.js`, `funnel.js`, `meter.js`), cada uno siguiendo
  `dataviz/references/interaction.md`.
- Autenticación (Fase 3): endpoint de `codigo_acceso` (login passwordless) + middleware
  de sesión que resuelve `membresia` para saber qué `cliente_id`(s) puede ver el
  usuario — hoy `src/web/` no tiene ninguna sesión, es el primer router que la necesita.

## 6. Cerrar el gap de documentación que motivó este plan

Una vez aprobado y guardado este documento en `docs/planes/portal-amalia.md`:
- Actualizar `docs/README.md` → sección `planes/` con la entrada nueva.
- Las citas `§3`, `§3.7 punto 4`, `§5.2`, etc. en `db/schema_v2.sql` y otros módulos
  seguirán siendo números de sección de un documento que ya no es este (este plan no
  reconstruye esas secciones exactas, construye uno nuevo desde cero sobre hechos
  verificados) — por eso, al tocar esos comentarios en una fase de implementación
  futura, reemplazar la cita por `docs/planes/portal-amalia.md` sección correspondiente,
  no dejar el número viejo colgando.

## 7. Entrega E11 — rediseño de la vista de país

**Estado:** en la rama `feat/portal-e11-vista-pais`, pendiente de merge y de despliegue.
No hay DDL: todo lo que usa ya existe en [`db/schema_v2.sql`](../../db/schema_v2.sql).

**Objetivo.** Pasar de «¿cómo va el sistema?» a «¿qué hago hoy?». La vista de país de
`/portal` mostraba nueve tarjetas de métricas y una tabla de 50 filas. Ahora el gerente
de país y el equipo de ventas ven primero lo que pide decisión o atención, y el detalle
queda abajo. La maqueta aprobada es la Version 4 del artefacto de diseño
(`https://claude.ai/artifact/6v6uPXVJs4o9T6rRchkf1A`). Los rangos siguen siendo los reales
de la vista (30, 90 y 180 días), no los de la maqueta.

### Orden de la pantalla

1. **Cabecera.** Una frase con el veredicto del día, la fecha larga y la última corrida
   (hora y procesos revisados).
2. **Cuatro KPIs**, sin scroll:
   - **Para decidir:** procesos del grupo «Para decidir» del rango, y cuántos entraron en
     la última semana.
   - **Pipeline en juego:** suma del monto de las oportunidades en NUEVO, EVALUANDO u
     OFERTADO. Se usa `analisis.monto_estimado` y, si no existe, `proceso.monto_usd`. Un
     monto nulo se muestra como «monto sin estimar», nunca como 0. Se indica cuántas
     tienen monto.
   - **Relevancia:** aptos para decidir sobre revisados, con el «% marcados relevantes»
     como apoyo, la diferencia en puntos frente al tramo anterior y una sparkline. Es la
     misma etiqueta honesta del KPI 2, no una precisión medida (ver KPI 9). Definición en
     [E11.1](#e111--relevancia-estricta-y-descartados).
   - **Estancadas:** oportunidades en EVALUANDO sin cambios hace más de 7 días.
3. **Calendario de cierres de 14 días** (barras por día de `fecha_limite`, con enlaces
   `?dia=` que filtran el tablero) y, a la derecha, **«Requiere atención»**: la lista de
   situaciones que piden acción hoy. Cada ítem enlaza a su proceso cuando lo tiene.
   Los procesos sin fecha límite o con la fecha vencida no entran al calendario; la
   vencida sale con el chip «vencida».
4. **Tablero del pipeline** en cuatro columnas: Nuevo, Evaluando, Ofertado y Cerradas
   (estados finales). Cada tarjeta muestra el match, el cierre con su urgencia, las
   marcas, el monto, el responsable, los días en el estado y, si lo hay, el voto del
   equipo. El cambio de estado sigue siendo el formulario «Mover a» de E5. Filtros por
   responsable, marca y día. Si el rango supera 300 procesos, el tablero avisa de que
   está truncado.
5. **Clasificación de AMALIA:** barra apilada por grupo (Para decidir, No competible,
   Baja prioridad y Descartados), los motivos y la lista de descartados, cada uno con
   **Recuperar**. La lista de descartados se llena desde el despliegue de E11.1: antes el
   pipeline no guardaba los no relevantes ([E11.1](#e111--relevancia-estricta-y-descartados)).
6. **Votos del equipo:** conteos de «Relevante» e «Irrelevante», los motivos (las 9
   categorías del catálogo) y los desacuerdos entre el voto y la clasificación. Con menos
   de 10 votos se muestra un aviso de pocos datos.
7. **Análisis:** tendencia semanal de 12 semanas (revisados frente a relevantes), marcas
   más detectadas y embudo. Debajo se conservan instituciones, severidad, conversión y
   tiempos por etapa.

### Estilo

- Paleta del correo: crema `#F3F1EC` de fondo, navy `#1F3A5F` como serie principal,
  verde `#2E7D4F` para lo bueno, rojo `#A33A2A` para lo crítico, ámbar `#8A6D1F` para el
  aviso, línea `#E2DED3` y tinta `#18181A` con gris `#6F6B62`. Tipografía Arial, chips y
  franjas como en el correo.
- Los colores viven como tokens en `:root` de `templates/portal_base.html`. El modo oscuro
  se conserva con una paleta derivada en `prefers-color-scheme: dark`. Ningún color fijo
  queda fuera de los tokens.
- Sin JavaScript nuevo: los gráficos son SVG del lado del servidor con los tooltips de
  `static/charts/` que ya existen. La CSP del portal no cambia.

### Datos

- **Sin DDL.** Las consultas leen `cliente_proceso` unido a `proceso`, `analisis`,
  `oportunidad_estado` y `usuario` (para el nombre del responsable). Todas filtran por
  `cliente_id`. La zona horaria para «hoy» y los cierres es `proceso.tz_portal`, con
  `America/El_Salvador` por defecto. La capa de datos vive en `src/analytics/tablero.py`.
- **«No competible» se deriva en lectura**, no se persiste: `evaluar_competibilidad` de
  `drilldown.analysis` sobre `analisis.matriz_riesgos` y `proceso.modalidad_raw`. Sin
  matriz de riesgos el proceso no se marca como no competible, así que nada se bloquea.
- **Agrupamiento compartido con el correo.** Los grupos Para decidir, No competibles y Baja
  prioridad se calculan en `src/notifier/agrupamiento.py`, que también usa el correo. Hay
  una sola regla para los dos canales.
- **Votos.** Se leen en conteos y motivos de `feedback`. Nunca se lee ni se muestra
  `destinatario`.

### Escrituras nuevas

Son dos, las dos bajo `/portal/oportunidad/{proceso_gid}/…`, con el mismo esqueleto que el
cambio de estado: CSRF, límite de intentos, rechazo si el alcance es simulado (E10), 404 si
el proceso es ajeno al cliente y auditoría.

- **Asignar responsable** (`POST …/responsable`). Lo puede hacer quien cambia estado. El
  responsable debe ser un usuario asignable del cliente: activo, de la misma organización,
  con un rol que cambia estado y acceso a ese país. También se puede quitar. Si el proceso
  no tiene fila en `oportunidad_estado`, se crea en NUEVO con origen MANUAL. Se escribe un
  evento en `oportunidad_evento` con el mismo estado y la auditoría `RESPONSABLE_ASIGNADO`,
  todo en una transacción.
- **Recuperar descartado** (`POST …/recuperar`). Solo aplica a un proceso del cliente que
  AMALIA no marcó como relevante. Crea la oportunidad en NUEVO con origen MANUAL, un evento
  «Recuperada de descartados de AMALIA» y la auditoría `DESCARTADO_RECUPERADO`.
  **No modifica `cliente_proceso`**: la clasificación original se conserva para evaluar al
  clasificador. La lista del portal incluye procesos con `relevante OR EXISTS(oportunidad_estado)`.
  Recuperar un proceso ya relevante o ya recuperado es inválido. Los no relevantes solo
  existen en `cliente_proceso` desde el despliegue de E11.1 (sin *backfill*; ver
  [E11.1](#e111--relevancia-estricta-y-descartados)): antes de eso, la lista de descartados
  está vacía, aunque el botón ya exista.

Las dos acciones nuevas se añaden a `politica.ACCIONES_AUDITADAS`.

### Votos: qué hacen y qué no hacen

Los votos «Irrelevante» se **muestran** y se **cruzan** con la clasificación (los
desacuerdos). **No ajustan el clasificador.** Eso es un proceso aparte que se hará cuando
haya volumen: al diseñar esta entrega había 2 votos.

### Limitaciones conocidas

- **Asignar responsable cuenta como movimiento:** el control de concurrencia usa `oportunidad_estado.actualizado_en`, así que asignar
  reinicia «días sin moverse» y `estancadas`. Los eventos de asignación (mismo estado) no cuentan para los KPI 15 y 16.
- **«Descartado» es una decisión explícita** (`DESCARTADO_*` o `es_relevante = 0`): lo que sigue en curso o falló no se muestra ni se recupera.
- **Zona horaria:** `clasificar` cuenta los días con `proceso.tz_portal` y el calendario con la zona del registro de portales; hoy ambas son
  El Salvador. Si un portal futuro difiere, hay que unificar.
- **Tope de 300 filas:** `procesos_del_rango` prioriza lo relevante o con estado y lo que aún cierra; la vista avisa si se truncó.

### Fuera de alcance

- Vistas de directores, CEO y gerentes Oracle (una entrega posterior sobre esta base).
- Arrastrar y soltar para cambiar el estado. Exigiría una ruta POST adicional y JavaScript
  de persistencia, y la CSP del portal no lo permite. Sigue el formulario «Mover a».
- Comentarios de usuario en las oportunidades, cambios de DDL y cualquier cambio en `oci/`
  o en `CHANGELOG.md`.

### E11.1 — relevancia estricta y descartados

**Problema medido.** `cliente_proceso` solo guardaba los relevantes: 257 de 261 filas
tenían `es_relevante = 1`. AMALIA clasifica unos 79 procesos al día, pero persistía solo
los ~15 que llegaban al drilldown; por eso la relevancia salía en 99,6 % y la sección de
descartados no tenía datos. `clasificado_en` no tenía ningún escritor (NULL en todas las
filas). Medido en 30 días (`ejecucion` + `cliente_proceso`): 1.139 revisados, 187
relevantes (16 %) y 85 aptos (7,5 %); hoy, 79 revisados, 15 relevantes y 8 aptos, que
coinciden con los «8 para decidir» del correo.

**Definiciones.** Las constantes SQL viven una sola vez en
[`src/memoria/relevancia.py`](../../src/memoria/relevancia.py); aquí solo se describen.

- **Apto** = relevante ∧ match ≥ 80 ∧ triaje ≠ `NO-GO`, con el mismo peso que el correo
  (0,4 técnico + 0,3 categoría + 0,3 factibilidad), sin mirar el pliego. El correo además
  descarta los «no competibles» (se derivan de la matriz de riesgos); el KPI no, y por eso
  se rotula «aptos».
- **Revisados** = `MAX(procesos_revisados)` por día de las corridas OK de `ejecucion`: las
  repeticiones del mismo día no suman. Es la fuente correcta también para fechas anteriores
  al cambio del pipeline.
- **Relevancia (KPI)** = aptos / revisados (cifra principal), con relevantes / revisados
  como apoyo. Un proceso se asigna al día de corrida con `TRUNC(fecha_pub_utc + 11 h)`: la
  corrida de las 07:00 (hora de El Salvador) mira las 24 h previas, así que las cifras
  diarias reproducen el correo.
- **Descartado** = `es_relevante = 0` o estado `DESCARTADO_*`. **Pendiente** = lo demás, sin
  decisión.
- **Rótulo:** «aptos para decidir», con «% marcados relevantes» como apoyo.

**Pipeline.** Tras clasificar, las tres rutas de `src/main.py` que lo hacen también
persisten los no relevantes, con estado `DESCUBIERTO` y `es_relevante = 0`. No hay DDL. El
registro (`registrar_clasificados` en `src/memoria/service.py`) es un MERGE idempotente que
no pisa el estado de lo ya avanzado, no detiene la corrida si falla y omite los procesos
«sin clasificar» (fallo del LLM). El MERGE de `src/memoria/repo.py` escribe `clasificado_en`.
Drilldown, correo y `reportada` no cambian: filtran en memoria por `es_relevante`.
No hay *backfill*: la lista de descartados se llena desde el despliegue, y el KPI usa
`ejecucion`, que tiene historia desde el 1 de octubre.

**Lectores ajustados.** Solo leen relevantes el embudo (`pais.embudo_pipeline`), las
instituciones (`stats_por_institucion_v2`), las tendencias (`tendencias_v2`) y, con filtro
opcional, la severidad (`pais.severidad_riesgos`). El resto no cambia. La tendencia semanal
toma los revisados de `ejecucion`.

**Vista.**

- «Para decidir» cuenta los procesos del grupo decidir sin estado final y no vencidos. Las
  vencidas sin decidir pasan a una línea «N vencidas», fuera del tablero y del KPI.
- Cada columna del tablero muestra como máximo 8 tarjetas, con «+ N más» (la cuenta es la
  real). El filtro de marcas muestra las 8 más frecuentes, más la elegida.
- El calendario no lleva lista de días. El pipeline no repite «monto sin estimar» en cada
  tarjeta: usa un guion y la leyenda «monto estimado en X de Y».
- La clasificación va en este orden: revisados → relevantes → (para decidir · no competibles
  · baja prioridad), y no relevantes = revisados − relevantes. Debajo, la lista de
  descartados guardados, con «Recuperar» y la fecha desde la que se guardan. Si no hay
  descartados guardados, la vista lo dice.

**Limitaciones conocidas.**

- **Volumen:** unas 60 filas por día (~22 k al año). `cliente_proceso` no se purga: la
  retención (`db/retention.sql`) no la toca.
- **«Apto» no mira el pliego.** El correo además excluye los «no competibles».
- **Sin histórico de descartados:** antes del despliegue no se guardaron los no relevantes,
  así que no hay forma de recuperarlos.

**Fuera de alcance.** Un estado nuevo `CLASIFICADO_NO_RELEVANTE` (exigiría DDL), el *backfill*
de históricos y la purga de `cliente_proceso`.

### Entrega E12 — selector de tema

**Objetivo.** Cada persona elige el tema del portal: **Auto · Claro · Oscuro**. Auto sigue al
sistema, como antes de esta entrega: quien no toca el selector no nota ningún cambio.

**Por qué sin JavaScript.** La CSP del portal es `script-src 'self'` y no admite JS inline ni
atributos `on*` (ver [`src/web/seguridad.py`](../../src/web/seguridad.py)). El selector es un
formulario con botones y una cookie: el modo se aplica en el servidor, antes del primer
pintado, y no hay parpadeo.

**Cookie `__Host-amalia_tema`.** Valor `auto | claro | oscuro`; cualquier otro valor (vacío,
basura, demasiado largo) se trata como `auto`. Duración de 1 año. Atributos
`Secure; HttpOnly; SameSite=Lax; Path=/`, el mismo estilo que la cookie de sesión. No es un
dato sensible: es solo una preferencia por navegador (no se guarda por usuario en la base).

**Ruta `POST /portal/tema`** (en [`src/web/portal.py`](../../src/web/portal.py)). Campos:
`tema`, `csrf` y `volver`. Orden: exige sesión, verifica CSRF y Origin, valida `tema` contra
la lista cerrada (un valor inválido no cambia la cookie), fija la cookie y responde **303**
(PRG). `volver` solo se acepta si empieza por `/portal` y no contiene `//`, `\`, esquema ni
saltos de línea; en otro caso se redirige a `/portal`, así que no hay redirección abierta. No
audita: no es un hecho de negocio.

**Bajo «ver como» sigue funcionando.** Guarda una preferencia de presentación, no datos del
portal, así que no se bloquea con `alcance.simulado`. Por eso el test guardián de rutas POST la
lista aparte (ver [E10 en el plan de identidad](portal-fase3-identidad.md), regla de
mantenimiento).

**Alcance.** Todas las vistas del portal (país, regional, operativa y sus páginas de error),
porque todas extienden la misma base. `/acceso` y las páginas `feedback_*` siguen al sistema:
aparecen antes de tener sesión o fuera del portal.

**Implementación.** El contexto de `pagina()` recibe `tema` (leído de la cookie, con la misma
validación) y la ruta actual para `volver`. En [`templates/portal_base.html`](../../templates/portal_base.html)
los tokens oscuros se definen una sola vez en una macro de Jinja, que se emite en dos sitios:
dentro de `@media (prefers-color-scheme: dark)` bajo `:root:not([data-tema="claro"])`, y bajo
`:root[data-tema="oscuro"]`. Los tokens claros quedan en `:root` y en `:root[data-tema="claro"]`,
para anular el `@media` cuando el sistema es oscuro. `color-scheme` sigue el modo efectivo, de
modo que los controles del navegador y las barras de desplazamiento coincidan. El control vive
en [`templates/portal_nav.html`](../../templates/portal_nav.html): tres botones con
`aria-pressed` en el actual, etiqueta «Tema» y objetivos táctiles de 44 px a 390 px.

**Limitaciones conocidas.** La preferencia es por navegador: en otro equipo vuelve a Auto. No
hay temas adicionales (alto contraste) ni transiciones animadas.

### Entrega E13 — vista de país compacta y arrastrar-y-soltar

**Problema medido.** Con datos pesados (≈ 260 oportunidades, títulos largos, sin montos) la
vista de país medía **6.369 px a 1360 px (7,1 pantallas) y 14.812 px a 390 px**. Las secciones
se apilaban sin plegarse, la lista de desacuerdos no tenía tope y «+ N más» era solo texto.
Además, Emilio pidió mover oportunidades arrastrando las tarjetas.

**Resultado.** Con el mismo contexto pesado: **1.555 px (1,73 pantallas) a 1360 px y 2.020 px
(2,24 pantallas) a 390 px**.

**Rúbrica.**
- Altura ≤ 2.250 px a 1360×900 con las secciones secundarias plegadas.
- Ningún listado muestra más de 5 elementos sin una acción «Ver N más».
- Tablero de alto fijo ≈ 540 px con scroll interno: la altura de la página no crece con la
  cantidad de oportunidades.
- Cero scroll horizontal a 390 px; contraste de texto ≥ 4,5:1 medido en claro y en oscuro
  (0 pares por debajo); objetivos táctiles de 44 px donde se pulsa.
- Sin JavaScript la vista sigue funcionando: el arrastre es una mejora progresiva.

**Estructura.**
- **Cabecera y KPIs compactos** en una fila.
- **Calendario y «Requiere atención»** en la misma fila: el calendario ocupa poco alto y
  «Requiere atención» muestra 5 filas de una línea (chip y título recortado); el resto va en
  «Ver N más» (hasta 20).
- **Tablero** a ancho completo, 4 columnas de alto fijo ≈ 540 px con scroll interno. Tope de 25
  tarjetas por columna con «+ N más». Cada tarjeta es compacta (referencia, título de 2 líneas,
  chips de cierre, match y primera marca con «+N»). «Mover a…» y «Responsable…» se agrupan en un
  solo menú por tarjeta.
- **Acordeones `<details>` plegados por defecto**: Clasificación de AMALIA, Lo que opina el
  equipo y Análisis. Cada uno muestra una línea-resumen con su cifra clave. Dentro, descartados
  y desacuerdos muestran 5 elementos y «Ver N más».
- **Índice de secciones fijo** (Resumen · Tablero · Clasificación · Equipo · Análisis). Con JS
  abre el acordeón correspondiente; sin JS, el ancla lleva a la cabecera de la sección.

**Arrastrar y soltar.** Lo implementa [`static/portal/tablero.js`](../../static/portal/tablero.js),
JS propio sin `fetch` (la CSP es `script-src 'self'`). Solo actúa si el servidor marca el tablero
como editable.
- **No añade rutas.** El JS rellena y envía el formulario de estado que cada tarjeta ya tiene:
  conserva el CSRF, el token de concurrencia `actualizado_en` y la redirección PRG. Reutiliza
  `POST /portal/oportunidad/{proceso_gid}/estado` y por eso hereda el rechazo bajo «ver como».
- **Columnas válidas.** Al arrastrar se resaltan las columnas que permite
  [`src/oportunidades/politica.py`](../../src/oportunidades/politica.py) y se atenúan las demás.
- **Diálogo nativo** (`<dialog>`, en [`templates/portal_pais_dialogo.html`](../../templates/portal_pais_dialogo.html))
  cuando hay varios destinos (Cerradas) o el motivo es obligatorio (DESCARTADO o corrección de un
  estado final). Se pide el estado, el motivo cuando aplica, el monto y el competidor.
- **Vuelta con ancla.** Tras guardar, el PRG redirige a `#op-{gid}` para no perder el sitio.
- **Alternativas.** «Mover a…» sigue siendo la vía de teclado y la de móvil, porque el DnD de
  HTML5 no funciona en táctil. Es la alternativa al arrastre que pide WCAG 2.5.7.

**Banco visual.** [`scripts/capturas_portal.py`](../../scripts/capturas_portal.py) se usa con
`python3 scripts/capturas_portal.py --salida <carpeta> --etiqueta <x>`. Renderiza la plantilla
real con datos pesados, fotografía claro y oscuro en 1360 y 390 px, y mide la altura de cada
sección. El trabajo fue iterativo, en 3 iteraciones: implementar, fotografiar, medir contra la
rúbrica y corregir.

**Limitaciones conocidas.** Se ven unas 4 tarjetas por columna a 540 px de alto. No hay arrastre
táctil ni teclado propio para arrastrar: el teclado usa «Mover a…». El hueco bajo el calendario
es más alto cuando «Requiere atención» tiene muchos elementos.

## Verificación (por fase)

- **Fase 1:** cada KPI de la tabla se valida con una query directa contra la ADB real
  de `multicomputos_dev` (mismo patrón de hoy: SSH + `docker compose run --rm amalia
  python -c "..."`) comparada a mano contra lo que la vista muestra. Capturas de
  pantalla del layout contra los 6 checks de `dataviz` (forma, color, specs de marca,
  interacción, accesibilidad, mirar el resultado renderizado) antes de dar una vista
  por cerrada.
- **Fase 2:** `ENABLE_FEEDBACK=true` en un tenant de prueba primero (nunca directo en
  `multicomputos_dev`), confirmar que los votos llegan antes de mostrar el KPI 9-10 con
  datos reales en vez de "sin datos todavía".
- **Todas:** `ruff check` + la suite de tests existente; cada función nueva de
  `src/analytics/` lleva su test unitario (mock de conexión, mismo patrón que
  `tests/test_repo_sql.py`) y, si toca una query nueva, su contraparte en
  `tests/integration/test_oracle_local.py` contra Oracle real.
- **Fase 3 (identidad/roles), específico:** antes de dar por cerrado el modelo de
  acceso, un test que verifique que un usuario con rol de país (`alcance`="su país")
  NUNCA recibe filas de otro `cliente_id` en ninguna de las funciones de
  `src/analytics/` — mismo espíritu que
  `test_buscar_precedentes_v2_no_cruza_clientes` (hoy en
  `tests/integration/test_oracle_local.py`, Fase 4 del esquema v2): la fuga entre
  países sería el mismo bug de fondo que la fuga entre clientes que ese test ya
  cubre, un nivel más arriba en la jerarquía `organizacion → cliente`.

## Archivos críticos
- `docs/planes/portal-amalia.md` (destino final de este documento)
- `docs/README.md` (registrar la entrada)
- `src/web/app.py`, `src/web/portal.py` (nuevo)
- `src/analytics/` (nuevo)
- `templates/portal_*.html` (nuevo)
- `db/schema_v2.sql` (referencia de columnas, no se edita en este plan)
