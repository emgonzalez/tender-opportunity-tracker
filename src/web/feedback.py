"""src.web.feedback — los enlaces "Relevante"/"Irrelevante" del correo.

    GET  /f/{token}/relevant      página con botón de confirmación   (NO escribe)
    POST /f/{token}/relevant      guarda el voto
    GET  /f/{token}/irrelevant    formulario de motivos y comentario (NO escribe)
    POST /f/{token}/irrelevant    guarda el voto

Un GET jamás escribe: Gmail y Outlook (SafeLinks) precargan los enlaces de los correos
para escanearlos, y un voto guardado por un escáner sería un voto fantasma. Por eso
también "Relevante" pasa por una página con un botón que hace POST.

El token es la única credencial y no hay sesión ni cookies (no hay CSRF clásico: quien
tiene el enlace ya tiene toda la capacidad). Cualquier fallo de resolución —firma mala,
cliente desconocido, feedback apagado— responde EXACTAMENTE lo mismo (404), para que
nadie pueda sondear qué falló.

Las rutas son `def` y no `async def`: oracledb es síncrono y Starlette corre las `def`
en un hilo aparte, así una consulta lenta a la ADB no congela el bucle de eventos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Form, Request

from src.classifier.alias_clientes import id_canonico
from src.classifier.client_profile import load_client_profile
from src.feedback import catalogo, service, tokens
from src.web import rutas
from src.web.plantillas import pagina

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/f")

_MENSAJE_404 = "Este enlace no es válido o ya no está disponible."
_MENSAJE_503 = (
    "Tu respuesta NO se guardó: no pudimos comunicarnos con el sistema. "
    "Vuelve a abrir el enlace del correo e inténtalo de nuevo en unos minutos."
)


@dataclass(frozen=True)
class _Contexto:
    """Lo que un token válido resuelve: quién vota, sobre qué y en nombre de quién."""

    cfg: object
    perfil: object
    proceso_id: str
    cliente_id: str
    dest_id: str
    email: str | None


def no_encontrado(request: Request):
    """El 404 único del servicio. También lo usa `app` para las rutas inexistentes."""
    return pagina(request, "feedback_error.html", status=404, titulo="Enlace no disponible",
                  mensaje=_MENSAJE_404)


def _resolver(request: Request, token: str) -> _Contexto | None:
    """Token → contexto, o `None` ante CUALQUIER causa (el llamador responde 404).

    La causa concreta se registra (sin el token) para quien opera el servicio; hacia
    afuera nunca se distingue.
    """
    cfg = request.app.state.cfg
    if not service.activo(cfg):
        logger.info("enlace rechazado: feedback apagado")
        return None

    payload = tokens.verificar(cfg.feedback_hmac_secret, token)
    if payload is None:
        logger.info("enlace rechazado: token inválido")
        return None

    try:
        perfil = load_client_profile(payload.cliente_id, cfg.clients_dir)
    except Exception:  # noqa: BLE001 — cliente inexistente o YAML roto: mismo 404
        logger.info("enlace rechazado: cliente desconocido (%s)", payload.cliente_id)
        return None

    # El token lleva un hash, no el correo: se busca a quién corresponde entre los
    # destinatarios que pudo haber tenido el correo. En MODO VALIDACIÓN el reporte sale
    # solo al buzón de `override_recipients`, y es ese el que quedó firmado en el token.
    notificacion = perfil.notification
    candidatos = [
        *notificacion.email_to,
        *notificacion.system_admins_email,
        *(getattr(cfg, "override_recipients", None) or []),
    ]
    email = next(
        (e for e in candidatos if tokens.destinatario_id(e) == payload.destinatario_id), None
    )
    # Sin correo resuelto el voto se guarda igual (email=None): perder el criterio de
    # alguien por un destinatario que ya salió del YAML sería peor que no atribuirlo.
    return _Contexto(
        cfg=cfg, perfil=perfil, proceso_id=payload.proceso_id, cliente_id=id_canonico(payload.cliente_id),
        dest_id=payload.destinatario_id, email=email,
    )


def _datos_proceso(ctx: _Contexto) -> dict:
    """Título e institución del proceso para mostrarlos; degrada a solo el código."""
    foto = service.leer_reportada(ctx.cfg, ctx.cliente_id, ctx.proceso_id) or {}
    return {
        "proceso_id": ctx.proceso_id,
        "titulo": foto.get("titulo") or "",
        "institucion": foto.get("institucion") or "",
    }


def _guardar(request: Request, ctx: _Contexto, voto: str, motivos: list[str], comentario: str):
    """Guarda y responde: gracias si quedó, 503 honesto si no. Nunca 'gracias' sin guardar."""
    guardado = service.guardar_voto(
        ctx.cfg, client_id=ctx.cliente_id, proceso_id=ctx.proceso_id, dest_id=ctx.dest_id,
        email=ctx.email, voto=voto, motivos=motivos, comentario=comentario,
    )
    # Ni el token ni el comentario van al log: uno es la credencial, el otro es texto
    # libre de una persona.
    logger.info("voto %s guardado=%s proceso=%s cliente=%s destinatario=%s",
                voto, guardado, ctx.proceso_id, ctx.cliente_id, ctx.dest_id)
    if not guardado:
        return pagina(request, "feedback_error.html", status=503,
                      titulo="No se guardó tu respuesta", mensaje=_MENSAJE_503)
    return pagina(request, "feedback_gracias.html", voto=voto, **_datos_proceso(ctx))


@router.get("/{token}/" + rutas.VOTO_RELEVANT)
def confirmar_relevante(request: Request, token: str):
    ctx = _resolver(request, token)
    if ctx is None:
        return no_encontrado(request)
    return pagina(request, "feedback_confirmar.html", **_datos_proceso(ctx))


@router.post("/{token}/" + rutas.VOTO_RELEVANT)
def votar_relevante(request: Request, token: str):
    ctx = _resolver(request, token)
    if ctx is None:
        return no_encontrado(request)
    return _guardar(request, ctx, "RELEVANTE", [], "")


@router.get("/{token}/" + rutas.VOTO_IRRELEVANT)
def formulario_irrelevante(request: Request, token: str):
    ctx = _resolver(request, token)
    if ctx is None:
        return no_encontrado(request)

    previo = service.leer_voto(ctx.cfg, ctx.cliente_id, ctx.proceso_id, ctx.dest_id)
    # Solo se precargan motivos y comentario de un voto IRRELEVANTE anterior; si el
    # previo fue RELEVANTE se avisa, pero el formulario arranca vacío.
    era_irrelevante = bool(previo) and previo["voto"] == "IRRELEVANTE"
    return pagina(
        request, "feedback_irrelevante.html",
        motivos=catalogo.motivos_de(ctx.perfil),
        seleccionados=list(previo["motivos"]) if era_irrelevante else [],
        comentario=previo["comentario"] if era_irrelevante else "",
        comentario_max=catalogo.COMENTARIO_MAX,
        voto_previo=previo["voto"] if previo else None,
        **_datos_proceso(ctx),
    )


@router.post("/{token}/" + rutas.VOTO_IRRELEVANT)
def votar_irrelevante(
    request: Request,
    token: str,
    motivos: Annotated[list[str] | None, Form()] = None,
    comentario: Annotated[str, Form()] = "",
):
    ctx = _resolver(request, token)
    if ctx is None:
        return no_encontrado(request)
    # Lo que llega del navegador no es de fiar: solo pasan los códigos del catálogo del
    # cliente (sin repetidos) y el comentario se recorta. Ambos son opcionales.
    validos = catalogo.validar_motivos(motivos or [], catalogo.motivos_de(ctx.perfil))
    return _guardar(request, ctx, "IRRELEVANTE", validos, catalogo.limpiar_comentario(comentario))
