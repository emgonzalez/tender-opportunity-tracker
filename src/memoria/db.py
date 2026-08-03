"""src.memoria.db — Fase 2 Track B. Ver docs/planes/fase2-track-b.md.

Conexión a Oracle Autonomous Database (ADB 23ai) en modo **THIN** (Python
puro, sin Oracle Instant Client). El modo thin de python-oracledb soporta
mTLS con wallet, que es justo lo que necesitamos para conectar a una ADB
desde una VM OCI Free Tier con ~1 GB de RAM: instalar el Instant Client
(modo "grueso") añadiría ~200 MB y no es viable ahí.

Responsabilidades de este módulo:
    - Obtener el wallet mTLS (local si ya existe, o descargado de OCI Vault
      vía Instance Principal) hacia un directorio en tmpfs (RAM), nunca en
      disco persistente.
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
import os
import shutil
import zipfile
from base64 import b64decode
from typing import TYPE_CHECKING, Any, Iterator

import oracledb
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    # Solo para type hints — evita que el SDK de OCI sea un import
    # obligatorio a nivel de módulo (py_compile debe pasar aunque no esté
    # instalado; solo hace falta si en efecto se usa Vault).
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


class WalletNoDisponibleError(Exception):
    """No hay wallet local ni OCID de Vault configurado para obtenerlo.

    Sin wallet no hay forma de establecer mTLS contra la ADB, así que
    cualquier intento de conexión debe abortar de inmediato con un mensaje
    claro en vez de fallar más adelante con un error críptico de oracledb.
    """


def _wallet_presente(wallet_dir: str) -> bool:
    """Determina si ``wallet_dir`` ya contiene un wallet utilizable.

    Se considera "presente" con que el directorio exista y tenga al menos
    un archivo dentro (p.ej. ``ewallet.pem``, ``tnsnames.ora``,
    ``cwallet.sso``): no exigimos nombres exactos porque el contenido del
    wallet puede variar según cómo fue generado.

    :param wallet_dir: ruta candidata al wallet.
    :return: True si ya hay un wallet ahí, False si hace falta descargarlo.
    """
    if not os.path.isdir(wallet_dir):
        return False
    try:
        entradas = [
            nombre
            for nombre in os.listdir(wallet_dir)
            if os.path.isfile(os.path.join(wallet_dir, nombre))
        ]
    except OSError:
        return False
    return len(entradas) > 0


def fetch_wallet(cfg: "Config") -> str:
    """Garantiza que el wallet mTLS esté disponible en ``cfg.adb_wallet_dir``.

    Es idempotente: si el wallet ya existe localmente (por ejemplo, de una
    descarga previa dentro del mismo proceso), se reutiliza tal cual sin
    volver a golpear OCI Vault. Solo si no existe y hay un OCID de secret
    configurado se descarga; si no hay ninguna de las dos cosas, se lanza
    ``WalletNoDisponibleError``.

    El SDK de OCI (`oci`) se importa DENTRO de esta función a propósito:
    así el módulo completo (y su import) no depende de que el SDK esté
    instalado cuando no se usa Vault (por ejemplo, en tests o en un entorno
    donde el wallet ya viene montado por otro medio).

    :param cfg: instancia de Config con ``adb_wallet_dir`` y
        ``adb_wallet_secret_ocid``.
    :return: ruta al directorio del wallet, lista para usar como
        ``config_dir`` / ``wallet_location`` de oracledb.
    :raises WalletNoDisponibleError: si no hay wallet local ni OCID de
        Vault configurado.
    """
    wallet_dir = cfg.adb_wallet_dir

    if _wallet_presente(wallet_dir):
        logger.info(
            "Wallet ya presente en %s (local); se omite descarga de Vault.",
            wallet_dir,
        )
        return wallet_dir

    if not cfg.adb_wallet_secret_ocid:
        raise WalletNoDisponibleError(
            f"No hay wallet local en {wallet_dir!r} ni "
            "ADB_WALLET_SECRET_OCID configurado; no es posible conectar a "
            "Oracle ADB."
        )

    logger.info(
        "Wallet no encontrado en %s; descargando desde OCI Vault (secret %s)...",
        wallet_dir,
        cfg.adb_wallet_secret_ocid,
    )

    import oci  # noqa: PLC0415 — import perezoso, ver docstring.

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    secrets_client = oci.secrets.SecretsClient(config={}, signer=signer)
    bundle = secrets_client.get_secret_bundle(cfg.adb_wallet_secret_ocid)
    contenido_b64 = bundle.data.secret_bundle_content.content
    zip_bytes = b64decode(contenido_b64)

    os.makedirs(wallet_dir, mode=0o700, exist_ok=True)
    os.chmod(wallet_dir, 0o700)

    zip_path = os.path.join(wallet_dir, "_wallet_download.zip")
    try:
        with open(zip_path, "wb") as fh:
            fh.write(zip_bytes)
        os.chmod(zip_path, 0o600)

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(wallet_dir)
            for nombre in zf.namelist():
                ruta = os.path.join(wallet_dir, nombre)
                if os.path.isfile(ruta):
                    os.chmod(ruta, 0o600)
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    logger.info("Wallet descargado y extraído en %s (OCI Vault).", wallet_dir)
    return wallet_dir


def _es_error_transitorio(exc: BaseException) -> bool:
    """Decide si una excepción de Oracle es transitoria (reintentable).

    Se inspecciona ``oracledb.DatabaseError`` y el ``.code`` de su primer
    argumento (el objeto de error interno de oracledb, ``_Error``, expone
    ``.code`` con el número ORA). Se consideran transitorios los errores de
    red/timeout de conexión: ORA-12170 (timeout de conexión TNS) y
    ORA-03113/ORA-03114 (conexión caída). Los errores de credenciales o
    privilegios (ORA-01017, ORA-01031) NUNCA se consideran transitorios:
    reintentarlos no cambiaría el resultado, solo retrasaría el fallo.

    :param exc: excepción capturada durante la creación del pool.
    :return: True si debe reintentarse, False si debe propagarse ya.
    """
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
def _crear_pool(cfg: "Config", wallet_dir: str) -> "oracledb.ConnectionPool":
    """Crea el pool de conexiones a Oracle ADB (modo THIN), con reintentos.

    Reintenta hasta 3 veces con backoff exponencial (2s, 4s, 8s) SOLO ante
    errores transitorios de red/timeout (ver ``_es_error_transitorio``);
    cualquier otro error (p.ej. credenciales inválidas) se propaga en el
    primer intento.

    :param cfg: instancia de Config con las credenciales/DSN de la ADB.
    :param wallet_dir: directorio del wallet mTLS ya resuelto por
        ``fetch_wallet``.
    :return: pool de conexiones de oracledb.
    """
    return oracledb.create_pool(
        user=cfg.adb_user,
        password=cfg.adb_password,
        dsn=cfg.adb_dsn,
        min=1,
        max=1,
        increment=0,
        config_dir=wallet_dir,
        wallet_location=wallet_dir,
        wallet_password=cfg.adb_wallet_password,
    )


def get_pool(cfg: "Config") -> "oracledb.ConnectionPool":
    """Devuelve el pool de conexiones singleton a Oracle ADB (modo THIN).

    Crea el pool la primera vez que se invoca (asegurando primero el wallet
    vía ``fetch_wallet``) y reutiliza la misma instancia en llamadas
    posteriores. El pool se limita a ``min=1, max=1``: la VM tiene 1 GB de
    RAM y el pipeline solo necesita una conexión a la vez (concurrencia 1).

    :param cfg: instancia de Config con credenciales, DSN y wallet.
    :return: pool de conexiones de oracledb (singleton a nivel de proceso).
    """
    global _pool
    if _pool is not None:
        return _pool

    wallet_dir = fetch_wallet(cfg)
    logger.info(
        "Creando pool Oracle ADB (modo THIN, min=1 max=1) hacia dsn=%s...",
        cfg.adb_dsn,
    )
    _pool = _crear_pool(cfg, wallet_dir)
    logger.info("Pool Oracle ADB creado correctamente.")
    return _pool


@contextlib.contextmanager
def connection(cfg: "Config") -> Iterator[Any]:
    """Cede una conexión del pool con commit/rollback automático.

    Toma una conexión del pool (``pool.acquire()``); si el bloque ``with``
    termina sin excepción, hace ``commit()``; si lanza una excepción, hace
    ``rollback()`` y la re-lanza. En cualquier caso, devuelve la conexión al
    pool en el ``finally`` (nunca la deja "fugada").

    :param cfg: instancia de Config.
    :yield: conexión de oracledb tomada del pool.
    """
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


def close_pool(cfg: "Config | None" = None) -> None:
    """Cierra el pool de conexiones y, opcionalmente, borra el wallet.

    Cierra el pool (si existe) con ``force=True`` y resetea el singleton
    global para que una futura llamada a ``get_pool`` cree uno nuevo. Si se
    pasa ``cfg``, además borra el wallet de tmpfs (``adb_wallet_dir``): el
    wallet es material sensible (mTLS) y no debe sobrevivir al proceso.

    :param cfg: instancia de Config; si se provee, se borra su wallet.
    """
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

    if cfg is not None:
        shutil.rmtree(cfg.adb_wallet_dir, ignore_errors=True)
        logger.info("Wallet eliminado de tmpfs (%s).", cfg.adb_wallet_dir)


def keepalive(cfg: "Config") -> bool:
    """Ejecuta un ping trivial (``SELECT 1 FROM dual``) contra la ADB.

    Por qué existe: las Autonomous Database "Always Free" de OCI se
    auto-detienen tras 7 días consecutivos sin actividad, y una instancia
    detenida puede llegar a ser reclamada por Oracle (recursos liberados)
    tras permanecer detenida ~90 días. Para no perder la base de memoria
    institucional entre corridas, este ping se ejecuta en CADA corrida
    diaria del pipeline, incluso si ``enable_memoria`` (u otros feature
    flags de Track B) están desactivados: basta con que la corrida exista
    para contar como actividad y resetear el contador de inactividad de la
    ADB.

    Nunca propaga excepciones: cualquier fallo (red, wallet ausente,
    credenciales, ADB detenida, etc.) se captura por completo, se registra
    como warning y se devuelve False, para no interrumpir el resto del
    pipeline (scraper/clasificador/notifier).

    :param cfg: instancia de Config.
    :return: True si el ping tuvo éxito, False ante cualquier excepción.
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
