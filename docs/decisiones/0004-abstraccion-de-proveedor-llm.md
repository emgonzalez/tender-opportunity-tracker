# ADR 0004 · El proveedor de LLM es configurable: Claude ⇄ OCI Generative AI

- **Fecha:** 2026-09-19
- **Estado:** aceptado
- **Ámbito:** `src/llm/` (nuevo), `src/config.py`, los 5 sitios que llamaban a
  `anthropic` directo, `src/drilldown/quota.py`, `oci/lib/04-app.sh`
- **Relacionado:** [ADR 0002](0002-presupuesto-en-dolares.md) — la guardia de
  gasto que esta decisión obliga a reabrir

---

## Contexto

AMALIA hablaba con Anthropic desde **cinco sitios**, cada uno con su propia
instanciación del cliente, su propio ID de modelo y su propia semántica de error:
`triage.py`, `analysis.py`, `engine.py` y dos llamadas en `trainer.py`. Dos de
esos IDs estaban **hardcodeados** como constantes de módulo, y los de
`validate_tracka_e2e.py` **diferían de los defaults de `config.py`** — o sea que
la validación end-to-end certificaba una combinación que producción no usaba.

El pedido fue poder apuntar cualquiera de esos cinco sitios a los modelos
generativos de OCI, de forma parametrizable. El motivo de negocio es doble:
negociar el costo de inferencia dentro del mismo contrato de OCI donde ya viven
la VM y la ADB, y dejar de depender de un único proveedor en la ruta crítica.

## Decisión

Una capa de abstracción en `src/llm/`, con **dos perillas ortogonales**:

| Variable | Valores | Qué decide |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` · `oci` | Quién factura y qué SDK se usa |
| `OCI_FAMILIA` | `nativo` · `xai` · `google` | Qué familia de modelos, dentro de OCI |

Más `MODEL_*` / `OCI_MODEL_*` por etapa, que pisan la familia y permiten mezclar
(triaje en Llama, análisis en Gemini). **Los defaults son `anthropic` y `nativo`
en los cuatro tenants**: producción no cambia de comportamiento hasta que una
persona lo decida.

### Forma de la abstracción: funciones y un dict, no un `Protocol`

No hay ni un `Protocol` en todo `src/`, y el molde más cercano del repo es
`alerts.py` — callables resueltos por nombre en el punto de uso. La razón de peso
no es estética: **el idioma de la suite es `monkeypatch.setattr` sobre la función
propia de más alto nivel** (14 ocurrencias en `test_pipeline.py`). Con
`llm.completar(...)` un test parchea `triage.llm.completar` y encaja sin aprender
nada nuevo; un `Protocol` obligaría a construir instancias, que es fricción nueva
sin beneficio — mypy no está en CI.

`test_pipeline.py` **no se tocó** en toda la migración. Es la prueba de que la
abstracción entró por debajo del contrato existente.

## Lo que se midió, y por qué importa

Nada de esto salía de la documentación. Cinco hallazgos cambiaron el diseño:

**1 · OCI GenAI no sirve inferencia en `us-ashburn-1`**, que es donde viven las
VMs. Todos los modelos devolvían `404 Entity with key <modelo> not found` — que
se lee igual que un ID mal escrito o una falta de permiso. Funciona en
`us-chicago-1`. Lo que destrabó el diagnóstico fue que fallara **también en el
tenant personal, donde el usuario es admin**: eso descartó IAM. ⚠️ El tenant
`emiliogonzalez` solo está suscrito a Ashburn, así que **no puede usar GenAI**.

**2 · El techo de salida es por MODELO, no del proveedor.** Medido por bisección:

| Modelo | Techo de `maxTokens` |
|---|---|
| `meta.llama-4-scout` / `-maverick` | 4.096 |
| `google.gemini-2.5-flash` | 65.536 |
| `xai.grok-4.6` | acepta 200.000 |

OCI lo valida **en el request** con un 400 duro, no con un truncamiento. Por eso
el clamp es requisito y no optimización: sin él, `analysis` (16.000) y `engine`
(16.384) fallan en la primera llamada.

**3 · Grok y Gemini gastan tokens de razonamiento que no reportan.** Con el mismo
prompt y `max_tokens=20`: Llama reportó 20 de salida sobre 59 totales (cierra
exacto); Grok reportó **18 sobre 514**. Usar `completion_tokens` subestimaría el
gasto **~25×**. Para esas familias el número honesto es `total - prompt`.

**4 · Esos tokens invisibles cuentan contra `max_tokens`.** El triaje pide 512,
calibrado para Haiku. Gemini gastó 508 pensando y emitió 21 de respuesta: el JSON
quedó cortado a la mitad, el parseo falló y **un GO se convirtió en NO-GO con
confianza 0.0**. Es el peor modo de falla del sistema —el pliego se descarta en
silencio— y es **intermitente**, así que documentarlo no alcanzaba. De ahí
`PISO_SALIDA_CON_RAZONAMIENTO = 2048`.

**5 · `cohere.command-a-*` exige otro formato**: responde `400 Chat request type
does not match serving model` a un `GenericChatRequest`. Queda fuera. Grok y
Gemini, en cambio, hablan el mismo formato que Meta y leen imágenes base64 — por
eso admitirlos costó una tabla, no un traductor nuevo.

## Consecuencias

### La contabilidad de gasto hay que reabrirla

`UsoModelo.acumular()` lee `input_tokens`/`output_tokens` por duck typing con
`getattr(..., 0)`: si los nombres no coinciden **devuelve 0 en silencio**. OCI
los llama `prompt_tokens`/`completion_tokens`. Esa traducción vive en un solo
punto (`Uso.desde_oci`) y hay un WARNING greppable cuando ambos campos dan cero.

Además `engine.py` entró al libro mayor —**solo `cobrar()`, nunca `cabe()`**—
porque es la ruta que corre siempre y su gasto estaba enteramente fuera de la
cuenta. La asimetría es deliberada: *el drill-down se frena por presupuesto; el
clasificador solo se contabiliza. Frenar el clasificador es apagar el sistema, y
eso lo decide una persona, no un contador.* `trainer.py` queda fuera (CLI manual).

### El análisis ve menos pliego con OCI

Anthropic acepta el PDF completo y lo rasteriza del lado servidor. OCI no tiene
equivalente (`DocumentContent` toma una URL, no base64). El adaptador traduce:
nativo → capa de texto; escaneado → N páginas rasterizadas, acotadas por
`OCI_ANALISIS_RASTER_PAGES`. Para pliegos nativos probablemente **mejora** (la
cita textual sale del texto real); para escaneados largos es una **pérdida real**.

### Gobernanza del dato

`xai` y `google` **no corren dentro de OCI**: la inferencia sale a servidores de
xAI y Google, o sea que los pliegos de los clientes pasan a un tercero que ningún
contrato contempla. Se decidió **advertir, no bloquear**: `validar_configuracion()`
loguea un WARNING con prefijo `GOBERNANZA DE DATOS` nombrando al tercero, y
`04-app.sh` lo repite en el log del despliegue — el único momento en que hay un
humano mirando. El default sigue siendo `nativo` en los cuatro tenants.

### Credencial en la VM

Instance Principal está denegado (`dynamic-groups`, ver
[`../../oci/PERMISOS-IAM.md`](../../oci/PERMISOS-IAM.md)) y un PAR no firma el
endpoint de inferencia. La API key viaja por el `.env`, inyectada desde
`oci/state/` — el mismo patrón ya probado de `ADB_PASSWORD`. Se descartó montar
un archivo porque `botuser` tiene uid distinto entre `Dockerfile` (`useradd -r`)
y `Dockerfile.oci` (`useradd -m`): un `0600` sería legible en una imagen y no en
la otra.

### El SDK cuesta 405 MB de imagen

Va como extra opcional (`pip install '.[oci]'`) con build-arg `INSTALL_OCI`. La
imagen por default no creció. Ojo con una trampa del repo: **`import oci` no
lanza `ModuleNotFoundError`** porque hay un directorio `oci/` en la raíz que
Python trata como namespace package. Hay que importar el submódulo y atrapar
`ImportError` **y** `AttributeError`; en los tests, `importorskip("oci.generative_ai_inference")`.

## Lo que queda abierto

**Cómo factura OCI las imágenes.** Oracle documenta que 1 transacción = 1
carácter, pero no dice cómo cuenta una imagen. Una página rasterizada son 45.329
caracteres de base64 contra 2.491 tokens reportados: un factor 3,3. Si factura el
base64, el triaje de escaneados sale **3,3× más caro que Anthropic**, justo para
el 66% del corpus que es escaneado. La Usage API está denegada
(`NotAuthorizedOrNotFound`), así que no se puede medir por API: se cierra mirando
Cost Analysis en la consola. Mientras tanto `quota.py` adopta la hipótesis
pesimista.

**Las tarifas de grok y gemini** no están en `PRECIOS_USD_MTOK` a propósito:
`_PRECIO_DESCONOCIDO` los cobra como el modelo más caro conocido. Una tarifa
inventada es peor que ninguna — se ve precisa y no lo es.
