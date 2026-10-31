"""src.web.admin — base común de la administración del portal (Fase G0 del plan de la Fase G).

Este módulo NO trae pantallas de recursos: trae el router (`/portal/admin`), el índice de solo lectura y la
plantilla de escritura `escritura_admin`, que G1 (usuarios), G2 (clientes y perfil) y G3 (plataforma) reutilizan
para que TODA escritura de administración pase por el mismo orden de guardas, igual que `_escritura_e11` del
portal:

    acceso → CSRF + Origin + límite de intentos → «ver como» rechazado → permiso → servicio → auditoría → 303

Cómo la usa un recurso nuevo (ejemplo de G1)::

    @router.post(rutas.ADMIN + "/users/{usuario_id}/deactivate")
    def baja_usuario(request: Request, usuario_id: str, csrf: str = Form("")):
        def escribir(conn, alcance, ip_hash):
            # El servicio revalida TODO (los <select> no son de fiar) y devuelve OK / NO_ENCONTRADO / INVALIDO /
            # CONFLICTO. Si no es OK no se audita y no se cambia nada.
            return service.baja_usuario(conn, alcance, usuario_id)

        return escritura_admin(request, csrf, "usuario_baja", "USUARIO_BAJA", f"usuario={usuario_id[:64]}",
                               escribir, destino=rutas.ADMIN + "/users")

- `plataforma=True` exige `Alcance.es_plataforma` (lo global); si no, basta `puede_administrar`.
- La conexión y la transacción las decide esta plantilla (`db.connection`): el servicio y la fila de auditoría
  van en la MISMA transacción, o ambos o ninguno.
- Lo ajeno (no es admin, el recurso no existe o es de otra organización) es el 404 uniforme del portal; el
  servicio lo señala devolviendo `NO_ENCONTRADO`.
- Una GET de administración usa `acceso_admin` y devuelve el 404 uniforme a quien no puede administrar.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from src.identidad import repo as identidad_repo
from src.memoria import db
from src.web import feedback, rutas
from src.web.acceso import _ip_hash, verificar_post
from src.web.plantillas import pagina
from src.web.portal import _acceso, _nav, _sin_datos

logger = logging.getLogger(__name__)

router = APIRouter()

# Resultados que devuelve el servicio que ejecuta `escribir`. Solo `OK` audita y confirma.
OK = "ok"
NO_ENCONTRADO = "no_encontrado"
INVALIDO = "invalido"
CONFLICTO = "conflicto"

# Códigos de `?notice=` de la administración (solo estos; nunca texto de la URL).
AVISOS_ADMIN = {
    OK: "admin_ok",
    INVALIDO: "admin_invalido",
    CONFLICTO: "admin_conflicto",
    "simulacion": "simulacion",
}
_TEXTOS_AVISO = {
    "admin_ok": ("ok", "Cambio guardado."),
    "admin_invalido": ("error", "Los datos no son válidos: no se guardó nada."),
    "admin_conflicto": ("error", "Alguien cambió esto antes que tú: revisa y vuelve a intentarlo."),
    "simulacion": ("error", "«Ver como» es de solo lectura: vuelve a tu vista para administrar."),
}


def _permiso(alcance, plataforma: bool) -> bool:
    return bool(alcance.es_plataforma if plataforma else alcance.puede_administrar)


def acceso_admin(request: Request, *, plataforma: bool = False):
    """``(alcance, None)`` si hay sesión Y permiso de administrar; si no, ``(None, respuesta)``: ir a iniciar
    sesión sin sesión y el 404 uniforme a quien no administra (ni se revela que la ruta existe)."""
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return None, respuesta
    if not _permiso(alcance, plataforma):
        return None, feedback.no_encontrado(request)
    return alcance, None


def _auditar_intento_ajeno(cfg, alcance, ambito: str) -> None:
    """Quien no administra y aun así hace POST a una ruta de administración: señal de abuso. Mejor esfuerzo."""
    try:
        with db.connection(cfg) as conn:
            identidad_repo.registrar_auditoria(
                conn, alcance.org_id, "ACCESO_FUERA_DE_ALCANCE", usuario_id=alcance.usuario_id,
                recurso=f"admin:{ambito[:100]}")
    except Exception:  # noqa: BLE001
        logger.exception("admin: no se pudo auditar un intento sin permiso")


def _con_aviso(destino: str, codigo: str) -> str:
    return destino + ("&" if "?" in destino else "?") + urlencode({rutas.Q_NOTICE: codigo})


def escritura_admin(
    request: Request, csrf: str, ambito: str, accion: str, recurso: str,
    escribir: Callable[..., str], *, destino: str = rutas.ADMIN, plataforma: bool = False,
):
    """Esqueleto común de toda escritura de administración (ver el docstring del módulo).

    :param ambito: nombre corto para el límite de intentos y el log (``"usuario_baja"``).
    :param accion: la acción de `ACCIONES_AUDITADAS` que se registra si `escribir` devuelve `OK`.
    :param recurso: sobre qué (``"usuario=<id>"``); sin correos ni secretos.
    :param escribir: ``escribir(conn, alcance, ip_hash) -> OK | NO_ENCONTRADO | INVALIDO | CONFLICTO``.
    :param destino: a dónde vuelve el 303 (ruta interna fija, nunca algo que mande el cliente).
    """
    cfg = request.app.state.cfg
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    rechazo = verificar_post(request, csrf, f"admin:{ambito}", limitador=request.app.state.limitador_estado)
    if rechazo is not None:
        return rechazo
    if getattr(alcance, "simulado", None):
        # Solo lectura: ni se consulta la base.
        return RedirectResponse(_con_aviso(rutas.PORTAL, AVISOS_ADMIN["simulacion"]), status_code=303)
    if not _permiso(alcance, plataforma):
        _auditar_intento_ajeno(cfg, alcance, ambito)
        return feedback.no_encontrado(request)
    ip_hash = _ip_hash(cfg, request)
    try:
        with db.connection(cfg) as conn:
            resultado = escribir(conn, alcance, ip_hash)
            if resultado == OK:
                identidad_repo.registrar_auditoria(
                    conn, alcance.org_id, accion, usuario_id=alcance.usuario_id, recurso=recurso,
                    ip_hash=ip_hash)
    except Exception:
        logger.exception("admin: no se pudo completar %s", ambito)
        return _sin_datos(request)
    if resultado == NO_ENCONTRADO:
        return feedback.no_encontrado(request)
    return RedirectResponse(_con_aviso(destino, AVISOS_ADMIN.get(resultado, AVISOS_ADMIN[INVALIDO])),
                            status_code=303)


def contexto_admin(request: Request, alcance, actual: str = "admin") -> dict:
    """Contexto común de las páginas de administración: navegación del portal + permisos + aviso."""
    return {**_nav(request, alcance, actual), "puede_administrar": alcance.puede_administrar,
            "admin_actual": True,
            "es_plataforma": alcance.es_plataforma,
            "aviso": _TEXTOS_AVISO.get(request.query_params.get(rutas.Q_NOTICE, ""))}


@router.get(rutas.ADMIN)
def indice(request: Request):
    """Índice de la administración. Solo lectura; G1-G3 le suman las tarjetas de sus recursos."""
    alcance, respuesta = acceso_admin(request)
    if respuesta is not None:
        return respuesta
    return pagina(request, "portal_admin_base.html", **contexto_admin(request, alcance))
