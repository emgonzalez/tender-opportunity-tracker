"""src.drilldown.pipeline — Orquestación del drill-down de pliegos (Fase 2, MVP Track A).

Divide el trabajo en DOS fases aisladas en memoria para no solapar Chromium con el
análisis LLM (disciplina de memoria del docs/planes/fase2-track-a.md):

  Fase A · descargar_pliegos()   → abre Playwright, localiza y descarga los PDFs,
                                    cierra el navegador. Devuelve {uuid: ruta}.
  Fase B · procesar_drilldown()  → SIN navegador: por cada pliego, pre-filtro →
                                    guardia de cuota → triaje (Haiku) → análisis
                                    (Sonnet) → persistencia. Un pliego a la vez.

El ciclo de vida de los PDFs temporales lo gestiona el caller (limpiar_pliegos()),
no la Fase B, porque un mismo proceso puede analizarse para varios clientes.
"""
import asyncio
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone

from src.scraper.models import ResultadoAnalisis
from src.classifier.client_profile import ClientProfile
from src.drilldown import downloader, pdf, prefilter, triage, analysis, quota
from src.drilldown.locator import find_pliego_pdf_url

logger = logging.getLogger(__name__)

# Umbral de confianza del triaje por debajo del cual se manda a revisión manual.
TRIAGE_MIN_CONFIANZA = 0.50

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Contexto del cliente para los prompts ────────────────────────────────────

def contexto_cliente(profile: ClientProfile) -> str:
    """Construye el bloque de contexto del cliente que se inyecta en triaje/análisis."""
    partes = [f"Cliente: {profile.client_name}."]
    if profile.client_description:
        partes.append(f"Descripción: {profile.client_description}")
    cats = ", ".join(
        f"{c.label} ({', '.join(b.name for b in c.brands)})" if c.brands else c.label
        for c in profile.catalog.categories
    )
    if cats:
        partes.append(f"Categorías del portafolio: {cats}.")
    if profile.rules.context_note:
        partes.append(f"Contexto de mercado: {profile.rules.context_note}")
    return "\n".join(partes)


# ── Fase A: descarga (requiere navegador) ────────────────────────────────────

async def _download_via_playwright(page, url: str, dest_dir: str, max_bytes: int) -> str | None:
    """Descarga el PDF usando la sesión autenticada del navegador (auth garantizada).

    Fallback cuando `requests` no lleva las cookies/token de COMPRASAL. Verifica
    Content-Length antes de traer el cuerpo; aun así el cuerpo entra en RAM una vez
    (acotado por max_bytes), aceptable con concurrencia=1 y PDFs <= 32 MB.
    """
    try:
        resp = await page.request.get(url, timeout=45_000)
        if not resp.ok:
            logger.warning("Descarga Playwright falló (%s) para %s", resp.status, url)
            return None
        cl = resp.headers.get("content-length")
        if cl and int(cl) > max_bytes:
            logger.warning("DESCARTADO_TAMANO (Content-Length %s) para %s", cl, url)
            return None
        body = await resp.body()
        if len(body) > max_bytes:
            logger.warning("DESCARTADO_TAMANO (cuerpo %d bytes) para %s", len(body), url)
            return None
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f"{_uuid.uuid4().hex}.pdf")
        with open(path, "wb") as f:
            f.write(body)
        return path
    except Exception as exc:  # noqa: BLE001 — no tumbar el batch
        logger.warning("Error en descarga Playwright de %s: %s", url, exc)
        return None


async def descargar_pliegos(procesos: list, cfg) -> dict[str, str]:
    """Fase A: localiza y descarga el PDF de cada proceso. Devuelve {numero_referencia: ruta}.

    Abre UN navegador Playwright (sesión de storage_state) y lo cierra al terminar.
    Intenta primero `requests` (streaming, sin memoria de navegador); si falla por
    auth/red, cae al request autenticado de Playwright.
    """
    from playwright.async_api import async_playwright
    from src.scraper.browser import build_chromium_args

    resultado: dict[str, str] = {}
    if not procesos:
        return resultado

    max_bytes = cfg.pliego_max_mb * 1024 * 1024
    loop = asyncio.get_event_loop()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=cfg.headless,
            executable_path=cfg.chromium_path or None,
            args=build_chromium_args(cfg),
        )
        context = await browser.new_context(
            storage_state=cfg.storage_state_path,
            user_agent=_USER_AGENT,
        )
        page = await context.new_page()
        try:
            for proceso in procesos:
                uuid = proceso.numero_referencia
                try:
                    url = await find_pliego_pdf_url(page, proceso, cfg)
                    if not url:
                        logger.info("Sin URL de pliego para %s", uuid)
                        continue
                    # 1) intento con requests (streaming, cero memoria de navegador)
                    path = None
                    try:
                        path = await loop.run_in_executor(
                            None, downloader.download_pliego, url, cfg.drilldown_tmp_dir, cfg.pliego_max_mb
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.info("requests no pudo descargar %s (%s); probando Playwright", uuid, exc)
                    # 2) fallback autenticado con Playwright
                    if not path:
                        path = await _download_via_playwright(page, url, cfg.drilldown_tmp_dir, max_bytes)
                    if path:
                        resultado[uuid] = path
                        logger.info("Pliego descargado para %s → %s", uuid, path)
                except Exception as exc:  # noqa: BLE001 — aislar por proceso
                    logger.warning("Error descargando pliego de %s: %s", uuid, exc)
        finally:
            await browser.close()

    logger.info("Fase A completa: %d/%d pliegos descargados.", len(resultado), len(procesos))
    return resultado


# ── Fase B: análisis (sin navegador) ─────────────────────────────────────────

def _persistir(cfg, client_id: str, res: ResultadoAnalisis) -> ResultadoAnalisis:
    """Persiste el estado. Track B (si está activo) hace dual-write ADB + JSONL."""
    from src.memoria import service
    try:
        service.guardar_estado(cfg, client_id, res)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo persistir análisis de %s: %s", res.proceso_uuid, exc)
    return res


def analizar_pliego_proceso(
    proceso, pdf_path: str, cfg, ctx_cliente: str, client_id: str,
    inclusion_signals: list, exclusion_signals: list,
) -> ResultadoAnalisis:
    """Fase B para UN pliego. No borra el PDF (lo hace el caller vía limpiar_pliegos)."""
    from src import history
    from src.memoria import service

    uuid = proceso.numero_referencia

    # Idempotencia / reanudación: si ya está en estado terminal, saltar.
    # Con Track B activo el estado se lee de ADB (con fallback a JSONL).
    prev = service.estado_de(cfg, client_id, uuid)
    if history.is_estado_terminal(prev):
        logger.info("Saltando %s: ya en estado terminal %s", uuid, prev)
        return ResultadoAnalisis(proceso_uuid=uuid, estado=prev, generado_en=_now())

    # Inspección del PDF (mmap, memory-safe).
    try:
        tamano = os.path.getsize(pdf_path)
    except OSError:
        tamano = 0
    try:
        n_pages = pdf.page_count(pdf_path)
        scanned = pdf.is_scanned(pdf_path)
    except pdf.PdfCorruptoError as exc:
        return _persistir(cfg, client_id, ResultadoAnalisis(
            proceso_uuid=uuid, estado="ERROR_PARSEO", error=str(exc),
            tamano_bytes=tamano, generado_en=_now()))
    formato = "ESCANEADO" if scanned else "NATIVO"

    def _res(**kw) -> ResultadoAnalisis:
        """ResultadoAnalisis con la instrumentación de costo ya rellenada.

        Todas las salidas llevan n_paginas/tamano_bytes, incluidas las que
        descartan el pliego: para calibrar la cuota hace falta saber también
        el tamaño de lo que NO se analizó.
        """
        return ResultadoAnalisis(
            proceso_uuid=uuid, formato=formato, n_paginas=n_pages,
            tamano_bytes=tamano, generado_en=_now(), **kw)

    # Guardia de páginas (límite de la API de Claude).
    if n_pages > cfg.pliego_max_pages:
        return _persistir(cfg, client_id, _res(
            estado="REQUIERE_REVISION_MANUAL",
            error=f"{n_pages} páginas > límite {cfg.pliego_max_pages}"))

    # Pre-filtro RegEx (solo pliegos nativos; los escaneados se saltan para evitar falso negativo).
    texto = None
    if not scanned:
        texto = pdf.extract_text_first_pages(pdf_path, cfg.triage_pages)
        if not prefilter.passes_prefilter(texto, inclusion_signals, exclusion_signals):
            return _persistir(cfg, client_id, _res(estado="DESCARTADO_REGEX"))

    # ── Guardia de gasto · triaje ────────────────────────────────────────────
    # Se cobra por etapa, no una sola vez al final: el triaje se paga aunque
    # después diga NO-GO. La guardia vieja estimaba antes del triaje pero
    # reservaba solo tras un GO, así que todo el gasto de triaje —el mayor del
    # pipeline en un corpus escaneado— nunca entraba al libro.
    if not quota.cabe(cfg, quota.costo_triaje_estimado_usd(
            cfg, pdf_path, scanned=scanned, texto=texto)):
        return _persistir(cfg, client_id, _res(
            estado="REQUIERE_REVISION_MANUAL",
            error="presupuesto diario agotado antes del triaje"))

    # Triaje (Haiku).
    try:
        veredicto, confianza, motivo, uso_triaje = triage.triage(
            cfg, ctx_cliente, texto=texto, pdf_path=pdf_path, scanned=scanned)
    except triage.TriageError as exc:
        return _persistir(cfg, client_id, _res(estado="ERROR_LLM", error=str(exc)))

    # El triaje ya se pagó, cualquiera sea el veredicto. Cobrar el consumo REAL.
    quota.cobrar(cfg, uso_triaje)

    if veredicto == "NO-GO":
        return _persistir(cfg, client_id, _res(
            estado="DESCARTADO_TRIAJE", triaje="NO-GO", triaje_confianza=confianza,
            uso_triaje=uso_triaje))
    if confianza < TRIAGE_MIN_CONFIANZA:
        return _persistir(cfg, client_id, _res(
            estado="REQUIERE_REVISION_MANUAL", triaje=veredicto,
            triaje_confianza=confianza, uso_triaje=uso_triaje,
            error=f"confianza de triaje baja ({confianza:.2f}): {motivo}"))

    # ── Guardia de gasto · análisis ──────────────────────────────────────────
    # Es la llamada cara. Si no cabe, el pliego queda para revisión manual con
    # el triaje ya cobrado: no se pierde el gasto hecho ni se oculta.
    if not quota.cabe(cfg, quota.costo_analisis_estimado_usd(cfg, n_pages)):
        return _persistir(cfg, client_id, _res(
            estado="REQUIERE_REVISION_MANUAL", triaje=veredicto,
            triaje_confianza=confianza, uso_triaje=uso_triaje,
            error="presupuesto diario agotado antes del análisis"))

    # Análisis profundo (Sonnet). analizar_pliego ya devuelve ERROR_LLM si falla.
    res = analysis.analizar_pliego(pdf_path, cfg, ctx_cliente, uuid, formato=formato)
    quota.cobrar(cfg, res.uso_analisis)  # real, también si terminó en ERROR_LLM
    res.triaje = veredicto
    res.triaje_confianza = confianza
    res.uso_triaje = uso_triaje
    res.n_paginas = n_pages
    res.tamano_bytes = tamano

    # ── Track B · memoria institucional (no-op si enable_memoria=False) ──
    # Fuera de la ruta crítica: cualquier fallo aquí solo implica un reporte
    # sin precedentes, nunca romper el drill-down.
    if res.estado == "ANALIZADO" and getattr(cfg, "enable_memoria", False):
        try:
            # Texto completo para vectorizar: en nativos se relee el PDF entero
            # (mmap); en escaneados usamos lo que haya (el texto de triaje).
            texto_completo = texto
            if not scanned:
                texto_completo = pdf.extract_text_first_pages(pdf_path, n_pages)
            service.persistir_memoria(cfg, proceso, res, texto_completo or "", client_id)
            del texto_completo  # no retener el pliego completo en RAM
            res.precedentes = service.obtener_precedentes(cfg, proceso, res)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memoria institucional falló para %s: %s", uuid, exc)

    return _persistir(cfg, client_id, res)


def procesar_drilldown(relevantes: list, pdf_paths: dict, cfg, profile: ClientProfile) -> dict:
    """Fase B sobre todos los procesos relevantes de un cliente. Un pliego a la vez.

    Devuelve {numero_referencia: ResultadoAnalisis}.
    """
    ctx = contexto_cliente(profile)
    incl = profile.catalog.inclusion_signals
    excl = profile.catalog.exclusion_signals
    resultados: dict[str, ResultadoAnalisis] = {}
    for proceso in relevantes:
        uuid = proceso.numero_referencia
        path = pdf_paths.get(uuid)
        if not path:
            continue  # no se pudo descargar el pliego; ya se logueó en Fase A
        try:
            resultados[uuid] = analizar_pliego_proceso(
                proceso, path, cfg, ctx, profile.client_id, incl, excl)
        except Exception as exc:  # noqa: BLE001 — aislar por pliego, no tumbar el batch
            logger.exception("Error analizando pliego %s: %s", uuid, exc)
    return resultados


def limpiar_pliegos(pdf_paths) -> None:
    """Borra los PDFs temporales (higiene de disco/PII). Acepta dict o iterable de rutas."""
    rutas = pdf_paths.values() if isinstance(pdf_paths, dict) else pdf_paths
    for path in rutas:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("No se pudo borrar %s: %s", path, exc)
