# Evaluación de precisión — relevancia del clasificador y GO/NO-GO del triaje

> **Qué es este documento:** el plan para medir, contra el criterio de una persona,
> cuánto acierta AMALIA al decir «relevante» y al decir «GO/NO-GO». **Nada está
> implementado**: el script de §6 está descrito, no escrito. Las cifras de §1 se
> calcularon sobre copias de solo lectura de los JSONL de la VM de `multicomputos_dev`
> (ventana 2026-09-10 → 2026-10-22) y **sólo dimensionan** el plan; ningún contenido de
> esos archivos se copió al repo.
>
> **Por qué ahora:** el KPI 2 de [`portal-amalia.md`](portal-amalia.md) §3 se llama
> «% marcado relevante **por el clasificador** *(etiqueta honesta, no es precisión
> real)*», y el KPI 9 (precisión real) depende de votos que hoy casi no existen. Además
> la inferencia ya corre en OCI (Llama 4 Scout/Maverick) y la comparación con Claude
> está pendiente: sin una muestra etiquetada, esa comparación sólo puede medir costo,
> nunca calidad.

---

## 0. La cascada que se evalúa

Una oportunidad se pierde o se hunde en cualquiera de estos pasos; cada uno se mide por
separado y luego de punta a punta.

| Paso | Dónde | Qué decide | Qué pasa si se equivoca hacia «no» |
|---|---|---|---|
| 1. Clasificador | [`src/classifier/engine.py:146`](../../src/classifier/engine.py) | `es_relevante` | **Invisible**: el proceso no aparece en el correo ni se analiza |
| 2. Pre-filtro RegEx | [`src/drilldown/pipeline.py:259-264`](../../src/drilldown/pipeline.py) | `DESCARTADO_REGEX` (sólo nativos) | No se analiza |
| 3. Triaje | [`src/drilldown/triage.py:111-220`](../../src/drilldown/triage.py), [`pipeline.py:278-295`](../../src/drilldown/pipeline.py) | `GO`/`NO-GO` + `confianza` | Sin matriz de riesgos y **degradado a «Baja prioridad»** en el correo ([`src/notifier/email_sender.py:222-228`](../../src/notifier/email_sender.py)) |
| 4. Agrupación del correo | [`src/notifier/email_sender.py:185`](../../src/notifier/email_sender.py) | `match < 80` → «Baja prioridad» | Igual que arriba |

Dos detalles del código que la evaluación tiene que respetar:

- Un triaje cuyo JSON no parsea devuelve **`NO-GO` con confianza 0.0 y motivo
  `parse_error`** ([`src/drilldown/triage.py:197-207`](../../src/drilldown/triage.py)).
  ADR 0004 lo llama «el peor modo de falla del sistema»
  ([`../decisiones/0004-abstraccion-de-proveedor-llm.md`](../decisiones/0004-abstraccion-de-proveedor-llm.md),
  hallazgo 4). El pipeline **no persiste el motivo** del triaje: `ResultadoAnalisis`
  guarda `triaje` y `triaje_confianza` ([`src/scraper/models.py:106-107`](../../src/scraper/models.py))
  y la rama NO-GO no escribe `error` ([`pipeline.py:287-290`](../../src/drilldown/pipeline.py)).
- El umbral `TRIAGE_MIN_CONFIANZA = 0.50` ([`pipeline.py:32`](../../src/drilldown/pipeline.py))
  sólo se evalúa **después** de descartar los NO-GO (`:287-291`): un NO-GO nunca va a
  revisión manual, tenga la confianza que tenga.

---

## 1. Insumos y lo que dicen (cifras calculadas, no copiadas)

### 1.1 Fuentes

| Fuente | Escritor | Granularidad | Útil para |
|---|---|---|---|
| `data/history/<cliente>.jsonl` | [`src/history.py:107-128`](../../src/history.py) | Una línea por corrida, con todos los procesos clasificados (relevantes y no) | Paso 1: población completa, scores, `razon` |
| `data/history/<cliente>.analisis.jsonl` | [`src/history.py:167-184`](../../src/history.py) | Una línea por transición de estado de un proceso; la última manda | Pasos 2-3: `estado`, `triaje`, `triaje_confianza`, `formato`, `matriz`, `uso_triaje` |
| `licitaciones_feedback` / `<cliente>.feedback.jsonl` | [`src/feedback/service.py:107-164`](../../src/feedback/service.py), DDL en [`db/schema.sql`](../../db/schema.sql) | Un voto por (proceso, cliente, destinatario) | Señal débil (§5) |
| `data/llm_consumo.jsonl` | [`src/llm/registro.py:355-386`](../../src/llm/registro.py) | Una línea por llamada; **sin `numero_referencia`** | Costo y latencia por etapa; no se puede unir a un proceso |

### 1.2 Tamaños y distribuciones (ventana de DEV, modo validación)

| Medida | Valor |
|---|---|
| Corridas / días distintos | 42 / 37 (29 corridas con >10 procesos: media 63 revisados y 10 relevantes) |
| Procesos únicos clasificados | **1.672** (198 instituciones) |
| Relevantes alguna vez | **274** (16,4 %) — no relevantes en su último estado: 1.403 |
| Procesos con línea de análisis | 258 (desde 2026-09-23) |
| Relevantes **sin** ninguna línea de análisis | 16 — causa **No verificable** con estos archivos |
| Estado final | `NOTIFICADO` 202 · `DESCARTADO_TRIAJE` 40 · `ERROR_LLM` 7 · `REQUIERE_REVISION_MANUAL` 6 (todos por >100 páginas) · `DESCARTADO_REGEX` 3 |
| Triados | 249: **GO 209 / NO-GO 40 (16,1 %)** |
| Formato | escaneado 171 (66 %) · nativo 80 · sin formato 7 |
| Tasa de NO-GO por formato | **nativo 24/73 = 33 %** · escaneado 16/169 = 9,5 % |
| `confianza` de los GO | sólo 0,8 (21) y 0,9 (188) |
| `confianza` de los NO-GO | 0,0 (**26**) · 0,1 (9) · 0,2 (5) |
| Procesos que cambiaron GO↔NO-GO entre reintentos | 2 |
| Procesos clasificados más de una vez | 121, de los cuales **11 cambiaron `es_relevante`** — 11 de los 24 que alguna vez fueron relevantes |
| Votos de feedback | **2**, ambos `IRRELEVANTE`, ambos del 2026-10-23, en modo validación |
| Llamadas al LLM (2026-09-23 → 10-22) | triaje 267 (Scout) · clasificación 171 (Scout) · análisis 226 (Maverick) · 1 triaje Claude fallido; **0 truncadas** |
| Costo **estimado** por llamada (`costo_estimado_usd`, [`registro.py:236-249`](../../src/llm/registro.py)) | triaje $0,0072 · clasificación $0,0039 · análisis $0,0149 |
| Tokens medianos (entrada / salida) | triaje 12.905 / 75 · análisis 25.562 / 504 |

### 1.3 Lo que estas cifras ya sugieren (hipótesis, no conclusiones)

1. **La `confianza` no significa lo que el pipeline supone.** Los GO están en 0,8-0,9 y
   los NO-GO en 0,0-0,2: Scout parece devolver «probabilidad de GO», no «confianza en el
   veredicto». Si es así, el umbral de `pipeline.py:32` mide otra cosa.
2. **26 de los 40 NO-GO tienen confianza exactamente 0,0**, que es también la firma de
   `parse_error`. Con lo que hoy se persiste es **No verificable** cuántos son descartes
   reales y cuántos fallos de parseo. El libro de consumo no lo resuelve: marca `ok`
   respecto del transporte, no del parseo, y no lleva el proceso.
3. **El triaje es 3,5× más NO-GO en nativos que en escaneados.** O el corpus nativo es
   distinto, o el extracto de texto ([`pipeline.py:259-262`](../../src/drilldown/pipeline.py))
   informa peor que las imágenes. La muestra se estratifica por formato por esto.
4. **La clasificación es inestable en el borde**: casi la mitad de los relevantes que se
   reclasificaron cambió de veredicto. Es un piso de desacuerdo que ninguna métrica de
   acuerdo humano-modelo va a superar.
5. **¿Paga el triaje?** En la ventana: triaje ≈ $1,93 estimados; análisis evitado por los
   40 NO-GO ≈ 40 × $0,0149 ≈ $0,60. Con tarifas estimadas (ambas etapas llevan el mismo
   factor de seguridad de [`src/drilldown/quota.py:84`](../../src/drilldown/quota.py), así
   que la razón se sostiene aunque el valor absoluto no). Si además el triaje se
   equivoca, su único argumento es la latencia.

---

## 2. Métricas

Etiqueta humana de referencia por proceso (ver §4): **`OFERTARIA`**, **`REVISARIA`** o
**`NO_ES_NUESTRO`**. Se mapea a:

- *relevante humano* = `OFERTARIA` ∪ `REVISARIA`;
- *GO humano* = `OFERTARIA` (más un análisis de sensibilidad con `REVISARIA` incluido).

| Métrica | Definición | Paso | Por qué |
|---|---|---|---|
| **Tasa de falso NO-GO** | P(GO humano \| NO-GO modelo) | 3 | El error caro del triaje: oportunidad sin análisis y hundida en «Baja prioridad» |
| Precisión del GO | P(GO humano \| GO modelo) | 3 | Costo de análisis desperdiciado y ruido en «Para decidir» |
| Precisión de relevancia | P(relevante humano \| `es_relevante`) | 1 | Lo que el KPI 2 dice medir y no mide |
| **Recall de relevancia** | TP / (TP + FN̂), con FN̂ = 1.403 × p̂(relevante humano \| no relevante modelo) | 1 | El error invisible: nunca llega al correo, ningún voto lo detecta |
| Recall de punta a punta | Fracción de `OFERTARIA` que termina en «Para decidir» | 1-4 | Lo que el negocio realmente pierde |
| Acuerdo humano-modelo | κ de Cohen + matriz de confusión; con prevalencia 16 % se reporta también el acuerdo bruto | 1, 3 | El κ solo engaña con clases desbalanceadas |
| Techo humano | κ entre dos etiquetadores (§4.3) | — | Si dos personas acuerdan κ=0,6, exigirle 0,9 al modelo no tiene sentido |
| Estabilidad | % de veredictos que cambian al reclasificar | 1, 3 | Ya medible sin etiquetas (§1.3.4) |

Intervalos: **Wilson al 95 %** para proporciones (corrección por población finita
cuando la muestra supera ~10 % del estrato); **bootstrap** estratificado para el recall
ponderado y el κ.

---

## 3. Diseño de la muestra

Estratificada, con pesos = tamaño del estrato / tamaño muestreado. Semilla fija y
registrada; la muestra se toma **del estado congelado** de los JSONL en una fecha de
corte (los NO-GO crecen ≈1 por día).

| Estrato | Población (corte 10-22) | Muestra | Precisión esperada (Wilson 95 %) |
|---|---|---|---|
| **E1 · NO-GO del triaje** | 40 | **Censo** (todos), subdividido en confianza 0,0 vs. >0 y nativo vs. escaneado | 0/40 → [0 %, 8,8 %] · 4/40 → [4,0 %, 23,1 %] |
| E2 · GO del triaje | 209 | **60**, proporcional por formato (≈44 escaneados / 16 nativos) | 6/60 → [4,7 %, 20,1 %], ≈15 % más estrecho con la corrección finita |
| E3 · No relevantes del clasificador | 1.403 | **200**, aleatoria simple, etiquetado sólo por título + institución + modalidad | 0/200 → ≤1,9 % (≤27 FN) · 4/200 → [0,8 %, 5,0 %] |
| E4 · Relevantes sin análisis | 16 | Censo | Diagnóstico |
| E5 · Inestables (cambiaron de veredicto) | 11 + 2 | Censo | Diagnóstico de borde |
| E6 · `DESCARTADO_REGEX` | 3 | Censo | Diagnóstico |

**Total ≈ 330 ítems**, de los cuales sólo ≈130 exigen abrir el pliego (E1, E2, E4-E6);
los 200 de E3 se juzgan por título.

**Por qué no más:** con 40 NO-GO no hay forma de afinar más la tasa de falso NO-GO que
censándolos; el intervalo se estrecha sólo con el tiempo. Se re-mide a los 90 días con
los NO-GO nuevos (≈60-90 más). Para comparar dos modelos sobre los mismos ítems (§7) se
usa McNemar sobre los pares discordantes: con ≈100 ítems etiquetados que pasan por el
triaje, una diferencia de ≈10-15 puntos es detectable; menos que eso, no.

---

## 4. Protocolo de etiquetado

### 4.1 Quién

Una persona del área comercial que hoy decide qué ofertar en El Salvador (pregunta Q1).
No un ingeniero de AMALIA: el criterio que se quiere aproximar es el comercial.

### 4.2 Ciego al modelo

La planilla muestra: código, título, institución, modalidad, fecha, enlace a COMPRASAL
y —para E1/E2/E4-E6— el pliego. **Oculta** `es_relevante`, `triaje`, `confianza`,
`razon`, scores y estrato. Orden aleatorio con los estratos mezclados, para que la
persona no infiera «este bloque son los que el modelo descartó».

Por cada ítem: la etiqueta de tres niveles de §2; si es `NO_ES_NUESTRO`, **un motivo del
catálogo** de [`src/feedback/catalogo.py:16-26`](../../src/feedback/catalogo.py) (mismos
códigos que el voto del correo, para que ambas señales sean comparables); comentario
libre opcional.

### 4.3 Calidad del etiquetado

- **Doble etiquetado del 20 %** (≈30 ítems de los que llevan pliego) por una segunda
  persona → κ entre humanos = techo (§2).
- **Calibración previa:** 10 ítems de práctica fuera de la muestra, discutidos antes de
  empezar.
- **Adjudicación:** los desacuerdos humano-humano los resuelve una tercera persona o una
  conversación; los desacuerdos humano-modelo **no** se adjudican (son el resultado).

### 4.4 Esfuerzo

| Bloque | Ítems | Minutos/ítem | Horas |
|---|---|---|---|
| Con pliego (E1, E2, E4-E6) | ≈130 | 4-6 | 9-13 |
| Sólo título (E3) | 200 | 0,5 | ≈2 |
| Doble etiquetado + calibración | ≈40 | 5 | ≈3,5 |
| **Total** | | | **≈15-18 h de persona comercial** |

---

## 5. Los votos del correo como señal débil

Útiles, pero **no** como verdad de referencia:

- **Sesgo de selección:** sólo se vota lo que llegó al correo, es decir lo que el
  clasificador marcó relevante ([`src/notifier/email_sender.py:594-600`](../../src/notifier/email_sender.py)).
  Sirven para precisión y para falso NO-GO (los NO-GO sí se muestran, en «Baja
  prioridad»); **nunca para recall**.
- **Sesgo de no-respuesta:** se vota lo que irrita (falsos positivos) más que lo que
  sirve. Un % de `IRRELEVANTE` sobre los votos sobreestima el error.
- **Atribución débil:** el token es una capacidad, no una identidad; un correo reenviado
  vota como el destinatario original ([`src/feedback/tokens.py:3-6`](../../src/feedback/tokens.py)).
- **Hoy no hay volumen ni realidad:** 2 votos, ambos de prueba, y en modo validación
  todo correo va al buzón del override
  ([`src/notifier/email_sender.py:44-61`](../../src/notifier/email_sender.py)); el
  resolutor del enlace incluso acepta al override como destinatario
  ([`src/web/feedback.py:83-94`](../../src/web/feedback.py)). **Hasta que el correo salga
  a destinatarios reales, los votos no son señal de cliente.**

Uso propuesto: (a) **filtrar** del análisis los votos de destinatarios del override y
los anteriores a la fecha de salida real; (b) contrastar, en los ítems que tengan ambos,
voto vs. etiqueta de la muestra → si concuerdan en ≥85 %, los votos pueden usarse como
monitoreo continuo de la precisión entre evaluaciones; (c) el motivo más votado guía la
siguiente revisión del perfil del cliente, no el umbral.

---

## 6. Guion reproducible — `scripts/evaluar_triaje.py` (a escribir, no escrito)

Mismo espíritu que [`scripts/comparar_llm.py`](../../scripts/comparar_llm.py): lee
archivos que ya existen, **no gasta tokens** salvo en el subcomando explícito
`rejugar`, y su salida es agregada.

| Subcomando | Entrada | Salida | Notas |
|---|---|---|---|
| `muestra --corte AAAA-MM-DD --semilla N` | `history/<cliente>.jsonl`, `.analisis.jsonl` | `data/evaluacion/<corte>/planilla.csv` (ciega) + `clave.csv` (estrato, veredictos del modelo) | Toma el **último** estado por `proceso_uuid`; reproduce la misma muestra con la misma semilla |
| `importar planilla.csv` | planilla etiquetada | `etiquetas.jsonl` | Valida etiquetas y códigos de motivo contra el catálogo |
| `metricas` | `clave.csv` + `etiquetas.jsonl` [+ votos] | Tabla de §2 con IC, cortes por formato y por banda de confianza; `--json` | Sólo agregados: ningún título ni texto de pliego en la salida |
| `votos --desde FECHA` | `<cliente>.feedback.jsonl` o `licitaciones_feedback` | Concordancia voto↔etiqueta | Excluye override; nunca imprime correos |
| `rejugar --modelo M --presupuesto-usd X` | ítems etiquetados de E1/E2 | Veredictos del modelo M + McNemar contra el actual | Requiere volver a **descargar** los pliegos: el pipeline los borra ([`src/drilldown/pipeline.py:392-400`](../../src/drilldown/pipeline.py)). Reutiliza `triage.triage()` con `LLM_PROVIDER`/`MODEL_*` de [`src/llm/modelos.py`](../../src/llm/modelos.py) y la guardia de [`src/drilldown/quota.py`](../../src/drilldown/quota.py) |

Tests: `tests/test_evaluar_triaje.py` con JSONL sintéticos (estratos, semilla,
último-estado-gana, Wilson contra valores conocidos, exclusión del override).
`data/evaluacion/` queda bajo `data/`, que no se versiona
([`CLAUDE.md`](../../CLAUDE.md) → árbol, entrada `data/`).

### 6.1 Instrumentación previa recomendada (cambio pequeño, PR propio)

Sin esto la hipótesis 1.3.2 queda sin respuesta para siempre:

- Persistir el `motivo` del triaje en `ResultadoAnalisis.anexos["triaje_motivo"]`
  (mismo truco que `monto_estimado_clausula`, [`src/scraper/models.py:110-117`](../../src/scraper/models.py),
  sin tocar el esquema) — así un `parse_error` queda distinguible de un NO-GO honesto.
- Opcional: incluir `numero_referencia` en el libro de consumo (hoy no lo lleva,
  [`src/llm/registro.py:355-386`](../../src/llm/registro.py)) para unir costo y veredicto.
  Choca con la regla de «tamaños, nunca contenido» del propio módulo sólo si se
  considera dato del cliente; un código de proceso público no lo es.

---

## 7. Decisiones que habilita

| Decisión | Con qué evidencia | Umbral sugerido para actuar |
|---|---|---|
| **Corregir el triaje de inmediato** si muchos NO-GO son `parse_error` | §6.1 + E1 | Cualquier `parse_error` confirmado → que el parseo fallido vaya a `REQUIERE_REVISION_MANUAL` o `ERROR_LLM`, nunca a NO-GO |
| **Eliminar el triaje** y analizar todo lo relevante | Tasa de falso NO-GO + §1.3.5 | Falso NO-GO > 10 % con IC por encima de 5 %, o costo del triaje > análisis evitado |
| **Reinterpretar `confianza` y el umbral 0,50** | Curva de acierto por banda de confianza en E1/E2 | Si la banda 0,1-0,2 tiene más GO humanos que la 0,0 → umbral para mandar NO-GO a revisión manual |
| **Scout vs. Maverick vs. Claude Haiku en el triaje** | `rejugar` + McNemar sobre E1+E2 | Cambiar sólo si mejora el falso NO-GO con p < 0,05 y el costo extra cabe en el presupuesto diario |
| **Bajar o subir `UMBRAL_MATCH_DECIDIR = 80`** | Recall de punta a punta por banda de `match` | Si >20 % de `OFERTARIA` cae en «Baja prioridad» por `match` |
| **Ajustar el perfil del cliente** (reglas, señales) | Motivos de E3 y de E1 | Motivo dominante en ≥30 % de los errores |
| Reetiquetar el KPI 2 como precisión real | Precisión de relevancia con IC | Cuando haya una evaluación vigente (< 90 días) |

---

## 8. Costo estimado

| Concepto | Estimación |
|---|---|
| Tiempo humano | ≈15-18 h de persona comercial (§4.4) + ≈2 h de quien prepara y corre el script |
| Desarrollo | `scripts/evaluar_triaje.py` + tests: 1-2 días; instrumentación §6.1: < 1 día |
| `rejugar` con Claude Haiku 4.5 sobre ≈100 pliegos | Tarifa de [`quota.py:135`](../../src/drilldown/quota.py) ($1/$5 por MTok) × tokens medianos de Scout (12.905/75) ≈ **$0,013 por pliego ≈ $1,5** en total. El conteo de tokens de imagen de Claude difiere del de Llama: **No verificable** hasta correrlo. Exige una key de Anthropic con saldo |
| `rejugar` con Scout/Maverick | Del orden de $0,7-1,5 (tarifa estimada de §1.2) |
| Re-descarga de pliegos | Sesión de COMPRASAL y ancho de banda; sin costo monetario |

---

## 9. Riesgos y gobernanza del dato

- **Los pliegos y la planilla son dato de clientes.** La planilla, las etiquetas y los
  pliegos re-descargados viven sólo en `data/evaluacion/` de una máquina controlada; no
  se suben al repo, ni a un Artifact, ni a una hoja compartida pública. El script nunca
  imprime contenido (misma regla que [`src/llm/registro.py`](../../src/llm/registro.py),
  sección «Privacidad»).
- **Terceros (ADR 0004 → *Gobernanza del dato*):** `rejugar` con las familias `xai` o
  `google` saca los pliegos de OCI hacia servidores que ningún contrato contempla
  ([`../decisiones/0004-abstraccion-de-proveedor-llm.md`](../decisiones/0004-abstraccion-de-proveedor-llm.md)).
  Para esta evaluación **sólo `nativo` (Meta en OCI) y `anthropic`**, salvo decisión
  explícita (Q5). `anthropic` también es un tercero, pero ya es el proveedor histórico.
- **Correos y votos:** `licitaciones_feedback` guarda `destinatario_email`
  ([`db/schema.sql`](../../db/schema.sql), tabla `licitaciones_feedback`); el script usa
  `destinatario_id` y nunca exporta correos.
- **Contaminación del etiquetador:** si la persona ya leyó el correo de AMALIA de ese
  proceso, no está ciega. Mitigación: preferir procesos con >2 semanas, y registrar
  «ya lo había visto» como columna.
- **Muestra pequeña y de un solo cliente/país:** los resultados no generalizan a otros
  perfiles ni a otros portales ([`portal-amalia.md`](portal-amalia.md) §1.1).
- **Deriva:** cambiar el modelo, el prompt o el perfil invalida la evaluación; se fecha
  y se repite tras cada cambio de esos tres.
- **Datos de DEV:** la ventana medida es la de `multicomputos_dev` en modo validación;
  el volumen y la mezcla de producción pueden diferir (`AMALIA_PROD` no existe).

---

## 10. Preguntas abiertas para Emilio

| # | Pregunta |
|---|---|
| Q1 | ~~¿Quién etiqueta?~~ **Resuelta en criterio (2026-10-24):** una persona comercial de El Salvador que conozca el portafolio, en 3-4 bloques de ≈4 h (≈15-18 h en total); el doble etiquetado del 20 % lo hace otra persona que decida ofertas (idealmente quien las aprueba). **Falta nombrar a las dos personas**: sin ellas la evaluación no arranca. |
| Q2 | ~~¿Tres niveles? ¿GO = OFERTARIA o REVISARIA?~~ **Resuelta (2026-10-24):** se conservan `OFERTARIA` / `REVISARIA` / `NO_ES_NUESTRO`, y el **GO del triaje equivale a `OFERTARIA` ∪ `REVISARIA`** (un falso NO-GO pierde una oportunidad; un falso GO solo cuesta un análisis de más). La definición se valida con los 10 ítems de práctica antes de empezar. |
| Q3 | ~~¿Tasa de falso NO-GO tolerable?~~ **Resuelta (2026-10-24): ≤ 5 %**; si el equipo oferta pocas licitaciones al año, bajarla a ≤ 3 %. Por encima de 10 % el plan ya contempla eliminar el triaje (§7). |
| Q4 | ~~¿Se aprueba primero el PR de instrumentación de §6.1?~~ **Hecha: #81** (`anexos["triaje_motivo"]`). |
| Q5 | ~~¿`rejugar` con Claude? ¿`xai`/`google`?~~ **Resuelta (2026-10-24):** `rejugar` puede usar Claude Haiku 4.5 (≈$1,5 sobre ≈100 pliegos, no verificado) **una vez recargada la key de Anthropic**, hoy sin saldo; mientras tanto, solo Scout/Maverick en OCI. Se **descartan `xai` y `google`**: sacan los pliegos de OCI hacia servidores que ningún contrato contempla (ADR 0004). |
| Q6 | ~~¿Cuándo sale el correo a destinatarios reales?~~ **Resuelta (2026-10-23): cuando Emilio lo indique.** Hasta entonces el modo validación sigue y los votos de feedback no cuentan. |
