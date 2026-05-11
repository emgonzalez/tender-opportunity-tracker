"""
Scheduler de ejecuciones — loop de polling por minuto.

Se usa un loop simple en lugar de APScheduler porque APScheduler usa threads
internos que entran en conflicto con el event loop de asyncio que Playwright
necesita para funcionar. El conflicto hace que los triggers del scheduler nunca
se disparen, aunque el proceso esté vivo y el scheduler "iniciado".

El loop comprueba la hora una vez por minuto y ejecuta el pipeline cuando
el minuto actual coincide con un slot programado y ese slot no se ha ejecutado
todavía hoy. La precisión de ±1 minuto es suficiente para este caso de uso.
"""
import json
import logging
import os
import time
from datetime import date, datetime

import pytz

from src.config import get_config
from src.main import run_pipeline

logger = logging.getLogger(__name__)

TZ_ELSALVADOR = pytz.timezone("America/El_Salvador")


def _parse_time(hhmm: str) -> tuple[int, int]:
    parts = hhmm.split(":")
    return int(parts[0]), int(parts[1])


def _last_successful_run_time(last_run_path: str) -> datetime | None:
    """Retorna el timestamp de la última ejecución exitosa, o None."""
    if not os.path.exists(last_run_path):
        return None
    try:
        with open(last_run_path) as f:
            data = json.load(f)
        if data.get("success"):
            return datetime.fromisoformat(data["timestamp"])
    except Exception:
        pass
    return None


def _missed_slots(config, now: datetime) -> list[str]:
    """
    Retorna los nombres de los slots del día actual que ya pasaron
    pero no tienen una ejecución exitosa registrada.
    """
    last_run = _last_successful_run_time(config.last_run_path)

    slots = [
        (_parse_time(config.schedule_morning), "mañana", config.schedule_morning),
    ]

    missed = []
    for (hour, minute), label, hhmm in slots:
        slot_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= slot_dt and (last_run is None or last_run < slot_dt):
            logger.info("Ejecución de las %s no registrada hoy — se ejecutará ahora.", hhmm)
            missed.append(label)

    return missed


def start_scheduler() -> None:
    config = get_config()
    now = datetime.now(TZ_ELSALVADOR)

    # Catch-up: ejecutar slots del día que ya pasaron sin ejecución registrada
    missed = _missed_slots(config, now)
    if missed:
        for label in missed:
            logger.info("Ejecutando pasada pendiente (%s)...", label)
            run_pipeline()
    else:
        logger.info("Sin ejecuciones pendientes. Esperando próximo horario programado.")

    morning_h, morning_m = _parse_time(config.schedule_morning)

    logger.info(
        "Scheduler iniciado. Ejecución programada: %s (hora El Salvador, todos los días).",
        config.schedule_morning,
    )

    ran_today: date | None = None

    while True:
        now = datetime.now(TZ_ELSALVADOR)

        if now.hour == morning_h and now.minute == morning_m:
            today = now.date()
            if ran_today != today:
                ran_today = today
                logger.info("Ejecutando ejecución programada (mañana)...")
                run_pipeline()

        # Dormir hasta el inicio del próximo minuto
        sleep_secs = 60 - now.second - now.microsecond / 1_000_000
        time.sleep(max(1.0, sleep_secs))
