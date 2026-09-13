# ADR 0002 · El presupuesto del drill-down se lleva en dólares, no en tokens

- **Fecha:** 2026-08-04
- **Estado:** aceptado
- **Supersede:** la spec de `T4.2 · Guardia de cuota (pre-flight)` en
  [`../planes/fase2-track-a.md`](../planes/fase2-track-a.md)
- **Ámbito:** `src/drilldown/quota.py`, `src/drilldown/pipeline.py`, `src/config.py`

---

## Contexto

La guardia original (T4.2) llevaba un contador diario de **tokens** en
`data/token_budget.json` y estimaba el costo de cada pliego con la heurística

```python
estimado = n_pages * 2000 + 5000
```

Funcionó para lo que se le pidió el 2026-08-03 —frenar un gasto que se estaba
disparando— pero al instrumentar el consumo real (`UsoModelo`, ver
[`../../CHANGELOG.md`](../../CHANGELOG.md)) quedaron a la vista tres defectos
independientes, dos de ellos estructurales.

### D-1 · El triaje no se cobraba (fuga)

El orden era: estimar → comprobar → **triaje** → si `GO`, reservar. Un veredicto
`NO-GO` volvía sin tocar el libro. Es decir: **todo el gasto de triaje quedaba
fuera de la cuenta**, y el triaje no es el gasto menor — con ~66% del corpus
escaneado se le mandan `TRIAGE_RASTER_PAGES` páginas rasterizadas (~2.500 tokens
cada una a 1568 px). Peor: el arreglo del rasterizado a 72 DPI del mismo día
multiplicó el costo del triaje por ~4, agrandando la fuga en la misma proporción.

### D-2 · El token no es una unidad de dinero

El drill-down usa dos modelos con precios distintos: Haiku ($1/MTok de entrada)
para el triaje y Sonnet ($3) para el análisis. Un contador único de tokens no se
puede traducir a gasto: 2 M de tokens son \$2 o \$6 según quién los gastó. Un
tope expresado en tokens no acota el dinero, que es lo que se quería acotar.

### D-3 · La heurística ignoraba el formato

`n_pages * 2000` trataba igual a un pliego nativo y a uno escaneado, y le
atribuía al triaje un costo proporcional a las páginas cuando el triaje está
**topado** en `TRIAGE_RASTER_PAGES` y no crece con el largo del pliego.

---

## Decisión

**1 · El libro mayor se lleva en dólares** (`data/gasto_diario.json`, reset por
fecha), con desglose por modelo. `DAILY_TOKEN_BUDGET` → `DAILY_BUDGET_USD`.

**2 · Estimar y cobrar se separan, deliberadamente asimétricos:**

| | Cuándo | Con qué | Para qué |
|---|---|---|---|
| `cabe()` | antes de llamar | una **estimación** | frenar |
| `cobrar()` | después de llamar | el `usage` **real** de la API | contabilizar |

El libro nunca acumula estimaciones. Esto es lo que habilitó la instrumentación
de `UsoModelo`: antes solo existía la estimación, así que el error del estimador
se acumulaba en el saldo corrido.

**3 · Se cobra por etapa, no una vez al final.** El triaje se cobra siempre,
`GO` o `NO-GO`; el análisis se cobra aunque termine en `ERROR_LLM`. Si el
presupuesto se agota entre ambos, el pliego queda `REQUIERE_REVISION_MANUAL` con
el triaje ya cobrado y su `uso_triaje` persistido: el gasto hecho no se pierde
ni se esconde.

**4 · La estimación se hace por etapa y con su propia unidad:**

- **Triaje** — se puede estimar con precisión, porque el contenido lo armamos
  nosotros: `pdf.tokens_imagen_estimados()` sobre las mismas páginas y con el
  mismo zoom que usará el renderizador, o `len(texto)/4` para pliegos nativos.
- **Análisis** — el PDF va como bloque `document` y **lo rasteriza Anthropic del
  lado servidor, a su propia resolución**. No se puede medir desde el cliente.
  Se usa un coeficiente configurable, `ANALISIS_TOKENS_POR_PAGINA`.

---

## El único número sin medir

`ANALISIS_TOKENS_POR_PAGINA = 3000` es un **prior, no una medición**: es el
extremo alto del rango documentado para PDFs (~1.500–3.000 tok/página según
cuánta capa de texto tengan). Se toma el extremo alto porque una guardia que
subestima no es una guardia.

No se puede calibrar antes de una corrida en producción con `ENABLE_DRILLDOWN=true`
que llegue a `ANALIZADO`. El lazo se cierra con:

```bash
python -m scripts.calibrar_cuota          # no gasta un solo token
```

que lee `data/history/*.analisis.jsonl` —que ya guarda `n_paginas`,
`uso_triaje` y `uso_analisis`— y contrasta lo estimado contra lo cobrado.
Recomienda el **p90**, no la mediana: la guardia debe dimensionarse para el
pliego caro, no para el típico.

---

## Consecuencias

**A favor**

- El tope acota dinero, que es lo que se quería acotar.
- El saldo del día es el gasto real, no la suma de los errores del estimador.
- El desglose por modelo dice *qué etapa* se está comiendo el presupuesto.
- Calibrar es cambiar una variable de entorno, no editar código.

**En contra / riesgos aceptados**

- **Los precios están hardcodeados** en `PRECIOS_USD_MTOK`. Si Anthropic los
  cambia, la contabilidad deriva en silencio. Mitigación: un modelo fuera de la
  tabla se cobra al precio del más caro conocido, así que apuntar `MODEL_ANALYSIS`
  a algo nuevo apreta la guardia en vez de soltarla. Se usa a propósito la tarifa
  estándar de Sonnet 5 y **no** su precio introductorio (\$2/\$10 hasta el
  2026-08-31), para que la guardia no se afloje un 33% el día que expire.
- **Se puede exceder el tope por el sobrante de un pliego.** Entre `cabe()` y
  `cobrar()` una llamada puede salir más cara que lo estimado. El exceso se
  descuenta del pliego siguiente —el dinero ya se gastó—, así que el error está
  acotado a un pliego y no se acumula.
- **Ruptura de configuración:** `DAILY_TOKEN_BUDGET` deja de existir. Sin
  migración: `ENABLE_DRILLDOWN=false` en producción, y el archivo del libro se
  resetea por fecha de todos modos (el formato viejo en tokens se detecta y se
  descarta, para que `{"consumido": 45000}` no se lea como un tope de \$45.000).

---

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Mantener tokens y usar un factor de conversión único | Es la premisa que falla: no hay un factor único cuando hay dos modelos con precios distintos. |
| `client.messages.count_tokens()` para el pre-flight | Obligaría a codificar el PDF en base64 dos veces (memoria) y no resuelve el bloque `document`, que es justo el caso caro. |
| Calibrar el coeficiente automáticamente desde el historial | Un lazo de realimentación que se ajusta solo puede derivar sin que nadie lo note. El script recomienda; la decisión de cambiar el número la toma una persona. |
| Cobrar el análisis por adelantado con la estimación | Es lo que se hacía. Convierte el error del estimador en deuda acumulada del saldo. |
