"""
Trainer de inteligencia histórica para tender-opportunity-tracker.

Extrae TODOS los procesos en los que el cliente ha participado previamente en
COMPRASAL, los procesa con el LLM activo en lotes y genera un perfil compacto
(~500-700 tokens) que mejora la precisión del clasificador.

Uso (una sola vez por cliente):
    python -m src.classifier.trainer --client multicomputos
    python -m src.classifier.trainer --client multicomputos --dry-run

El perfil se guarda en clients/{client_id}.yaml bajo la clave `intelligence`.

Estrategia de tokens (mínimo costo, máxima información):
  1. Extraer todos los procesos participados (campos compactos solamente)
  2. Agrupar en lotes de BATCH_SIZE procesos
  3. Por cada lote: el modelo genera un resumen parcial (~120 palabras)
  4. Con todos los resúmenes parciales: el modelo genera el perfil final (~500 palabras)

Costo estimado para 200 procesos históricos: < USD 0.05

⚠️ **Deuda conocida: el trainer queda FUERA de la contabilidad de cuota.** A
diferencia de `engine.py`, no llama a `quota.cobrar()`. Es una decisión, no un
olvido: esto es una CLI manual que se corre un puñado de veces por cliente, no
la corrida diaria de las 07:00, así que su gasto no distorsiona el libro mayor
del día. Si alguna vez se automatiza, hay que meterlo en la cuota.
"""
import argparse
import asyncio
import logging
import sys
import textwrap

from src import llm
from src.classifier.client_profile import load_client_profile, save_intelligence
from src.config import get_config
from src.llm import modelos
from src.llm.contrato import BloqueTexto, Pedido

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

COMPRASAL_BASE = "https://www.comprasal.gob.sv"
COMPRASAL_PAAC_URL = f"{COMPRASAL_BASE}/proveedor-paac"
API_PROCESOS = "/api/v1/procesos/proveedores"

BATCH_SIZE = 40          # Procesos por lote enviado al modelo
BATCH_SUMMARY_WORDS = 120  # Longitud máxima de cada resumen parcial
FINAL_PROFILE_WORDS = 550  # Longitud del perfil final

# Retrocompat: `tests/test_quota.py` importa este nombre para verificar que tiene
# precio en `PRECIOS_USD_MTOK`. Es el default de Anthropic para esta etapa; el
# modelo REAL lo resuelve `llm.modelo_de()`, que respeta LLM_PROVIDER.
MODEL = modelos.DEFAULTS[("anthropic", "perfilado")]

# Los dos están holgadamente por debajo del techo de salida de OCI (4096), así
# que acá `llm.completar` nunca recorta.
MAX_TOKENS_BATCH = 300
MAX_TOKENS_FINAL = 1200


# ── Extracción de procesos participados ──────────────────────────────────────

async def _fetch_page_participated(page, token: str, page_num: int) -> dict:
    """Llama la API con participacion=true y retorna {data, total_rows, per_page}."""
    url = f"{API_PROCESOS}?page={page_num}&participacion=true&recomendados=false"
    result = await page.evaluate(f"""
        async () => {{
            const r = await fetch('{url}', {{
                headers: {{
                    'Authorization': 'Bearer {token}',
                    'Accept': 'application/json',
                }},
            }});
            if (!r.ok) throw new Error('API error: ' + r.status);
            const headers = {{}};
            r.headers.forEach((v, k) => {{ headers[k] = v; }});
            const data = await r.json();
            return {{
                data: data,
                total_rows: parseInt(headers['total_rows'] || '0', 10),
                per_page: parseInt(headers['per_page'] || '12', 10),
            }};
        }}
    """)
    return result


async def extract_all_participated(config, max_pages: int = 0) -> list[dict]:
    """
    Extrae TODOS los procesos participados de COMPRASAL.
    Retorna lista de dicts con campos compactos para el análisis.
    max_pages=0 significa sin límite.
    """
    from playwright.async_api import async_playwright

    all_items: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            storage_state=config.storage_state_path,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        logger.info("Cargando COMPRASAL para obtener token...")
        await page.goto(COMPRASAL_PAAC_URL, wait_until="networkidle", timeout=30_000)

        token = await page.evaluate("() => localStorage.getItem('token')")
        if not token:
            raise RuntimeError("No se encontró JWT en localStorage — sesión expirada.")
        logger.info("Token obtenido (%d chars).", len(token))

        page_num = 1
        total_rows = None
        per_page = 12

        while True:
            logger.info("Extrayendo página %d de procesos participados...", page_num)
            result = await _fetch_page_participated(page, token, page_num)

            items = result["data"]
            if not items:
                logger.info("Sin más resultados en página %d.", page_num)
                break

            if total_rows is None:
                total_rows = result["total_rows"]
                per_page = result["per_page"] or per_page
                logger.info(
                    "Total de procesos participados en COMPRASAL: %d (~%d páginas)",
                    total_rows,
                    -(-total_rows // per_page),
                )

            for item in items:
                all_items.append({
                    "codigo": item.get("codigo_proceso", ""),
                    "nombre": item.get("nombre_proceso", "").strip(),
                    "institucion": item.get("institucion", "").strip(),
                    "metodo": item.get("forma_contratacion", "").strip(),
                    "fecha": item.get("fecha_publicacion", "")[:10],
                })

            await asyncio.sleep(0.5)

            if max_pages and page_num >= max_pages:
                logger.info("Límite de páginas alcanzado (%d).", max_pages)
                break

            if total_rows and page_num * per_page >= total_rows:
                logger.info("Todas las páginas extraídas.")
                break

            page_num += 1

        await browser.close()

    logger.info("Extracción completa: %d procesos participados.", len(all_items))
    return all_items


# ── Generación del perfil con el LLM ─────────────────────────────────────────

def _format_batch_for_prompt(batch: list[dict]) -> str:
    """Convierte un lote de procesos en texto compacto para el prompt."""
    lines = []
    for item in batch:
        lines.append(f"- [{item['codigo']}] {item['nombre']} | {item['institucion']} | {item['metodo']}")
    return "\n".join(lines)


def _summarize_batch(cfg, batch: list[dict], client_name: str, batch_num: int, total_batches: int) -> str:
    """
    Envía un lote de procesos al modelo activo y obtiene un resumen parcial.
    """
    batch_text = _format_batch_for_prompt(batch)
    prompt = textwrap.dedent(f"""
        Analiza estos {len(batch)} procesos de compra pública en los que {client_name} ha participado.
        Extrae los patrones más importantes en máximo {BATCH_SUMMARY_WORDS} palabras.

        Enfócate en:
        1. Categorías principales de productos/servicios comprados
        2. Instituciones más frecuentes
        3. Términos o frases recurrentes en las descripciones
        4. Patrones inesperados (procesos relevantes con categorías o nombres no obvios)

        Procesos (lote {batch_num}/{total_batches}):
        {batch_text}

        Responde directamente con el resumen, sin preámbulos.
    """).strip()

    # `sistema=""` a propósito: el trainer manda todo en el turno del usuario, y
    # los adaptadores omiten el system prompt cuando viene vacío.
    respuesta = llm.completar(cfg, Pedido(
        etapa="perfilado",
        contenido=[BloqueTexto(texto=prompt)],
        sistema="",
        max_tokens=MAX_TOKENS_BATCH,
    ))
    return respuesta.texto.strip()


def _generate_final_profile(
    cfg,
    batch_summaries: list[str],
    client_name: str,
    total_processes: int,
) -> str:
    """
    Sintetiza todos los resúmenes parciales en un perfil de inteligencia final.
    """
    summaries_text = "\n\n---\n\n".join(
        f"Resumen parcial {i+1}:\n{s}" for i, s in enumerate(batch_summaries)
    )

    prompt = textwrap.dedent(f"""
        Eres un sistema de inteligencia de negocios. Sintetiza los siguientes resúmenes parciales \
del historial de participación de {client_name} en licitaciones públicas de El Salvador \
en un perfil de clasificación compacto y accionable.

        El perfil será usado por un clasificador de IA para decidir si nuevas licitaciones son \
relevantes para {client_name}. Debe:

        1. Describir en 3-5 frases el tipo de licitaciones en las que {client_name} participa
        2. Listar las 5-8 categorías de productos/servicios más frecuentes (con ejemplos reales de los datos)
        3. Mencionar las 5 instituciones compradoras más recurrentes
        4. Señalar patrones no obvios: licitaciones relevantes con descripciones ambiguas, \
términos locales específicos, categorías mal etiquetadas pero relevantes
        5. Indicar qué tipos de procesos NO son relevantes aunque suenen tecnológicos \
(si aplica según los datos)

        Máximo {FINAL_PROFILE_WORDS} palabras. Escribe en español. No uses headers ni markdown. \
Escribe en prosa o listas cortas. El texto será insertado directamente en un prompt de IA.

        Total de procesos analizados: {total_processes}

        {summaries_text}
    """).strip()

    respuesta = llm.completar(cfg, Pedido(
        etapa="perfilado",
        contenido=[BloqueTexto(texto=prompt)],
        sistema="",
        max_tokens=MAX_TOKENS_FINAL,
    ))
    return respuesta.texto.strip()


# ── CLI principal ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera perfil de inteligencia histórica para un cliente de tender-opportunity-tracker"
    )
    parser.add_argument(
        "--client", required=True,
        help="ID del cliente (ej: multicomputos). Debe existir clients/{id}.yaml"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extrae procesos y muestra cuántos hay, sin llamar al modelo ni guardar nada"
    )
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Límite de páginas a extraer (0 = todas). Útil para pruebas rápidas."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    config = get_config()

    # Validar que el perfil del cliente existe
    try:
        client_profile = load_client_profile(args.client, config.clients_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    logger.info("Cliente: %s", client_profile.client_name)
    logger.info("Directorio de clientes: %s", config.clients_dir)

    # ANTES del scrape, no después: la extracción son ~20 minutos de Playwright
    # paginando COMPRASAL. Descubrir ahí que falta la credencial del proveedor
    # activo cuesta esos 20 minutos; descubrirlo acá cuesta 200 ms.
    llm.validar_configuracion(config)

    # 1. Extraer todos los procesos participados
    items = asyncio.run(extract_all_participated(config, max_pages=args.max_pages))

    if not items:
        logger.warning("No se encontraron procesos participados. Verificar que la cuenta tenga historial.")
        sys.exit(0)

    logger.info("Procesos extraídos: %d", len(items))

    if args.dry_run:
        print(f"\n[DRY-RUN] Se extraerían {len(items)} procesos participados.")
        print("Muestra de los primeros 5:")
        for item in items[:5]:
            print(f"  [{item['codigo']}] {item['nombre'][:70]}")
        print("\nUsar sin --dry-run para generar el perfil de inteligencia.")
        sys.exit(0)

    # 2. Generar perfil con el LLM activo
    logger.info(
        "Perfilando con %s (%s)...",
        llm.modelo_de(config, "perfilado"), llm.proveedor_activo(config),
    )

    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    total_batches = len(batches)
    logger.info("Procesando %d lotes de máximo %d procesos c/u...", total_batches, BATCH_SIZE)

    # Sin captura de errores a propósito: un LLMError propaga y tumba la CLI.
    # Es una corrida manual y supervisada; un perfil a medias es peor que ninguno.
    batch_summaries = []
    for i, batch in enumerate(batches, 1):
        logger.info("Lote %d/%d (%d procesos)...", i, total_batches, len(batch))
        summary = _summarize_batch(config, batch, client_profile.client_name, i, total_batches)
        batch_summaries.append(summary)
        logger.debug("Resumen lote %d: %s", i, summary[:100])

    logger.info("Sintetizando perfil final...")
    final_profile = _generate_final_profile(
        config, batch_summaries, client_profile.client_name, len(items)
    )

    logger.info("Perfil generado (%d palabras).", len(final_profile.split()))

    # 3. Guardar en el YAML del cliente
    save_intelligence(
        client_id=args.client,
        clients_dir=config.clients_dir,
        profile_text=final_profile,
        processes_analyzed=len(items),
    )

    print(f"\n✓ Perfil de inteligencia guardado en clients/{args.client}.yaml")
    print(f"  Procesos analizados: {len(items)}")
    print(f"  Palabras en el perfil: {len(final_profile.split())}")
    print(f"\nPerfil generado:\n{'-'*60}\n{final_profile}\n{'-'*60}")


if __name__ == "__main__":
    main()
