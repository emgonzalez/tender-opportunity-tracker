"""src.aprendizaje.service — fachada TOLERANTE de la memoria de aprendizaje para la corrida diaria.

Mismo contrato que `src.memoria.service`: si la memoria está apagada (``ENABLE_MEMORIA`` o
``ENABLE_ESQUEMA_V2`` en falso), la ADB no está configurada o falla, cada función devuelve vacío
(``[]`` / ``None``) y deja un warning en el log. Nunca propaga: la corrida diaria no puede caerse por
la memoria de aprendizaje. Las escrituras humanas o del ciclo (términos, calibraciones) NO pasan por
acá: van directo a `src.aprendizaje.repo`, dentro de una transacción que sí debe fallar ruidoso.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable, Optional

from src.aprendizaje import repo

logger = logging.getLogger(__name__)

Conectar = Callable[[], contextlib.AbstractContextManager]


def habilitado(cfg: Any) -> bool:
    """Las tablas son del esquema v2 y viven en la ADB: hacen falta los dos flags y el DSN."""
    return bool(getattr(cfg, "enable_memoria", False) and getattr(cfg, "enable_esquema_v2", False)
                and getattr(cfg, "adb_dsn", ""))


def _conectar(cfg: Any, conectar: Optional[Conectar]) -> contextlib.AbstractContextManager:
    if conectar is not None:
        return conectar()
    from src.memoria import db  # perezoso: no arrastra oracledb si la memoria está apagada
    return db.connection(cfg)


def _tolerante(nombre: str, cfg: Any, conectar: Optional[Conectar], vacio: Any,
               operacion: Callable[[Any], Any]) -> Any:
    if conectar is None and not habilitado(cfg):
        return vacio
    try:
        with _conectar(cfg, conectar) as conn:
            return operacion(conn)
    except Exception as exc:  # noqa: BLE001 — la ADB caída degrada, nunca rompe la corrida
        logger.warning("aprendizaje.%s: la ADB no respondió (%s); se sigue sin memoria de aprendizaje",
                       nombre, type(exc).__name__)
        return vacio


def terminos_activos(cfg: Any, cliente_id: str, marca_id: Optional[str] = None,
                     conectar: Optional[Conectar] = None) -> list[dict[str, Any]]:
    return _tolerante("terminos_activos", cfg, conectar, [],
                      lambda conn: repo.listar_terminos_activos(conn, cliente_id, marca_id))


def calibracion_activa(cfg: Any, cliente_id: str, marca_id: Optional[str] = None,
                       conectar: Optional[Conectar] = None) -> Optional[dict[str, Any]]:
    return _tolerante("calibracion_activa", cfg, conectar, None,
                      lambda conn: repo.calibracion_activa(conn, cliente_id, marca_id))


def buscar_decision(cfg: Any, entrada_hash: str, capa: str, *, version: Optional[str] = None,
                    variante: str = "activa", cliente_id: Optional[str] = None,
                    conectar: Optional[Conectar] = None) -> Optional[dict[str, Any]]:
    return _tolerante("buscar_decision", cfg, conectar, None,
                      lambda conn: repo.buscar_decision_por_hash(conn, entrada_hash, capa, version=version,
                                                                 variante=variante, cliente_id=cliente_id))


def guardar_decision(cfg: Any, cliente_id: str, proceso_gid: str, capa: str, *,
                     conectar: Optional[Conectar] = None, **campos: Any) -> Optional[int]:
    return _tolerante("guardar_decision", cfg, conectar, None,
                      lambda conn: repo.guardar_decision(conn, cliente_id, proceso_gid, capa, **campos))


def listar_ejemplos(cfg: Any, cliente_id: str, marca_id: Optional[str] = None, *,
                    conectar: Optional[Conectar] = None, **filtros: Any) -> list[dict[str, Any]]:
    return _tolerante("listar_ejemplos", cfg, conectar, [],
                      lambda conn: repo.listar_ejemplos(conn, cliente_id, marca_id, **filtros))
