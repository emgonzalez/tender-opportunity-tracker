"""
tender-opportunity-tracker
Entry point principal: orquesta login, scraping, clasificación y reportes.

Una sola instancia sirve a múltiples clientes y a varios portales de compras
(docs/arquitectura/portales.md §5):
  Para cada portal integrado con clientes activos, en el orden del registro (COMPRASAL primero):
    1. Si el portal exige login (COMPRASAL): credenciales y sesión válida
    2. Extrae los procesos una sola vez, con el extractor del portal (`cargar_extractor`)
    3. Para cada cliente del portal: clasifica con sus reglas y envía su correo
  Un portal que falla no detiene a los demás: tiene su propio estado y breaker
  (`data/portal_estado.json`). El breaker global queda para los errores críticos.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src import history, portales
from src.auth.login import perform_login
from src.auth.session import is_session_valid
from src.classifier.client_profile import ClientProfile, load_all_active_profiles
from src.classifier.engine import classify_procesos
from src.config import get_config
from src.history import save_analisis, save_run
from src.notifier.email_sender import (
    send_circuit_breaker_alert,
    send_report,
    send_session_expired_alert,
)
from src.portales.contrato import ContratoRoto, CredencialesFaltantes, ErrorPortal, SesionInvalida
from src.scraper.models import ResultadoAnalisis

logger = logging.getLogger(__name__)

TZ_ELSALVADOR = timezone(timedelta(hours=-6))

CIRCUIT_BREAKER_THRESHOLD = 3
SESSION_RESTORE_MAX_ATTEMPTS = 3
SESSION_RESTORE_DELAY_SECONDS = 10
_consecutive_errors = 0


ERROR_SESION_COMPRASAL = "Sesión de COMPRASAL expirada y no se pudo restaurar"


class LLMCaidoError(RuntimeError):
    """Ningún cliente activo pudo clasificar por caída del LLM."""


class TodosLosPortalesFallaron(RuntimeError):
    """Ningún portal intentado pudo extraer: es un error crítico de la corrida (mueve el breaker global)."""


def agrupar_por_portal(clientes: list[ClientProfile]) -> dict[str, list[ClientProfile]]:
    """Clientes agrupados por el portal del que leen (`client.portal`, COMPRASAL por defecto)."""
    grupos: dict[str, list[ClientProfile]] = {}
    for cliente in clientes:
        grupos.setdefault(cliente.portal_id, []).append(cliente)
    return grupos


def _agrupar_integrados(clientes: list[ClientProfile]) -> dict[str, list[ClientProfile]]:
    """Solo los portales con extractor. Los clientes de un portal sin extractor (o desconocido)
    se omiten con aviso: no es una falla de la corrida ni dispara alertas de sesión."""
    integrados: dict[str, list[ClientProfile]] = {}
    grupos = agrupar_por_portal(clientes)
    # Orden del registro (`portales_en_orden`, COMPRASAL primero) = orden de ejecución (D10); los
    # desconocidos al final, solo para avisar.
    orden = [p.portal_id for p in portales.portales_en_orden()]
    for portal_id in sorted(grupos, key=lambda pid: orden.index(pid) if pid in orden else len(orden)):
        grupo = grupos[portal_id]
        ids = ", ".join(c.client_id for c in grupo)
        try:
            portal = portales.obtener_portal(portal_id)
        except portales.PortalDesconocido as exc:
            logger.error("%s — se omiten los clientes: %s", exc, ids)
            continue
        if not portal.integrado:
            logger.warning(
                "Portal %s (%s) sin extractor todavía — se omiten los clientes: %s",
                portal_id, portal.nombre, ids,
            )
            continue
        integrados[portal_id] = grupo
    return integrados


def _portales_a_correr(config, clientes: list[ClientProfile]) -> list[tuple[portales.Portal, list[ClientProfile]]]:
    """(portal, sus clientes) para cada portal integrado con clientes activos, en orden de ejecución.

    `PORTALES_DESHABILITADOS` (interruptor de ops) salta un portal sin tocar YAML: no cuenta como fallo.
    """
    deshabilitados = getattr(config, "portales_deshabilitados", None) or frozenset()
    a_correr = []
    for portal_id, grupo in _agrupar_integrados(clientes).items():
        if portal_id in deshabilitados:
            logger.warning("Portal %s deshabilitado por PORTALES_DESHABILITADOS — se omiten los clientes: %s",
                           portal_id, ", ".join(c.client_id for c in grupo))
            continue
        a_correr.append((portales.obtener_portal(portal_id), grupo))
    return a_correr


def _afirmar_un_portal(procesos, portal_id: str | None = None) -> None:
    """Invariante I1 (docs/arquitectura/portales.md §4): toda estructura con llave `numero_referencia`
    vive dentro de una iteración de UN portal. Explícita (no `assert`) para que `-O` no la apague."""
    ids = {getattr(p, "portal_id", None) for p in procesos}
    if portal_id is not None:
        ids.add(portal_id)
    if len(ids) > 1:
        raise AssertionError(f"I1: procesos de varios portales en una estructura por portal: {sorted(map(str, ids))}")


# ── Extracción por portal (docs/arquitectura/portales.md §5.2) ───────────────────────────────────────

def _validar_credenciales(config, portal) -> None:
    """Portal con login: exige `<PREFIJO>_EMAIL` y `<PREFIJO>_PASSWORD` (nunca los loguea)."""
    prefijo = (portal.prefijo_credenciales or "").strip()
    if not prefijo:
        return
    faltan = [f"{prefijo.upper()}_{campo.upper()}" for campo in ("email", "password")
              if not str(getattr(config, f"{prefijo.lower()}_{campo}", "") or "").strip()]
    if faltan:
        raise CredencialesFaltantes(portal.portal_id, f"faltan {', '.join(faltan)}")


def _asegurar_sesion(config, portal) -> None:
    """Solo portales con `requiere_login`: credenciales y sesión válida (con auto-restauración por TOTP)."""
    if not portal.requiere_login:
        return
    _validar_credenciales(config, portal)
    if portal.portal_id != portales.PORTAL_ID_DEFAULT:
        raise ErrorPortal(portal.portal_id, "login no implementado para este portal")
    if asyncio.run(is_session_valid(config)):
        return
    logger.warning("Sesión de %s inválida o expirada. Intentando restaurar...", portal.portal_id)
    if not _try_restore_session(config):
        raise SesionInvalida(portal.portal_id, ERROR_SESION_COMPRASAL)


def _desde(config, portal) -> datetime:
    """Inicio de la ventana de extracción, aware y en la zona del portal."""
    dias = portal.lookback_dias or getattr(config, "days_lookback", 1)
    return datetime.now(ZoneInfo(portal.zona_horaria)) - timedelta(days=dias)


def _verificar_procesos(procesos, portal) -> list:
    """Hace cumplir el contrato del extractor (§2.2) antes de que nada use los procesos.

    Un `portal_id` ajeno rompe la invariante I1 ⇒ `ContratoRoto`. Una fila sin referencia, sin fecha aware
    o sin URL se descarta con WARNING; si se descartan todas, `ContratoRoto` (el portal cambió).
    """
    pid = portal.portal_id
    if not isinstance(procesos, list):
        raise ContratoRoto(pid, "el extractor no devolvió una lista", detalle=type(procesos).__name__)
    ajenos = sorted({str(getattr(p, "portal_id", None)) for p in procesos} - {pid})
    if ajenos:
        raise ContratoRoto(pid, "el extractor devolvió procesos de otro portal", detalle=", ".join(ajenos))
    validos = []
    for p in procesos:
        fecha = getattr(p, "fecha_publicacion", None)
        if (getattr(p, "numero_referencia", "") and getattr(p, "url_directa", "")
                and isinstance(fecha, datetime) and fecha.tzinfo is not None):
            validos.append(p)
    if len(validos) < len(procesos):
        logger.warning("[%s] %d proceso(s) descartado(s): sin referencia, sin URL o sin fecha con zona.",
                       pid, len(procesos) - len(validos))
        if not validos:
            raise ContratoRoto(pid, "ningún proceso cumple el contrato (referencia, URL, fecha con zona)")
    return validos


def _extraer_portal(config, portal) -> list:
    """Login si hace falta, extractor del registro y verificación. Cualquier fallo es de ESTE portal."""
    _asegurar_sesion(config, portal)
    extractor = portales.cargar_extractor(portal)
    procesos = asyncio.run(extractor(config, portal, _desde(config, portal)))
    return _verificar_procesos(procesos, portal)


def _texto_error(exc: BaseException) -> str:
    """El mensaje sin el prefijo de portal que `ErrorPortal` agrega a `str()`."""
    return exc.mensaje if isinstance(exc, ErrorPortal) else str(exc)


def _resumen_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_texto_error(exc)}"


def _mensaje_fallos(fallidos: dict) -> str:
    """Un solo portal: el mensaje tal cual (igual que antes del bucle). Varios: `pid: Clase: mensaje; …`."""
    if len(fallidos) == 1:
        return _texto_error(next(iter(fallidos.values())))
    return "; ".join(f"{pid}: {_resumen_error(exc)}" for pid, exc in fallidos.items())


def _ops(config, asunto: str, cuerpo: str) -> None:
    try:
        from src.notifier.ops_alert import send_ops_alert
        send_ops_alert(config, asunto, cuerpo)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo alertar a operación (%s): %s", asunto, exc)


def _alertar_fallo_portal(config, portal, clientes, exc: BaseException, fallos: int) -> bool:
    """Alertas de §5.3. Devuelve True si se mandó el breaker del portal a sus clientes.

    Nunca un correo al cliente por un contrato roto: eso va a operación (`send_ops_alert`).
    """
    umbral = CIRCUIT_BREAKER_THRESHOLD
    detalle = (f"Portal: {portal.portal_id} ({portal.nombre})\n"
               f"Error: {_resumen_error(exc)}\n"
               f"Fallos consecutivos: {fallos}\n"
               f"Detalle: {getattr(exc, 'detalle', '') or '-'}")
    if isinstance(exc, (SesionInvalida, CredencialesFaltantes)) and portal.requiere_login:
        for client in clientes:
            try:
                send_session_expired_alert(config, client)
            except Exception as e:  # noqa: BLE001
                logger.error("No se pudo enviar alerta a %s: %s", client.client_id, e)
    elif isinstance(exc, ContratoRoto):
        if fallos in (1, umbral):
            _ops(config, f"{portal.nombre}: contrato roto", detalle)
    elif fallos == umbral:
        _ops(config, f"{portal.nombre}: {fallos} fallos consecutivos", detalle)
    if fallos != umbral:
        return False
    logger.critical("Circuit breaker del portal %s (%d fallos).", portal.portal_id, fallos)
    for client in clientes:
        try:
            send_circuit_breaker_alert(config, client, fallos, f"[{portal.nombre}] {_texto_error(exc)}")
        except Exception as e:  # noqa: BLE001
            logger.error("No se pudo enviar alerta a %s: %s", client.client_id, e)
    return True


def _intentar_portal(config, portal, clientes, alertados: set | None = None):
    """Extrae un portal y registra su estado. Devuelve (procesos, None) o (None, excepción); nunca lanza."""
    pid = portal.portal_id
    try:
        procesos = _extraer_portal(config, portal)
    except Exception as exc:  # noqa: BLE001 — ningún fallo de un portal sale del bucle
        logger.error("Portal %s sin extraer: %s", pid, _resumen_error(exc),
                     exc_info=not isinstance(exc, ErrorPortal))
        fallos = history.registrar_resultado_portal(config, pid, ok=False, error=_resumen_error(exc))
        if _alertar_fallo_portal(config, portal, clientes, exc, fallos) and alertados is not None:
            alertados.update(c.client_id for c in clientes)
        return None, exc
    history.registrar_resultado_portal(config, pid, ok=True, procesos=len(procesos))
    return procesos, None


def _setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _save_last_run(config, success: bool, procesos_count: int, clients_processed: int,
                   run_timestamp: datetime | None = None, error: str | None = None,
                   portales_resumen: dict | None = None) -> None:
    """Deja `last_run.json` (atómico) y, con `ESTADO_FUENTE=adb`, la fila de `ejecucion` en la ADB.

    `run_timestamp`/`error` los usa la fila de la ADB. El JSON conserva su forma de siempre y gana, de
    forma aditiva, `portales` ({portal_id: {ok, procesos, error}}) y, en un éxito PARCIAL, `error`.
    """
    os.makedirs(config.data_dir, exist_ok=True)
    data = {
        "timestamp": (run_timestamp or datetime.now(TZ_ELSALVADOR)).isoformat(),
        "success": success,
        "procesos_revisados": procesos_count,
        "clientes_procesados": clients_processed,
        # Qué versión del código produjo esta corrida. Con el deploy en modo pull
        # (Fase 4) este campo es la única forma de saber si la VM aplicó la
        # imagen nueva: comprobar que la corrida "funcionó" no lo distingue,
        # porque la versión anterior también funciona.
        "version": config.app_version,
    }
    if portales_resumen:
        data["portales"] = portales_resumen
    if success and error:
        data["error"] = error
    history.guardar_last_run(config, data, error=error)


def _preparar_estado(config) -> None:
    """Inicio de corrida con `ESTADO_FUENTE` distinta de `archivo`: vacía el spool y refresca la caché de last_run.

    Nunca propaga: una ADB caída no puede impedir la corrida.
    """
    from src.persistencia.fuente import fuente
    if fuente("ESTADO") == "archivo":
        return
    try:
        history.drenar_spool(config)
        history.leer_last_run(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo preparar el estado desde la ADB; la corrida sigue: %s", exc)


def _registrar_ejecucion(config, run_timestamp, **kwargs) -> None:
    """`service.registrar_ejecucion`, salvo con `ESTADO_FUENTE=adb`: ahí `_save_last_run` ya escribió la fila."""
    from src.persistencia.fuente import fuente
    if fuente("ESTADO") == "adb":
        return
    from src.memoria import service
    service.registrar_ejecucion(config, run_timestamp, **kwargs)


def _try_restore_session(config) -> bool:
    """
    Intenta restaurar la sesión de COMPRASAL hasta SESSION_RESTORE_MAX_ATTEMPTS veces.
    Solo funciona si TOTP está habilitado (auto-login). Sin TOTP, retorna False inmediatamente.
    """
    if not getattr(config, "totp_enabled", False):
        logger.warning("TOTP no habilitado — no se puede restaurar sesión automáticamente.")
        return False

    for attempt in range(1, SESSION_RESTORE_MAX_ATTEMPTS + 1):
        logger.info(
            "Intento de restauración de sesión %d/%d...",
            attempt, SESSION_RESTORE_MAX_ATTEMPTS,
        )
        try:
            # perform_login() acepta solo `headless: bool` y obtiene config internamente
            # via get_config(). Pasar `config` aquí lo asignaba a `headless`, que termina
            # serializado a JSON en Playwright y reventaba el auto-login con
            # "Object of type Config is not JSON serializable".
            login_ok = perform_login()
            if login_ok:
                session_ok = asyncio.run(is_session_valid(config))
                if session_ok:
                    logger.info("Sesión restaurada exitosamente en intento %d.", attempt)
                    return True
                logger.warning("Login reportó éxito pero la sesión sigue inválida.")
            else:
                logger.warning("Login falló en intento %d.", attempt)
        except Exception as exc:
            logger.error("Error en intento %d de restauración: %s", attempt, exc)

        if attempt < SESSION_RESTORE_MAX_ATTEMPTS:
            logger.info("Esperando %ds antes del siguiente intento...", SESSION_RESTORE_DELAY_SECONDS)
            time.sleep(SESSION_RESTORE_DELAY_SECONDS)

    logger.error("No se pudo restaurar la sesión tras %d intentos.", SESSION_RESTORE_MAX_ATTEMPTS)
    return False


def _registrar_clasificados(config, client_id: str, clasificados: list) -> None:
    """Persiste en la ADB todo lo clasificado (relevante o no) — E11.1.

    ``service.registrar_clasificados`` ya respeta los flags y no propaga; el
    try/except de acá es un segundo cinturón: persistir nunca puede tumbar la
    clasificación, el drilldown ni el correo del cliente.
    """
    try:
        from src.memoria import service
        service.registrar_clasificados(config, client_id, clasificados)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] registrar_clasificados falló; la corrida sigue: %s", client_id, exc)


def _rescatar_semanticos(config, profile, clasificados) -> list:
    """Reserva C5 en una transacción; publicar sin marca requiere el correo v2."""
    modo = getattr(config, "intel_c5", "off")
    if modo not in {"sombra", "activo"}:
        return []
    from src.aprendizaje import service as aprendizaje
    if not aprendizaje.habilitado(config):
        logger.info("C5 %s no disponible: memoria deshabilitada o sin DSN", modo)
        return []
    pendientes = [p for p in clasificados if not p.es_relevante]
    if not pendientes:
        return []
    try:
        from contextlib import nullcontext

        from src.classifier.rescate import rescatar
        from src.memoria import db
        with db.connection(config) as conn:
            candidatos = rescatar(conn, config, pendientes, profile.client_id, 5)
            if modo == "sombra":
                # El rescatador registra sus similitudes; esto audita el cableado
                # incluso cuando no hubo vecinos, sin abrir otra transacción.
                for proceso in pendientes:
                    # Llave que cruza portales (`Proceso.proceso_gid`), sin respaldo.
                    gid = proceso.proceso_gid
                    aprendizaje.guardar_decision(
                        config, profile.client_id, gid, "C5", variante="sombra",
                        version="cableado-v1", salida={"destino": "sin_publicacion"},
                        conectar=lambda: nullcontext(conn),
                    )
        # Solo después del commit se consideran reservados. No se promueve la
        # relevancia ni se inventa marca para atravesar filtros del notificador.
        logger.info("C5 %s: %d revisados, %d candidatos Por confirmar registrados",
                    modo, len(pendientes), len(candidatos))
        return candidatos if modo == "activo" else []
    except Exception as exc:  # noqa: BLE001 — memoria opcional
        logger.warning("C5 no disponible (%s); la corrida sigue", type(exc).__name__)
        return []


def _alertar_llm_caido(config, profile, resultado, total_procesos) -> None:
    if not getattr(resultado, "llm_caido", False):
        return
    try:
        from src.notifier.ops_alert import send_ops_alert

        send_ops_alert(
            config,
            f"LLM caído: 0/{total_procesos} procesos clasificados",
            f"Cliente: {profile.client_id} ({profile.client_name})\n"
            f"Proveedor: {resultado.proveedor}\n"
            f"Primer error: {resultado.primer_error}",
        )
    except Exception as exc:
        logger.error("[%s] No se pudo enviar alerta de LLM caído: %s", profile.client_id, exc)


def _clasificar_con_alerta(config, profile, procesos):
    """Conserva el retorno y las excepciones del motor; alerta sin interrumpirlo."""
    from src.llm.contrato import LLMError

    try:
        resultado = classify_procesos(procesos, config, profile) if procesos else []
    except LLMError as exc:
        _alertar_llm_caido(config, profile, getattr(exc, "resultado_clasificacion", None), len(procesos))
        raise
    _alertar_llm_caido(config, profile, resultado, len(procesos))
    return resultado


def _clasificar_para_reporte(config, profile, procesos, clientes_con_llm_caido):
    """Normaliza solo la caída diagnosticada, incluso en el contrato de lote único."""
    from src.llm.contrato import LLMError

    try:
        resultado = _clasificar_con_alerta(config, profile, procesos)
    except LLMError as exc:
        resultado = getattr(exc, "resultado_clasificacion", None)
        if not getattr(resultado, "llm_caido", False):
            raise
    if getattr(resultado, "llm_caido", False):
        clientes_con_llm_caido.add(profile.client_id)
        return None
    return resultado


def _classify_and_notify(
    config,
    client_profile: ClientProfile,
    procesos: list,
    total_revisados: int,
    run_timestamp: datetime,
    clientes_con_llm_caido: set[str] | None = None,
) -> int:
    """
    Clasifica los procesos para un cliente y envía su reporte.
    Retorna el número de procesos relevantes encontrados.
    Aísla errores por cliente para que un fallo no detenga a los demás.
    """
    if clientes_con_llm_caido is None:
        clientes_con_llm_caido = set()
    try:
        logger.info("--- Cliente: %s ---", client_profile.client_name)

        if procesos:
            clasificados = _clasificar_para_reporte(
                config, client_profile, procesos, clientes_con_llm_caido)
            if clasificados is None:
                return 0
        else:
            clasificados = []
        _rescatar_semanticos(config, client_profile, clasificados)
        _registrar_clasificados(config, client_profile.client_id, clasificados)

        relevantes_count = sum(1 for p in clasificados if p.es_relevante)
        logger.info(
            "[%s] Clasificación: %d/%d relevantes.",
            client_profile.client_id,
            relevantes_count,
            len(clasificados),
        )

        _reconciliar_marcas(config, client_profile, clasificados, {})
        save_run(
            client_id=client_profile.client_id,
            run_timestamp=run_timestamp,
            procesos_clasificados=clasificados,
            total_revisados=total_revisados,
            data_dir=config.data_dir,
        )

        send_report(
            config=config,
            client_profile=client_profile,
            procesos_clasificados=clasificados,
            total_revisados=total_revisados,
            run_timestamp=run_timestamp,
        )
        return relevantes_count

    except Exception as exc:
        logger.exception(
            "[%s] Error al clasificar/notificar: %s",
            client_profile.client_id,
            exc,
        )
        return 0


def _run_clients_with_drilldown(
    config, active_clients, procesos, total_revisados, run_timestamp,
    clientes_con_llm_caido: set[str] | None = None,
) -> int:
    """Flujo con drill-down de pliegos (ENABLE_DRILLDOWN=true, Fase 2 · MVP Track A).

    Estructura en fases para AISLAR Chromium del análisis LLM (disciplina de memoria):
      1. Clasificar todos los clientes (sin navegador).
      2. Fase A: descargar los PDFs de los procesos relevantes (navegador; se cierra al final).
      3. Fase B por cliente: analizar pliegos (sin navegador) + enviar reporte con matriz.
      4. Limpiar los PDFs temporales.
    Devuelve el total de procesos relevantes across clientes.
    """
    from datetime import datetime, timezone

    from src.drilldown import pipeline
    from src.scraper.models import ResultadoAnalisis

    # 1. Clasificar cada cliente y juntar los procesos relevantes (dedup por referencia: I1, un portal).
    _afirmar_un_portal(procesos)
    if clientes_con_llm_caido is None:
        clientes_con_llm_caido = set()
    per_client = []
    global_relevant: dict = {}
    for profile in active_clients:
        try:
            clasificados = _clasificar_para_reporte(
                config, profile, procesos, clientes_con_llm_caido)
            if clasificados is None:
                continue
        except Exception as exc:
            logger.exception("[%s] Error al clasificar: %s", profile.client_id, exc)
            clasificados = []
        _rescatar_semanticos(config, profile, clasificados)
        _registrar_clasificados(config, profile.client_id, clasificados)
        relevantes = [c for c in clasificados if c.es_relevante]
        per_client.append((profile, clasificados, relevantes))
        for c in relevantes:
            global_relevant.setdefault(c.numero_referencia, c)
    _afirmar_un_portal(global_relevant.values())

    logger.info("Drill-down: %d proceso(s) relevante(s) único(s) a analizar.", len(global_relevant))

    # 2. Fase A: descargar todos los pliegos relevantes (navegador abierto y luego cerrado).
    pdf_paths: dict = {}
    if global_relevant:
        try:
            pdf_paths = asyncio.run(
                pipeline.descargar_pliegos(list(global_relevant.values()), config)
            )
        except Exception as exc:
            logger.exception("Fase A (descarga de pliegos) falló: %s", exc)

    # 3. Fase B + notificación por cliente (sin navegador).
    total_relevantes = 0
    try:
        for profile, clasificados, relevantes in per_client:
            resultados: dict = {}
            if relevantes and pdf_paths:
                try:
                    resultados = pipeline.procesar_drilldown(relevantes, pdf_paths, config, profile)
                except Exception as exc:
                    logger.exception("[%s] Fase B (análisis) falló: %s", profile.client_id, exc)

            _reconciliar_marcas(config, profile, clasificados, resultados)
            try:
                save_run(
                    client_id=profile.client_id,
                    run_timestamp=run_timestamp,
                    procesos_clasificados=clasificados,
                    total_revisados=total_revisados,
                    data_dir=config.data_dir,
                )
            except Exception as exc:
                logger.error("[%s] No se pudo guardar el run: %s", profile.client_id, exc)

            try:
                send_report(
                    config=config,
                    client_profile=profile,
                    procesos_clasificados=clasificados,
                    total_revisados=total_revisados,
                    run_timestamp=run_timestamp,
                    resultados_analisis=resultados or None,
                )
                # Marcar NOTIFICADO los pliegos analizados → idempotencia/reanudación.
                for uuid, res in resultados.items():
                    if res.estado == "ANALIZADO":
                        save_analisis(
                            profile.client_id,
                            ResultadoAnalisis(
                                proceso_uuid=uuid,
                                estado="NOTIFICADO",
                                formato=res.formato,
                                triaje=res.triaje,
                                triaje_confianza=res.triaje_confianza,
                                competible=res.competible,
                                motivo_bloqueo=res.motivo_bloqueo,
                                modelo_analisis=res.modelo_analisis,
                                generado_en=datetime.now(timezone.utc).isoformat(),
                            ),
                            config.data_dir,
                        )
            except Exception as exc:
                logger.exception("[%s] Error al enviar reporte: %s", profile.client_id, exc)

            analizados = sum(1 for r in resultados.values() if r.estado == "ANALIZADO")
            logger.info("[%s] Relevantes: %d | Pliegos analizados: %d",
                        profile.client_id, len(relevantes), analizados)
            total_relevantes += len(relevantes)
    finally:
        pipeline.limpiar_pliegos(pdf_paths)

    return total_relevantes


# ── Fase 2 · Track B — Dos etapas efímeras (aislamiento de memoria en OCI) ────
#
# `--stage scrape`  : Playwright abierto (extrae, clasifica, descarga PDFs) → exit
# `--stage analyze` : SIN navegador (analiza pliegos, notifica)             → exit
#
# Cada etapa es un `docker run --rm` distinto, encadenado por systemd (OnSuccess),
# así el pico de RAM de Chromium y el del análisis LLM nunca se suman.
# El handoff entre etapas es durable: manifiesto JSON + PDFs en disco.

def _handoff_path(config, portal_id: str | None = None) -> str:
    """Manifiesto de un portal: `stage_handoff_<portal_id>.json`. Sin `portal_id`, el heredado de un solo
    portal (`stage_handoff.json`), que `analyze` todavía lee como COMPRASAL durante una versión."""
    nombre = f"stage_handoff_{portal_id}.json" if portal_id else "stage_handoff.json"
    return os.path.join(config.data_dir, nombre)


def _manifiestos_pendientes(config) -> list[tuple[str, str]]:
    """(portal_id, ruta) de los manifiestos que hay en disco: el heredado primero, luego en orden del registro."""
    pendientes = []
    legado = _handoff_path(config)
    if os.path.exists(legado):
        pendientes.append((portales.PORTAL_ID_DEFAULT, legado))
    for portal in portales.portales_en_orden():
        ruta = _handoff_path(config, portal.portal_id)
        if os.path.exists(ruta):
            pendientes.append((portal.portal_id, ruta))
    return pendientes


def _stage_scrape(config) -> None:
    """Etapa A: por cada portal, extrae, clasifica y descarga los pliegos. Deja un manifiesto por portal."""
    run_timestamp = datetime.now(TZ_ELSALVADOR)
    _preparar_estado(config)
    active_clients = load_all_active_profiles(config.clients_dir)
    if not active_clients:
        logger.warning("No hay clientes activos. Nada que hacer.")
        return
    a_correr = _portales_a_correr(config, active_clients)
    if not a_correr:
        logger.warning("Ningún cliente activo lee de un portal integrado. Nada que hacer.")
        return

    fallidos: dict = {}
    for portal, clientes in a_correr:
        procesos, exc = _intentar_portal(config, portal, clientes)
        if exc is not None:
            fallidos[portal.portal_id] = exc  # sin manifiesto: queda en portal_estado.json
            continue
        _scrape_portal(config, portal, clientes, procesos, run_timestamp)

    if len(fallidos) == len(a_correr):
        _save_last_run(config, success=False, procesos_count=0, clients_processed=0,
                       run_timestamp=run_timestamp, error=_mensaje_fallos(fallidos),
                       portales_resumen={pid: {"ok": False, "procesos": 0, "error": _resumen_error(e)}
                                         for pid, e in fallidos.items()})


def _scrape_portal(config, portal, clientes, procesos, run_timestamp) -> None:
    """Clasifica, descarga los pliegos y escribe `stage_handoff_<portal_id>.json` (atómico)."""
    from src.drilldown import pipeline
    from src.history import _serialize

    pid = portal.portal_id
    _afirmar_un_portal(procesos, pid)
    logger.info("[scrape] Procesos extraídos de %s: %d", pid, len(procesos))

    manifiesto = {"portal_id": pid, "run_timestamp": run_timestamp.isoformat(),
                  "total_revisados": len(procesos), "clientes": {}}
    global_relevant: dict = {}
    clientes_con_llm_caido: set[str] = set()

    for profile in clientes:
        try:
            clasificados = _clasificar_para_reporte(
                config, profile, procesos, clientes_con_llm_caido)
            if clasificados is None:
                manifiesto["clientes"][profile.client_id] = {
                    "llm_caido": True, "clasificados": [], "relevantes": []}
                continue
        except Exception as exc:
            logger.exception("[%s] Error al clasificar: %s", profile.client_id, exc)
            clasificados = []
        _rescatar_semanticos(config, profile, clasificados)
        _registrar_clasificados(config, profile.client_id, clasificados)
        relevantes = [c for c in clasificados if c.es_relevante]
        for c in relevantes:
            global_relevant.setdefault(c.numero_referencia, c)
        manifiesto["clientes"][profile.client_id] = {
            "llm_caido": False,
            "clasificados": [_serialize(c) for c in clasificados],
            "relevantes": [c.numero_referencia for c in relevantes],
        }
    _afirmar_un_portal(global_relevant.values(), pid)

    # Descarga de pliegos (única fase con navegador vivo).
    pdf_paths: dict = {}
    if global_relevant:
        try:
            pdf_paths = asyncio.run(
                pipeline.descargar_pliegos(list(global_relevant.values()), config))
        except Exception as exc:
            logger.exception("[scrape] Descarga de pliegos de %s falló: %s", pid, exc)
    manifiesto["pdf_paths"] = pdf_paths

    os.makedirs(config.data_dir, exist_ok=True)
    destino = _handoff_path(config, pid)
    tmp = destino + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifiesto, f, ensure_ascii=False)
    os.replace(tmp, destino)
    logger.info("[scrape] Manifiesto de %s escrito: %d relevante(s), %d pliego(s) descargado(s).",
                pid, len(global_relevant), len(pdf_paths))


def _stage_analyze(config) -> None:
    """Etapa B: consume los manifiestos (uno por portal), analiza los pliegos y notifica. Sin navegador.

    Cada manifiesto va en su propio try/finally: el fallo de uno no impide los demás.
    """
    from src.memoria import service

    pendientes = _manifiestos_pendientes(config)
    if not pendientes:
        logger.warning("[analyze] No hay manifiestos en %s — ¿corrió la etapa scrape?", config.data_dir)
        return

    active_clients = {p.client_id: p for p in load_all_active_profiles(config.clients_dir)}
    total_revisados = clientes_procesados = 0
    resumen: dict = {}
    try:
        for portal_id, path in pendientes:
            try:
                revisados, clientes = _analizar_manifiesto(config, portal_id, path, active_clients)
            except Exception as exc:  # noqa: BLE001 — un manifiesto roto no frena a los demás
                logger.exception("[analyze] Manifiesto %s falló: %s", path, exc)
                resumen[portal_id] = {"ok": False, "procesos": 0, "error": _resumen_error(exc)}
                continue
            total_revisados += revisados
            clientes_procesados += clientes
            previo = resumen.get(portal_id, {"procesos": 0})
            resumen[portal_id] = {"ok": True, "procesos": previo.get("procesos", 0) + revisados, "error": None}
    finally:
        service.cerrar(config)

    exito = any(r["ok"] for r in resumen.values())
    _save_last_run(config, success=exito, procesos_count=total_revisados,
                   clients_processed=clientes_procesados, portales_resumen=resumen)
    logger.info("[analyze] Etapa completada.")


def _analizar_manifiesto(config, portal_id: str, path: str, active_clients: dict) -> tuple[int, int]:
    """Analiza y notifica un manifiesto; siempre limpia sus pliegos y lo borra. Devuelve (revisados, clientes)."""
    from src.drilldown import pipeline
    from src.history import _deserialize

    pdf_paths: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifiesto = json.load(f)
        portal_id = manifiesto.get("portal_id") or portal_id
        run_timestamp = datetime.fromisoformat(manifiesto["run_timestamp"])
        total_revisados = manifiesto.get("total_revisados", 0)
        pdf_paths = manifiesto.get("pdf_paths", {}) or {}

        for client_id, datos in manifiesto.get("clientes", {}).items():
            profile = active_clients.get(client_id)
            if profile is None:
                logger.warning("[analyze] Cliente %s ya no está activo — se omite.", client_id)
                continue
            if datos.get("llm_caido", False):
                continue
            clasificados = [_deserialize(d) for d in datos.get("clasificados", [])]
            relevantes = [c for c in clasificados if c.es_relevante]

            resultados = {}
            if relevantes and pdf_paths:
                try:
                    resultados = pipeline.procesar_drilldown(relevantes, pdf_paths, config, profile)
                except Exception as exc:
                    logger.exception("[%s] Análisis falló: %s", client_id, exc)

            _reconciliar_marcas(config, profile, clasificados, resultados)
            try:
                save_run(client_id=client_id, run_timestamp=run_timestamp,
                         procesos_clasificados=clasificados,
                         total_revisados=total_revisados, data_dir=config.data_dir)
                send_report(config=config, client_profile=profile,
                            procesos_clasificados=clasificados,
                            total_revisados=total_revisados, run_timestamp=run_timestamp,
                            resultados_analisis=resultados or None)
                _marcar_notificados(config, client_id, resultados)
            except Exception as exc:
                logger.exception("[%s] Error al reportar: %s", client_id, exc)
        return total_revisados, len(manifiesto.get("clientes", {}))
    finally:
        pipeline.limpiar_pliegos(pdf_paths)
        try:
            os.remove(path)
        except OSError:
            pass


def _reconciliar_marcas(config, profile, clasificados, resultados) -> None:
    """La reconciliación optativa nunca impide guardar ni enviar el reporte."""
    if getattr(config, "marca_veredicto", "off") == "off":
        return
    try:
        from src.drilldown.reconciliar import reconciliar
        reconciliar(clasificados, resultados, config, profile)
    except Exception:  # noqa: BLE001 — la corrida no depende de esta capa
        logger.exception("[%s] No se pudieron reconciliar las marcas", profile.client_id)


def _marcar_notificados(config, client_id: str, resultados: dict) -> None:
    """Marca NOTIFICADO los pliegos analizados (idempotencia/reanudación)."""
    from src.memoria import service
    for uuid, res in (resultados or {}).items():
        if res.estado != "ANALIZADO":
            continue
        service.guardar_estado(config, client_id, ResultadoAnalisis(
            proceso_uuid=uuid, estado="NOTIFICADO", formato=res.formato,
            triaje=res.triaje, triaje_confianza=res.triaje_confianza,
            competible=res.competible, motivo_bloqueo=res.motivo_bloqueo,
            modelo_analisis=res.modelo_analisis,
            generado_en=datetime.now(timezone.utc).isoformat(),
        ))


def _procesar_portal(config, portal, clientes, procesos, run_timestamp, clientes_con_llm_caido) -> int:
    """Clasifica, (drill-down) y notifica a los clientes de UN portal, aislado por cliente como siempre."""
    _afirmar_un_portal(procesos, portal.portal_id)
    if getattr(config, "enable_drilldown", False):
        # Fase 2 · MVP Track A: clasifica + drill-down de pliegos + reporte con matriz.
        return _run_clients_with_drilldown(
            config, clientes, procesos, len(procesos), run_timestamp, clientes_con_llm_caido
        )
    total = 0
    for client_profile in clientes:
        total += _classify_and_notify(
            config, client_profile, procesos, len(procesos), run_timestamp, clientes_con_llm_caido
        )
    return total


def run_pipeline() -> None:
    """
    Pipeline completo multi-cliente y multi-portal (docs/arquitectura/portales.md §5.2):
      1. Cargar todos los clientes activos y agruparlos por portal integrado
      2. Por cada portal, en el orden del registro: sesión (si exige login) y extracción una sola vez
      3. Por cada cliente del portal: clasificar + enviar reporte personalizado
    Un portal que falla se registra en su estado (`data/portal_estado.json`), alerta según §5.3 y NO
    detiene a los demás. `success` es falso solo si fallan todos los portales o hay un error crítico.
    """
    global _consecutive_errors
    from src.memoria import service

    config = get_config()
    run_timestamp = datetime.now(TZ_ELSALVADOR)

    # `_consecutive_errors` es una variable de PROCESO: con Track B apagado
    # (o en un ambiente sin ADB) sigue siendo lo único que hay, exactamente
    # como hoy. Con Track B prendido, se "hidrata" desde el historial
    # persistido (`ejecucion`) antes de decidir nada — así el circuit
    # breaker cuenta fallos de verdad entre corridas (cada una es un
    # `docker run --rm`, un proceso nuevo) y no una variable que siempre
    # nace en 0. `max(...)`: esto solo puede sumar información, nunca
    # esconder fallos que el propio proceso ya vio.
    _consecutive_errors = max(_consecutive_errors, service.fallos_consecutivos(config))
    _preparar_estado(config)

    # Cargar clientes activos
    active_clients = load_all_active_profiles(config.clients_dir)
    if not active_clients:
        logger.warning("No hay clientes activos en %s. Verificar los YAMLs.", config.clients_dir)
        return

    a_correr = _portales_a_correr(config, active_clients)
    active_clients = [c for _, grupo in a_correr for c in grupo]
    if not active_clients:
        logger.warning("Ningún cliente activo lee de un portal integrado. Nada que hacer.")
        return

    logger.info("=" * 60)
    logger.info(
        "Iniciando ejecución — %s | %d cliente(s) activo(s): %s | portales: %s",
        run_timestamp.strftime("%Y-%m-%d %H:%M %Z"),
        len(active_clients),
        ", ".join(c.client_id for c in active_clients),
        ", ".join(p.portal_id for p, _ in a_correr),
    )
    logger.info("=" * 60)

    resumen: dict = {}            # portal_id → {"ok", "procesos", "error"} (va a last_run.json)
    alertados: set[str] = set()   # clientes que ya recibieron el breaker de su portal en esta corrida
    try:
        total_procesos = 0
        total_relevantes = 0
        clientes_con_llm_caido: set[str] = set()
        clientes_ok: list = []
        fallidos: dict = {}
        for portal, clientes in a_correr:
            # 1-2. Sesión (si el portal la exige) y extracción una sola vez, para todos sus clientes.
            procesos, exc = _intentar_portal(config, portal, clientes, alertados)
            if exc is not None:
                fallidos[portal.portal_id] = exc
                resumen[portal.portal_id] = {"ok": False, "procesos": 0, "error": _resumen_error(exc)}
                continue  # ← NUNCA return: los demás portales siguen
            resumen[portal.portal_id] = {"ok": True, "procesos": len(procesos), "error": None}
            total_procesos += len(procesos)
            clientes_ok.extend(clientes)
            logger.info("Procesos extraídos de %s: %d (se reutilizarán para sus %d cliente(s))",
                        portal.portal_id, len(procesos), len(clientes))

            # 3. Clasificar y notificar por cada cliente del portal
            total_relevantes += _procesar_portal(
                config, portal, clientes, procesos, run_timestamp, clientes_con_llm_caido)

        if not clientes_ok:
            raise TodosLosPortalesFallaron(_mensaje_fallos(fallidos))
        if clientes_con_llm_caido == {c.client_id for c in clientes_ok}:
            raise LLMCaidoError("LLM caído para todos los clientes activos")

        parcial = None
        if fallidos:
            parcial = "PARCIAL: " + "; ".join(f"{pid}: {_resumen_error(e)}" for pid, e in fallidos.items())
            logger.warning("Corrida parcial — %s", parcial)
        _save_last_run(
            config,
            success=True,
            procesos_count=total_procesos,
            clients_processed=len(clientes_ok),
            run_timestamp=run_timestamp,
            error=parcial,
            portales_resumen=resumen,
        )
        _registrar_ejecucion(
            config, run_timestamp, success=True,
            procesos_revisados=total_procesos, clientes_procesados=len(clientes_ok),
            **({"error": parcial} if parcial else {}),
        )
        _consecutive_errors = 0

        logger.info(
            "Ejecución completada. Procesos: %d | Clientes: %d | Relevantes totales: %d",
            total_procesos,
            len(clientes_ok),
            total_relevantes,
        )

    except Exception as exc:
        # Errores críticos: todos los portales fallaron, LLM caído para todos, o una excepción fuera del
        # aislamiento por portal. Un portal que falla mientras otro funciona NO llega acá.
        _consecutive_errors += 1
        logger.exception("Error crítico en el pipeline (error #%d): %s", _consecutive_errors, exc)
        _save_last_run(config, success=False, procesos_count=0, clients_processed=0,
                       run_timestamp=run_timestamp, error=str(exc), portales_resumen=resumen or None)
        _registrar_ejecucion(config, run_timestamp, success=False, error=str(exc))

        if _consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
            logger.critical("Circuit breaker activado (%d errores).", _consecutive_errors)
            if isinstance(exc, LLMCaidoError):
                try:
                    from src.notifier.ops_alert import send_ops_alert

                    send_ops_alert(config, "Circuit breaker: LLM caído",
                                   f"{_consecutive_errors} errores consecutivos: {exc}")
                except Exception as notify_exc:
                    logger.error("No se pudo alertar a operación: %s", notify_exc)
                return
            for client in active_clients:
                if client.client_id in alertados:
                    continue  # ya recibió el breaker de su portal en esta corrida
                try:
                    send_circuit_breaker_alert(config, client, _consecutive_errors, str(exc))
                except Exception as notify_exc:
                    logger.error("No se pudo enviar alerta a %s: %s", client.client_id, notify_exc)


def main() -> None:
    config = get_config()
    _setup_logging(config.log_level)
    from src.scheduler.cron import start_scheduler
    start_scheduler()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="tender-opportunity-tracker")
    # Lee APP_VERSION del entorno, NO de get_config(): argparse imprime y sale
    # antes de construir la Config, que exige SMTP_* y reventaría si faltan
    # (COMPRASAL_* ya son opcionales: se validan al empezar el portal con login). Preguntar "qué versión corre aquí" tiene que funcionar en una VM
    # a medio configurar — que es justo cuando se pregunta.
    # (ANTHROPIC_API_KEY salió de esa lista en la Fase 9: la credencial del LLM
    # depende del proveedor activo y la valida src/llm/validar_configuracion.)
    parser.add_argument(
        "--version",
        action="version",
        version=os.getenv("APP_VERSION", "dev"),
        help="Imprimir el SHA del commit horneado en la imagen y salir",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Ejecutar el pipeline una sola vez y salir (sin iniciar el scheduler)",
    )
    parser.add_argument(
        "--stage",
        choices=["scrape", "analyze"],
        help=(
            "Fase 2 · Track B: ejecutar solo una etapa efímera. "
            "'scrape' usa el navegador y descarga los pliegos; "
            "'analyze' analiza y notifica sin navegador. Aísla los picos de RAM."
        ),
    )
    args = parser.parse_args()

    config = get_config()
    _setup_logging(config.log_level)

    # Keepalive de ADB en toda corrida: la instancia Always Free se auto-detiene
    # tras 7 días de inactividad. Dispara si hay credenciales ADB configuradas,
    # aunque ENABLE_MEMORIA=false (no-op solo si no hay ADB_DSN/ADB_PASSWORD).
    try:
        from src.memoria import service as _memoria_service
        estado = _memoria_service.ping_con_estado(config)
        # El umbral vive en keepalive.py: acá solo se pregunta si toca avisar.
        # Alertar al primer parpadeo generaría ruido por cortes de red pasajeros;
        # no alertar nunca deja que la Always Free se auto-detenga en silencio,
        # que es justamente el fallo que esta fase cierra.
        if estado.debe_alertar:
            from src.notifier.alerts import notify
            # Por alerts.notify y no por correo directo: si la ADB se cayó porque
            # la VM estuvo muerta una semana, el SMTP de esa misma VM tampoco es
            # de fiar. Este es el caso de uso que justifica el canal alterno.
            notify(config, estado.resumen, estado.resumen)
    except Exception:  # noqa: BLE001
        # El keepalive nunca puede tumbar la corrida: es una tarea de fondo.
        pass

    if args.stage == "scrape":
        _stage_scrape(config)
    elif args.stage == "analyze":
        _stage_analyze(config)
    elif args.run_now:
        run_pipeline()
    else:
        main()
