"""
Extractor de procesos de compra de COMPRASAL vía API REST.

COMPRASAL expone una API JSON en /api/v1/procesos/proveedores que usa un
JWT Bearer token almacenado en localStorage. Playwright carga la página
autenticada para obtener el token, luego llama la API con fetch() dentro
del contexto del browser (mismo dominio → cookies + token disponibles).

Campos retornados por la API que usamos:
  id               → ID numérico para construir URL del proceso
  codigo_proceso   → Código de referencia (ej: 4301-2026-P0079)
  nombre_proceso   → Descripción/objeto de la licitación
  institucion      → Nombre de la institución compradora
  forma_contratacion → Método (Licitación, Comparación de precios, etc.)
  fecha_publicacion → ISO datetime en UTC (fecha de publicación)
  actividades      → lista de clasificaciones económicas (rubro/giro); ver
                     `_tipo_actividad_de` — NO es un campo plano

⚠️ Esta respuesta NO trae `fecha_limite` ni `tipo_actividad` como campos
planos (verificado en vivo contra el endpoint real, autenticado, capturando
la llamada exacta que hace esta página): el docstring anterior lo daba por
hecho y era incorrecto — `item.get("fecha_limite")` devolvía `None` siempre
por construcción, no por un bug de parseo. `tipo_actividad` sale de
`actividades[].a.nombre` (ver `_tipo_actividad_de`, más abajo);
`fecha_limite` sale de un endpoint de DETALLE aparte, uno por proceso, que
por su costo solo se llama para procesos ya relevantes — ver
`drilldown/locator.py::obtener_fecha_limite`, llamado desde
`drilldown/pipeline.py::descargar_pliegos`.

URL de detalle: https://www.comprasal.gob.sv/pac-procesos/etapas/{id}
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from src.config import Config
from src.scraper.browser import build_chromium_args
from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

COMPRASAL_BASE = "https://www.comprasal.gob.sv"
COMPRASAL_PAAC_URL = f"{COMPRASAL_BASE}/proveedor-paac"
API_PROCESOS = "/api/v1/procesos/proveedores"

# El Salvador es UTC-6
TZ_ELSALVADOR = timezone(timedelta(hours=-6))

DEFAULT_TIMEOUT = 30_000

# COMPRASAL a veces tarda más de 30 s en responder (visto 2026-10-24 y 2026-10-27): un solo
# timeout no debe costar la pasada diaria entera.
GOTO_TIMEOUT = 60_000
GOTO_INTENTOS = 3
GOTO_ESPERA_S = 10


def _parse_fecha_api(fecha_str: str | None) -> datetime | None:
    """Parsea la fecha ISO de la API (UTC) y la convierte a timezone El Salvador.

    Retorna None si la API devuelve fecha_publicacion: null (procesos sin
    fecha de publicación efectiva — típicamente borradores o estados intermedios).
    """
    if not fecha_str:
        return None
    # Formato: "2026-03-30T22:34:44.000Z"
    fecha_str = fecha_str.rstrip("Z").split(".")[0]  # → "2026-03-30T22:34:44"
    dt = datetime.strptime(fecha_str, "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ_ELSALVADOR)


async def _get_token_and_page(context):
    """
    Navega a /proveedor-paac, obtiene el JWT del localStorage y retorna
    (page, token). El token se necesita para llamar la API con fetch().
    """
    logger.info("Cargando %s para obtener token de sesión...", COMPRASAL_PAAC_URL)
    page = None
    for intento in range(1, GOTO_INTENTOS + 1):
        page = await context.new_page()
        try:
            # 'domcontentloaded' es suficiente: el JWT ya viene del storage_state cargado
            # en el context. 'networkidle' se cuelga en esta SPA por polling de fondo.
            await page.goto(COMPRASAL_PAAC_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
            break
        except PlaywrightTimeout:
            await page.close()
            if intento == GOTO_INTENTOS:
                raise
            logger.warning("Timeout cargando %s (intento %d/%d); reintento en %ds",
                           COMPRASAL_PAAC_URL, intento, GOTO_INTENTOS, GOTO_ESPERA_S)
            await asyncio.sleep(GOTO_ESPERA_S)

    token = await page.evaluate("() => localStorage.getItem('token')")
    if not token:
        raise RuntimeError(
            "No se encontró token JWT en localStorage. "
            "La sesión puede haber expirado — ejecutar login manual."
        )
    logger.info("Token JWT obtenido (%d chars)", len(token))
    return page, token


async def _fetch_page(page, token: str, page_num: int) -> dict:
    """
    Llama /api/v1/procesos/proveedores?page=N con fetch() dentro del browser.
    Retorna dict con keys: data (list), total_rows (int), per_page (int).
    """
    url = f"{API_PROCESOS}?page={page_num}&participacion=false&recomendados=false"
    result = await page.evaluate(f"""
        async () => {{
            const r = await fetch('{url}', {{
                headers: {{
                    'Authorization': 'Bearer {token}',
                    'Accept': 'application/json',
                }},
            }});
            if (!r.ok) {{
                throw new Error('API error: ' + r.status + ' ' + r.statusText);
            }}
            const headers = {{}};
            r.headers.forEach((v, k) => {{ headers[k] = v; }});
            const data = await r.json();
            return {{
                data: data,
                total_rows: parseInt(headers['total_rows'] || '0', 10),
                per_page: parseInt(headers['per_page'] || '12', 10),
                page: parseInt(headers['page'] || '{page_num}', 10),
            }};
        }}
    """)
    return result


def _tipo_actividad_de(item: dict) -> str:
    """Extrae las actividades económicas del proceso ("rubro"/"giro").

    La API NO trae `tipo_actividad` como campo plano (verificado en vivo):
    viene anidado en `actividades`, una lista de 1 a 8 clasificaciones
    económicas por proceso (rubro/giro), SIN ninguna marcada como
    "principal" — se probó con procesos reales de las 9 modalidades vigentes
    y ninguno distingue una actividad "primaria". Por eso se unen los
    nombres distintos, en el orden en que la API los entrega, en vez de
    elegir uno arbitrariamente y perder las demás.

    Se recorta a 120 caracteres porque `licitaciones_metadata.categoria`
    (`db/schema.sql`) es `VARCHAR2(120)`: sin el recorte, un proceso con
    varias actividades largas rompería el `UPDATE`/`MERGE` de
    `memoria/repo.py` con un ORA-12899 (valor demasiado largo para la
    columna) — un campo de reporte nunca debe poder tumbar la escritura a
    la ADB.
    """
    vistos: set[str] = set()
    nombres: list[str] = []
    for act in item.get("actividades") or []:
        if not isinstance(act, dict):
            continue
        nombre = ((act.get("a") or {}).get("nombre") or "").strip()
        if nombre and nombre not in vistos:
            vistos.add(nombre)
            nombres.append(nombre)
    return "; ".join(nombres)[:120]


def _item_to_proceso(item: dict) -> Proceso:
    """Convierte un item de la API en un objeto Proceso."""
    proceso_id = item["id"]
    return Proceso(
        numero_referencia=item.get("codigo_proceso", ""),
        institucion=item.get("institucion", "Desconocida").strip(),
        descripcion=item.get("nombre_proceso", "").strip(),
        fecha_publicacion=_parse_fecha_api(item["fecha_publicacion"]),
        forma_contratacion=item.get("forma_contratacion", "").strip(),
        tipo_actividad=_tipo_actividad_de(item),
        url_directa=f"{COMPRASAL_BASE}/pac-procesos/etapas/{proceso_id}",
        # fecha_limite NO está en esta respuesta (ver docstring del módulo):
        # se enriquece después, solo para procesos relevantes, en
        # drilldown/pipeline.py::descargar_pliegos.
        fecha_limite=None,
    )


async def extract_recent_procesos(config: Config) -> list[Proceso]:
    """
    Extrae todos los procesos publicados en las últimas `config.days_lookback` días.

    Llama la API paginada de COMPRASAL y para cuando los procesos son más
    antiguos que el cutoff (los resultados vienen ordenados por fecha desc).
    """
    cutoff = datetime.now(TZ_ELSALVADOR) - timedelta(days=config.days_lookback)
    logger.info(
        "Extrayendo procesos publicados desde %s (cutoff)",
        cutoff.strftime("%Y-%m-%d %H:%M %Z"),
    )

    all_procesos: list[Proceso] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.headless,
            executable_path=config.chromium_path or None,
            args=build_chromium_args(config),
        )
        context = await browser.new_context(
            storage_state=config.storage_state_path,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        try:
            page, token = await _get_token_and_page(context)

            page_num = 1
            total_rows = None
            per_page = 12  # default

            while True:
                logger.info("Llamando API página %d...", page_num)
                result = await _fetch_page(page, token, page_num)

                items = result["data"]
                if not items:
                    logger.info("API retornó página vacía en pág %d — deteniendo.", page_num)
                    break

                # Actualizar metadata de paginación en primera respuesta
                if total_rows is None:
                    total_rows = result["total_rows"]
                    per_page = result["per_page"] or per_page
                    logger.info(
                        "Total de procesos en COMPRASAL: %d (páginas aprox: %d)",
                        total_rows,
                        -(-total_rows // per_page),  # ceiling div
                    )

                past_cutoff = False
                skipped_no_fecha = 0
                within_period = 0
                last_valid_fecha = None
                for item in items:
                    fecha = _parse_fecha_api(item.get("fecha_publicacion"))
                    if fecha is None:
                        skipped_no_fecha += 1
                        continue
                    last_valid_fecha = fecha
                    if fecha < cutoff:
                        past_cutoff = True
                        continue
                    within_period += 1
                    all_procesos.append(_item_to_proceso(item))

                if skipped_no_fecha:
                    logger.info(
                        "Página %d: %d items skipped por fecha_publicacion=null",
                        page_num,
                        skipped_no_fecha,
                    )
                logger.info(
                    "Página %d: %d items, %d dentro del período",
                    page_num,
                    len(items),
                    within_period,
                )

                # Detener si la última fecha válida de la página ya es más antigua que el cutoff
                if past_cutoff and last_valid_fecha is not None:
                    logger.info(
                        "Último proceso de pág %d tiene fecha %s < cutoff → deteniendo paginación.",
                        page_num,
                        last_valid_fecha.strftime("%Y-%m-%d %H:%M"),
                    )
                    break

                # Detener si no hay más páginas
                if total_rows and page_num * per_page >= total_rows:
                    logger.info("Última página alcanzada (%d).", page_num)
                    break

                page_num += 1
                await asyncio.sleep(1)  # pausa cortés entre llamadas

        except Exception as exc:
            os.makedirs(config.screenshots_dir, exist_ok=True)
            shot = os.path.join(config.screenshots_dir, "extractor_error.png")
            try:
                await page.screenshot(path=shot)
            except Exception:
                pass
            logger.exception("Error durante la extracción: %s", exc)
            raise
        finally:
            await browser.close()

    logger.info(
        "Extracción completa: %d procesos en los últimos %d día(s).",
        len(all_procesos),
        config.days_lookback,
    )
    return all_procesos
