"""src.identidad.ver_como — «ver como» de admin_plataforma (E10 del plan de identidad).

Un alcance SIMULADO dentro de la sesión del admin, no una suplantación: no hay sesión ni identidad
ajena y toda traza queda a nombre del admin real. La elección vive en la fila de `sesion` (repo), se
valida contra las opciones calculadas en servidor (nada de texto libre) y nunca queda escrita sin
su auditoría: columna y fila de `auditoria` entran en la misma transacción o no entra ninguna.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.analytics.regional import nombres_clientes
from src.identidad import politica, repo
from src.identidad.alcance import Alcance, puede_ver_como

logger = logging.getLogger(__name__)

GRUPO_ORG = "Roles de organización"
GRUPO_PAIS = "Roles de país"
SEPARADOR = "|"
MAX_LARGO = 100


@dataclass(frozen=True)
class Opcion:
    valor: str      # lo que viaja en el form: `rol` o `rol|cliente`
    etiqueta: str
    grupo: str


def etiqueta(rol: str, cliente_nombre: str | None = None) -> str:
    """``gerente_pais`` + «El Salvador» → «Gerente pais · El Salvador»."""
    texto = rol.replace("_", " ").capitalize()
    return f"{texto} · {cliente_nombre}" if cliente_nombre else texto


def opciones(conn: Any, real: Alcance | None) -> list[Opcion]:
    """Vistas que ``real`` puede elegir; vacía si no puede simular. «Mi vista» la agrega la UI.

    Roles de organización en orden alfabético; luego cada rol de país × cada país activo de la org
    (ordenados por nombre). Sin países activos solo se ofrecen los de organización.
    """
    if not puede_ver_como(real):
        return []
    salida = [Opcion(rol, etiqueta(rol), GRUPO_ORG)
              for rol in sorted(politica.ROLES_SIMULABLES - politica.ROLES_PAIS)]
    activos = list(repo.clientes_activos(conn, real.org_id))
    if activos:
        nombres = nombres_clientes(conn, activos)
        paises = sorted(activos, key=lambda c: (nombres.get(c) or c, c))
        for rol in sorted(politica.ROLES_SIMULABLES & politica.ROLES_PAIS):
            salida += [Opcion(f"{rol}{SEPARADOR}{c}", etiqueta(rol, nombres.get(c) or c), GRUPO_PAIS)
                       for c in paises]
    return salida


def _parsear(vista: Any) -> tuple[str, str | None] | None:
    """Forma estricta: ``rol`` o ``rol|cliente``, sin espacios ni controles, cada parte ≤ 100."""
    if not isinstance(vista, str) or not vista or len(vista) > 2 * MAX_LARGO + 1:
        return None
    if any(c.isspace() or not c.isprintable() for c in vista):
        return None
    partes = vista.split(SEPARADOR)
    if len(partes) > 2 or any(not p or len(p) > MAX_LARGO for p in partes):
        return None
    return partes[0], (partes[1] if len(partes) == 2 else None)


def validar(conn: Any, real: Alcance | None, vista: Any) -> tuple[str, str | None] | None:
    """``(rol, cliente|None)`` si ``vista`` es EXACTAMENTE una de `opciones`; si no, None.

    ``""`` («Mi vista») no es válido aquí: volver es `terminar`.
    """
    par = _parsear(vista)
    if par is None or not puede_ver_como(real):
        return None
    if vista not in {o.valor for o in opciones(conn, real)}:
        return None
    return par


def _rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 — el fallo original es el que importa
        logger.exception("ver_como: falló el rollback")


def iniciar(conn: Any, real: Alcance | None, token_hash: str, vista: Any, ip_hash: str | None) -> bool:
    """Fija la vista simulada en la sesión y la audita (`VER_COMO_INICIO`) en UNA transacción.

    False si la vista no es válida para ``real``, si la sesión no aceptó la escritura o ante
    cualquier error (rollback): nunca queda simulación sin auditoría.
    """
    par = validar(conn, real, vista)
    if par is None:
        return False
    rol, cliente = par
    try:
        if not repo.fijar_ver_como(conn, token_hash, rol, cliente):
            _rollback(conn)
            return False
        repo.registrar_auditoria(conn, real.org_id, "VER_COMO_INICIO", usuario_id=real.usuario_id,
                                 recurso=f"rol={rol};cliente={cliente or '-'}", ip_hash=ip_hash)
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 — cierra en falso: sin auditoría no hay simulación
        logger.exception("ver_como: no se pudo iniciar la simulación")
        _rollback(conn)
        return False


def terminar(conn: Any, real: Alcance | None, token_hash: str, ip_hash: str | None) -> bool:
    """Vuelve a la vista real y lo audita (`VER_COMO_FIN`) en UNA transacción.

    No exige seguir siendo admin: limpiar nunca amplía el alcance. False (sin auditar) si no había
    simulación que limpiar o ante cualquier error (rollback).
    """
    if real is None:
        return False
    try:
        if not repo.limpiar_ver_como(conn, token_hash):
            _rollback(conn)
            return False
        repo.registrar_auditoria(conn, real.org_id, "VER_COMO_FIN", usuario_id=real.usuario_id,
                                 ip_hash=ip_hash)
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 — sin auditoría no se confirma la salida
        logger.exception("ver_como: no se pudo terminar la simulación")
        _rollback(conn)
        return False
