"""src.web.portal — vistas del portal AMALIA: país (KPIs 1-6), regional (R1, R6) y operativa (11-13).

Plan de identidad, Entrega 4: todo `/portal*` exige sesión y se resuelve contra el `Alcance` de la
persona (`src/identidad/alcance.py`), nunca contra un cliente fijo. Un país fuera del alcance responde
el MISMO 404 que una ruta inexistente (y se audita como `ACCESO_FUERA_DE_ALCANCE`): quien sondea no
distingue «no existe» de «existe pero no es tuyo». Sin la identidad configurada (`IDENTIDAD_SECRET`)
el portal no existe: falla cerrado, no abierto.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from src import portales
from src.analytics import operativo, pais, regional
from src.identidad import repo as identidad_repo
from src.memoria import db
from src.mercado import repo as mercado_repo
from src.web import feedback, graficos
from src.web.acceso import csrf_para
from src.web.plantillas import pagina
from src.web.sesion import alcance_de, portal_con_identidad

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


def _acceso(request: Request):
    """``(alcance, None)`` si puede seguir; ``(None, respuesta)`` si no (404 o ir a iniciar sesión)."""
    cfg = request.app.state.cfg
    if not getattr(cfg, "enable_portal", False) or not portal_con_identidad(cfg):
        return None, feedback.no_encontrado(request)
    alcance = alcance_de(request)
    if alcance is None:
        destino = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return None, RedirectResponse("/acceso?" + urlencode({"siguiente": destino}), status_code=303)
    return alcance, None


def _auditar_fuera_de_alcance(cfg, alcance, solicitado: str) -> None:
    """Un `?cliente=` ajeno es la señal de abuso real (P6). Nunca rompe la respuesta."""
    try:
        with db.connection(cfg) as conn:
            identidad_repo.registrar_auditoria(
                conn, alcance.org_id, "ACCESO_FUERA_DE_ALCANCE", usuario_id=alcance.usuario_id,
                recurso=f"cliente={solicitado[:100]}")
    except Exception:  # noqa: BLE001
        logger.exception("portal: no se pudo auditar un acceso fuera de alcance")


def _nav(request: Request, alcance, actual: str, nombres: dict[str, str] | None = None) -> dict:
    """Navegación y botón de salida. Un rol de un solo país no ve selector (R6)."""
    nombres = nombres or {}
    items = []
    if alcance.es_regional:
        items.append({"texto": "Regional", "href": "/portal/regional", "actual": actual == "regional"})
    if len(alcance.clientes) > 1:
        for c in sorted(alcance.clientes):
            items.append({"texto": nombres.get(c, c), "href": "/portal?" + urlencode({"cliente": c}),
                          "actual": actual == f"pais:{c}"})
    if alcance.ve_operativo:
        items.append({"texto": "Operativo", "href": "/portal/operativo", "actual": actual == "operativo"})
    return {"nav": items, "csrf": csrf_para(request)}


def _sin_datos(request: Request):
    return pagina(request, "feedback_error.html", status=503, titulo="Portal no disponible",
                  mensaje="No pudimos leer los datos en este momento. Prueba de nuevo en unos minutos.")


@router.get("/portal")
def vista_pais(request: Request):
    cfg = request.app.state.cfg
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta

    solicitado = request.query_params.get("cliente")
    if solicitado is None:
        if len(alcance.clientes) == 1:
            solicitado = next(iter(alcance.clientes))
        elif alcance.es_regional:
            return RedirectResponse("/portal/regional", status_code=303)
        elif alcance.clientes:
            solicitado = sorted(alcance.clientes)[0]
        elif alcance.ve_operativo:
            return RedirectResponse("/portal/operativo", status_code=303)
        else:
            return feedback.no_encontrado(request)
    elif not alcance.puede_ver(solicitado):
        # Mismo 404 que una ruta inexistente: ni «no existe» ni «no es tuyo».
        _auditar_fuera_de_alcance(cfg, alcance, solicitado)
        return feedback.no_encontrado(request)
    cliente = solicitado
    dias = _rango(request.query_params.get("dias"))

    try:
        with db.connection(cfg) as conn:
            nombres = regional.nombres_clientes(conn, sorted(alcance.clientes)) if len(alcance.clientes) > 1 else {}
            oportunidades = pais.oportunidades_del_mes(conn, cliente)
            relevancia = pais.pct_relevancia_clasificador(conn, cliente, dias=30)
            instituciones = pais.instituciones_top(conn, cliente, dias=dias)
            embudo = pais.embudo_pipeline(conn, cliente, dias=dias)
            severidad = pais.severidad_riesgos(conn, cliente, dias=dias)
            conflictos = _conflictos(conn, cliente)
    except Exception:
        # Una ADB caída no debe mostrar un traceback ni un portal con ceros falsos.
        logger.exception("portal: no se pudieron leer los KPIs de %s", cliente)
        return _sin_datos(request)

    return pagina(
        request, "portal_pais.html",
        **_nav(request, alcance, f"pais:{cliente}", nombres),
        cliente=cliente, pais_nombre=nombres.get(cliente, cliente), dias=dias, rangos=_RANGOS,
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
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    if not alcance.ve_operativo:
        return feedback.no_encontrado(request)  # solo operador y admin_plataforma

    try:
        with db.connection(cfg) as conn:
            nombres = regional.nombres_clientes(conn, sorted(alcance.clientes)) if len(alcance.clientes) > 1 else {}
            corrida = operativo.estado_ultima_corrida(conn)
            gasto = operativo.gasto_llm_hoy(conn, cfg.daily_budget_usd)
            serie = operativo.latencia_y_errores_llm(conn, dias=14)
    except Exception:
        logger.exception("portal: no se pudieron leer los KPIs operativos")
        return _sin_datos(request)

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
        **_nav(request, alcance, "operativo", nombres),
        corrida=corrida, gasto=gasto, serie=serie,
        g_medidor=graficos.medidor(gasto["gasto_usd"], gasto["tope_usd"]),
        g_latencia=g_latencia, g_errores=g_errores,
    )


@router.get("/portal/regional")
def vista_regional(request: Request):
    """R1 + R6: comparación entre los países del alcance y selector. Solo roles regionales."""
    cfg = request.app.state.cfg
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    if not alcance.es_regional:
        return feedback.no_encontrado(request)  # un gerente de país no tiene esta vista
    dias = _rango(request.query_params.get("dias"))
    clientes = sorted(alcance.clientes)

    try:
        with db.connection(cfg) as conn:
            filas = regional.oportunidades_por_pais(conn, clientes, dias=dias)
            nombres = regional.nombres_clientes(conn, clientes)
    except Exception:
        logger.exception("portal: no se pudo leer la vista regional")
        return _sin_datos(request)

    paises = []
    for f in filas:
        try:
            portal = portales.obtener_portal(portales.portal_de_cliente(cfg.clients_dir, f["cliente_id"]))
            integrado = portal.integrado
        except portales.PortalDesconocido:
            integrado = False
        paises.append({**f, "nombre": nombres.get(f["cliente_id"], f["cliente_id"]), "integrado": integrado,
                       "href": "/portal?" + urlencode({"cliente": f["cliente_id"]})})
    return pagina(
        request, "portal_regional.html",
        **_nav(request, alcance, "regional", nombres),
        dias=dias, rangos=_RANGOS, paises=paises,
        g_paises=graficos.barras([(p["nombre"], p["conteo"]) for p in paises if p["conteo"] > 0]),
    )
