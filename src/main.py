"""
tender-opportunity-tracker
Entry point principal: orquesta login, scraping, clasificación y reportes.

Una sola instancia sirve a múltiples clientes:
  1. Valida sesión de COMPRASAL
  2. Extrae procesos una sola vez
  3. Para cada cliente activo en clients/:
       a. Clasifica con las reglas del cliente
       b. Envía email personalizado al destinatario del cliente
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

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
from src.scraper.extractor import extract_recent_procesos
from src.scraper.models import ResultadoAnalisis

logger = logging.getLogger(__name__)

TZ_ELSALVADOR = timezone(timedelta(hours=-6))

CIRCUIT_BREAKER_THRESHOLD = 3
SESSION_RESTORE_MAX_ATTEMPTS = 3
SESSION_RESTORE_DELAY_SECONDS = 10
_consecutive_errors = 0


def _setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _save_last_run(config, success: bool, procesos_count: int, clients_processed: int) -> None:
    os.makedirs(config.data_dir, exist_ok=True)
    data = {
        "timestamp": datetime.now(TZ_ELSALVADOR).isoformat(),
        "success": success,
        "procesos_revisados": procesos_count,
        "clientes_procesados": clients_processed,
        # Qué versión del código produjo esta corrida. Con el deploy en modo pull
        # (Fase 4) este campo es la única forma de saber si la VM aplicó la
        # imagen nueva: comprobar que la corrida "funcionó" no lo distingue,
        # porque la versión anterior también funciona.
        "version": config.app_version,
    }
    with open(config.last_run_path, "w") as f:
        json.dump(data, f, indent=2)


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


def _classify_and_notify(
    config,
    client_profile: ClientProfile,
    procesos: list,
    total_revisados: int,
    run_timestamp: datetime,
) -> int:
    """
    Clasifica los procesos para un cliente y envía su reporte.
    Retorna el número de procesos relevantes encontrados.
    Aísla errores por cliente para que un fallo no detenga a los demás.
    """
    try:
        logger.info("--- Cliente: %s ---", client_profile.client_name)

        if procesos:
            clasificados = classify_procesos(procesos, config, client_profile)
        else:
            clasificados = []

        relevantes_count = sum(1 for p in clasificados if p.es_relevante)
        logger.info(
            "[%s] Clasificación: %d/%d relevantes.",
            client_profile.client_id,
            relevantes_count,
            len(clasificados),
        )

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
    config, active_clients, procesos, total_revisados, run_timestamp
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

    # 1. Clasificar cada cliente y juntar los procesos relevantes (dedup global por referencia).
    per_client = []
    global_relevant: dict = {}
    for profile in active_clients:
        try:
            clasificados = classify_procesos(procesos, config, profile) if procesos else []
        except Exception as exc:
            logger.exception("[%s] Error al clasificar: %s", profile.client_id, exc)
            clasificados = []
        relevantes = [c for c in clasificados if c.es_relevante]
        per_client.append((profile, clasificados, relevantes))
        for c in relevantes:
            global_relevant.setdefault(c.numero_referencia, c)

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

def _handoff_path(config) -> str:
    return os.path.join(config.data_dir, "stage_handoff.json")


def _stage_scrape(config) -> None:
    """Etapa A: extrae, clasifica y descarga los pliegos. Deja el manifiesto."""
    from src.drilldown import pipeline
    from src.history import _serialize

    run_timestamp = datetime.now(TZ_ELSALVADOR)
    active_clients = load_all_active_profiles(config.clients_dir)
    if not active_clients:
        logger.warning("No hay clientes activos. Nada que hacer.")
        return

    session_ok = asyncio.run(is_session_valid(config))
    if not session_ok and not _try_restore_session(config):
        for client in active_clients:
            try:
                send_session_expired_alert(config, client)
            except Exception as exc:
                logger.error("No se pudo alertar a %s: %s", client.client_id, exc)
        _save_last_run(config, success=False, procesos_count=0, clients_processed=0)
        return

    procesos = asyncio.run(extract_recent_procesos(config))
    logger.info("[scrape] Procesos extraídos: %d", len(procesos))

    manifiesto = {"run_timestamp": run_timestamp.isoformat(),
                  "total_revisados": len(procesos), "clientes": {}}
    global_relevant: dict = {}

    for profile in active_clients:
        try:
            clasificados = classify_procesos(procesos, config, profile) if procesos else []
        except Exception as exc:
            logger.exception("[%s] Error al clasificar: %s", profile.client_id, exc)
            clasificados = []
        relevantes = [c for c in clasificados if c.es_relevante]
        for c in relevantes:
            global_relevant.setdefault(c.numero_referencia, c)
        manifiesto["clientes"][profile.client_id] = {
            "clasificados": [_serialize(c) for c in clasificados],
            "relevantes": [c.numero_referencia for c in relevantes],
        }

    # Descarga de pliegos (única fase con navegador vivo).
    pdf_paths: dict = {}
    if global_relevant:
        try:
            pdf_paths = asyncio.run(
                pipeline.descargar_pliegos(list(global_relevant.values()), config))
        except Exception as exc:
            logger.exception("[scrape] Descarga de pliegos falló: %s", exc)
    manifiesto["pdf_paths"] = pdf_paths

    os.makedirs(config.data_dir, exist_ok=True)
    tmp = _handoff_path(config) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifiesto, f, ensure_ascii=False)
    os.replace(tmp, _handoff_path(config))
    logger.info("[scrape] Manifiesto escrito: %d relevante(s), %d pliego(s) descargado(s).",
                len(global_relevant), len(pdf_paths))


def _stage_analyze(config) -> None:
    """Etapa B: consume el manifiesto, analiza los pliegos y notifica. Sin navegador."""
    from src.drilldown import pipeline
    from src.history import _deserialize
    from src.memoria import service

    path = _handoff_path(config)
    if not os.path.exists(path):
        logger.warning("[analyze] No hay manifiesto en %s — ¿corrió la etapa scrape?", path)
        return
    with open(path, "r", encoding="utf-8") as f:
        manifiesto = json.load(f)

    run_timestamp = datetime.fromisoformat(manifiesto["run_timestamp"])
    total_revisados = manifiesto.get("total_revisados", 0)
    pdf_paths = manifiesto.get("pdf_paths", {})
    active_clients = {p.client_id: p for p in load_all_active_profiles(config.clients_dir)}

    try:
        for client_id, datos in manifiesto.get("clientes", {}).items():
            profile = active_clients.get(client_id)
            if profile is None:
                logger.warning("[analyze] Cliente %s ya no está activo — se omite.", client_id)
                continue
            clasificados = [_deserialize(d) for d in datos.get("clasificados", [])]
            relevantes = [c for c in clasificados if c.es_relevante]

            resultados = {}
            if relevantes and pdf_paths:
                try:
                    resultados = pipeline.procesar_drilldown(relevantes, pdf_paths, config, profile)
                except Exception as exc:
                    logger.exception("[%s] Análisis falló: %s", client_id, exc)

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
    finally:
        pipeline.limpiar_pliegos(pdf_paths)
        try:
            os.remove(path)
        except OSError:
            pass
        service.cerrar(config)

    _save_last_run(config, success=True, procesos_count=total_revisados,
                   clients_processed=len(manifiesto.get("clientes", {})))
    logger.info("[analyze] Etapa completada.")


def _marcar_notificados(config, client_id: str, resultados: dict) -> None:
    """Marca NOTIFICADO los pliegos analizados (idempotencia/reanudación)."""
    from src.memoria import service
    for uuid, res in (resultados or {}).items():
        if res.estado != "ANALIZADO":
            continue
        service.guardar_estado(config, client_id, ResultadoAnalisis(
            proceso_uuid=uuid, estado="NOTIFICADO", formato=res.formato,
            triaje=res.triaje, triaje_confianza=res.triaje_confianza,
            modelo_analisis=res.modelo_analisis,
            generado_en=datetime.now(timezone.utc).isoformat(),
        ))


def run_pipeline() -> None:
    """
    Pipeline completo multi-cliente:
      1. Cargar todos los clientes activos
      2. Validar sesión de COMPRASAL
      3. Extraer procesos (una sola vez)
      4. Por cada cliente: clasificar + enviar reporte personalizado
    """
    global _consecutive_errors

    config = get_config()
    run_timestamp = datetime.now(TZ_ELSALVADOR)

    # Cargar clientes activos
    active_clients = load_all_active_profiles(config.clients_dir)
    if not active_clients:
        logger.warning("No hay clientes activos en %s. Verificar los YAMLs.", config.clients_dir)
        return

    logger.info("=" * 60)
    logger.info(
        "Iniciando ejecución — %s | %d cliente(s) activo(s): %s",
        run_timestamp.strftime("%Y-%m-%d %H:%M %Z"),
        len(active_clients),
        ", ".join(c.client_id for c in active_clients),
    )
    logger.info("=" * 60)

    try:
        # 1. Validar sesión (con auto-restauración si TOTP está habilitado)
        session_ok = asyncio.run(is_session_valid(config))
        if not session_ok:
            logger.warning("Sesión de COMPRASAL inválida o expirada. Intentando restaurar...")
            restored = _try_restore_session(config)
            if not restored:
                for client in active_clients:
                    try:
                        send_session_expired_alert(config, client)
                    except Exception as e:
                        logger.error("No se pudo enviar alerta a %s: %s", client.client_id, e)
                _save_last_run(config, success=False, procesos_count=0, clients_processed=0)
                return

        # 2. Extraer procesos (una sola vez para todos los clientes)
        procesos = asyncio.run(extract_recent_procesos(config))
        logger.info("Procesos extraídos: %d (se reutilizarán para todos los clientes)", len(procesos))

        # 3. Clasificar y notificar por cada cliente activo
        if config.enable_drilldown:
            # Fase 2 · MVP Track A: clasifica + drill-down de pliegos + reporte con matriz.
            total_relevantes = _run_clients_with_drilldown(
                config, active_clients, procesos, len(procesos), run_timestamp
            )
        else:
            total_relevantes = 0
            for client_profile in active_clients:
                relevantes = _classify_and_notify(
                    config, client_profile, procesos, len(procesos), run_timestamp
                )
                total_relevantes += relevantes

        _save_last_run(
            config,
            success=True,
            procesos_count=len(procesos),
            clients_processed=len(active_clients),
        )
        _consecutive_errors = 0

        logger.info(
            "Ejecución completada. Procesos: %d | Clientes: %d | Relevantes totales: %d",
            len(procesos),
            len(active_clients),
            total_relevantes,
        )

    except Exception as exc:
        _consecutive_errors += 1
        logger.exception("Error crítico en el pipeline (error #%d): %s", _consecutive_errors, exc)
        _save_last_run(config, success=False, procesos_count=0, clients_processed=0)

        if _consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
            logger.critical("Circuit breaker activado (%d errores).", _consecutive_errors)
            for client in active_clients:
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
    # antes de construir la Config, que exige COMPRASAL_* y SMTP_* y reventaría
    # si faltan. Preguntar "qué versión corre aquí" tiene que funcionar en una VM
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
