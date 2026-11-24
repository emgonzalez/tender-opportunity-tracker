# Evaluación de precisión — clasificación de Costa Rica (SICOP)

> **Qué es este documento:** el procedimiento de punta a punta para medir, contra el
> criterio del equipo de Multicómputos Costa Rica, cuánto acierta AMALIA al decir
> «relevante» (y qué marcas) en los concursos de SICOP del cliente `multicomputos_cr`.
> Es la Ola C del despliegue de SICOP. Las herramientas están escritas y probadas con
> datos simulados; **todavía no se corrieron contra datos reales**.
>
> **Qué no repite:** el porqué de las métricas, el protocolo general de etiquetado a
> ciegas y la discusión de votos como señal débil viven en
> [`evaluacion-precision-triaje.md`](evaluacion-precision-triaje.md) (§2, §4 y §5).
> Este plan es su aplicación a CR, sin la parte de triaje GO/NO-GO.

| Pieza | Dónde |
|---|---|
| Exportador de la muestra (solo lectura) | [`scripts/exportar_muestra_cr.py`](../../scripts/exportar_muestra_cr.py) |
| Evaluador | [`scripts/evaluar_precision_cr.py`](../../scripts/evaluar_precision_cr.py) |
| Estadística y umbrales por marca | [`src/aprendizaje/evaluacion.py`](../../src/aprendizaje/evaluacion.py) |
| Competibilidad por modalidad | [`src/portales/modalidades.py`](../../src/portales/modalidades.py) |
| Perfil del cliente | [`clients/multicomputos_cr.yaml`](../../clients/multicomputos_cr.yaml) (hereda de `clients/base/multicomputos.yaml`) |
| Qué publica SICOP (UNSPSC, modalidades) | [`../arquitectura/sicop-api.md`](../arquitectura/sicop-api.md) |

---

## 1. Cuándo exportar

Con **3 a 5 días de corridas reales** de SICOP en `multicomputos_dev`, y antes de
cambiar nada del perfil (si se cambia el perfil a mitad, se mide una mezcla). La
población es todo lo que `cliente_proceso` tiene de `multicomputos_cr` en `sicop_cr`,
relevante o no; los concursos nunca clasificados se excluyen y se cuentan.

## 2. Exportar

En la VM de DEV, dentro del contenedor (necesita `ADB_*` del entorno; no necesita
credenciales de SICOP ni del LLM):

```bash
python scripts/exportar_muestra_cr.py \
  --historial data/history/multicomputos_cr.jsonl \
  --desde 2026-11-25 --hasta 2026-11-29 \
  --salida data/evaluacion/cr-2026-11-30
```

- Abre la transacción con `SET TRANSACTION READ ONLY`: no puede escribir en la ADB, y
  no tiene modo `--aplicar` porque no hay nada que aplicar.
- `--historial` es necesario para el UNSPSC: la ADB **no** lo guarda (vive en
  `Proceso.datos_portal["unspsc"]` del JSONL). Sin él, la señal de TI del estrato de
  falsos negativos usa solo palabras del catálogo del perfil.
- Semilla fija (`--semilla`, por defecto `20261124`): misma población y misma semilla
  dan la misma muestra y el mismo orden. Anotar la semilla y las fechas usadas.

### Estratos (excluyentes)

| Estrato | Definición | Objetivo | Para medir |
|---|---|---|---|
| `relevante_alto` | relevante para AMALIA y match ≥ 80 | 60 (`--n-relevantes`) | precisión de lo que llega a «Para decidir» |
| `borde` | relevante con match < 80, o no relevante con match en [50, 80) | 40 (`--n-borde`) | dónde se equivoca el umbral |
| `no_relevante_senal_ti` | no relevante con UNSPSC de segmento 43/81 o palabra del catálogo | 30 (`--n-senal-ti`) | falsos negativos probables |
| `no_relevante_azar` | no relevante sin señal de TI | 50 (`--n-azar`) | falsos negativos de fondo |

«Relevante» y «match» son las definiciones del portal
([`src/memoria/relevancia.py`](../../src/memoria/relevancia.py)). Un estrato con menos
concursos que su objetivo se **censa** y el faltante no se redistribuye, para que el peso
(población / muestra) de cada estrato quede bien definido. Si el total baja de 150, el
script avisa: esperar más días.

### Lo que produce

| Archivo | Para quién | Contenido |
|---|---|---|
| `muestra_ciega.csv` | equipo CR | id, número de procedimiento, institución, descripción, detalle de partidas, UNSPSC, modalidad, monto y moneda, fecha; y cuatro columnas vacías |
| `modalidades.csv` | equipo CR | las modalidades de SICOP (LY, LE, LD, SE, PE, PX, CD, CE, PP, RE…) para responder si son ofertables |
| `clave_veredictos.csv` | **solo quien evalúa** | estrato, peso, veredicto de AMALIA, match, marcas con veredicto, competibilidad del código |
| `muestra_resumen.json` | quien evalúa | tamaños por estrato y semilla |

El ciego se construye desde una lista blanca de columnas (hay un test que lo verifica):
no lleva relevancia, match, marcas, razón ni estrato, y las filas van barajadas para que
no se adivinen los bloques. `modalidades.csv` tampoco muestra lo que decide hoy el código.
Todo queda bajo `data/`, que no se versiona.

## 3. Etiquetar a ciegas (instrucciones para el equipo CR)

Enviar **solo** `muestra_ciega.csv` y `modalidades.csv`, con estas instrucciones:

> Para cada concurso, respondé como si fueras a decidir hoy si Multicómputos lo trabaja.
> Podés buscarlo en SICOP por su número de procedimiento.
>
> - **relevante (sí/no)**: ¿es algo que Multicómputos querría al menos revisar? (TI,
>   licencias, equipo de cómputo, redes, seguridad, servicios de TI…).
> - **marcas**: las marcas de nuestro portafolio que encajan, separadas por coma
>   (p. ej. `Oracle, HPE`). Vacío o `ninguna` si no aplica ninguna.
> - **ofertable (sí/no)**: ¿podríamos presentar oferta por la forma del concurso
>   (modalidad, requisitos)? Vacío si no lo sabés.
> - **comentario**: opcional; útil cuando dudás o cuando el título engaña.
>
> No cambies los encabezados ni la columna `id`, no borres filas. Si una fila no la
> podés juzgar, dejala vacía: se cuenta como «sin etiquetar», no como error.
>
> En `modalidades.csv`, para cada modalidad: `sí`, `no` o `depende`; en «condiciones»,
> de qué depende.

Se aceptan `sí`/`si`/`s`/`x`/`1` y `no`/`n`/`0`, con o sin tilde, y el CSV puede
volver guardado por Excel con `;` como separador. Calidad del etiquetado (doble
etiquetado de una parte, calibración previa con ejemplos fuera de la muestra): como en
[`evaluacion-precision-triaje.md` §4.3](evaluacion-precision-triaje.md#43-calidad-del-etiquetado).

## 4. Evaluar

Con los archivos de vuelta, en cualquier máquina (no toca la ADB ni la red):

```bash
python scripts/evaluar_precision_cr.py \
  --ciego data/evaluacion/cr-2026-11-30/muestra_ciega.csv \
  --clave data/evaluacion/cr-2026-11-30/clave_veredictos.csv \
  --modalidades data/evaluacion/cr-2026-11-30/modalidades.csv
```

Escribe `informe_precision_cr.md` y `.json` junto al ciego (o donde indiquen
`--salida-md` / `--salida-json`). Un CSV mal formado (encabezado cambiado, valor que no
es sí/no, id que no está en la clave, ids repetidos, celdas de más) se rechaza con el
número de fila y la columna; las filas vacías solo se cuentan.

Qué reporta:

- **«Relevante»**: matriz de confusión, precisión y recobro en la muestra con intervalo
  de Wilson al 95 %, κ y acuerdo bruto; y la **estimación ponderada** por estrato (la que
  decide: la muestra sobrerrepresenta a propósito el borde y los no relevantes con señal
  de TI, así que el recobro de la muestra cruda subestima el real).
- **Por marca**: `evaluacion.evaluar` y sus umbrales (recobro ≥ 0,98, precisión
  confirmada ≥ 0,95, Wilson inferior de Oracle ≥ 0,85). Una marca sin veredicto en la ADB
  cuenta como `por_confirmar` (como la muestra el portal); una que AMALIA no puso, como
  `fuera`. Con `MARCA_VEREDICTO` apagado no hay `confirmada` y la precisión confirmada
  será 0: es lo esperado, no un error.
- **Errores por familia**: falsos positivos y negativos agrupados por familia UNSPSC
  (4 dígitos), institución y palabra del título; con los ids de cada error para revisarlos.
- **Modalidades**: dónde la respuesta del equipo (o las etiquetas de «ofertable» de la
  muestra) **contradice** la competibilidad de
  [`src/portales/modalidades.py`](../../src/portales/modalidades.py) (dice «sí» a una no
  competible, o «no» a una competible) y dónde solo **difiere** (una parte dice «depende»).

## 5. Umbrales para promover CR a PROD (propuestos, a confirmar por Emilio)

| Criterio | Umbral |
|---|---|
| Filas etiquetadas | ≥ 100 (`--minimo-etiquetadas`); con menos, la decisión es «insuficiente» |
| Precisión ponderada de «relevante» | ≥ 0,70 |
| Recobro ponderado de «relevante» | ≥ 0,85 |
| Por marca | la puerta de `src/aprendizaje/evaluacion.py` (solo marcas con positivos humanos) |
| Modalidades | ninguna contradicción sin resolver en `modalidades.py` |

El recobro pesa más que la precisión a propósito: un falso negativo no llega nunca al
correo y ningún voto lo detecta; un falso positivo lo descarta una persona en segundos.
Cumplir estos umbrales es una de las condiciones de la Puerta G2 (promover SICOP a PROD);
el resto del procedimiento de promoción está en
[`promocion-dev-a-prod.md`](promocion-dev-a-prod.md).

## 6. Ajustar y volver a medir

Con la tabla de errores, los ajustes van en este orden y cada uno en su PR:

1. **Perfil** (`clients/multicomputos_cr.yaml`): jerga costarricense y familias UNSPSC
   que aparecen como falsos negativos; exclusiones de las que aparecen como falsos
   positivos.
2. **Filtro de ruido** ([`src/classifier/filtro_ruido.py`](../../src/classifier/filtro_ruido.py)):
   familias de CR claramente ajenas a TI, primero en `sombra`.
3. **Modalidades** (`src/portales/modalidades.py`): solo las contradicciones que el equipo
   confirme; PE, CD y CE son hoy `condicional` y PX, PP y RE `no_competible`.

Después de cambiar el perfil, exportar **una muestra nueva** (otra semilla o fechas
posteriores al cambio): reutilizar la misma muestra con el perfil nuevo mide sobre los
ejemplos con los que se ajustó.

## 7. Subir flags `intel_*` de `sombra` a `activo`

Los flags `intel_*` de [`oci/tenants.yaml`](../../oci/tenants.yaml) son **por tenant, no
por portal**: activar uno en `multicomputos_dev` cambia también El Salvador. Por eso un
flag sube a `activo` solo con evidencia de ambos países (para El Salvador, el plan de
[`evaluacion-precision-triaje.md`](evaluacion-precision-triaje.md) y las herramientas por
marca de `scripts/evaluar_marca.py`).

Para CR, la evidencia es la misma muestra etiquetada evaluada contra la variante
`sombra` de la capa: un CSV `id,es_relevante` con la decisión de la sombra para los ids
de la muestra (sale de `decision_capa` con `variante = 'sombra'`; el formato de `salida`
depende de la capa) pasado con `--sombra`. El informe agrega precisión y recobro de la
sombra y **McNemar** sobre los pares discordantes. Regla propuesta:

- la sombra cumple los umbrales de §5 por sí misma, **y**
- no es peor que la activa en recobro, **y**
- McNemar no muestra a la activa significativamente mejor (p < 0,05 con más casos en
  «activa mejor»).

Con ≈150 etiquetas solo se detectan diferencias de 10-15 puntos (ver
[`evaluacion-precision-triaje.md` §3](evaluacion-precision-triaje.md#3-diseño-de-la-muestra)):
«sin diferencia significativa» no prueba igualdad, solo que la muestra no alcanza para
distinguirlas. El cambio de flag sigue el runbook de promoción y se mergea solo con orden
explícita.

## 8. Supuestos a verificar en la primera corrida real

- Que `cliente_proceso` tenga filas de `multicomputos_cr` con `es_relevante` poblado
  (desde E11.1 la corrida guarda también los no relevantes). Si casi todas tienen
  `clasificado_en` nulo, el exportador las excluye y lo informa.
- Que `p.portal_id = 'sicop_cr'` y que `proceso_gid` sea `sicop_cr:<número>`.
- Que `modalidad_raw` traiga el nombre del catálogo BD01 (de ahí se deduce el código) o
  que el historial traiga `modalidad_codigo`.
- Que el historial JSONL exista: con `ESTADO_FUENTE=adb` el JSONL es solo spool y puede
  no tener el UNSPSC; en ese caso el estrato de señal de TI queda solo por palabras.
