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
from src.scraper.models import Precedente, ProcesoClasificado

logger = logging.getLogger(__name__)


def _activo(cfg) -> bool:
    return bool(getattr(cfg, "enable_memoria", False))


def _portal(cfg, client_id: str) -> str:
    """Portal del cliente (su YAML); el `proceso_gid` v2 se arma con él."""
    from src.portales import portal_de_cliente
    return portal_de_cliente(getattr(cfg, "clients_dir", ""), client_id)


def _activo_v2(cfg) -> bool:
    """Doble escritura del esquema v2 (plan del portal §3.3 Fase 2).

    Depende de ``_activo`` (sin ADB no hay dónde escribir) Y de su propio
    flag (``ENABLE_ESQUEMA_V2``): apagarlo vuelve a v1 puro sin tocar
    ``ENABLE_MEMORIA``, que gobierna Track B en general.
    """
    return _activo(cfg) and bool(getattr(cfg, "enable_esquema_v2", False))


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
            if _activo_v2(cfg):
                try:
                    repo.upsert_proceso_v2(conn, proceso, client_id, estado=resultado.estado,
                                           portal_id=_portal(cfg, client_id))
                    repo.save_analisis_v2(conn, resultado, client_id,
                                          portal_id=_portal(cfg, client_id))
                except Exception as exc:  # noqa: BLE001 — v2 nunca revierte v1
                    logger.warning(
                        "Espejo v2 de persistir_memoria falló (%s); v1 queda intacto: %s",
                        proceso.numero_referencia, exc,
                    )
            n = 0
            if texto_completo and texto_completo.strip():
                n = vectorizer.vectorizar_proceso(
                    conn, proceso.numero_referencia, texto_completo, cfg)
                if _activo_v2(cfg):
                    try:
                        vectorizer.vectorizar_proceso_v2(
                            conn, proceso.numero_referencia, texto_completo, cfg,
                            portal_id=_portal(cfg, client_id))
                    except Exception as exc:  # noqa: BLE001 — v2 nunca revierte v1
                        logger.warning(
                            "Espejo v2 de vectorizar_proceso falló (%s); v1 queda intacto: %s",
                            proceso.numero_referencia, exc,
                        )
            logger.info("Memoria: %s persistido (%d chunks).", proceso.numero_referencia, n)
            return n
    except Exception as exc:  # noqa: BLE001 — degradación elegante
        logger.warning("No se pudo persistir en memoria (%s); el drill-down continúa: %s",
                       getattr(proceso, "numero_referencia", "?"), exc)
        return 0


# ── Estado del pliego (dual: ADB + JSONL) ────────────────────────────────────

def estado_de(cfg, client_id: str, proceso_id: str) -> str | None:
    """Lee el estado desde ADB; si está off o falla, cae al historial JSONL.

    Fase "switch" del esquema v2 (plan del portal §3.7 punto 4): con
    ``enable_esquema_v2`` activo, ``cliente_proceso`` (v2) es la fuente de
    verdad, no ``licitaciones_metadata`` (v1) — un fallo de la lectura v2
    NO degrada a v1 (eso reintroduciría la fuga entre clientes que esta fase
    cierra: v1 puede tener el estado de OTRO cliente pisado encima), salta
    directo al fallback de disponibilidad que ya existía, el JSONL.
    """
    if _activo_v2(cfg):
        try:
            from src.memoria import db, repo
            with db.connection(cfg) as conn:
                estado = repo.get_estado_v2(conn, client_id, proceso_id,
                                            portal_id=_portal(cfg, client_id))
            if estado is not None:
                return estado
        except Exception as exc:  # noqa: BLE001 — nunca cae a v1, ver docstring
            logger.error("Lectura ESQUEMA V2 de estado falló (%s/%s); NO se degrada a v1, "
                         "usando JSONL: %s", client_id, proceso_id, exc)
    elif _activo(cfg):
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
            if _activo_v2(cfg):
                try:
                    repo.set_estado_v2(
                        conn, resultado.proceso_uuid, client_id, resultado.estado,
                        error=resultado.error or None,
                        triaje=triaje,
                        triaje_conf=resultado.triaje_confianza if triaje else None,
                        formato=resultado.formato or None,
                        portal_id=_portal(cfg, client_id),
                    )
                except Exception as exc:  # noqa: BLE001 — v2 nunca revierte v1
                    logger.warning(
                        "Espejo v2 de guardar_estado falló (%s); v1 queda intacto: %s",
                        resultado.proceso_uuid, exc,
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
            if _activo_v2(cfg):
                try:
                    repo.upsert_proceso_v2(conn, proceso, client_id, estado=estado,
                                           portal_id=_portal(cfg, client_id))
                except Exception as exc:  # noqa: BLE001 — v2 nunca revierte v1
                    logger.warning(
                        "Espejo v2 de registrar_proceso falló (%s); v1 queda intacto: %s",
                        getattr(proceso, "numero_referencia", "?"), exc,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo registrar el proceso en ADB (%s): %s",
                       getattr(proceso, "numero_referencia", "?"), exc)


def sin_clasificar(proceso) -> bool:
    """True si ``proceso`` es un «sin clasificar»: el LLM nunca lo evaluó.

    ``classifier/engine.py`` devuelve ``ProcesoClasificado.from_proceso(p)`` tal
    cual cuando el lote falla (JSON inválido, error de API) o cuando la respuesta
    no trae el índice del proceso. Esa instancia queda con los defaults: scores en
    0.0, ``razon`` y ``categoria_detectada`` vacías y ``es_relevante=False``. Una
    clasificación real, en cambio, siempre trae ``categoria_detectada`` (el parser
    pone ``"otro"`` si el modelo la omite). Criterio exacto: se omite si
    ``razon`` vacía **y** ``match_tecnico == match_categoria == factibilidad == 0``
    **y** ``categoria_detectada`` vacía (y, por prudencia, no es relevante: nada
    marcado relevante se descarta nunca).
    """
    # El criterio vive en repo: `upsert_proceso_v2` lo usa para decidir si `[]` sobrescribe las marcas.
    from src.memoria import repo
    return repo.proceso_sin_clasificar(proceso)


def registrar_clasificados(cfg, client_id: str, clasificados: list[ProcesoClasificado]) -> dict:
    """Persiste en ``cliente_proceso`` (esquema v2) TODO lo clasificado, relevante o no (E11.1).

    Hasta E11.1 solo llegaban a la ADB los procesos que entraban al drilldown
    (los relevantes con pliego): ~15 de ~79 al día, así que el portal no podía
    mostrar descartados ni calcular la relevancia real. Este es el **único** punto
    de persistencia en lote, llamado justo después de ``classify_procesos``.

    - **Flags:** sin ``ENABLE_MEMORIA`` + ``ENABLE_ESQUEMA_V2`` no hace nada (ni
      abre conexión) y devuelve ``guardados=0``.
    - **Lote:** UNA conexión y UN commit (el de ``db.connection``) con
      ``repo.upsert_proceso_v2`` por proceso. Una fila inválida (score fuera de rango, texto demasiado
      largo, referencia vacía) se cuenta en ``fallidos`` y se sigue: Oracle revierte solo esa sentencia.
      Un fallo de conexión o del commit deshace el lote y devuelve ``error=True``.
    - **Estado:** los nuevos entran como ``DESCUBIERTO`` (relevantes o no; los no
      relevantes quedan con ``es_relevante=0``). No se usa ``DESCARTADO_REGEX``:
      ese estado es del prefiltro del drilldown. El MERGE no toca el ``estado``
      de una fila existente, así que lo ya avanzado (DESCARGADO, ANALIZADO…) no
      retrocede, y no baja ``es_relevante`` de una fila ANALIZADO/NOTIFICADO.
    - **Idempotente:** el MERGE por ``(cliente_id, proceso_gid)`` actualiza; dos
      corridas del mismo día no duplican.
    - **«Sin clasificar» se omite** (criterio en :func:`sin_clasificar`): un fallo
      del LLM no debe quedar guardado como «no relevante».
    - **Nunca rompe la corrida:** cualquier excepción se registra y se devuelve
      ``error=True`` (mismo contrato que ``src/feedback/service.py``).

    :returns: ``{"guardados": n, "omitidos": n, "fallidos": n, "error": bool}``. Con error,
        ``guardados`` es 0 (el lote se revirtió).
    """
    resultado = {"guardados": 0, "omitidos": 0, "fallidos": 0, "error": False}
    try:
        if not _activo_v2(cfg) or not clasificados:
            return resultado

        a_guardar = [p for p in clasificados if not sin_clasificar(p)]
        resultado["omitidos"] = len(clasificados) - len(a_guardar)
        if resultado["omitidos"]:
            logger.warning("[%s] %d proceso(s) sin clasificar (fallo del LLM): no se persisten.",
                           client_id, resultado["omitidos"])
        if not a_guardar:
            return resultado

        from src.memoria import db, repo
        portal_id = _portal(cfg, client_id)
        with db.connection(cfg) as conn:
            for proceso in a_guardar:
                try:
                    repo.upsert_proceso_v2(conn, proceso, client_id, portal_id=portal_id)
                    resultado["guardados"] += 1
                except Exception as exc:  # noqa: BLE001 — una fila inválida no tumba el lote del día
                    # Oracle revierte solo la sentencia que falló; el resto del lote sigue en la transacción.
                    resultado["fallidos"] += 1
                    logger.warning("[%s] No se pudo persistir %s: %s", client_id,
                                   getattr(proceso, "numero_referencia", "?"), exc)
        logger.info("[%s] Clasificados persistidos en la ADB: %d (omitidos: %d, fallidos: %d).",
                    client_id, resultado["guardados"], resultado["omitidos"], resultado["fallidos"])
    except Exception:  # noqa: BLE001 — nunca rompe la corrida
        logger.exception("[%s] No se pudieron persistir los clasificados en la ADB; la corrida "
                         "sigue.", client_id)
        resultado["guardados"] = 0
        resultado["error"] = True
    return resultado


def actualizar_marcas(cfg, client_id: str, numero_referencia: str, marcas) -> bool:
    """Reemplaza las marcas (con su veredicto) de un proceso ya persistido. Nunca rompe la corrida.

    Fachada tolerante de :func:`repo.actualizar_marcas_v2`: sin ``ENABLE_MEMORIA`` + ``ENABLE_ESQUEMA_V2`` no
    abre conexión; una ADB caída (DSN inválido, red, auto-detenida) se registra y devuelve ``False``.
    ``marcas=[]`` deja el proceso sin marcas («clasificado sin marcas»).

    :returns: ``True`` si se actualizó al menos una fila.
    """
    try:
        if not _activo_v2(cfg):
            return False
        from src.memoria import db, repo
        gid = repo._proceso_gid(numero_referencia, _portal(cfg, client_id))
        with db.connection(cfg) as conn:
            return repo.actualizar_marcas_v2(conn, client_id, gid, marcas) > 0
    except Exception as exc:  # noqa: BLE001 — la corrida nunca falla por la ADB
        logger.warning("[%s] No se pudieron actualizar las marcas de %s en la ADB: %s",
                       client_id, numero_referencia, type(exc).__name__)
        logger.debug("detalle", exc_info=True)
        return False


# ── Inteligencia inter-documento (el valor de Track B) ───────────────────────

def _pregunta_de(proceso, resultado) -> str:
    """Construye la consulta semántica a partir del pliego analizado."""
    partes = [getattr(proceso, "descripcion", "") or ""]
    matriz = getattr(resultado, "matriz", None)
    if matriz is not None and getattr(matriz, "resumen", ""):
        partes.append(matriz.resumen)
    return " ".join(p for p in partes if p).strip()[:2000]


def obtener_precedentes(cfg, proceso, resultado, client_id: str, misma_institucion: bool = False) -> list:
    """Busca pliegos históricos similares. Devuelve list[Precedente] (vacía si off/falla).

    Se ejecuta DESPUÉS del análisis: no está en la ruta crítica, así que un
    fallo aquí solo significa un reporte sin precedentes.

    ``client_id`` es obligatorio: se reenvía a ``rag.buscar_precedentes``
    (v1) o ``rag.buscar_precedentes_v2`` (v2, fase "switch" — plan del
    portal §3.7 punto 4) para que la búsqueda quede acotada al propio
    cliente. Con ``enable_esquema_v2`` activo, v2 es la fuente de verdad: un
    error de la búsqueda v2 NO cae a v1 (ver el docstring de
    ``rag.buscar_precedentes_v2`` — degradar a v1 reintroduciría la fuga que
    esta fase cierra), el resultado queda en ``[]`` como cualquier otro
    fallo de Track B.
    """
    if not _activo(cfg):
        return []
    pregunta = _pregunta_de(proceso, resultado)
    if not pregunta:
        return []
    try:
        from src.memoria import db, rag
        with db.connection(cfg) as conn:
            if _activo_v2(cfg):
                try:
                    filas = rag.buscar_precedentes_v2(
                        conn,
                        pregunta,
                        cfg,
                        cliente_id=client_id,
                        excluir_proceso_id=proceso.numero_referencia,
                        institucion=(proceso.institucion if misma_institucion else None),
                        limite=getattr(cfg, "precedentes_max", 3),
                        portal_id=_portal(cfg, client_id),
                    )
                except Exception as exc:  # noqa: BLE001 — nunca cae a v1, ver docstring
                    logger.error(
                        "Lectura ESQUEMA V2 de precedentes falló (%s); NO se degrada a v1: %s",
                        proceso.numero_referencia, exc,
                    )
                    return []
            else:
                filas = rag.buscar_precedentes(
                    conn,
                    pregunta,
                    cfg,
                    cliente_id=client_id,
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


def obtener_tendencias(cfg, cliente_id: str, dias: int = 90) -> list:
    """Agregado por institución/categoría, DE ESTE CLIENTE. [] si off o si falla.

    Cambio de firma respecto a versiones anteriores (``cliente_id`` ahora
    obligatorio, fase "switch" — plan del portal §3.7 punto 4): sin llamador
    real hoy en ``src/``, así que el costo de romper compatibilidad es cero.
    Con ``enable_esquema_v2`` activo, v2 es la fuente de verdad — un error
    de la consulta v2 no cae a v1 (ver el docstring de ``rag.tendencias_v2``
    sobre por qué el agregado global de v1 ya no es una opción correcta),
    degrada a ``[]`` como cualquier otro fallo de Track B.
    """
    if not _activo(cfg):
        return []
    try:
        from src.memoria import db, rag
        with db.connection(cfg) as conn:
            if _activo_v2(cfg):
                try:
                    return rag.tendencias_v2(conn, cliente_id, dias)
                except Exception as exc:  # noqa: BLE001 — nunca cae a v1, ver docstring
                    logger.error(
                        "Lectura ESQUEMA V2 de tendencias falló (%s); NO se degrada a v1: %s",
                        cliente_id, exc,
                    )
                    return []
            return rag.tendencias(conn, dias)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Consulta de tendencias falló: %s", exc)
        return []


# ── Historial de corridas / circuit breaker real ─────────────────────────────

def registrar_ejecucion(
    cfg,
    run_timestamp,
    success: bool,
    procesos_revisados: int = 0,
    clientes_procesados: int = 0,
    error: str | None = None,
) -> None:
    """Deja constancia de una corrida en la ADB. No-op si Track B está apagado.

    Nunca lanza: perder esta fila no puede costar la corrida (mismo contrato
    que el resto de este módulo). `version` sale de `cfg.app_version`, igual
    que ya hace `main._save_last_run` para el JSON.
    """
    if not _activo(cfg):
        return
    try:
        from src.memoria import db, repo
        with db.connection(cfg) as conn:
            repo.insertar_ejecucion(
                conn, run_timestamp, success,
                procesos_revisados=procesos_revisados,
                clientes_procesados=clientes_procesados,
                version=str(getattr(cfg, "app_version", "") or ""),
                error=error,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo registrar la ejecución en la ADB: %s", exc)


def fallos_consecutivos(cfg) -> int:
    """Fallos consecutivos según el historial persistido, no una variable de
    proceso — ver el docstring de `repo.contar_fallos_consecutivos`.

    Devuelve 0 si Track B está apagado o si la ADB falla: en ese caso el
    circuit breaker sigue funcionando exactamente como hoy (contador en
    memoria de `main.py`), ni mejor ni peor — esta función solo puede sumar
    información, nunca quitarla.
    """
    if not _activo(cfg):
        return 0
    try:
        from src.memoria import db, repo
        with db.connection(cfg) as conn:
            return repo.contar_fallos_consecutivos(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo leer el historial de ejecuciones: %s", exc)
        return 0


def precedentes_compactos(cfg, client_id, proceso, k=3) -> list[dict]:
    """Reutiliza la búsqueda sin exponer títulos ni extractos del pliego histórico."""
    if not _activo(cfg) or not getattr(cfg, "adb_dsn", ""):
        return []
    try:
        limite = min(3, max(0, int(k)))
        if not limite:
            return []
        filas = obtener_precedentes(cfg, proceso, None, client_id)
        # Precedente no incluye un desenlace: no inferirlo del extracto.
        return [{"institucion": f.institucion,
                 "desenlace": getattr(f, "desenlace", "desconocido"),
                 "distancia": f.distancia} for f in filas[:limite]]
    except Exception as exc:  # noqa: BLE001 — contexto opcional
        logger.warning("Precedentes compactos no disponibles (%s)", type(exc).__name__)
        return []


def casos_similares(cfg, client_id, titulo, marca_ids, k=3) -> list[dict]:
    """Hasta tres ejemplos resueltos, aislados por cliente y marca, sin extractos."""
    try:
        from src.aprendizaje import service as aprendizaje
        from src.aprendizaje import vecinos
        if not aprendizaje.habilitado(cfg):
            return []
        limite = min(3, max(0, int(k)))
        if not limite or not marca_ids:
            return []
        from src.memoria import db
        filas = []
        with db.connection(cfg) as conn:
            for marca in dict.fromkeys(marca_ids):
                for fila in vecinos.consultar_vecinos(conn, client_id, marca, titulo):
                    if fila.get("etiqueta") in {"positivo", "negativo"}:
                        filas.append({**fila, "marca_id": marca})
        filas.sort(key=lambda f: float(f.get("distancia", 2)))
        return [{campo: f.get(campo) for campo in
                 ("titulo_texto", "etiqueta", "marca_id")} for f in filas[:limite]]
    except Exception as exc:  # noqa: BLE001 — la corrida no depende de la ADB
        logger.warning("Casos similares no disponibles (%s)", type(exc).__name__)
        return []
