"""src.drilldown.pipeline — Orquestación del drill-down de pliegos (Fase 2, MVP Track A).

Divide el trabajo en DOS fases aisladas en memoria para no solapar Chromium con el
análisis LLM (disciplina de memoria del docs/planes/fase2-track-a.md):

  Fase A · descargar_pliegos()   → abre el navegador del portal (su LocalizadorPliego),
                                    localiza y descarga el documento base, lo cierra.
                                    Devuelve {uuid: ruta}.
  Fase B · procesar_drilldown()  → SIN navegador: por cada pliego, pre-filtro →
                                    guardia de cuota → triaje → análisis →
                                    persistencia. Un pliego a la vez.
                                    (Qué modelo atiende cada etapa lo decide
                                    `src.llm` según `LLM_PROVIDER`; desde la
                                    Fase 9 este módulo no nombra proveedor.)

El ciclo de vida de los PDFs temporales lo gestiona el caller (limpiar_pliegos()),
no la Fase B, porque un mismo proceso puede analizarse para varios clientes.
"""
import hashlib
import logging
import os
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone

from src.classifier.client_profile import ClientProfile
from src.drilldown import analysis, contexto, downloader, lectura, pdf, prefilter, quota, triage
from src.drilldown.locator import PUNTAJE_NOMBRE
from src.drilldown.locator import _download_via_playwright as _download_via_playwright  # compat
from src.portales import PORTAL_ID_DEFAULT, cargar_localizador, obtener_portal
from src.portales.contrato import ErrorPortal
from src.portales.modalidades import NO_COMPETIBLE, competibilidad_de
from src.scraper.models import ResultadoAnalisis, UsoModelo

logger = logging.getLogger(__name__)

# Tope del motivo del triaje que se guarda en `anexos` (el modelo lo pide "breve").
TRIAGE_MOTIVO_MAX = 500

# Umbral de confianza del triaje por debajo del cual se manda a revisión manual.
TRIAGE_MIN_CONFIANZA = 0.50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Contexto del cliente para los prompts ────────────────────────────────────

# Tope del bloque de señales del triaje. Cada entrada del perfil agrupa varios términos separados por comas y los
# más decisivos ("equipo informático", "UPS") suelen estar al final de la lista: un tope corto los recorta
# justo cuando hacen falta (piloto 5). El perfil de SV mide ~1.8k caracteres de inclusión y ~0.6k de exclusión.
SENALES_MAX_ENTRADAS = 60
SENALES_MAX_CARACTERES = 3000


def contexto_cliente(profile: ClientProfile, *, con_senales: bool = False) -> str:
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
    if con_senales:
        for titulo, senales in (
            ("Señales que el cliente considera dentro de su alcance", profile.catalog.inclusion_signals),
            ("Señales fuera de su alcance", profile.catalog.exclusion_signals),
        ):
            terminos = []
            vistos = set()
            # El límite incluye el rótulo; nunca partir un término ni una entidad HTML.
            for senal in senales:
                termino = contexto._texto(senal, limite=len(str(senal or "")) + 1)
                clave = termino.casefold()
                if not termino or clave in vistos:
                    continue
                vistos.add(clave)
                propuesta = f"{titulo}: {', '.join([*terminos, termino])}"
                if len(propuesta) > SENALES_MAX_CARACTERES:
                    break
                terminos.append(termino)
                if len(terminos) == SENALES_MAX_ENTRADAS:
                    break
            if terminos:
                partes.append(f"{titulo}: {', '.join(terminos)}")
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
#
# Despacha por portal (docs/arquitectura/portales.md §2.3): cada portal declara su `LocalizadorPliego`
# (`Portal.localizador`). COMPRASAL usa `locator.LocalizadorComprasal`, que envuelve sin cambios lo que
# esta función hacía antes; SICOP usa `localizador_sicop.LocalizadorSicop`.

# Formatos que la Fase B sabe analizar. Lo que no es PDF se reescribe como PDF de texto en la Fase B.
EXTENSIONES_ANALIZABLES = ("pdf", "docx", "xlsx", "txt")


def _portal_del_lote(procesos: list):
    """El único portal de `procesos` (invariante I1, §4): mezclar portales es un `ValueError`."""
    ids = {getattr(p, "portal_id", "") or PORTAL_ID_DEFAULT for p in procesos}
    if len(ids) > 1:
        raise ValueError(f"descargar_pliegos recibió procesos de varios portales: {sorted(ids)}")
    return obtener_portal(ids.pop())


def no_competible_por_modalidad(proceso) -> bool:
    """True si la modalidad del proceso no admite competencia abierta (PX, PP, RE…, §9)."""
    return competibilidad_de(
        getattr(proceso, "portal_id", "") or PORTAL_ID_DEFAULT,
        getattr(proceso, "modalidad_codigo", "") or "",
        getattr(proceso, "forma_contratacion", "") or "",
    ) == NO_COMPETIBLE


def _escribir_texto_base(texto: str, destino_dir: str) -> str:
    os.makedirs(destino_dir, exist_ok=True)
    ruta = os.path.join(destino_dir, f"{_uuid.uuid4().hex}.txt")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    return ruta


async def _documento_del_proceso(loc, proceso, cfg, max_bytes: int) -> str | None:
    """Ruta del documento base del proceso, o None.

    Orden (§7 del spike: «el nombre identifica el pliego en 19 de 33; el resto es anexo y el cartel vive
    en el JSON del detalle»):

    1. Archivos que el NOMBRE identifica como documento base (`puntaje >= PUNTAJE_NOMBRE`), en el orden
       del localizador (PDF primero).
    2. El texto que el portal publica sin adjunto (`texto_base`), como `.txt` UTF-8.
    3. El resto de archivos aceptables: otros PDF (el más grande primero) y luego DOCX o XLSX.

    COMPRASAL no tiene `texto_base`, así que su resultado es el de siempre: el mejor PDF.
    """
    docs = [d for d in (await loc.documentos(proceso) or [])
            if (d.extension or "pdf").lower() in EXTENSIONES_ANALIZABLES]
    nombrados = [d for d in docs if d.puntaje >= PUNTAJE_NOMBRE]
    resto = [d for d in docs if d.puntaje < PUNTAJE_NOMBRE]
    for doc in nombrados:
        ruta = await loc.descargar(doc, cfg.drilldown_tmp_dir, max_bytes)
        if ruta:
            return ruta
    texto = await loc.texto_base(proceso)
    if texto and texto.strip():
        return _escribir_texto_base(texto, cfg.drilldown_tmp_dir)
    for doc in resto:
        ruta = await loc.descargar(doc, cfg.drilldown_tmp_dir, max_bytes)
        if ruta:
            return ruta
    return None


async def descargar_pliegos(procesos: list, cfg) -> dict[str, str]:
    """Fase A: localiza y descarga el documento base de cada proceso. Devuelve {numero_referencia: ruta}.

    1. Todos los `procesos` deben ser de un mismo portal (si no, `ValueError`).
    2. Sin localizador declarado (o no importable) devuelve `{}`.
    3. Omite, sin gastar navegador ni LLM, los de modalidad NO competible (§9: PX, PP, RE).
    4. Abre UN localizador (un navegador) para todo el lote, con concurrencia 1, y por cada proceso:
       `completar` → `documentos` → `descargar` (ver `_documento_del_proceso`). Un fallo de un proceso
       no tumba el lote.

    La ruta puede ser `.pdf`, `.docx`, `.xlsx` o `.txt`: la Fase B se bifurca por extensión.
    """
    resultado: dict[str, str] = {}
    if not procesos:
        return resultado

    portal = _portal_del_lote(procesos)
    candidatos = []
    for proceso in procesos:
        if no_competible_por_modalidad(proceso):
            logger.info("Sin descarga para %s: modalidad no competible (%s).",
                        proceso.numero_referencia,
                        getattr(proceso, "modalidad_codigo", "") or getattr(proceso, "forma_contratacion", ""))
            continue
        candidatos.append(proceso)
    if not candidatos:
        logger.info("Fase A completa: 0/%d pliegos descargados (ninguno competible).", len(procesos))
        return resultado

    try:
        localizador = cargar_localizador(portal)
    except ErrorPortal as exc:
        logger.error("Fase A sin localizador para %s: %s", portal.portal_id, exc)
        return resultado
    if localizador is None:
        logger.info("El portal %s no tiene drill-down de pliegos.", portal.portal_id)
        return resultado

    max_bytes = cfg.pliego_max_mb * 1024 * 1024
    async with localizador(cfg, portal) as loc:
        for proceso in candidatos:
            uuid = proceso.numero_referencia
            try:
                # Lo que el listado no trae (COMPRASAL: fecha_limite; SICOP: el detalle). Solo se paga
                # para procesos ya relevantes, no para todo el listado del día. Nunca lanza.
                await loc.completar(proceso)
                path = await _documento_del_proceso(loc, proceso, cfg, max_bytes)
                if not path:
                    logger.info("Sin URL de pliego para %s", uuid)
                    continue
                resultado[uuid] = path
                logger.info("Pliego descargado para %s → %s", uuid, path)
            except Exception as exc:  # noqa: BLE001 — aislar por proceso
                logger.warning("Error descargando pliego de %s: %s", uuid, exc)

    logger.info("Fase A completa: %d/%d pliegos descargados.", len(resultado), len(procesos))
    return resultado


# ── Documentos que no son PDF (SICOP) ────────────────────────────────────────

def _texto_a_pdf(texto: str, destino: str, max_paginas: int) -> tuple[int, bool]:
    """Escribe `texto` como un PDF con capa de texto (Unicode) y devuelve `(páginas, truncado)`.

    Así la Fase B (prefiltro, triaje, análisis, citas, memoria) trata el cartel publicado como texto
    exactamente igual que un PDF nativo. Se trunca a `max_paginas` (PLIEGO_MAX_PAGES), como un PDF largo.
    """
    import html as _html

    import fitz

    # Monoespaciada a propósito: las proporcionales generan ligaduras («ﬁ») que `get_text` devuelve
    # tal cual, y el prefiltro, las citas y la memoria dejarían de encontrar «firewall».
    story = fitz.Story(html=(
        '<pre style="font-family: monospace; font-size: 9px; white-space: pre-wrap;">'
        + _html.escape(texto) + "</pre>"))
    escritor = fitz.DocumentWriter(destino)
    paginas, quedan = 0, True
    try:
        while quedan and paginas < max(1, max_paginas):
            dispositivo = escritor.begin_page(fitz.paper_rect("a4"))
            quedan, _ = story.place(fitz.Rect(40, 40, 555, 802))
            story.draw(dispositivo)
            escritor.end_page()
            paginas += 1
    finally:
        escritor.close()
    return paginas, bool(quedan)


def _ruta_pdf_derivado(ruta: str) -> str:
    return f"{ruta}.pdf"


def _preparar_documento(ruta: str, cfg) -> tuple[str, dict]:
    """Ruta de un PDF analizable para `ruta` y los anexos que describen su origen.

    Un `.pdf` (COMPRASAL, siempre) se devuelve tal cual y sin anexos. Un `.txt` (texto del detalle de
    SICOP), `.docx` o `.xlsx` se pasa a texto y se reescribe como PDF de texto junto al original
    (`<ruta>.pdf`, que `limpiar_pliegos` también borra); si ya existe —otro cliente lo analizó en esta
    corrida— se reutiliza. Levanta `ValueError` si el documento no tiene texto.
    """
    extension = os.path.splitext(ruta)[1].lower().lstrip(".")
    if extension not in {"txt", "docx", "xlsx"}:
        return ruta, {}
    anexos = {"documento_origen": "texto_detalle" if extension == "txt" else extension}
    destino = _ruta_pdf_derivado(ruta)
    if os.path.exists(destino):
        return destino, anexos
    texto = downloader.texto_de_documento(ruta)
    if not texto.strip():
        raise ValueError(f"documento {extension.upper()} sin texto legible")
    _, truncado = _texto_a_pdf(texto, destino, getattr(cfg, "pliego_max_pages", 100))
    if truncado:
        anexos["documento_truncado"] = True
    return destino, anexos


def _traspasar_monto(proceso, monto, anexos: dict | None) -> None:
    """El monto que declara el pliego llena `proceso.monto` SOLO si el portal no trajo uno (§7.3).

    COMPRASAL nunca publica monto, así que su comportamiento es el de siempre. En un portal con varias
    monedas (SICOP) la moneda es la que el modelo leyó en el pliego (`monto_estimado_moneda`) o ""; en
    uno de moneda única no se toca `proceso.moneda` (`moneda_de` ya resuelve la del portal).
    """
    if monto is None or getattr(proceso, "monto", None) is not None:
        return
    if getattr(proceso, "montos_por_moneda", None):
        return
    proceso.monto = monto
    try:
        portal = obtener_portal(getattr(proceso, "portal_id", "") or PORTAL_ID_DEFAULT)
    except Exception:  # noqa: BLE001 — portal desconocido: sin moneda
        return
    if not portal.moneda_unica:
        proceso.moneda = str((anexos or {}).get("monto_estimado_moneda") or "")


# ── Fase B: análisis (sin navegador) ─────────────────────────────────────────

def _persistir(cfg, client_id: str, proceso, res: ResultadoAnalisis,
               intentos: int | None = None) -> ResultadoAnalisis:
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
        service.guardar_estado(
            cfg, client_id, res, **({"intentos": intentos} if intentos is not None else {}))
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo persistir análisis de %s: %s", res.proceso_uuid, exc)
    return res


def _ultima_linea_con_estado(client_id: str, uuid: str, estado: str, data_dir: str):
    """Último resultado del estado solicitado, incluido el spool; solo lee."""
    from src import history
    return history.ultimo_resultado(client_id, uuid, estado, data_dir)


def _rehidratar_terminal(cfg, client_id: str, proceso, prev: str,
                         marcas_cliente: tuple[str, ...] | None) -> ResultadoAnalisis:
    """El resultado previo de un pliego en estado terminal, para que el correo no lo pierda.

    Una 2.ª corrida del mismo día salta los pliegos ya terminales; devolver un
    `ResultadoAnalisis` vacío borraba `competible`, `triaje`, la matriz y el
    `error`, y el proceso subía a «Para decidir» (caso real: 99-2027-P0016).
    Se rehidrata con lo mismo que usa `resend` (`history.load_analisis`: spool,
    JSONL y ADB, que cae solo al JSONL si la ADB no responde) y, si no trae
    nada, con el último resultado del mismo estado (JSONL o spool). Nunca cobra LLM, nunca
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
                marcas_cliente=marcas_cliente,
                nuevo=getattr(cfg, "intel_c0", "off") == "activo",
                **_modalidad(proceso))
        except Exception:  # noqa: BLE001
            logger.warning("Competibilidad no evaluable al rehidratar %s", uuid)
    previo.estado = prev
    if previo.anexos is None:
        previo.anexos = {}
    # Igual que tras el análisis: el monto del pliego es el `proceso.monto` del correo.
    _traspasar_monto(proceso, previo.monto_estimado, previo.anexos)
    return previo


def analizar_pliego_proceso(
    proceso, pdf_path: str, cfg, ctx_cliente: str, client_id: str,
    inclusion_signals: list, exclusion_signals: list,
    marcas_cliente: tuple[str, ...] | None = None,
    marca_ids_cliente: tuple[str, ...] | None = None,
    portal_id: str = PORTAL_ID_DEFAULT,
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

    # Inspección del documento (mmap, memory-safe).
    try:
        tamano = os.path.getsize(pdf_path)
    except OSError:
        tamano = 0
    # Bifurcación por extensión: lo que no es PDF (texto del detalle de SICOP, DOCX, XLSX) se reescribe
    # como PDF de texto y desde aquí sigue el mismo camino que un PDF nativo.
    try:
        pdf_path, anexos_origen = _preparar_documento(pdf_path, cfg)
    except Exception as exc:  # noqa: BLE001 — documento ilegible: ERROR_PARSEO, como un PDF corrupto
        return _persistir(cfg, client_id, proceso, ResultadoAnalisis(
            proceso_uuid=uuid, estado="ERROR_PARSEO", error=str(exc),
            tamano_bytes=tamano, generado_en=_now()))
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
        if anexos_origen:
            kw["anexos"] = {**anexos_origen, **(kw.get("anexos") or {})}
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

    activo = getattr(cfg, "intel_c0", "off") == "activo"

    def _error_triaje(motivo, uso=None):
        previo = _ultima_linea_con_estado(client_id, uuid, "ERROR_LLM", cfg.data_dir)
        locales = int((previo.anexos or {}).get("triaje_intentos", 0)) if previo else 0
        persistidos = service.intentos_de(cfg, client_id, uuid)
        intentos = max(locales, persistidos or 0) + 1
        return _persistir(cfg, client_id, proceso, _res(
            estado="REQUIERE_REVISION_MANUAL" if activo and intentos >= 3 else "ERROR_LLM",
            error=motivo, uso_triaje=uso,
            anexos={"triaje_intentos": intentos, "triaje_motivo": motivo}), intentos=intentos)

    contexto_c1 = None
    modo_c1 = getattr(cfg, "intel_c1", "off")
    if modo_c1 in {"sombra", "activo"}:
        try:
            ids_contexto = marca_ids_cliente or tuple(m.lower() for m in (marcas_cliente or ()))
            contexto_c1 = contexto.armar_contexto(
                proceso, proceso, getattr(proceso, "marcas_recomendadas", ()),
                service.precedentes_compactos(cfg, client_id, proceso),
                service.casos_similares(cfg, client_id, proceso.descripcion, ids_contexto))
        except Exception:  # noqa: BLE001 — C1 es apoyo opcional
            logger.warning("C1: contexto no disponible para %s", uuid)

    # Triaje (Haiku).
    try:
        opciones = {"portal_id": portal_id}
        if getattr(cfg, "intel_c0", "off") != "off":
            opciones["metadatos"] = {"cliente_id": client_id, "proceso_gid": uuid}
        resultado_triaje = _triaje_c1(
            cfg, ctx_cliente, texto, pdf_path, scanned, opciones,
            contexto_c1, modo_c1, client_id, uuid)
        veredicto, confianza, motivo, uso_triaje = resultado_triaje
    except triage.TriageError as exc:
        if activo:
            return _error_triaje(str(exc))
        return _persistir(cfg, client_id, proceso, _res(estado="ERROR_LLM", error=str(exc)))

    # El triaje ya se pagó, cualquiera sea el veredicto. Cobrar el consumo REAL.
    quota.cobrar(cfg, uso_triaje)
    if activo and veredicto == "ERROR_LLM":
        return _error_triaje(motivo, uso_triaje)
    datos_triaje = getattr(resultado_triaje, "datos", {})
    if "decision_sombra" in datos_triaje:
        from src.aprendizaje import service as aprendizaje
        aprendizaje.guardar_decision(cfg, client_id, uuid, "C0", variante="sombra",
                                    salida=datos_triaje["decision_sombra"])

    # El motivo viaja en `anexos` (como la competibilidad): sin él un NO-GO con
    # confianza 0.0 por parse_error no se distingue de un descarte honesto.
    anexos_triaje = {"triaje_motivo": motivo[:TRIAGE_MOTIVO_MAX]}

    marcas_citadas = getattr(resultado_triaje, "marcas_mencionadas", [])
    rescatar = activo and any(
        m.get("evidencia_valida") and any(
            analysis._contiene_marca(analysis._normalizar(m.get("marca", "")), marca)
            for marca in (marcas_cliente or ())) for m in marcas_citadas)
    if rescatar:
        anexos_triaje["triaje_original"] = veredicto
        veredicto = "GO"
    if veredicto == "NO-GO":
        return _persistir(cfg, client_id, proceso, _res(
            estado="DESCARTADO_TRIAJE", triaje="NO-GO", triaje_confianza=confianza,
            uso_triaje=uso_triaje, anexos=dict(anexos_triaje)))
    if confianza < TRIAGE_MIN_CONFIANZA and not (activo and (motivo.startswith("INCIERTO:") or rescatar)):
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

    seleccionadas = []
    paginas_leidas = []
    modo_c2 = getattr(cfg, "intel_c2", "off")
    if scanned and modo_c2 in {"sombra", "activo"}:
        try:
            seleccionadas = lectura.seleccionar_paginas(
                pdf_path, n_pages, cfg,
                {"cliente_id": client_id, "proceso_gid": uuid})
            from src.aprendizaje import service as aprendizaje
            aprendizaje.guardar_decision(
                cfg, client_id, uuid, "C2", variante=modo_c2,
                salida={"paginas": seleccionadas})
        except Exception:  # noqa: BLE001 — C2 nunca impide el análisis previo
            logger.warning("C2: selector no disponible para %s", uuid)
            seleccionadas = []

    # Análisis profundo. analizar_pliego ya devuelve ERROR_LLM si falla.
    #
    # `n_pages` no se pasa: el analizador cuenta las páginas cuando no se indican.
    opciones_analisis = {}
    if getattr(cfg, "intel_c0", "off") != "off":
        opciones_analisis = {
            "metadatos": {"cliente_id": client_id, "proceso_gid": uuid},
            "senales_triaje": datos_triaje,
        }
    opciones_analisis["portal_id"] = portal_id
    if modo_c1 == "activo" and contexto_c1:
        opciones_analisis["contexto_proceso"] = contexto_c1
    from src.llm.contrato import paginas_documento
    seleccion = (paginas_documento(pdf_path, seleccionadas)
                 if modo_c2 == "activo" and seleccionadas else nullcontext())
    with seleccion:
        res = analysis.analizar_pliego(pdf_path, cfg, ctx_cliente, uuid, formato=formato,
                                      **opciones_analisis)
    if modo_c2 == "activo" and scanned and seleccionadas:
        try:
            orden = seleccionadas + [n for n in range(1, n_pages + 1)
                                     if n not in seleccionadas]
            paginas_leidas = lectura.transcribir_paginas(pdf_path, orden, cfg)
        except Exception:  # noqa: BLE001
            logger.warning("C2: transcripción no disponible para %s", uuid)
    quota.cobrar(cfg, res.uso_analisis)  # real, también si terminó en ERROR_LLM
    res.triaje = veredicto
    res.triaje_confianza = confianza
    res.uso_triaje = uso_triaje
    res.n_paginas = n_pages
    res.tamano_bytes = tamano
    if res.anexos is None:
        res.anexos = {}
    res.anexos.update(anexos_origen)
    res.anexos.update(anexos_triaje)

    # COMPRASAL no publica el monto estimado en ningún endpoint (verificado
    # exhaustivamente contra la API real, pública y autenticada): la única
    # fuente es que el propio pliego lo declare y el LLM lo extraiga. Se
    # traspasa a `proceso.monto` —no a un campo aparte— porque ES ese mismo
    # campo el que ya recorre todo el sistema (JSONL, export, ADB vía
    # `upsert_proceso`, y el correo); `proceso` es el mismo objeto que
    # `procesar_drilldown` reutiliza después para el reporte, así que esta
    # mutación alcanza sin tocar nada más.
    #
    # Con varios portales (§7.3) la regla gana una guarda: el monto del pliego NO pisa el que el portal
    # ya trajo (SICOP lo publica en las partidas) y viaja con su moneda.
    _traspasar_monto(proceso, res.monto_estimado, res.anexos)

    # Competibilidad (P4): `analizar_pliego` ya la derivó sin portafolio; aquí
    # se reevalúa con lo que solo el pipeline sabe —la forma de contratación
    # que publica COMPRASAL y las marcas del cliente—. Es pura e idempotente,
    # y va antes de persistir para que el JSONL y `anexos` de la ADB la lleven.
    analysis.aplicar_competibilidad(
        res,
        forma_contratacion=getattr(proceso, "forma_contratacion", "") or "",
        marcas_cliente=marcas_cliente,
        nuevo=activo,
        **_modalidad(proceso, portal_id),
    )

    if getattr(cfg, "intel_c3", "off") in {"sombra", "activo"}:
        try:
            _verificar_c3(cfg, proceso, res, pdf_path, paginas_leidas, marcas_cliente, client_id,
                          marca_ids_cliente)
        except Exception:  # noqa: BLE001 — la verificación nunca impide la corrida
            logger.warning("C3: verificación no disponible para %s", uuid)

    # ── Track B · memoria institucional (no-op si enable_memoria=False) ──
    # Fuera de la ruta crítica: cualquier fallo aquí solo implica un reporte
    # sin precedentes, nunca romper el drill-down.
    if res.estado == "ANALIZADO" and getattr(cfg, "enable_memoria", False):
        try:
            # Texto completo para vectorizar: en nativos se relee el PDF entero
            # (mmap); en escaneados usamos lo que haya (el texto de triaje).
            texto_completo = texto
            if not scanned:
                if modo_c2 == "activo":
                    import fitz
                    with fitz.open(pdf_path) as documento:
                        texto_completo = "\f".join(p.get_text() for p in documento)
                else:
                    texto_completo = pdf.extract_text_first_pages(pdf_path, n_pages)
            elif paginas_leidas:
                por_pagina = {p["pagina"]: p["texto"] for p in paginas_leidas}
                texto_completo = "\f".join(por_pagina.get(n, "") for n in range(1, n_pages + 1))
            service.persistir_memoria(cfg, proceso, res, texto_completo or "", client_id)
            if paginas_leidas:
                _guardar_lectura(cfg, client_id, uuid, pdf_path, paginas_leidas)
            del texto_completo  # no retener el pliego completo en RAM
            res.precedentes = service.obtener_precedentes(cfg, proceso, res, client_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memoria institucional falló para %s: %s", uuid, exc)

    if getattr(cfg, "intel_c3", "off") in {"sombra", "activo"} and res.estado == "ANALIZADO":
        try:
            paginas = _paginas_verificables(pdf_path, res.formato, paginas_leidas)
            _aprender_pliego(cfg, client_id, uuid, res, paginas)
        except Exception:  # noqa: BLE001 — aprender no condiciona el veredicto
            logger.warning("No se pudo aprender del pliego de %s", uuid)

    return _persistir(cfg, client_id, proceso, res)


def _guardar_lectura(cfg, client_id, referencia, pdf_path, paginas):
    """La tabla por página es optativa; su fallo no deshace el análisis."""
    try:
        from src.aprendizaje.paginas import guardar_paginas
        from src.memoria import db, repo, service
        huella = hashlib.sha256()
        with open(pdf_path, "rb") as archivo:
            for bloque in iter(lambda: archivo.read(65536), b""):
                huella.update(bloque)
        portal = service._portal(cfg, client_id)
        with db.connection(cfg) as conn:
            gid = repo._proceso_gid(referencia, portal)
            guardar_paginas(conn, gid, paginas, hash_pdf=huella.hexdigest())
    except Exception:  # noqa: BLE001
        logger.warning("C2: no se pudieron persistir las páginas de %s", referencia)


def procesar_drilldown(relevantes: list, pdf_paths: dict, cfg, profile: ClientProfile) -> dict:
    """Fase B sobre todos los procesos relevantes de un cliente. Un pliego a la vez.

    Devuelve {numero_referencia: ResultadoAnalisis}.
    """
    ctx = contexto_cliente(profile, con_senales=getattr(cfg, "intel_c0", "off") != "off")
    incl = profile.catalog.inclusion_signals
    excl = profile.catalog.exclusion_signals
    marcas = marcas_del_portafolio(profile)
    marca_ids = tuple(b.id for c in profile.catalog.categories for b in c.brands)
    resultados: dict[str, ResultadoAnalisis] = {}
    for proceso in relevantes:
        uuid = proceso.numero_referencia
        path = pdf_paths.get(uuid)
        if not path:
            continue  # no se pudo descargar el pliego; ya se logueó en Fase A
        try:
            resultados[uuid] = analizar_pliego_proceso(
                proceso, path, cfg, ctx, profile.client_id, incl, excl,
                marcas_cliente=marcas, marca_ids_cliente=marca_ids, portal_id=profile.portal_id)
        except Exception as exc:  # noqa: BLE001 — aislar por pliego, no tumbar el batch
            logger.exception("Error analizando pliego %s: %s", uuid, exc)
    return resultados


def _modalidad(proceso, portal_id: str = "") -> dict:
    """`portal_id` y `modalidad_codigo` del proceso para `aplicar_competibilidad` (§9.2)."""
    return {
        "portal_id": getattr(proceso, "portal_id", "") or portal_id or PORTAL_ID_DEFAULT,
        "modalidad_codigo": getattr(proceso, "modalidad_codigo", "") or "",
    }


def limpiar_pliegos(pdf_paths) -> None:
    """Borra los PDFs temporales (higiene de disco/PII). Acepta dict o iterable de rutas.

    También borra el PDF de texto derivado de un `.txt`/`.docx`/`.xlsx` (`_preparar_documento`).
    """
    rutas = list(pdf_paths.values() if isinstance(pdf_paths, dict) else pdf_paths)
    rutas += [_ruta_pdf_derivado(r) for r in rutas if r and not str(r).lower().endswith(".pdf")]
    for path in rutas:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("No se pudo borrar %s: %s", path, exc)


def _triaje_c1(cfg, ctx, texto, pdf_path, scanned, opciones, contexto_c1, modo,
                client_id, referencia):
    """En sombra se conserva la salida base y se cobra aparte la observación."""
    def llamar(contexto_extra=None):
        kw = dict(opciones)
        if contexto_extra:
            kw["contexto_proceso"] = contexto_extra
        return triage.triage(cfg, ctx, texto=texto, pdf_path=pdf_path, scanned=scanned, **kw)

    if contexto_c1 and modo == "activo":
        return llamar(contexto_c1)
    if not (contexto_c1 and modo == "sombra" and quota.cabe(
            cfg, 2 * quota.costo_triaje_estimado_usd(
                cfg, pdf_path, scanned=scanned, texto=texto))):
        return llamar()
    with ThreadPoolExecutor(max_workers=2) as executor:
        base = executor.submit(llamar)
        sombra = executor.submit(llamar, contexto_c1)
        try:
            observado = sombra.result()
            veredicto, confianza, motivo, uso = observado
            quota.cobrar(cfg, uso)
            from src.aprendizaje import service as aprendizaje
            aprendizaje.guardar_decision(
                cfg, client_id, _gid_aprendizaje(cfg, client_id, referencia), "C1", variante="sombra",
                salida={"veredicto": veredicto, "confianza": confianza, "motivo": motivo})
        except Exception:  # noqa: BLE001 — un fallo de sombra no afecta la salida base
            logger.warning("C1: triaje sombra no disponible para %s", referencia)
        return base.result()


def _paginas_verificables(pdf_path, formato, paginas_leidas):
    if formato == "ESCANEADO":
        return {p["pagina"]: {"texto": p["texto"],
                               "fuente_texto": p.get("fuente", "vision_llm")}
                for p in paginas_leidas}
    import fitz
    with fitz.open(pdf_path) as documento:
        return {n: {"texto": pagina.get_text(), "fuente_texto": "capa_texto"}
                for n, pagina in enumerate(documento, 1)}


def _verificar_c3(cfg, proceso, res, pdf_path, paginas_leidas, marcas_cliente, client_id,
                  marca_ids_cliente=None):
    """Verifica afirmaciones materiales; la negativa sin respaldo no excluye."""
    from dataclasses import asdict

    from src import llm
    from src.aprendizaje import service as aprendizaje
    from src.classifier.marcas import contiene, normalizar
    from src.drilldown import verificacion
    from src.notifier.veredicto_marca import EQUIVALENCIAS, veredicto_marca

    paginas = _paginas_verificables(pdf_path, res.formato, paginas_leidas)
    if not paginas or res.estado != "ANALIZADO":
        return
    exigida = res.marca_exigida or {}
    candidatas = list(marcas_cliente or ())
    texto = "\n".join(p["texto"] for p in paginas.values())
    recomendadas = getattr(proceso, "marcas_recomendadas", ())
    primera = recomendadas[0].marca.lower() if recomendadas else ""
    no_competible = res.competible is False
    motivo_bloqueo = res.motivo_bloqueo
    for indice, marca in enumerate(candidatas):
        marca_id = (marca_ids_cliente[indice] if marca_ids_cliente
                    else normalizar(marca)).lower()
        previo = veredicto_marca(proceso, res, marca_id, textos_pliego=(texto,))
        aliases = EQUIVALENCIAS.get(marca_id, (marca_id, marca))
        ajena = bool(exigida and res.equivalente_admitido is not True and not any(
            contiene(exigida.get("marca", ""), alias) for alias in aliases))
        if not verificacion.requiere_verificacion(
                confirmada=previo.nivel == "confirmada", primer_lugar=marca.lower() == primera,
                match=getattr(proceso, "match_tecnico", 0) * 100,
                marca_exigida_ajena=ajena, proveedor_unico=res.proveedor_unico is True,
                no_competible=no_competible):
            continue
        modelo = llm.modelo_de(cfg, "verificacion")
        if llm.proveedor_activo(cfg) == "oci":
            modelo = (verificacion.SCOUT if res.modelo_analisis == verificacion.MAVERICK
                      else verificacion.MAVERICK)
        if not quota.cabe(cfg, quota.costo_verificacion_estimado_usd(cfg, texto, modelo=modelo)):
            continue
        negativa = ajena or no_competible
        afirmacion = {"texto": (f"La marca {marca} queda fuera: "
                               f"{motivo_bloqueo or exigida.get('marca', '')}"
                               if negativa else f"El proceso admite productos de la marca {marca}"),
                      "negativa": negativa}
        if res.proveedor_unico is True:
            afirmacion["texto"] += "; el pliego exige proveedor único"
        vista = verificacion._ConfiguracionModelo(cfg, "verificacion_presupuesto_disponible", True)
        comprobacion = verificacion.verificar_afirmacion(
            afirmacion, paginas, vista, modelo_distinto_de=res.modelo_analisis)
        uso_verificacion = UsoModelo(modelo=comprobacion.modelo or modelo)
        uso_verificacion.acumular(comprobacion.uso)
        quota.cobrar(cfg, uso_verificacion)
        salida = {k: v for k, v in asdict(comprobacion).items() if k != "uso"}
        salida.update({"marca": marca_id, "afirmacion": afirmacion})
        aprendizaje.guardar_decision(
            cfg, client_id, _gid_aprendizaje(cfg, client_id, proceso.numero_referencia), "C3",
            variante="sombra" if cfg.intel_c3 == "sombra" else "activa", salida=salida)
        if cfg.intel_c3 == "activo":
            valida = comprobacion.soporta in {"si", "no"} and not comprobacion.posible_inyeccion
            # El verificador responde a la afirmación, que puede ser negativa.
            respuesta = "incierta"
            if comprobacion.soporta == "si":
                respuesta = "no" if negativa else "si"
            elif comprobacion.soporta == "no" and not negativa and res.proveedor_unico is not True:
                respuesta = "no"
            # Negar un bloqueo o una conjunción no confirma la marca.
            res.anexos.setdefault("verificacion_c3", {})[marca_id] = {
                "respuesta": respuesta, "cita": comprobacion.cita,
                "pagina": comprobacion.pagina, "evidencia_valida": valida,
                "fuente_texto": ("vision_llm" if comprobacion.solo_vision else
                                 paginas.get(comprobacion.pagina, {}).get(
                                     "fuente_texto", "desconocida"))}
            if negativa and (not valida or comprobacion.solo_vision or comprobacion.soporta != "si"):
                res.competible = None
                res.motivo_bloqueo = "Por confirmar: verificación insuficiente del bloqueo"
                res.anexos["competibilidad"] = {"motivo": res.motivo_bloqueo}


def _aprender_pliego(cfg, client_id, referencia, res, paginas):
    """Solo cosecha citas nativas validadas y páginas persistidas en la misma transacción."""
    from src.aprendizaje import paginas as almacenamiento
    from src.aprendizaje import repo as aprendizaje_repo
    from src.aprendizaje import service as aprendizaje
    from src.drilldown.citas import validar_cita
    from src.drilldown.inyeccion import detectar_inyeccion
    from src.memoria import db, repo, service

    exigida = res.marca_exigida or {}
    pagina = exigida.get("pagina")
    fuente = paginas.get(pagina, {})
    cita = exigida.get("cita", "")
    validada = validar_cita(cita, fuente)
    if (not aprendizaje.habilitado(cfg) or not validada.ok or validada.puntuacion < .95
            or fuente.get("fuente_texto") == "vision_llm" or detectar_inyeccion(cita)):
        return
    from src.classifier.marcas import contiene
    from src.notifier.veredicto_marca import EQUIVALENCIAS
    nombre = exigida.get("marca", "").strip()
    terminos = aprendizaje.terminos_activos(cfg, client_id)
    posibles = {t["marca_id"] for t in terminos
                if t.get("tipo") in {"fuerte", "ajena"}
                and contiene(nombre, t.get("termino", ""))}
    posibles.update(m for m, aliases in EQUIVALENCIAS.items()
                    if any(contiene(nombre, alias) for alias in aliases))
    marca = next(iter(posibles)) if len(posibles) == 1 else ""
    if not marca:
        return
    portal = service._portal(cfg, client_id)
    with db.connection(cfg) as conn:
        gid = repo._proceso_gid(referencia, portal)
        if almacenamiento.guardar_paginas(conn, gid, [{
                "pagina": pagina, "texto": fuente["texto"], "fuente": fuente["fuente_texto"]}]):
            aprendizaje_repo.guardar_decision(
                conn, client_id, gid, "pliego", variante="activa", score=validada.puntuacion,
                salida={"marca_exigida": {"marca": marca, "cita": cita, "pagina": pagina}})


def _gid_aprendizaje(cfg, client_id, referencia):
    from src.memoria import repo, service
    return repo._proceso_gid(referencia, service._portal(cfg, client_id))
