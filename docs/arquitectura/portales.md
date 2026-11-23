# Núcleo portal-agnóstico: COMPRASAL + SICOP

> **Qué es este documento.** Es el diseño de la Fase 1 del plan para SICOP Costa Rica
> (cliente `multicomputos_cr`). Define los contratos que **comparten** los portales de
> compras y reparte el trabajo en territorios disjuntos para que varios agentes
> editen en paralelo. **No contiene código de producción**: las firmas Python de
> abajo son el contrato que cada tarea tiene que respetar.
>
> **Hogares relacionados (no se copian acá):**
> - Contrato de la API de SICOP: `docs/arquitectura/sicop-api.md`. Llega con la rama
>   del spike (Fase 0) y es requisito del paso 0 de §10. Hasta entonces no se puede
>   enlazar porque el archivo no existe en `develop`.
> - Esquema: [`db/schema_v2.sql`](../../db/schema_v2.sql) (`portal`, `cliente_portal`,
>   `proceso`, `fx_tasa`, `taxonomia_modalidad`, `ejecucion_etapa`).
> - Plan del país: [`docs/planes/multicomputos-costa-rica.md`](../planes/multicomputos-costa-rica.md).
>
> Las referencias `ruta:línea` corresponden a `develop` en `da19daf`.

---

## 0. Decisiones en una tabla

| # | Tema | Decisión |
|---|---|---|
| D1 | Registro | `Portal` gana campos de despacho, como `extractor` y `localizador` en forma de rutas `"modulo:atributo"` que se importan tarde. `src.portales` sigue sin importar nada de `src.scraper` ni de Playwright |
| D2 | Extracción | Un contrato único, `async extraer(config, portal, desde) -> list[Proceso]`. COMPRASAL queda detrás de un adaptador fino sin cambiar su comportamiento |
| D3 | SICOP | Navegador anónimo propio (`src/scraper/navegador_anonimo.py`) que llama la API JSON con `fetch` desde la página. No se toca `browser.py` y **nunca se fija `user_agent`** |
| D4 | WAF | Si llega HTML con 200 es `BloqueoAcceso` (un `ContratoRoto`), no datos. **No se reintenta**: reintentar un bloqueo es evadirlo |
| D5 | Modelo | `Proceso` gana `portal_id`, `moneda`, `montos_por_moneda`, `monto_usd`, `id_externo`, `modalidad_codigo`, `detalle` y `datos_portal`, todos con valores por defecto. `monto` sigue siendo el monto **original**, en `moneda` |
| D6 | Llave | La llave compuesta `portal_id:numero_referencia` (`Proceso.proceso_gid`) se usa donde una estructura **cruza portales**. Donde la estructura es por cliente o por iteración de portal se conserva `numero_referencia`, con una invariante comprobada. **No hay migración de datos** |
| D7 | Fallos | Breaker, estado y manifiesto **por portal**. Un portal que falla nunca hace `return` del bucle. El breaker global queda solo para fallos críticos (LLM caído para todos, excepción fuera del bucle) |
| D8 | Config | `COMPRASAL_*` pasan a ser opcionales y se validan solo si un portal con `requiere_login` está activo. SICOP solo tiene perillas opcionales de cortesía |
| D9 | Moneda | Se rotula siempre la moneda original. La conversión a USD se hace solo en la ADB (`fx_tasa` mensual, `proceso.monto_usd`), nunca en la corrida diaria. Un monto sin moneda conocida no se rotula «USD» |
| D10 | Cuota LLM | **Sin sub-tope por portal.** Los portales se procesan en el orden del registro (COMPRASAL primero) y el gasto por portal se mide a partir del libro de consumo |
| D11 | Modalidades | La competibilidad por modalidad vive en Python (`src/portales/modalidades.py`). `taxonomia_modalidad` es un espejo para analytics que se genera desde ahí. Reemplaza la heurística `"directa" in forma` |
| D12 | Orden de trabajo | A2 entrega primero un PR chico y sin cambio de comportamiento (**A2a**: contratos y modelo). Después A2b ∥ A3 ∥ A4 ∥ A5 ∥ A6 ∥ A7 ∥ A8 corren en paralelo |

---

## 1. `Portal` ampliado

Hoy (`src/portales/__init__.py:28-38`) `Portal` solo describe el portal. Se le agrega
lo necesario para **despachar** (qué extractor y qué localizador usar), para **rotular**
(etiquetas y enlaces) y para la **moneda**.

```python
# src/portales/__init__.py  (A2a)
@dataclass(frozen=True)
class Portal:
    portal_id: str
    nombre: str
    pais: str                       # ISO 3166-1 alpha-2
    zona_horaria: str
    moneda: str                     # moneda por defecto (ISO 4217); = portal.moneda_default en la ADB
    base_url: str
    idioma: str = "es"
    integrado: bool = False
    pais_nombre: str = ""
    # ── nuevos ──────────────────────────────────────────────────────────────
    requiere_login: bool = False
    extractor: str = ""             # "paquete.modulo:funcion"  → ExtractorFn
    localizador: str = ""           # "paquete.modulo:Clase"    → LocalizadorPliego; "" = sin drill-down
    lookback_dias: int | None = None  # None = config.days_lookback (DAYS_LOOKBACK)
    prefijo_credenciales: str = ""  # "COMPRASAL" → COMPRASAL_EMAIL/_PASSWORD/_TOTP_SECRET
    monedas: tuple[str, ...] = ()   # monedas que el portal publica; () = solo `moneda`
    etiqueta_proceso: str = "proceso"           # «proceso» | «concurso»
    etiqueta_proceso_plural: str = "procesos"
    etiqueta_documento: str = "pliego"          # sustantivo del documento base
    palabras_documento: tuple[str, ...] = ()    # para puntuar adjuntos (locator)
    url_consulta: str = ""          # página PÚBLICA donde una persona busca el proceso
    url_detalle: str = ""           # plantilla con {id_externo} / {numero_referencia}; "" = no hay enlace profundo

    @property
    def moneda_unica(self) -> bool:            # True ⇒ un monto sin moneda se asume en `moneda`
        return len(self.monedas) <= 1
```

**Valores:**

| Campo | `comprasal_sv` | `sicop_cr` |
|---|---|---|
| `integrado` | `True` | `False` en A2a. **Pasa a `True` al reconciliar A3** (SOLICITUD de A3) |
| `requiere_login` | `True` | `False` |
| `extractor` | `"src.scraper.extractor:extraer_comprasal"` | `"src.scraper.sicop:extraer"` |
| `localizador` | `"src.drilldown.locator:LocalizadorComprasal"` | `"src.drilldown.localizador_sicop:LocalizadorSicop"` |
| `lookback_dias` | `None` | `None` (ver la nota) |
| `prefijo_credenciales` | `"COMPRASAL"` | `""` |
| `moneda` / `monedas` | `"USD"` / `()` | `"CRC"` / `("CRC", "USD")` |
| `etiqueta_proceso` (plural) | `"proceso"` (`"procesos"`) | `"concurso"` (`"concursos"`) |
| `etiqueta_documento` | `"pliego"` | `"pliego de condiciones"` (ver A-1 en §12) |
| `palabras_documento` | `()`: conserva la puntuación actual de `locator.py:86` | `("pliego de condiciones", "cartel", "especificaciones tecnicas", "condiciones")` |
| `url_consulta` | `"https://www.comprasal.gob.sv/"` | `"https://www.sicop.go.cr/app/module/bid/public/tenders"` |
| `url_detalle` | `"https://www.comprasal.gob.sv/pac-procesos/etapas/{id_externo}"` | `""`: SICOP no tiene enlace profundo, la SPA recibe el id por estado de navegación |

**Por qué `lookback_dias = None` en los dos.** Los dos portales corren a la misma hora
y en la misma zona (UTC−6). Cuando se sube `DAYS_LOOKBACK` para recuperar días caídos,
la recuperación tiene que aplicar a los dos. El campo existe para un portal futuro con
otro ritmo. El extractor de SICOP **limita** la ventana a 31 días por cortesía (§2.4).
`cliente_portal.lookback_dias` de la ADB **no** se lee en la corrida: la extracción es
por portal y no por cliente, y la ADB es opcional. A7 deja esa fila sembrada como espejo.

**Por qué los ids de proveedor NO van en `Portal`** (el encargo los pedía ahí). El
`id_proveedor` 20371 en COMPRASAL y las cédulas `3102813498` y `9000018410` en SICOP
identifican a **la organización MULTICOMPUTOS** en cada portal, no al portal. Si mañana
otra organización usa SICOP, sus ids serían otros. Su hogar es
`src/mercado/identidad.py` (A8), en un mapa por `portal_id` (§9.3). Cuando haya más de
una organización, ese mapa se muda a `cliente_config`.

**Sin enlace profundo.** `Proceso.url_directa` de SICOP **es** `portal.url_consulta`.
El correo y el portal muestran el `numero_referencia` con la instrucción «Búscalo por
número de procedimiento en SICOP» (A5). Nunca se inventa una URL de detalle.

### 1.1 Funciones nuevas del registro (A2a)

```python
# src/portales/__init__.py
ExtractorFn = Callable[["Config", Portal, datetime], Awaitable[list["Proceso"]]]

def portales_en_orden() -> list[Portal]: ...
    # Orden de inserción de PORTALES: comprasal_sv primero. Es el orden de ejecución (D10)
def cargar_extractor(portal: Portal) -> ExtractorFn: ...
    # importlib sobre portal.extractor; si no se puede importar levanta ErrorPortal
def cargar_localizador(portal: Portal) -> type["LocalizadorPliego"] | None: ...
    # None si portal.localizador == ""
```

`src.portales` **no importa** `src.scraper` ni Playwright al cargarse. La importación
ocurre dentro de `cargar_*`, y así se conserva la regla de `src/portales/__init__.py:9-10`
(sin ciclos).

---

## 2. Contratos de extracción y de localización de pliegos

### 2.1 Errores (A2a, `src/portales/contrato.py`)

```python
class ErrorPortal(RuntimeError):
    def __init__(self, portal_id: str, mensaje: str, *, detalle: str = ""): ...
    portal_id: str
    detalle: str                    # texto para ops; jamás secretos ni cuerpos completos (≤ 500 chars)

class CredencialesFaltantes(ErrorPortal): ...   # requiere_login y falta COMPRASAL_EMAIL/PASSWORD
class SesionInvalida(ErrorPortal): ...          # login agotado (3 reintentos TOTP)
class PortalNoDisponible(ErrorPortal): ...      # red, timeout, 5xx tras reintentos: transitorio
class ContratoRoto(ErrorPortal): ...            # respuesta que no cumple el contrato (422, esquema, tipo)
class BloqueoAcceso(ContratoRoto): ...          # WAF: HTML/texto con 200 donde se esperaba JSON
```

Cualquier otra excepción que salga de un extractor se trata como `ErrorPortal`
genérico de ese portal (§5). **Ninguna** sale del bucle por portal.

### 2.2 Extractor

```python
# Contrato (cada portal implementa UNA función async con esta firma)
async def extraer(config: Config, portal: Portal, desde: datetime) -> list[Proceso]:
    """Procesos publicados en el portal con fecha_publicacion >= desde.

    - `desde` es aware, en la zona del portal; lo calcula main (§5.2).
    - Cada Proceso sale con portal_id == portal.portal_id, numero_referencia no vacío,
      fecha_publicacion aware, url_directa no vacía.
    - Deduplica por numero_referencia dentro de la corrida (se queda con la fila más reciente).
    - Si el portal no responde lo que el contrato promete, levanta ErrorPortal (§2.1).
      Una lista vacía significa «el día no tuvo procesos», nunca «falló».
    """
```

- **COMPRASAL (A2b):** `src/scraper/extractor.py` gana un adaptador
  `extraer_comprasal(config, portal, desde)` que llama a
  `extract_recent_procesos(config, cutoff=desde)`. `extract_recent_procesos` recibe un
  `cutoff` opcional; sin él calcula lo de siempre (`extractor.py:194`), así que los
  llamadores y tests actuales siguen valiendo. El adaptador completa
  `portal_id="comprasal_sv"` e `id_externo=str(item["id"])` (en `_item_to_proceso`,
  `extractor.py:169`) y deja `moneda=""`: el portal es de moneda única y el lector
  resuelve USD (§7). No cambia nada más: ni el UA, ni las pausas, ni la paginación.
- **SICOP (A3):** `src/scraper/sicop.py::extraer`, descrito en §2.4.

### 2.3 Localizador de pliegos

Hoy `pipeline.descargar_pliegos` (`src/drilldown/pipeline.py:150-218`) abre un
Playwright con la sesión de COMPRASAL y llama a `obtener_fecha_limite` y a
`find_pliego_pdf_url` (`locator.py:137`, `:245`). Se abstrae así:

```python
# src/portales/contrato.py  (A2a)
@dataclass(frozen=True)
class DocumentoPliego:
    nombre: str                     # nombre visible (SICOP: clientFileNm)
    url: str                        # URL de descarga
    extension: str = ""             # "pdf" | "docx" | "xlsx" | …, en minúsculas y sin punto
    tamano_bytes: int | None = None
    categoria: str = ""             # SICOP: fileTypeNm («Documentos del Pliego de condiciones», …)
    puntaje: int = 0                # lo pone el localizador; mayor = mejor candidato

class LocalizadorPliego(Protocol):
    def __init__(self, config: Config, portal: Portal) -> None: ...
    async def __aenter__(self) -> "LocalizadorPliego": ...   # abre UN navegador
    async def __aexit__(self, *exc) -> None: ...             # lo cierra siempre
    async def completar(self, proceso: Proceso) -> None: ...
        # Rellena lo que el listado no trajo (fecha_limite, monto/moneda, tipo_actividad,
        # datos_portal). Muta `proceso`. NUNCA lanza: si falla, deja todo como estaba.
    async def documentos(self, proceso: Proceso) -> list[DocumentoPliego]: ...
        # Ordenados del mejor candidato al peor. Lista vacía si no hay adjuntos.
    async def texto_base(self, proceso: Proceso) -> str: ...
        # Texto del documento base que el portal publica sin adjunto (SICOP: generalCondsCont,
        # specCondsCont y adminCondsCont pasados de HTML a texto). COMPRASAL: "".
    async def descargar(self, doc: DocumentoPliego, destino_dir: str, max_bytes: int) -> str | None: ...
        # Ruta del archivo guardado o None. Respeta max_bytes antes de bajar el cuerpo.

class TransporteJSON(Protocol):
    """Lo que el navegador anónimo ofrece. Se inyecta en A4 y A8 para probar sin red."""
    async def json(self, metodo: str, url: str, cuerpo: Any | None = None) -> Any: ...
    async def binario(self, url: str, max_bytes: int) -> bytes | None: ...
```

`descargar_pliegos(procesos, cfg) -> dict[str, str]` **conserva su firma**, porque
`main.py` la llama en `:390` y `:532`. Por dentro (A4):

1. Exige que todos los `procesos` sean de **un mismo portal**. Si no, `ValueError`
   (invariante de §4).
2. Toma `cargar_localizador(portal)`. Si es `None`, devuelve `{}`.
3. Omite los procesos cuya modalidad es `NO_COMPETIBLE` (§9) y lo deja en el log.
4. `async with Loc(cfg, portal) as loc:` y, por cada proceso: `completar`, luego
   `documentos`, luego `descargar` del primer documento aceptable (PDF primero; DOCX o
   XLSX como segunda opción). Si no hay archivo pero `texto_base` no está vacío, escribe
   un `.txt` UTF-8 en `cfg.drilldown_tmp_dir` y lo devuelve como ruta. El análisis
   (A4) se bifurca por extensión.
5. Devuelve `{numero_referencia: ruta}`.

`LocalizadorComprasal` (A4, en `locator.py`) envuelve **sin cambiar comportamiento** lo
que hoy hace `descargar_pliegos`: la sesión `storage_state`, `obtener_fecha_limite`,
`find_pliego_pdf_url`, `requests` primero y Playwright como respaldo.

### 2.4 SICOP: navegador anónimo, WAF y cortesía

**Navegador anónimo (A3, `src/scraper/navegador_anonimo.py`):**

```python
class NavegadorAnonimo:             # implementa TransporteJSON
    def __init__(self, config: Config, origen: str, *, headless: bool | None = None,
                 pausa_s: float | None = None) -> None: ...
    async def __aenter__(self) -> "NavegadorAnonimo": ...
        # chromium.launch(headless=…, executable_path=config.chromium_path or None,
        #                 args=build_chromium_args(config))  ← SOLO SE IMPORTA de browser.py
        # browser.new_context()  ← sin storage_state, sin user_agent, sin extra_http_headers
        # page.goto(origen, wait_until="domcontentloaded")
    async def __aexit__(self, *exc) -> None: ...
    async def json(self, metodo: str, url: str, cuerpo: Any | None = None) -> Any: ...
        # page.evaluate(fetch(url, {method, headers: {"Content-Type": "application/json"},
        #   body, credentials: "omit"})) → {status, contentType, texto}
        # Aplica la pausa ANTES de cada llamada (≥ pausa_s desde la anterior).
    async def binario(self, url: str, max_bytes: int) -> bytes | None: ...
```

- **No se edita `src/scraper/browser.py`.** `build_chromium_args` solo se **importa**
  (`browser.py:36`), igual que ya hace `extractor.py:40`. No se usa `storage_state`, ni
  login, ni TOTP.
- **Prohibido fijar el User-Agent** (ni `user_agent=` ni `--user-agent=`). A diferencia
  de COMPRASAL (`extractor.py:203-209`), SICOP filtra por UA, y falsificarlo sería
  evadir el filtro del operador. Hay un test que lo verifica (§11).
- **Headless:** `SICOP_HEADLESS` vale por defecto lo mismo que `HEADLESS`. Si en la VM el
  Chromium *headless* queda bloqueado (P8 del spike), la salida es correr *headful* bajo
  `xvfb` (SOLICITUD a `Dockerfile`/`oci/`) o pedir autorización a Hacienda. **Nunca
  cambiar el UA.**

**Clasificación de respuestas (A3, `sicop.py`), en este orden:**

| Respuesta | Resultado |
|---|---|
| `content-type` sin `application/json`, cuerpo que empieza con `<`, o que contiene «No fue posible acceder» | `BloqueoAcceso`. **No se reintenta.** Se corta la extracción del portal |
| `422` con `{status, message, module}` | `ContratoRoto` (la petición quedó mal o la API cambió). No se reintenta |
| `5xx`, timeout o error de red | 2 reintentos con espera de 5 s y 15 s. Si persiste, `PortalNoDisponible` |
| `200` JSON al que le faltan los campos mínimos (`content`, `totalElements` en listados; `instCartelNo`, `cartelNo`, `regDt` por fila) | `ContratoRoto` con el campo faltante en `detalle` |

**Cortesía de tráfico (A3):**

- **Ventana por fecha:** `regDtFrom = desde.date()` y `regDtTo = hoy` en la zona del
  portal. `openbidDtFrom/To = [hoy−180 d, hoy+183 d]` (363 días, la petición
  grabada por el spike: es obligatorio y +185 roza el máximo de 1 año por rango). Las partidas
  se paginan hasta 5 × 100. `cartelTestYn=Y`. Después se filtra
  `regDt >= desde` con precisión de segundos. La ventana se limita a 31 días: si
  `desde` es más viejo, se registra un WARNING y se recorta.
- **Paginación:** `pageSize = SICOP_PAGE_SIZE` (por defecto 50; valores válidos 10, 20
  o 50, y cualquier otro se corrige a 50). Orden `regDt desc`. Se detiene con `last`,
  con una página vacía o con `SICOP_MAX_PAGINAS` (por defecto 20).
- **Partidas** (`epCartelProd/findByEpCartelId`, una llamada por concurso con
  `pageSize=100`): dan `monto`, `moneda`, el UNSPSC y los nombres de línea para
  clasificar. Se piden para todos los concursos del día **salvo** los de modalidad
  `RE` (remate) y los `cartelSecretYn=Y`, con un tope de `SICOP_MAX_DETALLES` (por
  defecto 300). Al pasar el tope, los restantes salen sin monto y queda un WARNING.
- **El detalle pesado** (`findPublishedById`, unos 208 campos con HTML) **no** se pide
  en la extracción. Lo pide `LocalizadorSicop.completar/texto_base` solo para los
  relevantes (A4).
- **Pausa:** `SICOP_PAUSA_S` (por defecto 1.0, mínimo forzado 0.5) entre **todas** las
  llamadas. Un día típico son unos 4 listados más unos 170 de partidas, unos 3 a 4
  minutos.
- **Caché de partidas y detalle:** `data/cache/sicop/<cartelNo>-<cartelSeq>.<tipo>.json`,
  con escritura atómica y TTL `SICOP_CACHE_DIAS` (por defecto 3). Una segunda corrida
  del mismo día, o la etapa `analyze`, no vuelve a pedir. La caché se purga por TTL al
  iniciar la extracción.
- **Nunca persistir datos personales:** `executorNm`, `executorId`, `writerNm`,
  `*StaffNm`, IDs `G…`, `colonesAccount` y `certCont` no entran en `Proceso` ni en
  `datos_portal`.

**Mapeo a `Proceso`** (sigue la §4 de `sicop-api.md`):

| Campo | Fuente |
|---|---|
| `numero_referencia` | `instCartelNo` |
| `id_externo` | `cartelNo` |
| `datos_portal["cartel_seq"]` | `cartelSeq` |
| `institucion` | `cartelInstNm` |
| `descripcion` | `cartelNm` |
| `fecha_publicacion` | `regDt` en `America/Costa_Rica`; acepta `dd-MM-yyyy HH:mm:ss` e ISO |
| `fecha_limite` | `biddocEndDt` (ya viene en el listado, a diferencia de COMPRASAL) |
| `datos_portal["fecha_apertura"]` | `openbidDt` (ISO) |
| `modalidad_codigo` | `proceType` |
| `forma_contratacion` | nombre del catálogo BD01 para `proceType` (tabla fija en `modalidades.py`) |
| `tipo_actividad` | `""` en la extracción; lo completa el localizador con `ppsTypeNm` |
| `monto` / `moneda` / `montos_por_moneda` | Σ `budgetAmt` por `currencyType` (§7) |
| `detalle` | hasta 600 caracteres: `prodNm` únicos de las partidas, unidos con « · » |
| `datos_portal["unspsc"]` | los primeros 8 dígitos de `prodId`, únicos, hasta 20 |
| `datos_portal["estado"]` | `cartelStatStr` |
| `url_directa` | `portal.url_consulta` |

---

## 3. `Proceso` y `ProcesoClasificado`

Hoy: `src/scraper/models.py:7-17` y `from_proceso` en `:354-366`. Todo lo nuevo va **al
final** de `Proceso`, con valores por defecto. Así los constructores posicionales y los
`ProcesoClasificado(...)` de los tests no cambian.

```python
@dataclass
class Proceso:
    institucion: str
    descripcion: str
    fecha_publicacion: datetime
    url_directa: str
    numero_referencia: str = ""
    forma_contratacion: str = ""
    tipo_actividad: str = ""
    monto: Optional[float] = None          # monto ORIGINAL, en `moneda`
    fecha_limite: Optional[datetime] = None
    # ── nuevos (A2a) ────────────────────────────────────────────────────────
    portal_id: str = PORTAL_ID_DEFAULT     # "comprasal_sv"
    moneda: str = ""                       # ISO 4217; "" = desconocida (ver moneda_de, §7)
    montos_por_moneda: dict[str, float] = field(default_factory=dict)  # solo si hay mezcla
    monto_usd: Optional[float] = None      # lo llena SOLO la ADB/analytics; la corrida no convierte
    id_externo: str = ""                   # COMPRASAL: id numérico; SICOP: cartelNo
    modalidad_codigo: str = ""             # código del portal (SICOP: "LD", "PX"…); COMPRASAL: ""
    detalle: str = ""                      # texto extra para clasificar (partidas); ≤ 600 chars
    datos_portal: dict[str, Any] = field(default_factory=dict)  # específico del portal, sin PII

    @property
    def proceso_gid(self) -> str:          # "portal_id:numero_referencia" (= memoria.repo._proceso_gid)
        return f"{self.portal_id}:{self.numero_referencia}"
```

- `PORTAL_ID_DEFAULT` se importa de `src.portales`, que no depende de `models.py`, así
  que no hay ciclo.
- `proceso_gid` es una **propiedad** y no un campo: `asdict` no la serializa y los
  `getattr(p, "proceso_gid", None) or …` de `main.py:218` y `rescate.py:91` la toman sin
  cambios. A2b **simplifica** esos dos sitios a `p.proceso_gid`.
- `from_proceso` copia de forma genérica, para que un campo nuevo no se pierda nunca
  más:

```python
@classmethod
def from_proceso(cls, proceso: Proceso) -> "ProcesoClasificado":
    valores = {f.name: getattr(proceso, f.name) for f in dataclasses.fields(Proceso)}
    valores["montos_por_moneda"] = dict(proceso.montos_por_moneda)
    valores["datos_portal"] = dict(proceso.datos_portal)
    return cls(**valores)
```

- `MOTIVOS_BLOQUEO` (`models.py:169-176`) gana
  `MOTIVO_MODALIDAD_NO_COMPETIBLE = "modalidad_no_competible"`, con el texto
  `"Modalidad sin competencia abierta (excepción, remate o equivalente)"` (A2a).
- **`history._serialize/_deserialize`** (`history.py:45-109`, A2b): escriben los ocho
  campos nuevos (menos `proceso_gid`). Al leer, una clave ausente toma el valor por
  defecto. Una línea vieja de JSONL o del manifiesto se lee como
  `portal_id="comprasal_sv"` y `moneda=""`, que resuelve a USD (§7).
- **Prompt del clasificador** (`engine.py:96-106`, A2b): `_procesos_to_dicts` agrega
  `"detalle"` y `"monto"` (texto de `formatear_monto`) **solo si no están vacíos**. Para
  COMPRASAL el JSON del prompt queda **byte a byte igual** (hay un test que lo
  verifica), así que no cambian ni la caché de prompts ni la calibración.

---

## 4. Llave compuesta `portal_id:numero_referencia`

**Hecho que simplifica todo:** un cliente lee de **un** portal
(`ClientProfile.portal_id`, `client_profile.py:114`). Todo lo que se guarda **por
cliente** queda aislado por portal sin cambiar su llave. La colisión solo es posible
en estructuras que mezclan portales.

| Estructura | Hoy | Decisión | Migración |
|---|---|---|---|
| `global_relevant` (`main.py:366,381`; `:502,520`) | dict por `numero_referencia` | Se conserva, bajo la **invariante I1**: vive dentro de una iteración del bucle por portal y nunca recibe procesos de otro. A2b agrega `assert all(p.portal_id == portal_id …)` al entrar a `_run_clients_with_drilldown` y a la clasificación por portal | Ninguna |
| `pdf_paths` y `resultados` (`main.py:386`, `pipeline.py:591-613`) | dict por `numero_referencia` | Lo mismo, con I1. `descargar_pliegos` levanta un error si recibe procesos de varios portales (§2.3) | Ninguna |
| Manifiesto de etapas (`main.py:467-468`) | `data/stage_handoff.json`, uno solo | **Un manifiesto por portal:** `data/stage_handoff_<portal_id>.json`, con la clave `"portal_id"` adentro. `analyze` lee el archivo viejo sin sufijo como `comprasal_sv` durante una versión | Lectura de compatibilidad; se borra después de consumir |
| History JSONL (`history.py:120-123`, `:216-219`) | un archivo por cliente; `proceso_uuid` = `numero_referencia` | Se conserva la llave (cliente = un portal). Cada línea gana `"portal_id"` para ser autodescriptiva | Ninguna: las líneas viejas se leen como `comprasal_sv` |
| `ResultadoAnalisis.proceso_uuid` | `numero_referencia` | **Se conserva sin prefijo.** `memoria.service` ya arma el gid v2 con `_proceso_gid(proceso_uuid, _portal(cfg, client_id))` (`service.py:229-236`); con prefijo quedaría duplicado | Ninguna |
| v2 (`proceso`, `cliente_proceso`, `analisis`, `chunk`, feedback, mercado v2) | `proceso_gid` = `portal_id:numero_referencia` (`repo.py:602`, `uq_proceso_natural`) | Ya resuelto; A7 solo completa columnas | Ninguna |
| Tablas v1 (`licitaciones_metadata`, `licitaciones_analisis`, `licitaciones_chunks`; PK `proceso_id` en `db/schema.sql:25,122`) | `numero_referencia` sin prefijo | **Se conserva sin prefijo para todos los portales** (ver la justificación). Activar `multicomputos_cr` exige `ENABLE_ESQUEMA_V2=true`, que ya está encendido en DEV y en PROD | Ninguna |
| `ejecucion` (v1) | una fila por corrida | Sigue siendo el resumen de la corrida (§5). El detalle por portal va a `data/portal_estado.json` | Ninguna |

**Justificación de no prefijar v1.**
1. Los formatos son **disjuntos por construcción**. COMPRASAL usa `\d+-\d{4}-[A-Z]\d+`
   (`96-2026-P0011`) y SICOP usa `\d{4}[A-Z]{2}-\d{6}-\d{10}` (`2026LD-000364-0015499999`).
   A3 agrega un test que lo verifica sobre las fixtures.
2. Prefijar v1 obliga a pasar `portal_id` por `memoria/service.py`,
   `vectorizer.vectorizar_proceso`, `rag` v1, `set_estado` y `save_analisis_db`: una
   superficie grande, en un esquema que va camino al «Contract», sin territorio
   asignado y con riesgo real para COMPRASAL.
3. Las lecturas que importan (estado, precedentes, tendencias) ya leen v2 y **no**
   degradan a v1 (`service.py:153-185`, `:402-470`).

Si un tercer portal llegara a compartir formato con otro, la regla se revisa
**antes** de integrarlo. Esto queda escrito como condición de `integrado=True`.

**Invariante I1 (A2b la hace cumplir y la prueba):** *toda estructura en memoria con
llave `numero_referencia` vive dentro de una iteración de un solo portal; lo que cruza
portales usa `proceso_gid`.*

---

## 5. Aislamiento de fallos por portal

### 5.1 Estado por portal (A2b, `src/history.py`)

```python
def registrar_resultado_portal(config, portal_id: str, ok: bool, *,
                               procesos: int = 0, error: str | None = None) -> int: ...
    # Escribe data/portal_estado.json de forma atómica (persistencia.cache.escribir_atomico).
    # {portal_id: {"fallos_consecutivos", "ultimo_ok", "ultimo_intento", "ultimo_error", "procesos"}}
    # ok=True pone fallos_consecutivos en 0. Devuelve el contador ya actualizado. Nunca lanza.
def fallos_portal(config, portal_id: str) -> int: ...
def estado_portales(config) -> dict[str, dict]: ...
```

`data/` sobrevive entre corridas efímeras porque es un volumen montado; `last_run.json`
usa el mismo mecanismo. El espejo en la ADB (`ejecucion_etapa`, con
`etapa='SCRAPE'` y `estado` igual a `OK`, `ERROR` o `PARCIAL`) queda **diferido**: esa
tabla exige un `ejecucion_id`, y `insertar_ejecucion` no lo devuelve. No bloquea.

### 5.2 Bucle por portal (A2b, `src/main.py`, reemplaza `:679-723`)

```
for portal in portales_en_orden() ∩ integrados:                 # D10: COMPRASAL primero
    if portal.portal_id in config.portales_deshabilitados: log; continue
    try:
        if portal.requiere_login:
            validar_credenciales(config, portal)                 # → CredencialesFaltantes
            sesión válida, o _try_restore_session, o SesionInvalida
            # requiere_login con portal ≠ comprasal_sv ⇒ ErrorPortal("login no implementado")
        desde = now(ZoneInfo(portal.zona_horaria)) - timedelta(days=portal.lookback_dias or config.days_lookback)
        procesos = asyncio.run(cargar_extractor(portal)(config, portal, desde))
        verificar_procesos(procesos, portal)                     # portal_id, referencia, fecha; si no, ContratoRoto
    except Exception as exc:
        fallos = registrar_resultado_portal(config, portal_id, ok=False, error=…)
        _alertar_fallo_portal(config, portal, clientes, exc, fallos)   # §5.3
        portales_fallidos[portal_id] = exc
        continue                                                 # ← NUNCA return
    registrar_resultado_portal(config, portal_id, ok=True, procesos=len(procesos))
    clasificar, drill-down y notificar exactamente como hoy (aislado por cliente)
```

- Se elimina el `return` de `main.py:702`. Una sesión de COMPRASAL expirada ya no
  impide correr SICOP, y un SICOP caído no impide correr COMPRASAL.
- **Resultado global:** `success = (al menos un portal extrajo) and no hubo error
  crítico`. Si **todos** los portales intentados fallan, `success=False`, que es
  exactamente el comportamiento de hoy cuando COMPRASAL es el único portal. Con un
  éxito parcial, `success=True` y `error = "PARCIAL: sicop_cr: <clase>: <mensaje>"` en
  `last_run` y en `ejecucion.error_msg`.
- `last_run.json` gana la clave `"portales": {portal_id: {"ok", "procesos", "error"}}`.
  Es aditiva: sus lectores ignoran las claves que no conocen.
- **Breaker global** (`_consecutive_errors`, `main.py:655`, `:745-767`): solo lo mueven
  los errores críticos (excepciones fuera del bucle, `LLMCaidoError` en `:722`, o que
  todos los portales hayan fallado). Un portal que falla mientras otro funciona **no**
  lo incrementa.
- **Breaker por portal:** al llegar a `fallos_portal >= CIRCUIT_BREAKER_THRESHOLD` (3),
  se llama a `send_circuit_breaker_alert(config, client, fallos, f"[{portal.nombre}] {error}")`
  para cada cliente de **ese** portal. Va a los admins de plataforma
  (`email_sender.py:820-860`), no al cliente. La firma no cambia (§10, interfaz E3).
- Se elimina `_un_solo_portal` (`main.py:78-86`).
- `_agrupar_integrados` devuelve los grupos en el orden de `portales_en_orden()`.

### 5.3 Alertas (A2b)

| Error | Canal | Cuándo |
|---|---|---|
| `SesionInvalida`, `CredencialesFaltantes` (solo `requiere_login`) | `send_session_expired_alert(config, client)` (`email_sender.py:777`), sin cambios: hoy solo COMPRASAL tiene login y el texto es correcto | En cada fallo, igual que hoy |
| `ContratoRoto` / `BloqueoAcceso` | `send_ops_alert(config, f"{portal.nombre}: contrato roto", detalle)` (`ops_alert.py:42`). **Nunca un correo al cliente** | Cuando `fallos == 1` y cuando `fallos == umbral`. En el medio solo log, para no saturar |
| `PortalNoDisponible` / otro error | `send_ops_alert` | Solo cuando `fallos == umbral` |
| Cualquiera de los anteriores, con `fallos >= umbral` | `send_circuit_breaker_alert` (§5.2) | Cuando `fallos == umbral` |
| Recuperación (`ok` después de `fallos > 0`) | Log INFO «portal recuperado tras N fallos» | — |

### 5.4 Etapas `--stage scrape|analyze` (A2b)

- **`scrape`:** recorre los portales igual que §5.2. Por cada portal que extrae bien,
  clasifica, descarga los pliegos y escribe `data/stage_handoff_<portal_id>.json`
  (escritura atómica, como en `main.py:537-541`). Un portal que falla no deja
  manifiesto y queda registrado en `portal_estado.json`.
- **`analyze`:** procesa `stage_handoff_*.json` en el orden del registro (más el
  `stage_handoff.json` heredado, como `comprasal_sv`). Cada manifiesto va en su propio
  `try/finally`, que limpia sus pliegos y lo borra. El fallo de uno no impide los
  demás. `_save_last_run` resume todos.
- El encadenamiento por systemd (`OnSuccess`) no cambia: `scrape` termina con 0 aunque
  falle un portal, porque eso no es un error del proceso.

---

## 6. Configuración

**A2b, `src/config.py`:**

| Variable | Hoy | Después |
|---|---|---|
| `COMPRASAL_EMAIL`, `COMPRASAL_PASSWORD` | `os.environ[...]` (`config.py:37-38`): sin ellas `Config()` lanza `KeyError` | `os.getenv(..., "")`. Se validan solo en `validar_credenciales(config, portal)` si hay un portal activo con `requiere_login`; si faltan, ese portal falla con `CredencialesFaltantes` y los demás siguen |
| `COMPRASAL_TOTP_SECRET` | opcional | sin cambio |
| `PORTALES_DESHABILITADOS` | — | nueva. Lista separada por comas (p. ej. `sicop_cr`) que salta un portal sin tocar YAML. Es el interruptor de ops |
| `SICOP_PAUSA_S` | — | `1.0` (mínimo forzado `0.5`) |
| `SICOP_PAGE_SIZE` | — | `50` (∈ {10, 20, 50}) |
| `SICOP_MAX_PAGINAS` | — | `20` |
| `SICOP_MAX_DETALLES` | — | `300` |
| `SICOP_CACHE_DIAS` | — | `3` |
| `SICOP_HEADLESS` | — | por defecto el valor de `HEADLESS` |

Atributos de `Config`: `portales_deshabilitados: frozenset[str]`, `sicop_pausa_s`,
`sicop_page_size`, `sicop_max_paginas`, `sicop_max_detalles`, `sicop_cache_dias`,
`sicop_headless`. **A3 y A4 los leen con `getattr(config, "sicop_…", <default>)`**,
así que funcionan aunque A2b todavía no haya mergeado.

- `.env.example` (A2b): documentar las variables `SICOP_*` y
  `PORTALES_DESHABILITADOS` como opcionales. **No hay credenciales de SICOP.**
- El comentario de `main.py:781-786` sobre argparse sigue siendo cierto, pero hay que
  quitar `COMPRASAL_*` de la lista de lo que «exige» `Config`.

---

## 7. Moneda

### 7.1 Modelo

| Dónde | Campo | Regla |
|---|---|---|
| `Proceso` | `monto`, `moneda` | Monto **original**, en la moneda del portal o de las partidas. `moneda=""` significa desconocida |
| `Proceso` | `montos_por_moneda` | Solo cuando las partidas mezclan monedas: `{"CRC": 1.2e7, "USD": 3.5e4}`, con `monto=None` y `moneda=""`. Nunca se suman monedas distintas |
| ADB `proceso` (`schema_v2.sql:208-211`) | `moneda`, `monto_original`, `monto_usd`, `fx_fecha` | A7: `monto_original = monto`, `moneda = moneda_de(p)`; `monto_usd = monto × fx_tasa(moneda, mes de fecha_pub)` si la tasa existe, y si no `NULL`; `fx_fecha` = primer día del mes. Con USD, la tasa vale 1. Con mezcla: `moneda` y montos en `NULL` (ver la nota) |
| ADB `fx_tasa` (`schema_v2.sql:81-87`) | `(moneda, mes) → tasa_a_usd` | Mensual. Fuente: el BCCR (tipo de cambio de referencia, venta, promedio del mes), cargado **a mano** por un operador mientras no haya integración (A-2 en §12). No se usa `coExchangerate` de SICOP: devuelve 1:1 |
| ADB `organizacion.moneda_reporte` (`schema_v2.sql:105`) | `USD` por defecto | Moneda en la que el portal web **agrega** KPIs (sumas y promedios) usando `monto_usd` |

> **Montos mezclados en v2:** son raros (el spike no vio ninguno) y no tienen columna.
> Se guardan con `moneda`, `monto_original` y `monto_usd` en `NULL`, y el detalle por
> moneda viaja solo en el JSONL o el manifiesto. Si aparecen con frecuencia, se decide
> agregar DDL (SOLICITUD a A7 en ese momento).

### 7.2 Funciones (A2a, `src/portales/moneda.py`; dependencia solo de `src.portales`)

```python
def moneda_de(proceso) -> str: ...
    # proceso.moneda; si es "" y el portal es moneda_unica → portal.moneda; si no → "".
    # Una línea vieja (sin portal_id ni moneda) da "USD", que es lo correcto para COMPRASAL.
def formatear_monto(monto: float | None, moneda: str, *, miles: str = ",") -> str: ...
    # "" si monto es None, 0 o negativo (nunca «USD 0»: misma regla que web/portal.py:365-371).
    # f"{moneda} {monto:,.0f}" con `miles` como separador; si moneda == "" → f"{monto:,.0f} (moneda sin confirmar)".
def texto_monto(proceso, *, miles: str = ",") -> str: ...
    # Lo que se muestra: formatear_monto(monto, moneda_de(p)) o, si hay mezcla,
    # " + ".join(formatear_monto(v, k) for k, v in sorted(montos_por_moneda.items())).
```

El formato con prefijo ISO (`"USD 45,000"`, `"CRC 12,500,000"`) se eligió porque
**COMPRASAL queda idéntico** al texto actual (`email_sender.py:335`) y porque no da
lugar a ambigüedad. `₡` y `US$` serían más naturales para Costa Rica, pero `$` solo es
ambiguo.

### 7.3 Rotulado (A5 y A6)

- **Correo:** siempre `texto_monto(p)` en la moneda original. **No se convierte**,
  porque la corrida diaria no depende de la ADB. Reemplaza `email_sender.py:335`.
- **Portal web:** las tarjetas usan `texto_monto` (moneda original). Los KPIs que suman
  montos usan `proceso.monto_usd` en `organizacion.moneda_reporte` y aclaran «N procesos
  sin tasa de cambio, excluidos». Reemplaza `_monto_txt` (`web/portal.py:365-371`), que
  pasa a recibir la moneda.
- **Montos de oportunidad** (`oportunidad_estado.monto_ofertado/adjudicado`, sin columna
  de moneda): se interpretan en `moneda_de(proceso)` o, si es `""`, en `portal.moneda`.
  La etiqueta del formulario dice esa moneda (`templates/portal_pais_macros.html:26`,
  `portal_pais_dialogo.html:20`). **Sin DDL.**
- **Prompts:** el análisis del pliego (`analysis.py:51`, `:691`, A4) gana la línea
  «Moneda(s) del portal: CRC y USD; indica la moneda del monto» y el campo de salida
  `monto_estimado_moneda` (ISO o null). En `pipeline.py:517-518` (A4), el monto del
  pliego **solo** llena `proceso.monto` si el portal no lo trajo
  (`proceso.monto is None and not proceso.montos_por_moneda`). En ese caso
  `proceso.moneda` toma `monto_estimado_moneda`, o `portal.moneda` si el portal es de
  moneda única, o `""`. Para COMPRASAL es el mismo comportamiento de hoy: `monto` del
  listado siempre es `None` y la moneda resuelve a USD.

---

## 8. Cuota LLM: sin sub-tope por portal

**Decisión: no se agrega sub-tope por portal.** Se mantiene el tope diario global
(`quota._tope`, `quota.py:431`; `cuota_gasto` con PK `(dia, modelo)`,
`schema_v2.sql:933`).

Razones:
1. **Prioridad garantizada sin código nuevo:** los portales corren en el orden del
   registro (COMPRASAL primero, §5.2). COMPRASAL consume primero en cada corrida;
   SICOP solo puede quedarse sin presupuesto él mismo, y no puede quitárselo a
   COMPRASAL. Es el riesgo correcto para un portal nuevo, que además nace con
   `override_to`.
2. **El gasto esperado es marginal.** SICOP publica unos 170 concursos por día; los de
   TI relevantes que llegan al drill-down se estiman en menos de 10. Los pliegos son
   chicos (p50 de 24 páginas y 0,69 MB, §7 del spike), y en muchos casos basta el texto
   del detalle, sin PDF. Eso es del orden de centavos frente al tope de `$5`
   (`quota.py:160`).
3. Un sub-tope exige DDL (`cuota_gasto` no tiene `portal_id`) o un segundo libro, y
   duplica la lógica atómica de `CUOTA_FUENTE=adb`, que justo ahora está en el periodo
   de corridas limpias.
4. **Se mide igual:** el libro de consumo ya guarda `proceso_gid` en los metadatos de
   cada llamada (`llm/registro.py:95`), así que el gasto por portal se obtiene
   agrupando por prefijo (`scripts/comparar_llm.py`).

**Disparador para revisar:** si el gasto de `sicop_cr` supera el 30 % del tope durante
7 días seguidos, o si COMPRASAL se queda alguna vez sin cuota en un día con SICOP
activo, se diseña el sub-tope.

---

## 9. Modalidades y competibilidad

### 9.1 Fuente única (A2a, `src/portales/modalidades.py`)

```python
COMPETIBLE = "competible"; CONDICIONAL = "condicional"; NO_COMPETIBLE = "no_competible"

@dataclass(frozen=True)
class Modalidad:
    portal_id: str
    codigo: str          # SICOP: proceType; COMPRASAL: "" (COMPRASAL publica texto, no código)
    nombre: str          # texto tal como lo publica el portal (= proceso.modalidad_raw en v2)
    norm: str            # vocabulario CHECK de taxonomia_modalidad (schema_v2.sql:62-73)
    competibilidad: str

MODALIDADES: tuple[Modalidad, ...]          # tabla de SICOP (abajo)
def modalidad_de(proceso) -> Modalidad | None: ...
def competibilidad_de(portal_id: str, codigo: str = "", forma_contratacion: str = "") -> str: ...
```

| Código SICOP | `norm` | Competibilidad |
|---|---|---|
| `LY`, `LE`, `LD`, `LN`, `LI`, `LA` | `LICITACION_ABIERTA` | `COMPETIBLE` |
| `SE` | `SUBASTA_INVERSA` | `COMPETIBLE` |
| `PE` | `CONVENIO_MARCO` | `CONDICIONAL` |
| `CD`, `CE` | `CONTRATACION_DIRECTA` | `CONDICIONAL` |
| `PX` | `CONTRATACION_DIRECTA` | `NO_COMPETIBLE` |
| `PP`, `RE` | `OTRO` | `NO_COMPETIBLE` |
| código desconocido | `OTRO` | `CONDICIONAL` (que decida el pliego) |

**COMPRASAL** (sin código): `competibilidad_de("comprasal_sv", "", forma)` devuelve
`CONDICIONAL` si `"directa"` aparece en la forma normalizada, `COMPETIBLE` si la forma
no está vacía, y `CONDICIONAL` si está vacía. Es **exactamente** la regla actual
(`analysis.py:320-327` y `:389-390`), ahora con nombre.

`taxonomia_modalidad` (ADB) es un **espejo** para analytics: A7 genera sus filas desde
`MODALIDADES` (`modalidad_raw = nombre`) y no las copia a mano. Así se respeta la regla
de un solo hogar. La competibilidad **no** se guarda en la ADB: la corrida no puede
depender de ella, y el `CHECK` no tiene columna para eso. `upsert_proceso_v2` llena
`proceso.modalidad_norm` con `modalidad_de(p).norm` (A7).

### 9.2 Qué reemplaza (A4 y A5)

| Sitio | Hoy | Después |
|---|---|---|
| `analysis.py:320-322` (regla 1) | `if forma and "directa" not in forma: return True` | `c = competibilidad_de(portal_id, modalidad_codigo, forma)`. Si es `COMPETIBLE`, `True`. Si es `NO_COMPETIBLE`, `(False, MOTIVO_MODALIDAD_NO_COMPETIBLE, nombre de la modalidad)`. Si es `CONDICIONAL`, siguen las reglas 2 a 6 como si fuera directa |
| `analysis.py:389-390` (ruta nueva) | `directa = "directa" in forma` | `directa = c != COMPETIBLE` |
| `analysis.evaluar_competibilidad` / `aplicar_competibilidad` | reciben `forma_contratacion` | Ganan `*, portal_id: str = PORTAL_ID_DEFAULT, modalidad_codigo: str = ""` como argumentos de palabra clave con valor por defecto. Las llamadas viejas siguen funcionando igual |
| `pipeline.py:524-529` | pasa `forma_contratacion` | Pasa además `portal_id=proceso.portal_id` y `modalidad_codigo=proceso.modalidad_codigo` |
| `pipeline.descargar_pliegos` | descarga todo lo relevante | Omite `NO_COMPETIBLE` (§2.3, paso 3): no gasta navegador ni LLM en una excepción ya adjudicada |
| `notifier/agrupamiento.py` (A5) | grupo «No competibles» solo con `ra.competible is False` | Agrega: proceso relevante con `competibilidad_de(...) == NO_COMPETIBLE` va a «No competibles», con motivo `MOTIVOS_BLOQUEO[MOTIVO_MODALIDAD_NO_COMPETIBLE]`, aunque no tenga `ResultadoAnalisis` |

---

## 10. Territorios, orden e interfaces congeladas

### 10.1 Orden

```
Paso 0 (quien lanza): mergear la rama del spike A0 (sicop-api.md + tests/fixtures/sicop/)
                      y este documento. Sin las fixtures, A3, A4 y A8 no tienen contra qué probar.
Paso 1:  A2a  (contratos + modelo, SIN cambio de comportamiento) → PR → merge
Paso 2:  A2b ∥ A3 ∥ A4 ∥ A5 ∥ A6 ∥ A7 ∥ A8    (todos desde develop con A2a ya adentro)
Paso 3:  reconciliar de a una rama, en este orden: A2b → A3 → A7 → A4 → A5 → A6 → A8
         (`ruff check . && pytest -q` tras cada merge; después se aplican las SOLICITUDes, §13)
```

**Por qué A2a va aparte.** A3, A4, A5, A7 y A8 importan `src.portales.contrato`,
`src.portales.modalidades`, `src.portales.moneda` y los campos nuevos de `Proceso`. Si
esos archivos no existen, sus tests no corren. A2a es chico, aditivo y no toca
`main.py`; se revisa en minutos y libera a todos los demás.

**Por qué A4, A5 y A8 ya no esperan a A2b ni a A3.** Todo lo que necesitan está en A2a.
El navegador anónimo de A3 se inyecta como `TransporteJSON` y se importa tarde, dentro
de `__aenter__` o de la CLI. Los tests usan un transporte falso.

### 10.2 Territorios (cada tarea escribe **solo** esto; lo demás está `[CONGELADO]`)

| Tarea | Escribe | Notas |
|---|---|---|
| **A2a** `nucleo-portal` (1.ª entrega) | `src/portales/__init__.py`, `src/portales/contrato.py` (nuevo), `src/portales/modalidades.py` (nuevo), `src/portales/moneda.py` (nuevo), `src/scraper/models.py`, `tests/test_portales.py`, `tests/test_portales_contrato.py` (nuevo), `tests/test_models_portal.py` (nuevo); en `CLAUDE.md`, **solo** la línea `src/portales/` del árbol | `sicop_cr` se queda con `integrado=False` |
| **A2b** `nucleo-portal` (2.ª entrega) | `src/main.py`, `src/config.py`, `src/history.py`, `src/classifier/rescate.py`, `src/classifier/engine.py` (**solo** `_procesos_to_dicts`), `src/scraper/extractor.py` (**solo** `cutoff`, el adaptador `extraer_comprasal` y los campos nuevos en `_item_to_proceso`), `.env.example`, `tests/test_main_run_pipeline.py`, `tests/test_main_portales.py` (nuevo), `tests/test_history_portal.py` (nuevo), `tests/test_extractor.py`, `tests/test_rescate.py`, `tests/test_wire1_history.py`, `tests/test_freno_llm_caido.py` | No toca `browser.py`, `auth/` ni `pipeline.py` |
| **A3** `extractor-sicop` | `src/scraper/sicop.py` (nuevo), `src/scraper/navegador_anonimo.py` (nuevo), `tests/test_sicop_extractor.py` (nuevo), `tests/test_navegador_anonimo.py` (nuevo); fixtures **nuevas** en `tests/fixtures/sicop/` (no modifica las de A0); en `CLAUDE.md`, **solo** una línea nueva bajo `src/scraper/` | SOLICITUD para poner `integrado=True` (§13) |
| **A4** `pliegos-sicop` | `src/drilldown/locator.py`, `src/drilldown/pipeline.py`, `src/drilldown/analysis.py`, `src/drilldown/triage.py`, `src/drilldown/downloader.py`, `src/drilldown/localizador_sicop.py` (nuevo), `tests/test_locator.py`, `tests/test_pipeline.py`, `tests/test_pipeline_c1.py`, `tests/test_pipeline_c3.py`, `tests/test_drilldown_analysis.py`, `tests/test_lectura_pipeline.py`, `tests/test_localizador_sicop.py` (nuevo), `tests/test_competibilidad_modalidad.py` (nuevo); en `CLAUDE.md`, **solo** la línea `locator.py · downloader.py · …` bajo `src/drilldown/` | **Cambio respecto del plan:** el localizador vive en `src/drilldown/localizador_sicop.py` y no en `src/scraper/sicop_pliegos.py`. Es dominio del drill-down y su línea del árbol de `CLAUDE.md` queda lejos de la de A3, así que no hay conflicto de merge |
| **A5** `moneda-textos` | `src/notifier/email_sender.py`, `src/notifier/agrupamiento.py`, `src/notifier/por_marca.py` (solo si hace falta), `src/web/portal.py`, `src/analytics/operativo.py`, `src/analytics/pais.py`, `src/analytics/tablero.py`, `src/analytics/regional.py`, `tests/test_email_digest.py`, `tests/test_email_estilo.py`, `tests/test_email_feedback.py`, `tests/test_email_override.py`, `tests/test_email_peso.py`, `tests/test_email_por_marca.py`, `tests/test_agrupamiento.py`, `tests/test_web_portal.py`, `tests/test_web_portal_e11.py`, `tests/test_analytics_*.py`, `tests/test_moneda_rotulado.py` (nuevo) | Entrega en el contexto de plantillas las variables de E5 |
| **A6** `plantillas` | `templates/*.html`, `tests/test_plantillas_pais.py`, `tests/test_plantillas_portal_textos.py` (nuevo) | Cada variable nueva con `|default(...)` igual al literal de hoy (E5). Así la plantilla funciona antes y después de A5 |
| **A7** `persistencia-v2` | `src/memoria/repo.py`, `scripts/semilla_esquema_v2.py`, `db/schema_v2.sql` (solo semillas o `MERGE`, sin DDL), `src/persistencia/sesion.py` (no se espera cambio), `tests/test_repo_sql.py`, `tests/test_semilla_esquema_v2.py` (nuevo), `tests/integration/test_oracle_local.py` | `upsert_proceso_v2` escribe `moneda`, `monto_usd`, `fx_fecha`, `tz_portal`, `idioma` y `modalidad_norm`; agrega `recalcular_monto_usd(conn) -> int`; siembra `portal` (`sicop_cr`), `cliente_portal` y `taxonomia_modalidad` desde `src.portales` |
| **A8** `mercado-sicop` | `src/mercado/sicop_publica.py` (nuevo), `src/mercado/identidad.py`, `src/mercado/repo.py`, `src/mercado/service.py`, `src/mercado/__main__.py`, `db/schema.sql` (**solo** un bloque nuevo al final, §9.3), `tests/test_mercado_sicop.py` (nuevo), `tests/test_mercado_identidad.py`, `tests/test_mercado_repo.py`, `tests/test_mercado_service.py`; en `CLAUDE.md`, **solo** una línea nueva bajo `src/mercado/` | El timer en `oci/` va como SOLICITUD |

**Excepción controlada sobre `CLAUDE.md`.** `tests/test_documentacion.py` exige que todo
módulo nuevo de `src/` aparezca en el árbol. Sin esa línea, la rama queda en rojo. Por
eso quien lanza concede a cada tarea **su línea**, en bloques distantes entre sí
(`portales`, `scraper`, `drilldown` y `mercado`), y git las mezcla sin conflicto.
Cualquier otra edición de `CLAUDE.md` es SOLICITUD para A11.

**Siempre `[CONGELADO]` para todas:** `oci/`, `src/scraper/browser.py`, `CHANGELOG.md`,
`.gitignore`, `pyproject.toml`, `Dockerfile*`, `docker-compose*.yml`,
`src/memoria/service.py`, `src/auth/`, `clients/` (salvo A9) y `docs/` (salvo
este documento, que es de A1, y lo que se le asigne a A11).

### 10.3 Interfaces congeladas (nombres exactos)

Las tareas del paso 2 pueden asumir lo siguiente **tal cual**:

- **E1 (A2a): `src.portales`.** `Portal` (§1, con todos sus campos),
  `PORTALES["comprasal_sv" | "sicop_cr"]`, `PORTAL_ID_DEFAULT`, `obtener_portal`,
  `portales_en_orden`, `cargar_extractor`, `cargar_localizador`, `ExtractorFn`.
- **E2 (A2a): `src.portales.contrato`.** `ErrorPortal`, `CredencialesFaltantes`,
  `SesionInvalida`, `PortalNoDisponible`, `ContratoRoto`, `BloqueoAcceso`,
  `DocumentoPliego`, `LocalizadorPliego`, `TransporteJSON`.
- **E2b (A2a): `src.portales.modalidades`.** `COMPETIBLE`, `CONDICIONAL`,
  `NO_COMPETIBLE`, `Modalidad`, `MODALIDADES`, `modalidad_de`, `competibilidad_de`.
- **E2c (A2a): `src.portales.moneda`.** `moneda_de`, `formatear_monto`, `texto_monto`.
- **E2d (A2a): `src.scraper.models`.** Los campos nuevos de `Proceso` y la propiedad
  `proceso_gid` (§3); el `from_proceso` genérico; `MOTIVO_MODALIDAD_NO_COMPETIBLE`.
- **E3 (sin cambios, todas):** `send_session_expired_alert(config, client_profile)`,
  `send_circuit_breaker_alert(config, client_profile, error_count, last_error)`,
  `send_ops_alert(config, subject, body)`,
  `pipeline.descargar_pliegos(procesos, cfg) -> dict[str, str]`,
  `pipeline.procesar_drilldown(relevantes, pdf_paths, cfg, profile) -> dict`,
  `send_report(config=…, client_profile=…, procesos_clasificados=…, total_revisados=…, run_timestamp=…, resultados_analisis=…)`.
- **E4 (A3): `src.scraper.sicop`.**
  `async def extraer(config, portal, desde) -> list[Proceso]` y
  `def es_bloqueo_waf(status: int, content_type: str, texto: str) -> bool`.
  **`src.scraper.navegador_anonimo`:** `NavegadorAnonimo(config, origen, *, headless=None, pausa_s=None)`,
  que implementa `TransporteJSON`. Constantes en `sicop.py` que A4 y A8 reutilizan:
  `API_BASE = "https://prod-api.sicop.go.cr/"`, `ORIGEN_SPA = "https://www.sicop.go.cr/app/"`.
  Hasta que A3 mergee, A4 y A8 **no** importan `sicop.py`: definen sus rutas de
  endpoint localmente y reciben el transporte inyectado.
- **E5 (A5 → A6): variables de plantilla.**

  | Variable | Default en plantilla (= literal de hoy) |
  |---|---|
  | `portal_nombre` | `'COMPRASAL'` |
  | `portal_url` | `comprasal_url` |
  | `etiqueta_documento` | `'pliego'` |
  | `etiqueta_proceso_plural` | `'procesos'` |
  | `moneda_monto` (formularios del portal) | `'USD'` |
  | `o.enlace_texto` | `'Abrir en ' ~ (portal_nombre or 'COMPRASAL') ~ ' →'` |
  | `o.instruccion_busqueda` | `''`: si no está vacío, se muestra bajo el enlace |
  | `o.monto_txt` | lo calcula A5 con `texto_monto` |

  A5 **mantiene** `comprasal_url` en el contexto durante una versión, como alias de
  `portal_url`.
- **E6 (A7): `src.memoria.repo`.** `upsert_proceso_v2` conserva su firma
  (`repo.py:663-670`) y `recalcular_monto_usd(conn) -> int` es nueva. Nadie más llama a
  funciones nuevas de A7 en este plan.

### 10.4 Notas por tarea

- **A3:** hay que medir en la VM si el modo *headless* pasa el WAF (P8) **antes** de
  pedir `integrado=True`, y adjuntar el resultado al reporte.
- **A4:** cuando no hay PDF, se analiza el texto del detalle (`.txt`). Se aceptan DOCX y
  XLSX como segunda opción. `mp4`, `mpp`, `png` y `zip` quedan excluidos. Los límites
  `PLIEGO_MAX_MB=32` y `PLIEGO_MAX_PAGES=100` no cambian (§7 del spike). La regla de
  `pipeline.py:517-518` cambia según §7.3.
- **A7:** `portal.activo` para `sicop_cr` queda en 1 (el registro manda). `cliente_portal`
  se siembra para `multicomputos_cr` con `lookback_dias=1` y `credencial_ref=NULL`.
- **A8 (§9.3).** El cliente público de adjudicaciones de SICOP:
  - `src/mercado/sicop_publica.py` usa el `TransporteJSON` sobre
    `epAwardedProcedureSupplier/listPage` (con `supplierCd` por cédula: GANADO) y
    `epAdjuFirmAwarded/listPage` (mercado completo en CRC y USD).
  - `identidad.py` gana `IDS_PROPIOS: dict[str, frozenset[str]] = {"comprasal_sv": frozenset({"20371"}), "sicop_cr": frozenset({"3102813498", "9000018410"})}`
    y `es_propio(portal_id, id_proveedor) -> bool`, comparando texto exacto.
    `ID_PROVEEDOR_MULTICOMPUTOS` y `es_nuestro` se conservan para COMPRASAL.
  - Bloque nuevo en `db/schema.sql`: la tabla `adjudicacion_partida`, con `portal_id`,
    `codigo_proceso`, `id_externo`, `partida`, `nombre_proceso`, `institucion`,
    `modalidad_codigo`, `fecha_adjudicacion`, `monto_crc`, `monto_usd`, `proveedor_id`
    (VARCHAR2), `es_propio`, `datos_crudos` (JSON) y `cargado_en`, con PK
    `(portal_id, codigo_proceso, partida, proveedor_id)`. `adjudicacion_item` no se
    toca porque su PK y sus columnas son de COMPRASAL.
  - Los resultados se escriben en `proceso_participado` solo como **GANADO**. PERDIDO no
    se puede inferir sin el reporte de datos abiertos «Ofertas» (P3 del spike), y esa
    limitación queda documentada en el módulo.

---

## 11. Plan de pruebas y riesgos para COMPRASAL

Todas las pruebas corren **sin red**: fixtures de A0, `time.sleep`/`asyncio.sleep`
parcheados, y Playwright y el transporte falsos, con el patrón de
`tests/test_mercado_api_publica.py`.

| Tarea | Pruebas mínimas |
|---|---|
| A2a | `Portal` frozen con los valores de §1 · `portales_en_orden()[0].portal_id == "comprasal_sv"` · `cargar_extractor` de COMPRASAL importable (ruta válida) y error claro con una ruta inexistente · `from_proceso` copia **todos** los `fields(Proceso)` (test con `dataclasses.fields`, que detecta un campo olvidado) · `proceso_gid` · `competibilidad_de` reproduce la regla vieja de COMPRASAL en una tabla de formas reales · `moneda_de` y `texto_monto`: línea vieja → USD, SICOP sin moneda → sin rótulo, mezcla, nunca «USD 0» · `src.portales` no importa Playwright (`sys.modules`) |
| A2b | `Config()` sin `COMPRASAL_*` no lanza · solo SICOP (sin `COMPRASAL_*`) corre de punta a punta con extractor falso · **SICOP lanza `BloqueoAcceso` y COMPRASAL igual clasifica y notifica; su estado por portal queda en 0; `_consecutive_errors` no sube; sale un `send_ops_alert`** · COMPRASAL con sesión inválida no impide SICOP · todos los portales fallan ⇒ `success=False`, igual que hoy · breaker por portal en el 3.er fallo · `PORTALES_DESHABILITADOS` · un manifiesto por portal y lectura del `stage_handoff.json` heredado · prompt de COMPRASAL **byte a byte igual** (snapshot de `_procesos_to_dicts`) · JSONL viejo sin `portal_id` se deserializa · invariante I1 (assert) |
| A3 | Listado de 2 páginas con `last` · filtro exacto `regDt >= desde` · 422 → `ContratoRoto` · `prod-api_bloqueo_waf.html` → `BloqueoAcceso` **sin reintento** (contador de llamadas = 1) · 5xx → 2 reintentos → `PortalNoDisponible` · mapeo completo con la fixture de partidas (CRC, UNSPSC, `detalle`) · mezcla de monedas → `montos_por_moneda` · tope `SICOP_MAX_DETALLES` · caché (segunda corrida sin llamadas) · **ningún campo de datos personales en `Proceso` ni en `datos_portal`** · **`new_context` sin `user_agent` y args sin `--user-agent`** · formatos disjuntos de referencia COMPRASAL/SICOP · pausa ≥ `SICOP_PAUSA_S` entre llamadas |
| A4 | `LocalizadorComprasal` produce exactamente las mismas llamadas y resultados que el `descargar_pliegos` viejo (tests actuales en verde sin tocarlos más que el import) · `LocalizadorSicop` con transporte falso: elige el pliego por `palabras_documento`, cae a DOCX o XLSX, escribe `.txt` con el texto del detalle cuando no hay archivo, `completar` no lanza · `descargar_pliegos` con portales mezclados → `ValueError` · `PX` no se descarga · `competibilidad`: LD → True, PX → `modalidad_no_competible`, PE → reglas de texto; COMPRASAL igual que hoy en todos los casos de `test_drilldown_analysis.py` · el monto del pliego no pisa el monto del portal |
| A5 | Correo de COMPRASAL igual a hoy (snapshot del HTML con un caso fijo) · correo SICOP: «CRC 12,500,000», «SICOP», «concursos», instrucción de búsqueda por número, **cero apariciones de «COMPRASAL» y de «USD» delante de un monto en colones** · «No competibles» por modalidad · KPIs del portal: suma `monto_usd` y excluye los que no tienen tasa · zona horaria por portal en `operativo` y `tablero` |
| A6 | Cada plantilla se renderiza con el contexto viejo (sin variables nuevas) y da el mismo texto que hoy · con `portal_nombre="SICOP"` no queda ningún «COMPRASAL» literal |
| A7 | SQL de `upsert_proceso_v2` liga `moneda`, `monto_usd` (con tasa y sin tasa) y `modalidad_norm` · `recalcular_monto_usd` · la semilla de `taxonomia_modalidad` sale de `MODALIDADES` (mismo conteo y valores dentro del `CHECK`) · prueba contra Oracle local (`oracle_local`) |
| A8 | Paginación de `epAwardedProcedureSupplier` y `epAdjuFirmAwarded` con fixtures · `es_propio` con las dos cédulas · `upsert` idempotente en `adjudicacion_partida` · GANADO en `proceso_participado` · COMPRASAL (`sincronizar_mercado`) sin cambios |

**Riesgos de regresión para COMPRASAL y su mitigación:**

| Riesgo | Mitigación |
|---|---|
| Que el bucle nuevo cambie la semántica de `success` y del contador de corridas limpias | Con un solo portal, el resultado es idéntico (todos fallan ⇒ `False`). Hay un test explícito |
| Que el prompt del clasificador cambie y se pierda la calibración | Las claves nuevas solo se agregan si no están vacías; snapshot byte a byte |
| Que `from_proceso` genérico rompa los constructores de tests | Los campos nuevos van al final y tienen valor por defecto. Se corre toda la suite |
| Que el monto del pliego deje de llenar `proceso.monto` | La guarda `monto is None` mantiene a COMPRASAL igual; hay un test |
| Que la competibilidad cambie en COMPRASAL | `competibilidad_de` reproduce la regla; los casos de `test_drilldown_analysis.py` quedan intactos |
| Que el localizador de COMPRASAL cambie la descarga | Es un envoltorio sin lógica nueva; mismos tests de `test_locator.py` y `test_pipeline.py` |
| Que una etapa `analyze` nueva encuentre un manifiesto viejo durante un deploy | Lectura de compatibilidad de `stage_handoff.json` |
| Que la corrida pase el `TimeoutStartSec=30min` (`oci/lib/render-cloud-init.sh:168`) | SICOP suma unos 4 a 6 minutos. SOLICITUD para medirlo en DEV antes de activar |
| Que `Config()` acepte la falta de credenciales y COMPRASAL falle tarde | Se valida al empezar el portal (`CredencialesFaltantes`) y sale la alerta de sesión de siempre |

---

## 12. Ambigüedades que decide Emilio

> **Resueltas por Emilio (2026-11-23):**
> - **A-1:** el correo de Costa Rica dice **«cartel»** (no «pliego de condiciones»); las
>   etiquetas de `sicop_cr` en `PORTALES` se fijan así.
> - **A-2 (moneda):** cada monto se muestra en su moneda original con el **equivalente
>   aproximado entre paréntesis** (`CRC 12,500,000 (≈ USD 24,000)`); los KPIs llevan
>   totales por moneda más un total convertido a USD. La fuente del tipo de cambio queda
>   por verificar (¿`coExchangerate_lastExchangeRate` de SICOP?). Esto amplía §7.3: A5 y
>   A6 rotulan el equivalente, siempre marcado con «≈».
> - **A-3:** los **PX** van en «No competibles», sin descargar pliego; el equipo CR valida
>   qué PX son ofertables (en el listado hay PX «En recepción de ofertas»).
> - **Acceso:** Playwright anónimo sin falsificar el User-Agent; las cédulas
>   `3102813498` y `9000018410` son ambas de Multicómputos (`IDS_PROPIOS` de `sicop_cr`).
> - **A-4 a A-7:** siguen pendientes.

- **A-1. Terminología CR:** ¿el correo dice «pliego de condiciones» (término de la Ley
  9986 y de los adjuntos de SICOP) o «cartel» (el de la API y el uso coloquial)? Por
  defecto queda «pliego de condiciones». Cambiarlo es una línea en `PORTALES`.
- **A-2. Moneda de reporte de la organización y fuente FX:** ¿KPIs del portal en USD
  (por defecto) o en CRC para la vista de Costa Rica? ¿Quién carga mensualmente el tipo
  de cambio del BCCR en `fx_tasa` (manual) o se integra su servicio web, que pide
  registro?
- **A-3. `PX` en el correo:** este diseño los muestra en «No competibles», con motivo.
  La alternativa es omitirlos del correo (son unos 13 % de los concursos de TI). ¿Cuál
  se prefiere?
- **A-4. `PE`, `CD` y `CE` como `CONDICIONAL`** (que decida el pliego): falta que lo
  valide el equipo CR (P7 del spike).
- **A-5. Tablas v1 sin prefijo** (§4): se acepta porque los formatos son disjuntos y
  porque activar `multicomputos_cr` exige `ENABLE_ESQUEMA_V2=true`. ¿De acuerdo?
- **A-6. Sin sub-tope de cuota por portal** (§8), con el disparador para revisar. ¿De
  acuerdo?
- **A-7. Enlace del correo de SICOP** = buscador público más el número visible (P4 del
  spike). ¿Alcanza?

---

## 13. Solicitudes de integración que este diseño ya prevé

Las tareas las repiten en sus reportes cuando corresponda. Se listan acá para que quien
lanza no las descubra al reconciliar.

| Archivo | Cambio | De | Bloqueante |
|---|---|---|---|
| `src/portales/__init__.py` | `sicop_cr`: `integrado=True` | A3, tras medir P8 | no (`multicomputos_cr` está `active: false`) |
| `docs/README.md` | Fila de índice para este documento y para `sicop-api.md` | A1 | no |
| `CLAUDE.md` (Business Context / Current Status) | Mencionar SICOP como segundo portal | A11 | no |
| `docs/planes/multicomputos-costa-rica.md` | Cambiar «login/credenciales» por «datos públicos con navegador anónimo» | A11 | no |
| `oci/lib/render-cloud-init.sh` | Revisar `TimeoutStartSec` con una corrida real de DEV con dos portales | quien lanza / Emilio | no |
| `Dockerfile`, `oci/` | `xvfb` y modo *headful* solo si P8 falla | A3 | no |
| `oci/lib/09-mercado` | Timer semanal de `src.mercado` para `sicop_cr` | A8 | no |
| `CHANGELOG.md` | Una entrada al reconciliar | quien lanza | no |
