"""src.memoria.service — Fachada de la memoria institucional (Fase 2, Track B).

**Contrato de aislamiento (regla de oro):** este módulo es la ÚNICA puerta de
entrada a Oracle ADB desde el resto de la aplicación. Garantiza dos cosas:

  1. Si `cfg.enable_memoria` es False → todo es **no-op** (Track A intacto).
  2. Si ADB falla (caída, wallet ausente, credenciales) → se **degrada con
     elegancia**: loguea y sigue. Un fallo de Track B NUNCA puede romper el
     drill-down de Track A, que ya está en producción.

Por eso cada función pública envuelve todo en try/except y jamás propaga.
"""
import logging

# Import directo (no perezoso como el resto de `src.memoria`): keepalive solo
# usa la stdlib, así que no arrastra oracledb ni viola el aislamiento.
from src.memoria.keepalive import EstadoKeepalive, registrar
from src.scraper.models import Precedente

logger = logging.getLogger(__name__)


def _activo(cfg) -> bool:
    return bool(getattr(cfg, "enable_memoria", False))


# ── Salud de la conexión ─────────────────────────────────────────────────────

def _adb_configurada(cfg) -> bool:
    """True si hay credenciales ADB en la config (independiente del feature flag)."""
    return bool(getattr(cfg, "adb_dsn", "")) and bool(getattr(cfg, "adb_password", ""))


def ping(cfg) -> bool:
    """Keepalive de la ADB. Devuelve True si la BD respondió. Nunca lanza.

    Envoltorio de ``ping_con_estado`` para quien solo necesita el sí/no.
    """
    return ping_con_estado(cfg).ok


def ping_con_estado(cfg) -> EstadoKeepalive:
    """Keepalive de la ADB **con umbral de fallos consecutivos**.

    Se llama en CADA corrida diaria, incluso con los feature flags apagados: la
    ADB Always Free se auto-detiene tras 7 días sin actividad y puede
    reclamarse tras ~90 días detenida.

    La guardia es ``_adb_configurada`` (hay credenciales), NO ``_activo``
    (feature flag encendido): el keepalive debe disparar mientras la ADB
    exista, aunque ``ENABLE_MEMORIA=false`` durante la validación.

    Sin credenciales no se registra nada en disco: "no hay ADB" no es un fallo
    de la ADB, y contarlo como tal dispararía alertas en cualquier despliegue
    de Track A puro.

    El ``EstadoKeepalive`` devuelto trae ``debe_alertar`` ya resuelto — ver
    ``src.memoria.keepalive`` para el porqué del umbral.
    """
    if not _adb_configurada(cfg):
        return EstadoKeepalive(ok=False, fallos_consecutivos=0, debe_alertar=False)

    detalle = ""
    try:
        from src.memoria import db
        ok = db.keepalive(cfg)
        if not ok:
            detalle = "la ADB no respondió al SELECT 1 FROM dual"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keepalive de ADB falló: %s", exc)
        ok, detalle = False, str(exc)

    return registrar(getattr(cfg, "data_dir", "data"), ok, detalle)


def cerrar(cfg) -> None:
    """Cierra el pool de conexiones ADB. Llamar al final del proceso."""
    if not _activo(cfg):
        return
    try:
        from src.memoria import db
        db.close_pool()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error cerrando el pool de ADB: %s", exc)


# ── Persistencia de memoria ──────────────────────────────────────────────────

def persistir_memoria(cfg, proceso, resultado, texto_completo: str, client_id: str) -> int:
    """Guarda metadatos + análisis y vectoriza el pliego. Devuelve nº de chunks.

    Disciplina de memoria: el texto completo puede ser grande; se vectoriza por
    lotes dentro de `vectorizar_proceso` y aquí no se retiene ninguna copia extra.
    """
    if not _activo(cfg):
        return 0
    try:
        from src.memoria import db, repo, vectorizer
        with db.connection(cfg) as conn:
            repo.upsert_proceso(conn, proceso, client_id, estado=resultado.estado)
            repo.save_analisis_db(conn, resultado)
            n = 0
            if texto_completo and texto_completo.strip():
                n = vectorizer.vectorizar_proceso(
                    conn, proceso.numero_referencia, texto_completo, cfg)
            logger.info("Memoria: %s persistido (%d chunks).", proceso.numero_referencia, n)
            return n
    except Exception as exc:  # noqa: BLE001 — degradación elegante
        logger.warning("No se pudo persistir en memoria (%s); el drill-down continúa: %s",
                       getattr(proceso, "numero_referencia", "?"), exc)
        return 0


# ── Estado del pliego (dual: ADB + JSONL) ────────────────────────────────────

def estado_de(cfg, client_id: str, proceso_id: str) -> str | None:
    """Lee el estado desde ADB; si está off o falla, cae al historial JSONL."""
    if _activo(cfg):
        try:
            from src.memoria import db, repo
            with db.connection(cfg) as conn:
                estado = repo.get_estado(conn, proceso_id)
            if estado is not None:
                return estado
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lectura de estado en ADB falló (%s); usando JSONL: %s",
                           proceso_id, exc)
    from src import history
    return history.analisis_estado(client_id, proceso_id, cfg.data_dir)


def guardar_estado(cfg, client_id: str, resultado) -> None:
    """Dual-write: JSONL (siempre, respaldo) + ADB (si la memoria está activa)."""
    from src import history
    try:
        history.save_analisis(client_id, resultado, cfg.data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo escribir el historial JSONL de %s: %s",
                     resultado.proceso_uuid, exc)

    if not _activo(cfg):
        return
    try:
        from src.memoria import db, repo
        with db.connection(cfg) as conn:
            # triaje_conf solo tiene sentido si triaje se llegó a calcular:
            # el default de ResultadoAnalisis.triaje_confianza es 0.0 (no
            # None), así que no se puede usar `or None` sin perder una
            # confianza real de exactamente 0.0 — se ata al propio triaje.
            triaje = resultado.triaje or None
            repo.set_estado(
                conn, resultado.proceso_uuid, resultado.estado,
                error=resultado.error or None,
                triaje=triaje,
                triaje_conf=resultado.triaje_confianza if triaje else None,
                formato=resultado.formato or None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo actualizar el estado en ADB (%s): %s",
                       resultado.proceso_uuid, exc)


def registrar_proceso(cfg, proceso, client_id: str, estado: str) -> None:
    """Da de alta (o refresca) la fila de ``proceso`` en ``licitaciones_metadata``.

    Existe para cerrar el hueco del embudo: hasta ahora ``upsert_proceso``
    solo se llamaba desde :func:`persistir_memoria`, es decir, únicamente
    cuando un pliego llegaba a ``ANALIZADO``. Cualquier otro desenlace
    (descartado por regex/triaje/tamaño, error de descarga o de parseo,
    revisión manual) nunca creaba fila: el embudo completo de descartes solo
    vivía en el JSONL de la VM, invisible para la ADB. Llamar a esto en TODO
    desenlace (no solo el feliz) es lo que permite que el portal muestre un
    embudo real (ver el plan del portal, §0 y §9 Fase 0).

    Idempotente (usa el MERGE de :func:`repo.upsert_proceso`) y con la misma
    degradación elegante que el resto de este módulo: si falla o si
    ``enable_memoria`` está apagado, no propaga.
    """
    if not _activo(cfg):
        return
    try:
        from src.memoria import db, repo
        with db.connection(cfg) as conn:
            repo.upsert_proceso(conn, proceso, client_id, estado=estado)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo registrar el proceso en ADB (%s): %s",
                       getattr(proceso, "numero_referencia", "?"), exc)


# ── Inteligencia inter-documento (el valor de Track B) ───────────────────────

def _pregunta_de(proceso, resultado) -> str:
    """Construye la consulta semántica a partir del pliego analizado."""
    partes = [getattr(proceso, "descripcion", "") or ""]
    matriz = getattr(resultado, "matriz", None)
    if matriz is not None and getattr(matriz, "resumen", ""):
        partes.append(matriz.resumen)
    return " ".join(p for p in partes if p).strip()[:2000]


def obtener_precedentes(cfg, proceso, resultado, misma_institucion: bool = False) -> list:
    """Busca pliegos históricos similares. Devuelve list[Precedente] (vacía si off/falla).

    Se ejecuta DESPUÉS del análisis: no está en la ruta crítica, así que un
    fallo aquí solo significa un reporte sin precedentes.
    """
    if not _activo(cfg):
        return []
    pregunta = _pregunta_de(proceso, resultado)
    if not pregunta:
        return []
    try:
        from src.memoria import db, rag
        with db.connection(cfg) as conn:
            filas = rag.buscar_precedentes(
                conn,
                pregunta,
                cfg,
                excluir_proceso_id=proceso.numero_referencia,
                institucion=(proceso.institucion if misma_institucion else None),
                limite=getattr(cfg, "precedentes_max", 3),
            )
        salida = []
        for f in filas:
            salida.append(Precedente(
                proceso_id=str(f.get("proceso_id", "")),
                institucion=str(f.get("institucion") or ""),
                titulo=str(f.get("titulo") or ""),
                fecha_pub=str(f.get("fecha_pub") or ""),
                url_comprasal=str(f.get("url_comprasal") or ""),
                extracto=(str(f.get("chunk_text") or ""))[:600],
                distancia=float(f.get("distancia") or 0.0),
            ))
        logger.info("Memoria: %d precedente(s) para %s.", len(salida), proceso.numero_referencia)
        return salida
    except Exception as exc:  # noqa: BLE001
        logger.warning("Búsqueda de precedentes falló (%s): %s",
                       getattr(proceso, "numero_referencia", "?"), exc)
        return []


def obtener_tendencias(cfg, dias: int = 90) -> list:
    """Agregado de mercado por institución/categoría. [] si off o si falla."""
    if not _activo(cfg):
        return []
    try:
        from src.memoria import db, rag
        with db.connection(cfg) as conn:
            return rag.tendencias(conn, dias)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Consulta de tendencias falló: %s", exc)
        return []
