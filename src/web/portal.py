"""src.web.portal — vista de país del portal AMALIA (KPIs 1-6, Fase 1 del plan).

Ver `docs/planes/portal-amalia.md` §4. Sin autenticación hasta la Fase 3: por eso el
router solo responde con `ENABLE_PORTAL=true` y sirve un único cliente
(`PORTAL_CLIENTE_ID`). Apagado, `/portal` es un 404 idéntico al de cualquier ruta.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from src.analytics import operativo, pais
from src.memoria import db
from src.mercado import repo as mercado_repo
from src.web import feedback, graficos
from src.web.plantillas import pagina

logger = logging.getLogger(__name__)

router = APIRouter()

def _num(n):
    return f"{n:,}".replace(",", ".")


_RANGOS = (30, 90, 180)
_COLOR_SEVERIDAD = {"alta": "var(--critico)", "media": "var(--aviso)", "baja": "var(--bueno)"}


def _rango(valor: str | None) -> int:
    """`?dias=` solo acepta los rangos ofrecidos; cualquier otra cosa cae al de 90."""
    return int(valor) if valor and valor.isdigit() and int(valor) in _RANGOS else 90


def _conflictos(conn, cliente: str) -> list[dict]:
    """Procesos donde la persona dejó un estado final distinto del de COMPRASAL (P8).

    Aparte del resto de KPIs: si esta lectura falla, el portal se muestra sin la
    tarjeta en vez de caerse entero."""
    try:
        filas = mercado_repo.listar_conflictos_oportunidad_v2(conn, cliente)
    except Exception:
        logger.exception("portal: no se pudieron leer los conflictos de %s", cliente)
        return []
    return [
        {**f, "codigo": f["proceso_gid"].split(":", 1)[-1],
         "fecha": f["actualizado_en"].strftime("%d/%m/%Y")
         if hasattr(f["actualizado_en"], "strftime") else ""}
        for f in filas
    ]


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
            conflictos = _conflictos(conn, cliente)
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
        conflictos=conflictos,
    )


@router.get("/portal/operativo")
def vista_operativa(request: Request):
    """KPIs operativos 11-13: salud del sistema (no por cliente). Mismo gate que `/portal`."""
    cfg = request.app.state.cfg
    if not getattr(cfg, "enable_portal", False):
        return feedback.no_encontrado(request)

    try:
        with db.connection(cfg) as conn:
            corrida = operativo.estado_ultima_corrida(conn)
            gasto = operativo.gasto_llm_hoy(conn, cfg.daily_budget_usd)
            serie = operativo.latencia_y_errores_llm(conn, dias=14)
    except Exception:
        logger.exception("portal: no se pudieron leer los KPIs operativos")
        return pagina(request, "feedback_error.html", status=503, titulo="Portal no disponible",
                      mensaje="No pudimos leer los datos en este momento. "
                              "Probá de nuevo en unos minutos.")

    def _dia(p):
        return p["dia"].strftime("%d/%m") if hasattr(p["dia"], "strftime") else str(p["dia"])

    g_latencia = graficos.linea(
        [(_dia(p), p["p95_ms"],
          f"{_dia(p)}: p95 {_num(p['p95_ms'])} ms · p50 {_num(p['p50_ms'])} ms"
          f" · {p['llamadas']} llamadas")
         for p in serie], unidad=" ms")
    g_errores = graficos.linea(
        [(_dia(p), p["tasa_error"],
          f"{_dia(p)}: {p['tasa_error']:.1f}% de error · {p['llamadas']} llamadas")
         for p in serie], unidad="%")

    return pagina(
        request, "portal_operativo.html",
        corrida=corrida, gasto=gasto, serie=serie,
        g_medidor=graficos.medidor(gasto["gasto_usd"], gasto["tope_usd"]),
        g_latencia=g_latencia, g_errores=g_errores,
    )
