"""src.drilldown.pipeline — Orquestación del drill-down de pliegos (Fase 2, MVP Track A).

Divide el trabajo en DOS fases aisladas en memoria para no solapar Chromium con el
análisis LLM (disciplina de memoria del docs/planes/fase2-track-a.md):

  Fase A · descargar_pliegos()   → abre Playwright, localiza y descarga los PDFs,
                                    cierra el navegador. Devuelve {uuid: ruta}.
  Fase B · procesar_drilldown()  → SIN navegador: por cada pliego, pre-filtro →
                                    guardia de cuota → triaje → análisis →
                                    persistencia. Un pliego a la vez.
                                    (Qué modelo atiende cada etapa lo decide
                                    `src.llm` según `LLM_PROVIDER`; desde la
                                    Fase 9 este módulo no nombra proveedor.)

El ciclo de vida de los PDFs temporales lo gestiona el caller (limpiar_pliegos()),
no la Fase B, porque un mismo proceso puede analizarse para varios clientes.
"""
import asyncio
import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone

from src.classifier.client_profile import ClientProfile
from src.drilldown import analysis, downloader, pdf, prefilter, quota, triage
from src.drilldown.locator import find_pliego_pdf_url, obtener_fecha_limite
from src.scraper.models import ResultadoAnalisis

logger = logging.getLogger(__name__)

# Tope del motivo del triaje que se guarda en `anexos` (el modelo lo pide "breve").
TRIAGE_MOTIVO_MAX = 500

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


def marcas_del_portafolio(profile: ClientProfile) -> tuple[str, ...]:
    """Nombres de marca del catálogo del cliente (para `marca_especifica`)."""
    return tuple(
        b.name for c in profile.catalog.categories for b in c.brands if b.name
    )


def descartados_por_triaje(resultados: dict) -> set[str]:
    """`numero_referencia` de los procesos que el triaje descartó (NO-GO).

    Es la vista consultable de D6: el pipeline NO toca `es_relevante` del
    proceso al descartarlo, así que quien arma el correo pregunta aquí (o a
    `ResultadoAnalisis.descartado_por_triaje`) en vez de inferirlo.
    """
    return {
        uuid for uuid, res in (resultados or {}).items()
        if res is not None and res.descartado_por_triaje
    }


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
                    # La API de listado no trae fecha_limite (verificado contra
                    # el endpoint real, ver locator.py); esta es la única
                    # oportunidad de conseguirla sin abrir un segundo navegador,
                    # y solo se paga para procesos ya relevantes, no para todo
                    # el listado del día. Nunca lanza: un fallo aquí no debe
                    # impedir la descarga del pliego, que es lo crítico.
                    if getattr(proceso, "fecha_limite", None) is None:
                        proceso.fecha_limite = await obtener_fecha_limite(page, proceso, cfg)

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

def _persistir(cfg, client_id: str, proceso, res: ResultadoAnalisis) -> ResultadoAnalisis:
    """Persiste el estado. Track B (si está activo) hace dual-write ADB + JSONL.

    Para el desenlace ``ANALIZADO`` la fila de ``licitaciones_metadata`` ya
    la creó :func:`service.persistir_memoria` (llamada antes que esta
    función, más abajo en ``analizar_pliego_proceso``); para CUALQUIER otro
    desenlace (descartes, errores, revisión manual) esta es la única
    oportunidad de que el proceso quede registrado en la ADB — sin esto, el
    embudo completo de descartes solo existía en el JSONL de la VM.
    """
    from src.memoria import service
    try:
        if res.estado != "ANALIZADO":
            service.registrar_proceso(cfg, proceso, client_id, res.estado)
        service.guardar_estado(cfg, client_id, res)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo persistir análisis de %s: %s", res.proceso_uuid, exc)
    return res


def _ultima_linea_con_estado(client_id: str, uuid: str, estado: str, data_dir: str):
    """Última línea del JSONL de `uuid` cuyo estado es `estado`, o None. Solo lee; tolera todo.

    Cubre lo que `history.load_analisis` no devuelve porque no lleva matriz:
    un REQUIERE_REVISION_MANUAL con su `error`, o la marca NOTIFICADO (que
    `main._marcar_notificados` escribe con `competible`/`motivo_bloqueo`).
    """
    from src import history
    try:
        path = history._analisis_path(client_id, data_dir)
        if not os.path.exists(path):
            return None
        ultima = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("proceso_uuid") == uuid and e.get("estado") == estado:
                    ultima = e
        return ResultadoAnalisis.desde_dict(ultima) if ultima else None
    except Exception as exc:  # noqa: BLE001 — sin historial legible, el vacío de siempre
        logger.warning("No se pudo leer el historial JSONL de %s: %s", uuid, exc)
        return None


def _rehidratar_terminal(cfg, client_id: str, proceso, prev: str,
                         marcas_cliente: tuple[str, ...] | None) -> ResultadoAnalisis:
    """El resultado previo de un pliego en estado terminal, para que el correo no lo pierda.

    Una 2.ª corrida del mismo día salta los pliegos ya terminales; devolver un
    `ResultadoAnalisis` vacío borraba `competible`, `triaje`, la matriz y el
    `error`, y el proceso subía a «Para decidir» (caso real: 99-2027-P0016).
    Se rehidrata con lo mismo que usa `resend` (`history.load_analisis`: spool,
    JSONL y ADB, que cae solo al JSONL si la ADB no responde) y, si no trae
    nada, con la última línea JSONL del mismo estado. Nunca cobra LLM, nunca
    persiste y nunca cambia el estado: el resultado sale con `estado=prev`, así
    que `main._marcar_notificados` (solo ANALIZADO) lo sigue ignorando. Sin
    historial, el vacío de antes.
    """
    from src import history
    uuid = proceso.numero_referencia
    previo = None
    try:
        previo = history.load_analisis(
            client_id, [uuid], cfg.data_dir, marcas_cliente=marcas_cliente).get(uuid)
    except Exception as exc:  # noqa: BLE001 — la rehidratación nunca tumba la corrida
        logger.warning("No se pudo rehidratar el análisis previo de %s: %s", uuid, exc)
    if previo is None:
        previo = _ultima_linea_con_estado(client_id, uuid, prev, cfg.data_dir)
    if previo is None:
        return ResultadoAnalisis(proceso_uuid=uuid, estado=prev, generado_en=_now())

    # Una línea anterior a P4 (o una fila de la ADB ya en NOTIFICADO) trae la
    # matriz pero no `competible`: se deriva aquí, puro y sin LLM, igual que
    # en el análisis (`aplicar_competibilidad` solo actúa sobre ANALIZADO).
    if previo.competible is None and previo.matriz is not None and previo.matriz.items:
        previo.estado = "ANALIZADO"
        try:
            analysis.aplicar_competibilidad(
                previo, forma_contratacion=getattr(proceso, "forma_contratacion", "") or "",
                marcas_cliente=marcas_cliente)
        except Exception:  # noqa: BLE001
            logger.warning("Competibilidad no evaluable al rehidratar %s", uuid)
    previo.estado = prev
    if previo.anexos is None:
        previo.anexos = {}
    # Igual que tras el análisis: el monto del pliego es el `proceso.monto` del correo.
    if getattr(proceso, "monto", None) is None and previo.monto_estimado is not None:
        proceso.monto = previo.monto_estimado
    return previo


def analizar_pliego_proceso(
    proceso, pdf_path: str, cfg, ctx_cliente: str, client_id: str,
    inclusion_signals: list, exclusion_signals: list,
    marcas_cliente: tuple[str, ...] | None = None,
) -> ResultadoAnalisis:
    """Fase B para UN pliego. No borra el PDF (lo hace el caller vía limpiar_pliegos).

    `marcas_cliente` (opcional) es el portafolio del cliente: con él, una
    «contratación directa por marca específica» se resuelve a competible o no;
    sin él queda en None (ver `analysis.evaluar_competibilidad`).
    """
    from src import history
    from src.memoria import service

    uuid = proceso.numero_referencia

    # Idempotencia / reanudación: si ya está en estado terminal, saltar.
    # Con Track B activo el estado se lee de ADB (con fallback a JSONL).
    prev = service.estado_de(cfg, client_id, uuid)
    if history.is_estado_terminal(prev):
        logger.info("Saltando %s: ya en estado terminal %s", uuid, prev)
        return _rehidratar_terminal(cfg, client_id, proceso, prev, marcas_cliente)

    # Inspección del PDF (mmap, memory-safe).
    try:
        tamano = os.path.getsize(pdf_path)
    except OSError:
        tamano = 0
    try:
        n_pages = pdf.page_count(pdf_path)
        scanned = pdf.is_scanned(pdf_path)
    except pdf.PdfCorruptoError as exc:
        return _persistir(cfg, client_id, proceso, ResultadoAnalisis(
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
        return _persistir(cfg, client_id, proceso, _res(
            estado="REQUIERE_REVISION_MANUAL",
            error=f"{n_pages} páginas > límite {cfg.pliego_max_pages}"))

    # Pre-filtro RegEx (solo pliegos nativos; los escaneados se saltan para evitar falso negativo).
    texto = None
    if not scanned:
        texto = pdf.extract_text_first_pages(pdf_path, cfg.triage_pages)
        if not prefilter.passes_prefilter(texto, inclusion_signals, exclusion_signals):
            return _persistir(cfg, client_id, proceso, _res(estado="DESCARTADO_REGEX"))

    # ── Guardia de gasto · triaje ────────────────────────────────────────────
    # Se cobra por etapa, no una sola vez al final: el triaje se paga aunque
    # después diga NO-GO. La guardia vieja estimaba antes del triaje pero
    # reservaba solo tras un GO, así que todo el gasto de triaje —el mayor del
    # pipeline en un corpus escaneado— nunca entraba al libro.
    if not quota.cabe(cfg, quota.costo_triaje_estimado_usd(
            cfg, pdf_path, scanned=scanned, texto=texto)):
        return _persistir(cfg, client_id, proceso, _res(
            estado="REQUIERE_REVISION_MANUAL",
            error="presupuesto diario agotado antes del triaje"))

    # Triaje (Haiku).
    try:
        veredicto, confianza, motivo, uso_triaje = triage.triage(
            cfg, ctx_cliente, texto=texto, pdf_path=pdf_path, scanned=scanned)
    except triage.TriageError as exc:
        return _persistir(cfg, client_id, proceso, _res(estado="ERROR_LLM", error=str(exc)))

    # El triaje ya se pagó, cualquiera sea el veredicto. Cobrar el consumo REAL.
    quota.cobrar(cfg, uso_triaje)

    # El motivo viaja en `anexos` (como la competibilidad): sin él un NO-GO con
    # confianza 0.0 por parse_error no se distingue de un descarte honesto.
    anexos_triaje = {"triaje_motivo": motivo[:TRIAGE_MOTIVO_MAX]}

    if veredicto == "NO-GO":
        return _persistir(cfg, client_id, proceso, _res(
            estado="DESCARTADO_TRIAJE", triaje="NO-GO", triaje_confianza=confianza,
            uso_triaje=uso_triaje, anexos=dict(anexos_triaje)))
    if confianza < TRIAGE_MIN_CONFIANZA:
        return _persistir(cfg, client_id, proceso, _res(
            estado="REQUIERE_REVISION_MANUAL", triaje=veredicto,
            triaje_confianza=confianza, uso_triaje=uso_triaje,
            anexos=dict(anexos_triaje),
            error=f"confianza de triaje baja ({confianza:.2f}): {motivo}"))

    # ── Guardia de gasto · análisis ──────────────────────────────────────────
    # Es la llamada cara. Si no cabe, el pliego queda para revisión manual con
    # el triaje ya cobrado: no se pierde el gasto hecho ni se oculta.
    #
    # `pdf_path` y `scanned` van porque con LLM_PROVIDER=oci el costo del
    # análisis ya no depende solo de cuántas páginas tiene el pliego, sino de
    # cómo lo traduce el adaptador (capa de texto si es nativo, N imágenes si
    # está escaneado). Con Anthropic la estimación es la de siempre.
    if not quota.cabe(cfg, quota.costo_analisis_estimado_usd(
            cfg, n_pages, pdf_path=pdf_path, escaneado=scanned)):
        return _persistir(cfg, client_id, proceso, _res(
            estado="REQUIERE_REVISION_MANUAL", triaje=veredicto,
            triaje_confianza=confianza, uso_triaje=uso_triaje,
            anexos=dict(anexos_triaje),
            error="presupuesto diario agotado antes del análisis"))

    # Análisis profundo. analizar_pliego ya devuelve ERROR_LLM si falla.
    #
    # `n_pages` NO se pasa, aunque `analizar_pliego` acepta `n_paginas`: los
    # dobles de `tests/test_pipeline.py` tienen firma fija y cualquier kwarg
    # nuevo los rompe con TypeError (verificado), y ese archivo es contrato de
    # regresión de la Fase 9 — tiene que pasar sin cambios. `analizar_pliego`
    # cuenta las páginas por su cuenta cuando nadie se las dice, así que no se
    # pierde nada más que un `fitz.open` de microsegundos.
    res = analysis.analizar_pliego(pdf_path, cfg, ctx_cliente, uuid, formato=formato)
    quota.cobrar(cfg, res.uso_analisis)  # real, también si terminó en ERROR_LLM
    res.triaje = veredicto
    res.triaje_confianza = confianza
    res.uso_triaje = uso_triaje
    res.n_paginas = n_pages
    res.tamano_bytes = tamano
    if res.anexos is None:
        res.anexos = {}
    res.anexos.update(anexos_triaje)

    # COMPRASAL no publica el monto estimado en ningún endpoint (verificado
    # exhaustivamente contra la API real, pública y autenticada): la única
    # fuente es que el propio pliego lo declare y el LLM lo extraiga. Se
    # traspasa a `proceso.monto` —no a un campo aparte— porque ES ese mismo
    # campo el que ya recorre todo el sistema (JSONL, export, ADB vía
    # `upsert_proceso`, y el correo); `proceso` es el mismo objeto que
    # `procesar_drilldown` reutiliza después para el reporte, así que esta
    # mutación alcanza sin tocar nada más.
    if res.monto_estimado is not None:
        proceso.monto = res.monto_estimado

    # Competibilidad (P4): `analizar_pliego` ya la derivó sin portafolio; aquí
    # se reevalúa con lo que solo el pipeline sabe —la forma de contratación
    # que publica COMPRASAL y las marcas del cliente—. Es pura e idempotente,
    # y va antes de persistir para que el JSONL y `anexos` de la ADB la lleven.
    analysis.aplicar_competibilidad(
        res,
        forma_contratacion=getattr(proceso, "forma_contratacion", "") or "",
        marcas_cliente=marcas_cliente,
    )

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
            res.precedentes = service.obtener_precedentes(cfg, proceso, res, client_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memoria institucional falló para %s: %s", uuid, exc)

    return _persistir(cfg, client_id, proceso, res)


def procesar_drilldown(relevantes: list, pdf_paths: dict, cfg, profile: ClientProfile) -> dict:
    """Fase B sobre todos los procesos relevantes de un cliente. Un pliego a la vez.

    Devuelve {numero_referencia: ResultadoAnalisis}.
    """
    ctx = contexto_cliente(profile)
    incl = profile.catalog.inclusion_signals
    excl = profile.catalog.exclusion_signals
    marcas = marcas_del_portafolio(profile)
    resultados: dict[str, ResultadoAnalisis] = {}
    for proceso in relevantes:
        uuid = proceso.numero_referencia
        path = pdf_paths.get(uuid)
        if not path:
            continue  # no se pudo descargar el pliego; ya se logueó en Fase A
        try:
            resultados[uuid] = analizar_pliego_proceso(
                proceso, path, cfg, ctx, profile.client_id, incl, excl,
                marcas_cliente=marcas)
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
