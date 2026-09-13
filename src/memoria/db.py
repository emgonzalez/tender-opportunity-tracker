"""src.memoria.db — Fase 2 Track B. Ver docs/planes/fase2-track-b.md.

Conexión a Oracle Autonomous Database (ADB 23ai) en modo **THIN** (Python
puro, sin Oracle Instant Client) con **TLS one-way** (sin wallet mTLS).

TLS one-way requiere que la ADB tenga habilitada la conexión TLS (no solo
mTLS) y una ACL de red (VCN o IP) configurada — ver ``oci/lib/05-adb.sh``.
El DSN es un connection string completo que incluye el host, puerto 1521 y
service_name (se obtiene de la ADB tras habilitar TLS y se pone en .env
como ``ADB_DSN``).

Responsabilidades de este módulo:
    - Mantener un pool de conexiones singleton de tamaño 1 (la VM y la
      concurrencia del pipeline no dan para más).
    - Reintentar la creación del pool solo ante errores transitorios de
      red/timeout, nunca ante errores de credenciales o privilegios.
    - Exponer un context manager con commit/rollback automático.
    - Un "keepalive" que se ejecuta en cada corrida diaria (incluso con
      Track B apagado) para evitar que la ADB Always Free se auto-detenga
      por inactividad.
"""
from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, Iterator

import oracledb
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from src.config import Config

logger = logging.getLogger(__name__)

# Códigos ORA que consideramos transitorios (red/timeout de conexión) y por
# lo tanto reintentables.
_ORA_TRANSITORIOS = {
    12170,  # TNS:Connect timeout occurred
    3113,  # end-of-file on communication channel
    3114,  # not connected to ORACLE
}

# Códigos ORA que NUNCA deben reintentarse: son errores de credenciales o
# privilegios, reintentar no cambia el resultado y solo retrasa el fallo.
_ORA_NO_REINTENTABLES = {
    1017,  # invalid username/password; logon denied
    1031,  # insufficient privileges
}

# Pool de conexiones singleton a nivel de módulo.
_pool: "oracledb.ConnectionPool | None" = None


def _es_error_transitorio(exc: BaseException) -> bool:
    """Decide si una excepción de Oracle es transitoria (reintentable)."""
    if not isinstance(exc, oracledb.DatabaseError):
        return False
    if not exc.args:
        return False
    error_obj = exc.args[0]
    code = getattr(error_obj, "code", None)
    if not code:
        return False
    if code in _ORA_NO_REINTENTABLES:
        return False
    return code in _ORA_TRANSITORIOS


@retry(
    retry=retry_if_exception(_es_error_transitorio),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    reraise=True,
)
def _crear_pool(cfg: "Config") -> "oracledb.ConnectionPool":
    """Crea el pool de conexiones a Oracle ADB (modo THIN, TLS one-way).

    Reintenta hasta 3 veces con backoff exponencial (2s, 4s, 8s) SOLO ante
    errores transitorios de red/timeout; cualquier otro error se propaga en
    el primer intento.
    """
    return oracledb.create_pool(
        user=cfg.adb_user,
        password=cfg.adb_password,
        dsn=cfg.adb_dsn,
        min=1,
        max=1,
        increment=0,
    )


def get_pool(cfg: "Config") -> "oracledb.ConnectionPool":
    """Devuelve el pool de conexiones singleton a Oracle ADB (modo THIN).

    Crea el pool la primera vez y reutiliza la misma instancia en llamadas
    posteriores. Pool limitado a ``min=1, max=1``.
    """
    global _pool
    if _pool is not None:
        return _pool

    logger.info(
        "Creando pool Oracle ADB (modo THIN, TLS one-way, min=1 max=1) hacia dsn=%s...",
        cfg.adb_dsn,
    )
    _pool = _crear_pool(cfg)
    logger.info("Pool Oracle ADB creado correctamente.")
    return _pool


@contextlib.contextmanager
def connection(cfg: "Config") -> Iterator[Any]:
    """Cede una conexión del pool con commit/rollback automático."""
    pool = get_pool(cfg)
    conn = pool.acquire()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.release(conn)


def close_pool() -> None:
    """Cierra el pool de conexiones y resetea el singleton."""
    global _pool
    if _pool is not None:
        try:
            _pool.close(force=True)
        except Exception:
            logger.warning(
                "Error cerrando el pool de Oracle ADB (se ignora).",
                exc_info=True,
            )
        _pool = None


def keepalive(cfg: "Config") -> bool:
    """Ejecuta un ping trivial (``SELECT 1 FROM dual``) contra la ADB.

    Evita el auto-stop de la ADB Always Free (7 días sin actividad) y la
    reclamación (~90 días detenida). Se ejecuta en CADA corrida diaria,
    incluso con ``enable_memoria`` apagado.

    Nunca propaga excepciones.
    """
    try:
        with connection(cfg) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM dual")
            cursor.fetchone()
        return True
    except Exception:
        logger.warning("Keepalive de Oracle ADB falló (se ignora).", exc_info=True)
        return False
