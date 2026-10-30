"""Sesión de COMPRASAL (storage_state de Playwright) en la ADB, cifrada — Fase 5 de persistencia-adb.

`SESION_FUENTE=archivo` (default): este módulo no hace nada y la sesión vive en `data/storage_state.json`.
`SESION_FUENTE=adb`: la sesión vive cifrada en `sesion_portal_compras` (contexto distinto del de MFA y del resto
de la persistencia). Se MATERIALIZA en un directorio temporal del contenedor (no en el volumen `data/`) para que
Playwright la lea, y se devuelve a la ADB solo si cambió. Cualquier fallo de la ADB (caída, DSN, blob dañado,
clave distinta) se traga: la corrida sigue con el archivo local o con un login nuevo con el TOTP del `.env`.
El contenido en claro jamás se loguea ni se escribe fuera de la ruta temporal (0o600, directorio 0o700).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from typing import Protocol

from src.persistencia.cache import escribir_atomico, leer_json
from src.persistencia.cifrado import cifrar, descifrar
from src.persistencia.fuente import manda_adb

logger = logging.getLogger(__name__)

PORTAL_ID = "comprasal_sv"
_CONTEXTO = "sesion_portal_compras/"


class Conflicto(Exception):
    """La versión de la fila cambió entre la lectura y la escritura."""


class Almacen(Protocol):
    def leer(self, portal_id: str) -> tuple[bytes, int] | None: ...

    def escribir(self, portal_id: str, contenido: bytes, version_esperada: int) -> int:
        """Devuelve la versión nueva; levanta `Conflicto` si `version_esperada` ya no es la vigente."""


class AlmacenOracle:
    """`sesion_portal_compras` en la ADB (DDL en la solicitud de integración de la Fase 5)."""

    def __init__(self, cfg):
        self.cfg = cfg

    def leer(self, portal_id):
        from src.memoria.db import connection
        with connection(self.cfg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT contenido, version FROM sesion_portal_compras WHERE portal_id = :p", p=portal_id)
            fila = cur.fetchone()
            if not fila:
                return None
            blob = fila[0].read() if hasattr(fila[0], "read") else fila[0]
            return bytes(blob), int(fila[1])

    def escribir(self, portal_id, contenido, version_esperada):
        import oracledb

        from src.memoria.db import connection
        with connection(self.cfg) as conn:
            cur = conn.cursor()
            if version_esperada <= 0:
                try:
                    cur.execute(
                        "INSERT INTO sesion_portal_compras (portal_id, contenido, version) VALUES (:p, :c, 1)",
                        p=portal_id, c=contenido,
                    )
                except oracledb.IntegrityError as exc:  # ORA-00001: otro la creó antes
                    raise Conflicto() from exc
                return 1
            cur.execute(
                "UPDATE sesion_portal_compras SET contenido = :c, version = version + 1, "
                "actualizado_en = SYSTIMESTAMP WHERE portal_id = :p AND version = :v",
                c=contenido, p=portal_id, v=version_esperada,
            )
            if cur.rowcount != 1:
                raise Conflicto()
            return version_esperada + 1


# ── Rutas ────────────────────────────────────────────────────────────────────

def directorio_temporal() -> str:
    """Directorio del contenedor (no el volumen `data/`). `SESION_TMP_DIR` lo fija en tests."""
    return os.environ.get("SESION_TMP_DIR") or os.path.join(tempfile.gettempdir(), "amalia-sesion")


def ruta_sesion(data_dir: str, env: dict | None = None) -> str:
    """Dónde leen y escriben `storage_state` los demás módulos: `data/` con `archivo`, el temporal con `adb`."""
    if manda_adb("SESION", env):
        return os.path.join(directorio_temporal(), "storage_state.json")
    return os.path.join(data_dir, "storage_state.json")


def _meta(ruta: str) -> str:
    return ruta + ".meta"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _clave(cfg) -> str:
    return getattr(cfg, "identidad_secret", "") or ""


def _asegurar_dir(ruta: str) -> None:
    carpeta = os.path.dirname(ruta)
    os.makedirs(carpeta, exist_ok=True)
    try:
        os.chmod(carpeta, 0o700)
    except OSError:
        pass


# ── API ──────────────────────────────────────────────────────────────────────

def preparar_sesion(cfg, almacen: Almacen | None = None, portal_id: str = PORTAL_ID) -> bool:
    """Trae la sesión de la ADB al temporal. True si quedó una sesión materializada nueva. Nunca levanta."""
    if not manda_adb("SESION"):
        return False
    try:
        ruta = ruta_sesion(cfg.data_dir)
        almacen = almacen or AlmacenOracle(cfg)
        fila = almacen.leer(portal_id)
        if fila is None:
            logger.info("sesión: la ADB no tiene sesión de %s; se usará login", portal_id)
            return False
        blob, version = fila
        meta = leer_json(_meta(ruta), {}) or {}
        if os.path.exists(ruta) and int(meta.get("version", 0)) >= version:
            return False  # lo local ya es igual o más nuevo
        claro = descifrar(_clave(cfg), blob.decode("ascii"), _CONTEXTO + portal_id)
        if claro is None:
            logger.warning("sesión: el blob de la ADB no se pudo descifrar (clave distinta o dañado); se usará login")
            return False
        _asegurar_dir(ruta)
        escribir_atomico(ruta, claro, 0o600)
        escribir_atomico(_meta(ruta), json.dumps({"version": version, "sha": _sha(claro)}), 0o600)
        logger.info("sesión: materializada desde la ADB (versión %s)", version)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("sesión: ADB no disponible (%s); se sigue con lo local o login", type(exc).__name__)
        return False


def devolver_sesion(cfg, almacen: Almacen | None = None, portal_id: str = PORTAL_ID) -> bool:
    """Sube la sesión a la ADB si cambió respecto de lo materializado. True si se escribió. Nunca levanta."""
    if not manda_adb("SESION"):
        return False
    try:
        ruta = ruta_sesion(cfg.data_dir)
        if not os.path.exists(ruta):
            return False
        with open(ruta, "rb") as f:
            claro = f.read()
        meta = leer_json(_meta(ruta), {}) or {}
        if meta.get("sha") == _sha(claro):
            return False
        blob = cifrar(_clave(cfg), claro, _CONTEXTO + portal_id).encode("ascii")
        almacen = almacen or AlmacenOracle(cfg)
        version = int(meta.get("version", 0))
        try:
            nueva = almacen.escribir(portal_id, blob, version)
        except Conflicto:
            # Otra VM/corrida escribió antes. Esta sesión es la recién obtenida: gana sobre la vigente.
            fila = almacen.leer(portal_id)
            nueva = almacen.escribir(portal_id, blob, fila[1] if fila else 0)
        _asegurar_dir(ruta)
        escribir_atomico(_meta(ruta), json.dumps({"version": nueva, "sha": _sha(claro)}), 0o600)
        logger.info("sesión: devuelta a la ADB (versión %s)", nueva)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("sesión: no se pudo guardar en la ADB (%s); queda solo en el temporal", type(exc).__name__)
        return False
