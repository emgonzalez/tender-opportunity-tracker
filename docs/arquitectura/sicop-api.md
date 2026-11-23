# SICOP (Costa Rica): contrato de la API pública

> **Qué es este documento.** Resultado del *spike* de la Fase 0 del plan de
> SICOP para `multicomputos_cr` (referencia: [`docs/planes/multicomputos-costa-rica.md`](../planes/multicomputos-costa-rica.md)).
> Es el único lugar donde se documenta el contrato de la API de SICOP. Las fixtures
> grabadas están en [`tests/fixtures/sicop/`](../../tests/fixtures/sicop/).
>
> **Cómo se obtuvo:** el 2026-11-22, solo con acceso público y anónimo (sin cuenta,
> sin login y sin ofertas). Se combinaron tres fuentes: (1) los *bundles* JS públicos
> de la SPA (`www.sicop.go.cr/app/`), que muestran rutas, métodos y cuerpos;
> (2) llamadas reales hechas desde una pestaña de Chrome con la SPA abierta
> (`fetch` con `credentials: "omit"`); (3) pruebas con `requests`/`curl` desde la
> terminal. Se hicieron unas 175 llamadas a `prod-api`, con pausas de 1 s o más entre
> ellas, contando 36 descargas de adjuntos.

---

## 0. Decisión

### **GO condicionado: Playwright anónimo. `requests` directo, NO-GO sin autorización**

| Vía | Viable técnicamente | Recomendación |
|---|---|---|
| `requests` con un User-Agent honesto o genérico | **No.** El WAF de `prod-api` (F5 Distributed Cloud, `server: volt-adc`) responde `200` con una página HTML de bloqueo | — |
| `requests` haciéndose pasar por un navegador (User-Agent de Chrome) | **Sí.** Con un UA de Chrome completo, la API responde JSON | **No adoptarlo sin autorización.** Sería saltarse a propósito el filtro anti-bot del operador; el spike lo comprobó con 3 llamadas y no lo volvió a usar |
| Navegador real (Playwright Chromium) que abre la SPA y llama la API desde la página | **Sí.** Así se hizo todo el spike | **Vía recomendada.** Conviene aislarla en un módulo propio y **nunca** en `src/scraper/browser.py`. El UA es el del propio navegador, sin falsificar |
| Descargas de «Datos abiertos» (`ce-open-data`) | Sí, pero de forma asíncrona y con un **código enviado por correo** | Plan C y fuente para `src/mercado`. Requiere un buzón de servicio (decisión de Emilio) |

**No hubo motivo de NO-GO:** no hay `robots.txt` (404) ni términos de uso que
prohíban consultar los datos públicos (§1). El riesgo principal es que el operador
filtra activamente a los clientes que no son navegadores. Eso pide consultar a la
Dirección de Contratación Pública (Hacienda, administradora de SICOP) antes de
escalar el volumen (§9, P2).

**Medido en la VM de DEV (2026-11-23, `python -m src.scraper.navegador_anonimo --sonda`):**
- **Chromium *headless*: NO pasa.** Su UA dice `HeadlessChrome/133`; las dos llamadas fallan con
  `TypeError: Failed to fetch` (el navegador descarta la respuesta de bloqueo, que no trae cabeceras
  CORS). La VM sí llega a SICOP: la SPA responde 200 y un `curl` honesto recibe la página de bloqueo.
- **Chromium con ventana virtual (`Xvfb`): SÍ pasa.** Corre en su modo normal, con el UA de fábrica
  `Chrome/133` (sin «Headless»); las dos llamadas dan 200 con JSON. No se falsifica nada. Se validó como
  `botuser`, sin `xvfb-run`, con el `Xvfb` que levanta `navegador_anonimo.py` cuando `SICOP_HEADLESS=false` y
  no hay `DISPLAY`. Memoria: ~519 MB libres después, en la E2.1.Micro de 1 GB.
- **Lo que no cambia:** el UA no se sobrescribe y un bloqueo del WAF sigue siendo `BloqueoAcceso` sin
  reintento. Que pase con `Xvfb` no sustituye la autorización de Hacienda: sigue siendo tráfico automatizado hacia un portal del Estado y conviene pedirla.

---

## 1. Términos de uso, robots y datos abiertos

- **`robots.txt`:** `https://www.sicop.go.cr/robots.txt` devuelve **404**, y
  `https://prod-api.sicop.go.cr/robots.txt` devuelve la página del WAF. No hay
  directivas.
- **Términos de uso:** la consulta anónima no tiene una página de términos. Los
  términos (`comn/api/v1/public/coTermCondTemplate`, `umUserAcceptedTermCond`) se
  aceptan **al registrarse como usuario o proveedor**, y AMALIA no se registra. No se
  encontró ninguna cláusula que prohíba el acceso automatizado a la información
  pública. **Esto no reemplaza una revisión legal** (P2).
- **Datos abiertos:** la SPA tiene la ruta `/app/module/pcont/public/ce-open-data`
  («Módulo descarga datos (Datos Abiertos)»). Descarga en Excel, CSV o JSON, con
  diccionario en `/atDocs/Diccionario_de_datos_reporte_de_contratos_de_Datos_abiertos.pdf`
  y ERD en `/atDocs/diagrama_erd.pdf`. Ofrece estos reportes:

  | `reportType` | Conjunto |
  |---|---|
  | `SV_CONT_0008` | Solicitud de contratación |
  | `SV_CONT_0009` | Detalle pliego de condiciones |
  | `SV_CONT_0010` | Aclaraciones |
  | `SV_CONT_0011` | Recursos |
  | `SV_CONT_0012` | **Ofertas** (oferentes, insumo para GANADO/PERDIDO) |
  | `SV_CONT_0013` | **Adjudicaciones en firme** |
  | `SV_CONT_0014` / `0015` | Contratos / Órdenes de pedido |
  | `SV_CONT_0016` / `0017` / `0018` | Instituciones / Proveedores / Catálogo |

  **El flujo es asíncrono y pide un correo:**
  `POST pcont/api/v1/public/ceOpenDataPublicController/requestDownload` crea un
  proceso (`processId`). Al usuario anónimo le toca la vista `req-code`, que pide un
  correo (`POST bid/api/v1/public/coConfirmCodeProc/reqCode`) y un código de 6 o 7
  caracteres (`…/confirmCode`). El enlace final usa `coReport/checkDownload` y
  `coReport/download?reportId=`. Por eso **no sirve para la extracción diaria** (es
  lento, por correo y en lotes), pero es **la fuente preferible para `src/mercado`**,
  que se hace semanalmente y necesita ofertas y adjudicaciones con proveedor. No se
  invocó en el spike, porque exige ingresar un correo.
  **No se encontró publicación OCDS** de SICOP.

---

## 2. Acceso técnico

| Aspecto | Hallazgo |
|---|---|
| Hosts | SPA Angular en `https://www.sicop.go.cr/app/`; API en `https://prod-api.sicop.go.cr/` (módulos `bid`, `comn`, `epum`, `pcont`, `search-engine`, `tcata`, …, todos `/api/v1/public/…`). Adjuntos en OCI Object Storage (`objectstorage.us-ashburn-1.oraclecloud.com`, que figura en la CSP) |
| WAF | F5 Distributed Cloud (`server: volt-adc`, `x-volterra-location`). **El bloqueo llega con HTTP 200** y `text/html` o `text/plain`: «No fue posible acceder a la página solicitada… ID de soporte: …» (fixture `prod-api_bloqueo_waf.html`) |
| Criterio del WAF (medido) | Bloquea `curl/…`, `python-requests/…`, `Mozilla/5.0` a secas, `Mozilla/5.0 (compatible; …)` y UAs identificables (`tender-tracker/1.0 (+url)`). Deja pasar un UA de Chrome completo, aunque falten Origin y Referer. **No** exige cookies, `Origin` ni `Referer`. `www.sicop.go.cr` (estáticos y JSP heredadas) **no** filtra el UA |
| Autenticación | Ninguna para `…/public/…`. La SPA no manda token en estas llamadas |
| CORS | La SPA llama a `prod-api` desde `www`, y `fetch(…, {credentials:"omit"})` funciona desde la página |
| Latencia (Chrome, CR→US) | Listado 100–290 ms · detalle ~340 ms · partidas ~185 ms · adjuntos (metadatos) ~140 ms · descarga de adjunto 0,45–1,45 s · `epAdjuFirmAwarded/listPage` **~6,9 s** (pesado) |
| Rate limit | **No se observó** a ~1 llamada/s durante unas 175 llamadas. No hay cabeceras `RateLimit-*` ni `Retry-After` |
| Estabilidad | Rutas versionadas (`/api/v1/`). Las respuestas paginadas son `Page` de Spring Data (`content`, `totalElements`, `totalPages`, `last`, …). Los errores de validación son `422` con `{status, message, module, userError}`. **El extractor tiene que validar `Content-Type: application/json`**: el WAF responde 200 con HTML |
| Zona horaria | Las fechas vienen **sin zona**. Se asume `America/Costa_Rica` (UTC-6, sin DST, que es la misma hora que El Salvador). Llegan en dos formatos: `"20-11-2026 19:49:29"` en el listado e ISO `"2026-11-20T19:49:29"` en el detalle |

---

## 3. Contrato de los endpoints

Todos van bajo `https://prod-api.sicop.go.cr/`. «PageRequest» es el cuerpo común de
las búsquedas paginadas (lo construye `pageService` de la SPA):

```json
{"pageNumber": 0, "pageSize": 50,
 "tableSorter": {"sorter": "", "order": "desc"},
 "tableFilters": [],
 "formFilters": [{"field": "<campo>", "value": "<valor>"}]}
```

`createFormFilter` descarta los campos con valor `null` o `""`.

### 3.1 Listado: `POST bid/api/v1/public/epCartel/searchEpCartels` (búsqueda avanzada)

Ejemplo de cuerpo en `epCartel_searchEpCartels_request.json`. Filtros (`formFilters.field`):

| Campo | Formato | Notas medidas |
|---|---|---|
| `regDtFrom` / `regDtTo` | `dd/MM/yyyy` | **Fecha de publicación** (`regDt`), con ambos extremos inclusivos. Es el filtro de la corrida diaria |
| `openbidDtFrom` / `openbidDtTo` | `dd/MM/yyyy` | **Obligatorio en la práctica:** sin él responde `422` «Las búsquedas por fechas, no deben superar el rango máximo de un año», aunque `regDt` sea un solo día. **Cada rango tiene un máximo de 1 año.** Para la corrida diaria conviene `[hoy−180 d, hoy+185 d]`: para el 20-11-2026, `[24/05/2026, 22/05/2027]` devolvió los mismos 171 concursos que la unión de las dos ventanas de un año |
| `cartelTestYn` | `Y` | `Y` = «Real» y `N` = «Prueba» (catálogo `BD23`). `N` devolvió 0. Conviene mandar `Y` siempre |
| `biddocRcvYn` | `Y` | Solo «En recepción de ofertas» (catálogo `ES00`): 80 de 171 el 20-11-2026. **No** sirve para AMALIA, porque deja fuera los «Publicado» que aún no abren recepción |
| `proceType` | `LD`, `LY`, … o `Todos` | Catálogo `BD01` (§5) |
| `cartelNm` | texto | Coincidencia parcial **por palabra**. Si el texto tiene varias palabras, el filtro deja de servir: «equipo de cómputo» devolvió 9 514, prácticamente todo. Hay que usar una sola palabra |
| `cartelInstCd`, `instNm`, `instCartelNo`, `cartelNo`, `cateId` (código UNSPSC del catálogo), `pexcep` (catálogo `EX00`) | | No se probaron uno por uno |

- **Paginación:** `pageSize` admite **10, 20 o 50** (son las opciones de la UI). Con 51
  o 100 responde `422` «EL tamaño de la página no es válido». Un día típico tiene unos
  170 concursos, así que bastan 4 páginas de 50.
- **Orden:** el orden por defecto es `regDt` descendente. `tableSorter.sorter`
  admite el nombre de un campo (p. ej. `regDt`).
- **Respuesta** (`epCartel_searchEpCartels_pagina.json`): el `Page` trae
  `content[]` con `instCartelNo`, `cartelNo`, `cartelSeq`, `cartelNm`, `regDt`,
  `biddocStartDt`, `biddocEndDt`, `openbidDt`, `cartelInstCd`, `cartelInstNm`,
  `proceType`, `proceTypeDetailCd`, `cartelStat`/`cartelStatStr`, `cartelClsYn`,
  `cartelSecretYn`, `biddocType`, `executorNm`/`executorId` (persona, **no
  persistir**). **No trae monto:** el monto viene del detalle o de las partidas.
- **Vacío:** `epCartel_searchEpCartels_vacio.json`. **Errores:** `…_422_rango_fechas.json`
  y `…_422_tamano_pagina.json`.

Hay variantes que AMALIA no necesita:
- `searchEpCartelsSp` busca por número (`cartelNoNm`, `cartelNoType` ∈ `NP01`).
- `searchEpCartelsByPpsType` filtra por `ppsType`.
- `GET …/searchEpCartelsByType` devuelve los totales por bienes, servicios y obras
  (`epCartel_searchEpCartelsByType.json`). Sirve como *health-check* barato.
- `POST search-engine/api/v1/public/advancedEpCartelSearch/searchCarteles` con
  `formFilters=[{field:"searchTerm"}]` hace búsqueda de texto completo por relevancia
  («servidores» → 4 905 resultados ruidosos). **No sirve para filtrar.**

### 3.2 Detalle: `POST bid/api/v1/public/epCartel/findPublishedById`

Cuerpo: `{"cartelNo": "20261001249", "cartelSeq": "00"}`. Devuelve un objeto de
**unos 208 campos**; la fixture `epCartel_findPublishedById.json` trae un recorte. Lo
relevante:
- **Identidad:** `cartelNo`, `cartelSeq`, `cartelVersion`, `instCartelNo` y
  `publishedCartelSeqList[]` (versiones por modificación).
- **Clasificación:** `proceType`/`proceTypeNm`,
  `proceTypeDetail`/`proceTypeDetailNm` («Cantidad definida» u otra, según demanda),
  `ppsType`/`ppsTypeNm` (BIENES, SERVICIOS u OBRAS) y `cartelSecretYn` (concurso
  confidencial).
- **Fechas:** `regDt`, `biddocStartDt`, `biddocEndDt` (cierre de recepción),
  `openbidDt` (apertura), `explainRcvEndDt` (aclaraciones) y
  `recurso.recursoEndDt` (objeción).
- **Montos:** `cartelBudget` (estimación; en los casos vistos coincide con partidas
  en CRC) y `cartelBudgetUsd` (venía `null` en los vistos). **Pendiente de verificar
  con un concurso en dólares** (P5).
- **Condiciones (el «cartel» propiamente):** `generalCondsCont`, `specCondsCont` y
  `adminCondsCont` vienen en **HTML dentro del JSON**. Muchas veces el PDF adjunto es
  solo un anexo, así que el drill-down puede analizar este texto **sin descargar PDF**.
- **Estado:** `cartelStatStr` y `progressStat[]` por partida (`cartelCate`,
  `adjStatCd`, `cartelProgressCd`, `show*Result`).
- **No persistir:** `executorNm`, `writerNm`, `*StaffNm` e IDs `G…`, que son
  funcionarios. Tampoco `colonesAccount` (cuenta bancaria institucional) ni
  `certCont` (certificado).

### 3.3 Partidas y líneas: `POST bid/api/v1/public/epCartelProd/findByEpCartelId`

El cuerpo es un PageRequest con `formFilters=[{cartelNo},{cartelSeq}]` y `pageSize`
de hasta 100 (la SPA usa 100). La fixture es `epCartelProd_findByEpCartelId.json`.
Cada línea trae:
- `id.cartelCate`: la **partida**.
- `id.cateSeqno`: la línea.
- `prodId`: los **primeros 8 dígitos son el código UNSPSC**; por ejemplo
  `43211503…` corresponde a computadoras portátiles. Es una señal útil para el
  clasificador (familias 43 y 81).
- `prodNm`, `prodQty`, `prodUnit` y `prodPrc` (precio unitario estimado).
- **`currencyType`** (`CRC` y presumiblemente `USD`).
- `budgetAmt`: total de la línea.

Hay otros endpoints relacionados:
- `GET bid/api/v1/public/epCartelCate/findCriteriaAmt?cartelNo=&cartelSeq=`: una fila
  por partida con `criteriaAmt` y fechas por partida (en el ejemplo, todo `null`).
  Fixture: `epCartelCate_findCriteriaAmt.json`.
- `POST epCartelRoleStaff/findByEpCartelId` devuelve funcionarios. **No usarlo.**
- `GET epNotice/getNotifyList?cartelNo=&cartelSeq=&cartelCate=&noticeCl=` devuelve
  avisos (`[]` en el ejemplo).

### 3.4 Adjuntos: metadatos y descarga

- **Metadatos:**
  `GET bid/api/v1/public/epCartelFile/getCartelFilesById?cartelNo=&cartelSeq=&docFileSeqno=`
  (con `docFileSeqno` vacío devuelve todos). Trae `docNm`, `clientFileNm`,
  `serverFileNm` (UUID + extensión), `fileServerPath` (`/EDOCS/attach/bid/AAAAMM`),
  `fileTypeNm` («Documentos del Pliego de condiciones», «Especificaciones técnicas»,
  «Cronograma» u «Otros»), `fileSizeMb` (texto) y `releaseYn`/`supplierReleaseYn`.
  Fixture: `epCartelFile_getCartelFilesById.json`.
- **Descarga:**
  `GET comn/api/v1/public/comnFile/download?fileUrl=<fileServerPath sin "/" inicial>/<serverFileNm>`
  (por ejemplo `fileUrl=EDOCS/attach/bid/202611/66fc…7e.docx`, con la URL codificada).
  Responde `200 application/octet-stream` **sin `Content-Disposition`**: el nombre
  sale de `clientFileNm`. No hace falta URL firmada.
- **Alternativa:** `GET comn/api/v1/public/comnFile/downloadWithPAR?fileUrl=…`
  devuelve como texto una **URL pre-autenticada (PAR) de OCI Object Storage**
  (`https://objectstorage.us-ashburn-1.oraclecloud.com/p/<token>/n/…/o/…`, unos 225
  caracteres). Ese host **no está detrás del WAF de SICOP**, así que una vez obtenida
  la PAR se podría bajar el archivo con `requests` sin falsificar nada. No se
  verificó cuánto dura la PAR (P6).
- **`releaseYn=N` no restringe el acceso:** los 143 adjuntos medidos tenían
  `releaseYn=N` y `supplierReleaseYn=N`, y todos se descargaron en forma anónima. No
  se encontró ningún adjunto inaccesible. Los concursos `cartelSecretYn=Y`
  (confidenciales) se excluyeron de la muestra y quedan sin verificar.

### 3.5 Catálogos: `GET comn/api/v1/public/coCommonCd/list/{cdCls}`

Cada entrada trae `cd`, `cdNmEs`, `cdCls`, entre otros campos. Clases útiles:
- `BD01`: tipo de procedimiento (fixture `coCommonCd_list_BD01.json`).
- `EX00`: causales de excepción (fixture `coCommonCd_list_EX00.json`).
- `ES00`: `Y` = en recepción, `N` = todos.
- `BD23`: `Y` = real, `N` = prueba.
- `NP01`: `01` = número de procedimiento, `02` = número de SICOP.

### 3.6 Tipo de cambio: `GET bid/api/v1/public/coExchangerate/lastExchangeRate?nationalCD=USD`

Con `nationalCD=USD` devuelve la base 1:1, lo cual no sirve
(`coExchangerate_lastExchangeRate.json`). `exchangerateSearchByDay?adjYmd=AAAAMMDD&nationalCD=`
devolvió `[]`. **No hay que depender de esto:** `fx_tasa` debe venir del BCCR, como
prevé el plan. Hay una salida práctica: `epAdjuFirmAwarded` (§6) trae `crcAmt` y
`usdAmt` juntos, de los que se deduce la tasa que usa SICOP.

---

## 4. Mapeo a `Proceso`

| Campo `Proceso` | Fuente SICOP | Nota |
|---|---|---|
| `portal_id` | constante `sicop_cr` | |
| `numero_referencia` | `instCartelNo` (ej. `2026LD-000364-0015499999`) | Es el número que ve el usuario. El prefijo (`2026LD`, `2026XE`, …) **no siempre** coincide con `proceType`: se vio `XE` con `proceType=PE` |
| `id_externo` | `cartelNo` (+ `cartelSeq`, normalmente `"00"`) | Llave para todas las llamadas de detalle |
| `institucion` | `cartelInstNm` | `cartelInstCd` es la cédula jurídica de la institución |
| `descripcion` | `cartelNm` | Enriquecer con `prodNm` de las partidas para clasificar |
| `fecha_publicacion` | `regDt`, con TZ `America/Costa_Rica` | Es la «Fecha/hora de publicación» de la UI |
| `fecha_limite` | `biddocEndDt` | Cierre de recepción de ofertas |
| (apertura) | `openbidDt` | Útil en el correo |
| `forma_contratacion` | `proceTypeNm` del detalle, o el catálogo `BD01[proceType]` | Más `proceTypeDetailNm` («Cantidad definida» u otro, según demanda) |
| `tipo_actividad` | `ppsTypeNm` (BIENES, SERVICIOS u OBRAS) | Solo viene en el detalle |
| `monto` | Σ `budgetAmt` de las partidas, por `currencyType`; si no hay, `cartelBudget` | |
| `moneda` | `currencyType` de las partidas | Si las partidas mezclan monedas, hay que guardar el monto por moneda o marcarlo como mixto. **Nunca rotular «USD» por defecto** |
| `url_directa` | **No hay enlace profundo.** El detalle de la SPA recibe el id por estado de navegación (`cartel-specification-params-published`), y sin él redirige al listado. El JSP heredado `EP_SEJ_COQ603.jsp` redirige a una página de error | Propuesta: `https://www.sicop.go.cr/app/module/bid/public/tenders` más el `numero_referencia` visible en el correo («Búsqueda por número de procedimiento»). Ver P4 |
| `estado` (nuevo, opcional) | `cartelStatStr` | §5 |

**Costo por corrida:** un día trae unos 170 concursos, que son 4 listados. Hace falta
el detalle y las partidas solo de los candidatos que pasen el prefiltro léxico, o de
todos con caché. Son unas 2 llamadas por concurso: con 1 s de pausa, unos 6 minutos
para todos; con prefiltro, menos de 1 minuto.

---

## 5. Estados y modalidades

**Estados (`cartelStatStr`) observados:**
- Publicado.
- En recepción de ofertas.
- En evaluación.
- Objetado.
- Acto Final en Firme.
- Apelación o Revocación.
- Adjudicado.
- Contrato, más «Contrato finalizado» y «Finiquitado» (este último solo en el código).
- Infructuoso.
- Sin efecto (`cartelClsYn=Y`).

La corrida diaria trae lo publicado ayer **en cualquier estado**. Mismo día se ven
excepciones (`PX`) ya adjudicadas: hay que filtrarlas por competibilidad, no por estado.

**Modalidades (`BD01`) y competibilidad propuesta para `taxonomia_modalidad`:**

| `cd` | Nombre | Régimen | Competible | Nota |
|---|---|---|---|---|
| `LY` | Licitación mayor | Ley 9986 | **sí** | Abierta |
| `LE` | Licitación menor | Ley 9986 | **sí** | |
| `LD` | Licitación reducida | Ley 9986 | **sí** | Es la más frecuente en TI: 210 de 302 en la muestra |
| `SE` | Subasta inversa electrónica | Ley 9986 | **sí** | Compite por precio |
| `PE` | Procedimientos especiales | Ley 9986 | **condicional** | Incluye convenio marco y otros. Se ven con prefijo `XE`. Revisar subtipo y `proceTypeDetail` |
| `PX` | Procedimiento por excepción | Ley 9986 | **no** por defecto | Causales `EX00`: proveedor único, patrocinio, capacitación abierta, etc. Mostrar solo como «exploratoria» |
| `LN` | Licitación pública nacional | Ley 7494 (derogada) | sí | Histórico |
| `LI` | Licitación pública internacional | Ley 7494 | sí | Histórico |
| `LA` | Licitación abreviada | Ley 7494 | sí | Histórico |
| `CD` | Contratación directa | Ley 7494 | condicional | Histórico. La SPA mapea `CE`→`CD` en la vista |
| `CE` | Contratación especial | Ley 7494 | condicional | Histórico |
| `PP` | Procedimiento por principio | Ley 7494 | no | Histórico |
| `RE` | Remate | — | no | La institución **vende** |

La equivalencia de régimen es inferencia propia sobre los nombres. **Que la valide
alguien del equipo CR (P7).**

---

## 6. Resultados, adjudicaciones y proveedores (para `src/mercado`)

| Endpoint (`POST`, PageRequest) | Filtros | Campos | Uso |
|---|---|---|---|
| `bid/api/v1/public/epAwardedProcedureSupplier/listPage` | `startDt`, `endDt` (ISO, p. ej. `2025-11-23T06:00:00.000Z`), **`supplierCd`** (cédula de 10 o 12 dígitos), `instRegNo`, `proceType`, `instCartelNo` | `instCartelNo`, `instNm`, `cartelNo`, `cartelCate`, `cartelNm`, `adjuFinalDt`, `submitAmt`, `currencyType` (**venía `null`**) | **GANADO por proveedor**: concursos adjudicados a una cédula. Con `supplierCd=3102813498` devolvió 1 adjudicación en el último año. Fixture: `epAwardedProcedureSupplier_listPage.json` |
| `bid/api/v1/public/epAdjuFirmAwarded/listPage` | `bgnYmd`, `endYmd` (ISO), `instRegNo`, `instCartelNo`, `typeKey` | `instCartelNo`, `instNm`, **`cartelCate`** (partida), `cartelNm`, `proceType`, `adjuFinalDt`, **`crcAmt` y `usdAmt`** | Mercado completo: monto adjudicado en firme por partida, en las dos monedas. **No trae adjudicatario.** Octubre de 2026 tuvo 11 003 filas, unos 7 s por página |
| `search-engine/api/v1/public/umPublicSupplier/searchUmPublicSupplier` | `supplierNm`, `bizRegNo`, `cateId`, … | `bizRegNo`, `supplierNm`, `bizCl`, `bienYn`, `servicioYn`, `obraYn` | Resolver la identidad del proveedor. Fixture: `umPublicSupplier_searchUmPublicSupplier.json` |
| `epum/api/v1/public/umSupplier/getSupplierInfo` y otros | | | No probados |

**Oferentes y adjudicatario por concurso:** la SPA los muestra en **JSP heredadas
dentro de un modal**:
- `moduloOferta/search/EP_SEJ_COQ610.jsp?cartelNo&cartelSeq&cartelCate&isPEP2=Y`
  muestra el resultado del adjudicatario.
- `moduloOferta/servlet/search/EP_SEV_COQ622?…&phase=2` muestra el resultado de la
  apertura.
- `EP_SEJ_COQ607.jsp` muestra la evaluación.

Están en `www`, que **no filtra el UA**, y devuelven HTML. Con los parámetros probados
contestaron «La información del registro del adjudicatario no se encuentra
disponible» y «Ha ocurrido un error». La combinación exacta de parámetros queda para
A8, que tiene que capturarla con `read_network_requests` al abrir el modal en un
concurso adjudicado. **Para GANADO/PERDIDO con oferentes, la fuente más limpia es el
reporte de datos abiertos «Ofertas» (`SV_CONT_0012`) cruzado con «Adjudicaciones en
firme» (`SV_CONT_0013`)** (§1).

---

## 7. Medición de pliegos

**Muestra:** 36 concursos de TI publicados entre el 01-09-2026 y el 22-11-2026.
Salieron de buscar `servidores`, `firewall`, `switch`, `ciberseguridad`,
`almacenamiento`, `licencias`, `software` y `computadoras`, excluyendo los
confidenciales. Por tipo: 24 LD, 4 LE, 3 LY, 3 PX y 2 PE.

**Método:** se leyeron los metadatos de los 143 adjuntos. Por concurso, se descargó el
PDF principal: el que tenía en el nombre «pliego», «cartel», «condiciones» o
«especificaciones»; si no había, el PDF más grande. Las páginas, el texto y si era
escaneado se midieron **en el navegador, con un analizador heurístico** (cuenta
`/Type /Page` y operadores `Tj`/`TJ` en los *streams* inflados), **no con PyMuPDF**,
porque los archivos no se guardaron en disco. Las cifras de páginas y escaneo son
aproximadas.

| Métrica | Valor |
|---|---|
| Extensiones (143 adjuntos) | pdf 112 · docx 16 · xlsx 8 · zip 2 · png 2 · doc 1 · mp4 1 · mpp 1 |
| Concursos con PDF | 33 de 36. Los otros tres: 2 solo con DOCX o ZIP, 1 sin adjuntos |
| Concursos con XLSX | 7 de 36 (19 %). Siempre como anexo (presupuesto, estructura de precio) |
| Concursos con DOCX como pliego principal | 2 de 36 (6 %). Además, 16 DOCX sueltos |
| PDF principal, páginas | p50 **24** · p90 **60** · p95 74 · máx **123**. 3 de 33 pasan de 60 y 1 de 33 pasa de 100 |
| PDF principal, MB | p50 **0,69** · p90 **3,84** · máx **6,08** |
| Total de adjuntos por concurso, MB | p50 1,59 · p90 5,36 · máx 37,08 (incluye un MP4) |
| Escaneados (sin capa de texto) | 3 de 33 (**9 %**), todos de 1 o 2 páginas |
| Descarga | 0,45–1,45 s por archivo y ningún error en 36 |
| El nombre identifica el pliego | 19 de 33. El resto es anexo, y el texto del cartel vive en el JSON del detalle (§3.2) |

**Recomendación:**
- `PLIEGO_MAX_MB`: **mantener 32**. El máximo observado de un PDF fue 6 MB, así que
  sobra margen. Si se quiere un tope por portal, 16 MB basta para SICOP.
- `PLIEGO_MAX_PAGES`: **mantener 100**. Cubre el 97 % de la muestra, y el caso de 123
  páginas se trunca.
- Excluir del drill-down los adjuntos `mp4`, `mpp`, `png` y `zip`.
- Aceptar DOCX y XLSX como entrada secundaria, como prevé la Fase 3.
- **Primero el texto del detalle** (`*CondsCont` y partidas) y después el PDF.

---

## 8. Fixtures (`tests/fixtures/sicop/`)

Todas son respuestas reales recortadas a pocos elementos, con funcionarios
anonimizados (`FUNCIONARIO ANONIMIZADO`, `G0000000000000`) y los IDs de soporte o
error en ceros. Los campos `_nota` no forman parte de la respuesta real.

| Archivo | Endpoint |
|---|---|
| `epCartel_searchEpCartels_request.json` | Cuerpo de la búsqueda diaria |
| `epCartel_searchEpCartels_pagina.json` | Listado paginado: 2 elementos, con `totalElements` 80 y `totalPages` 8 reales |
| `epCartel_searchEpCartels_vacio.json` | Listado sin resultados |
| `epCartel_searchEpCartels_422_rango_fechas.json` | Error de rango mayor a un año o sin `openbidDt` |
| `epCartel_searchEpCartels_422_tamano_pagina.json` | Error de `pageSize` inválido |
| `epCartel_searchEpCartelsByType.json` | Totales por tipo (*health-check*) |
| `epCartel_findPublishedById.json` | Detalle, recortado de unos 208 campos |
| `epCartelProd_findByEpCartelId.json` | Partidas y líneas (CRC, UNSPSC) |
| `epCartelCate_findCriteriaAmt.json` | Montos y fechas por partida |
| `epCartelFile_getCartelFilesById.json` | Metadatos de adjuntos (PDF y XLSX) |
| `coCommonCd_list_BD01.json` · `coCommonCd_list_EX00.json` | Catálogos de modalidad y de excepción |
| `epAwardedProcedureSupplier_listPage.json` | Adjudicaciones a un proveedor |
| `epAdjuFirmAwarded_listPage.json` | Adjudicaciones en firme por partida (CRC y USD) |
| `umPublicSupplier_searchUmPublicSupplier.json` | Búsqueda de proveedor |
| `coExchangerate_lastExchangeRate.json` | Tipo de cambio (inútil con USD) |
| `epNotice_getNotifyList_vacio.json` | Avisos vacíos |
| `prod-api_bloqueo_waf.html` | Bloqueo del WAF (HTTP 200, HTML) |

---

## 9. Riesgos y preguntas abiertas (para Emilio)

**Riesgos:**
1. **El WAF filtra clientes que no son navegador.** Una actualización del WAF podría
   bloquear también a Chromium automatizado. Mitigación: módulo aislado, alerta de
   ops si `Content-Type` no es JSON, y respaldo de datos abiertos.
2. **La API no está documentada** (es la API interna de la SPA) y puede cambiar sin
   aviso. Mitigación: validar el esquema en el extractor (campos mínimos) y usar estas
   fixtures como contrato de regresión.
3. **No hay enlace profundo al concurso.** Afecta `url_directa` y la experiencia de
   uso del correo.
4. **Moneda:** el listado no trae monto, y `cartelBudgetUsd` y `currencyType` en
   `epAwardedProcedureSupplier` venían `null`. Hay que derivar la moneda de las
   partidas.
5. **Ruido de PX:** las excepciones son unos 40 de 302 concursos de TI y llegan ya
   adjudicadas. Si no se filtran por competibilidad, inflan el reporte.

**Preguntas:**
- **P1. Identidad de Multicómputos CR en SICOP.** La búsqueda pública dio dos
  proveedores:
  - **`3102813498`, MULTICOMPUTOS ADVANCED TECHNOLOGIES SRL.** Es una cédula jurídica
    tica de SRL, con 1 adjudicación en el último año: la licencia SOC de la UCR por
    ₡12,5 M.
  - **`9000018410`, MULTICOMPUTOS S.R.L.** Por el formato parece un identificador de
    proveedor extranjero, y no tiene adjudicaciones en el último año.

  ¿Cuál o cuáles son de Multicómputos CR? Hace falta para `ID_PROVEEDOR` por portal y
  para GANADO/PERDIDO.
- **P2. Autorización.** ¿Se acepta Playwright anónimo con un navegador real, o se
  pide primero a la Dirección de Contratación Pública (Hacienda) autorización o una
  lista blanca para un cliente identificable? **No recomiendo** `requests` con UA de
  Chrome falsificado sin esa autorización.
- **P3. Datos abiertos.** ¿Hay un buzón de servicio para recibir el código y el
  enlace de descarga? Habilitaría «Ofertas» y «Adjudicaciones en firme» para
  `src/mercado`.
- **P4. `url_directa`.** ¿Basta enlazar al buscador público con el número de
  procedimiento visible en el correo?
- **P5. Monto en USD.** Hay que verificar con un concurso en dólares cómo vienen
  `cartelBudget`, `cartelBudgetUsd` y `currencyType`. No apareció ninguno en la
  muestra.
- **P6. Vigencia de la PAR** de `downloadWithPAR`. Si dura lo suficiente, las
  descargas pueden hacerse con `requests` contra Object Storage, sin pasar por el WAF.
- **P7. Competibilidad** de `PE`, `CD` y `CE` según la práctica del equipo CR.
- **P8. Headless.** Medido: *headless* no pasa; con `Xvfb` (`SICOP_HEADLESS=false`) sí, sin tocar el UA.
