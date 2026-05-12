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
from datetime import datetime, timezone, timedelta

from src.config import get_config
from src.auth.session import is_session_valid
from src.auth.login import perform_login
from src.scraper.extractor import extract_recent_procesos
from src.classifier.client_profile import load_all_active_profiles, ClientProfile
from src.classifier.engine import classify_procesos
from src.history import save_run
from src.notifier.email_sender import (
    send_report,
    send_session_expired_alert,
    send_circuit_breaker_alert,
)

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
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Ejecutar el pipeline una sola vez y salir (sin iniciar el scheduler)",
    )
    args = parser.parse_args()

    config = get_config()
    _setup_logging(config.log_level)

    if args.run_now:
        run_pipeline()
    else:
        main()
