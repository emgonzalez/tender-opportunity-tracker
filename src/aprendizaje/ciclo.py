"""Ciclo semanal optativo, sin timer; dry-run no entrena ni escribe.

Una sola transacción para las escrituras. Cualquier fallo o vencimiento hace
rollback de TODO el ciclo; las alertas externas no son transaccionales.
El presupuesto se comprueba entre pasos y limita cada llamada SQL. En la CLI
Unix también se interrumpe mediante SIGALRM; en hilos el entrenador inyectado
debe respetar el plazo recibido.
"""
from __future__ import annotations

import contextlib
import logging
import math
import signal
import threading
import time

from src.aprendizaje import cosecha, deriva, minado, promocion, repo
from src.identidad.politica import AccionNoAuditable

logger = logging.getLogger(__name__)


class ErrorConfiguracion(ValueError):
    """Parámetros del ciclo inválidos."""


@contextlib.contextmanager
def _limite(segundos):
    """Interrumpe también conexión/entrenador en la CLI Unix, sin pisar alarmas ajenas."""
    usar = (hasattr(signal, 'setitimer') and threading.current_thread() is threading.main_thread()
            and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
    anterior = None
    if usar:
        def agotar(signum, frame):
            raise TimeoutError('presupuesto agotado')
        anterior = signal.signal(signal.SIGALRM, agotar)
        signal.setitimer(signal.ITIMER_REAL, segundos)
    try:
        yield
    finally:
        if usar:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, anterior)


def ejecutar(conectar, cliente_id, *, aplicar=False, segundos=300, entrenador=None,
             cfg=None, reloj=time.monotonic):
    if not cliente_id or not math.isfinite(segundos) or segundos <= 0:
        raise ErrorConfiguracion('cliente y presupuesto positivo son obligatorios')
    plazo = reloj() + segundos
    resumen = {'estado': 'omitido', 'aplicar': aplicar, 'pasos': {}}
    try:
        with _limite(segundos), conectar() as conn:
            previo_timeout = getattr(conn, 'call_timeout', None)
            try:
                def comprobar():
                    restante = plazo - reloj()
                    if restante <= 0:
                        raise TimeoutError('presupuesto agotado')
                    if previo_timeout is not None:
                        conn.call_timeout = max(1, int(restante * 1000))

                comprobar()
                # Serializa ciclos del mismo cliente, incluida la comprobación idempotente.
                cliente = repo._uno(conn, 'SELECT cliente_id FROM cliente WHERE cliente_id = :cliente_id '
                                    'FOR UPDATE', {'cliente_id': cliente_id})
                if not cliente:
                    raise ErrorConfiguracion('el cliente no existe')
                for nombre, paso in [('cosecha', cosecha.ejecutar), ('minado', minado.ejecutar)]:
                    comprobar()
                    resumen['pasos'][nombre] = paso(conn, cliente_id, aplicar=aplicar)
                comprobar()
                if entrenador is not None and aplicar:
                    resumen['pasos']['entrenamiento'] = entrenador(conn, cliente_id, plazo=plazo)
                else:
                    logger.info('Entrenamiento omitido: sin entrenador o dry-run')
                    resumen['pasos']['entrenamiento'] = 'omitido'
                comprobar()
                resumen['pasos']['promocion'] = promocion.ejecutar(conn, cliente_id, aplicar=aplicar, cfg=cfg)
                comprobar()
                resumen['pasos']['deriva'] = deriva.ejecutar(conn, cliente_id, aplicar=aplicar, cfg=cfg)
                comprobar()
                if not aplicar:
                    conn.rollback()
                resumen['estado'] = 'completado'
            finally:
                if previo_timeout is not None:
                    conn.call_timeout = previo_timeout
    except (ErrorConfiguracion, AccionNoAuditable):
        raise
    except Exception as exc:  # noqa: BLE001 — no exponer DSN ni abortar la corrida diaria
        resumen['estado'] = 'no ejecutado o revertido'
        resumen['motivo'] = type(exc).__name__
        logger.warning('Ciclo no ejecutado o revertido (%s); sin cambios persistidos', type(exc).__name__)
    return resumen


def texto(resumen):
    modo = 'aplicar' if resumen['aplicar'] else 'dry-run'
    lineas = [f"Ciclo semanal ({modo}): {resumen['estado']}."]
    if resumen.get('motivo'):
        lineas.append('ADB no disponible o paso fallido: ' + resumen['motivo'])
    for paso, resultado in resumen['pasos'].items():
        lineas.append(f'{paso}: {resultado}')
    return '\n'.join(lineas)
