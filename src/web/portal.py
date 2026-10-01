"""src.web.portal — vista de país del portal AMALIA (KPIs 1-6, Fase 1 del plan).

Ver `docs/planes/portal-amalia.md` §4. Sin autenticación hasta la Fase 3: por eso el
router solo responde con `ENABLE_PORTAL=true` y sirve un único cliente
(`PORTAL_CLIENTE_ID`). Apagado, `/portal` es un 404 idéntico al de cualquier ruta.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from src.analytics import pais
from src.memoria import db
from src.web import feedback, graficos
from src.web.plantillas import pagina

logger = logging.getLogger(__name__)

router = APIRouter()

_RANGOS = (30, 90, 180)
_COLOR_SEVERIDAD = {"alta": "var(--critico)", "media": "var(--aviso)", "baja": "var(--bueno)"}


def _rango(valor: str | None) -> int:
    """`?dias=` solo acepta los rangos ofrecidos; cualquier otra cosa cae al de 90."""
    return int(valor) if valor and valor.isdigit() and int(valor) in _RANGOS else 90


@router.get("/portal")
def vista_pais(request: Request):
    cfg = request.app.state.cfg
    if not getattr(cfg, "enable_portal", False):
        return feedback.no_encontrado(request)
    cliente = cfg.portal_cliente_id
    dias = _rango(request.query_params.get("dias"))

    try:
        with db.connection(cfg) as conn:
            oportunidades = pais.oportunidades_del_mes(conn, cliente)
            relevancia = pais.pct_relevancia_clasificador(conn, cliente, dias=30)
            instituciones = pais.instituciones_top(conn, cliente, dias=dias)
            embudo = pais.embudo_pipeline(conn, cliente, dias=dias)
            severidad = pais.severidad_riesgos(conn, cliente, dias=dias)
    except Exception:
        # Una ADB caída no debe mostrar un traceback ni un portal con ceros falsos.
        logger.exception("portal: no se pudieron leer los KPIs de %s", cliente)
        return pagina(request, "feedback_error.html", status=503, titulo="Portal no disponible",
                      mensaje="No pudimos leer los datos en este momento. Probá de nuevo en unos minutos.")

    return pagina(
        request, "portal_pais.html",
        cliente=cliente, dias=dias, rangos=_RANGOS,
        oportunidades=oportunidades,
        spark=graficos.sparkline(oportunidades["serie"]),
        relevancia=relevancia,
        g_instituciones=graficos.barras([(i["institucion"], i["conteo"]) for i in instituciones]),
        g_embudo=graficos.embudo([(e["estado"], e["conteo"]) for e in embudo["camino"]]),
        salidas=embudo["salidas"],
        g_severidad=graficos.barras(
            [(s["severidad"], s["conteo"]) for s in severidad],
            [_COLOR_SEVERIDAD.get(s["severidad"], "var(--serie)") for s in severidad],
        ),
        hay_severidad=bool(severidad),
    )
