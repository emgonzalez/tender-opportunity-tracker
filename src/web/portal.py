"""src.web.portal — vistas del portal AMALIA: país (KPIs 1-6), regional (R1, R6) y operativa (11-13).

Plan de identidad, Entrega 4: todo `/portal*` exige sesión y se resuelve contra el `Alcance` de la
persona (`src/identidad/alcance.py`), nunca contra un cliente fijo. Un país fuera del alcance responde
el MISMO 404 que una ruta inexistente (y se audita como `ACCESO_FUERA_DE_ALCANCE`): quien sondea no
distingue «no existe» de «existe pero no es tuyo». Sin la identidad configurada (`IDENTIDAD_SECRET`)
el portal no existe: falla cerrado, no abierto.

«Ver como» (E10): `admin_plataforma` puede ver el portal con el alcance de otro rol. Es de SOLO LECTURA
en tres capas (este módulo, `oportunidades.service` y la plantilla): **toda ruta que escriba debe
rechazar `alcance.simulado`** — el test guardián de `tests/test_web_ver_como.py` avisa si aparece otra.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from src import portales
from src.analytics import operativo, pais, regional
from src.identidad import politica as identidad_politica
from src.identidad import repo as identidad_repo
from src.identidad import sesiones, ver_como
from src.memoria import db
from src.mercado import repo as mercado_repo
from src.oportunidades import politica as pipeline
from src.oportunidades import repo as oportunidades_repo
from src.oportunidades import service as oportunidades
from src.web import feedback, graficos
from src.web.acceso import _ip_hash, csrf_para, verificar_post
from src.web.plantillas import pagina
from src.web.sesion import (
    alcance_de,
    alcance_real_de,
    portal_con_identidad,
    token_de_request,
    ver_como_de,
)

logger = logging.getLogger(__name__)

router = APIRouter()

def _num(n):
    return f"{n:,}".replace(",", ".")


_RANGOS = (30, 90, 180)
_COLOR_SEVERIDAD = {"alta": "var(--critico)", "media": "var(--aviso)", "baja": "var(--bueno)"}


_AVISOS = {
    "ok": ("ok", "Estado actualizado."),
    "conflicto": ("error", "Alguien cambió esa oportunidad mientras la editabas: se recargó con el estado actual. "
                           "Revisa y vuelve a intentarlo."),
    "invalido": ("error", "Ese cambio no es válido (transición no permitida, o falta el motivo)."),
    # «Ver como» (E10). `{vista}` se completa con la etiqueta de la vista simulada.
    "simulacion": ("error", "Estás viendo como {vista}: la vista es de solo lectura."),
    "ver_como_ok": ("ok", "Ahora ves el portal como {vista} (solo lectura)."),
    "ver_como_invalido": ("error", "Esa vista no está disponible: sigues viendo lo mismo que antes."),
    "ver_como_fin": ("ok", "Volviste a tu vista."),
}


def _aviso(request: Request, ver: dict) -> tuple[str, str] | None:
    """El aviso de `?aviso=`: solo los códigos conocidos (nunca texto de la URL)."""
    aviso = _AVISOS.get(request.query_params.get("aviso", ""))
    if aviso is None:
        return None
    return aviso[0], aviso[1].replace("{vista}", ver.get("simulando") or "otro rol")


def _con_aviso(destino: str, request: Request) -> str:
    """Conserva un `?aviso=` conocido al redirigir desde `/portal` (el PRG de «ver como» aterriza ahí)."""
    codigo = request.query_params.get("aviso", "")
    return destino + "?" + urlencode({"aviso": codigo}) if codigo in _AVISOS else destino


def _monto(valor: str) -> Decimal | None:
    """Monto ofertado opcional: positivo, hasta 12 enteros y 2 decimales; ``ValueError`` si no es válido."""
    texto = (valor or "").strip().replace(",", "")
    if not texto:
        return None
    try:
        monto = Decimal(texto)
    except InvalidOperation:
        raise ValueError("monto inválido") from None
    if not monto.is_finite() or monto < 0 or monto >= Decimal("1e12") or monto.as_tuple().exponent < -2:
        raise ValueError("monto fuera de rango")
    return monto


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
        # Tras iniciar sesión se vuelve con un GET: solo un GET puede ser el destino (un POST, p. ej. el
        # cambio de estado, daría 405).
        destino = (request.url.path + (f"?{request.url.query}" if request.url.query else "")
                   if request.method == "GET" else "/portal")
        return None, RedirectResponse("/acceso?" + urlencode({"siguiente": destino}), status_code=303)
    return alcance, None


def _auditar_fuera_de_alcance(cfg, alcance, solicitado: str, recurso: str | None = None) -> None:
    """Un `?cliente=` ajeno es la señal de abuso real (P6). Nunca rompe la respuesta.

    Bajo «ver como» el 404 es lo que vería esa persona, no un abuso del admin: el recurso lleva la marca
    ` [ver_como=rol]` para que no se lea como tal. La fila siempre apunta al usuario real."""
    recurso = recurso or f"cliente={solicitado[:100]}"
    simulado = getattr(alcance, "simulado", None)
    if simulado:
        recurso += f" [ver_como={simulado}]"
    try:
        with db.connection(cfg) as conn:
            identidad_repo.registrar_auditoria(
                conn, alcance.org_id, "ACCESO_FUERA_DE_ALCANCE", usuario_id=alcance.usuario_id,
                recurso=recurso)
    except Exception:  # noqa: BLE001
        logger.exception("portal: no se pudo auditar un acceso fuera de alcance")


def _puede_ver_como(real) -> bool:
    return bool(real is not None and getattr(real, "roles", frozenset()) & identidad_politica.ROLES_VER_COMO)


def _restante_min(desde: datetime | None) -> int | None:
    """Minutos que le quedan a la simulación (TTL fijo desde que se eligió, decisión 2).

    `desde` sin zona se toma como UTC: es `SYSTIMESTAMP` de la ADB, que corre en UTC."""
    if desde is None:
        return None
    ttl = identidad_politica.TTL_VER_COMO_MIN
    ahora = datetime.now(timezone.utc)
    if desde.tzinfo is None:
        ahora = ahora.replace(tzinfo=None)
    transcurrido = (ahora - desde).total_seconds() / 60
    return max(0, min(ttl, int(ttl - transcurrido)))


def _ver_como_ctx(request: Request, alcance, nombres: dict[str, str]) -> dict:
    """Selector y banner de «ver como». Las opciones se calculan con el alcance REAL y solo si puede."""
    ctx = {"puede": False, "opciones": [], "simulando": None, "restante_min": None}
    real = alcance_real_de(request)
    if not _puede_ver_como(real):
        return ctx
    ctx["puede"] = True
    simulado = getattr(alcance, "simulado", None)
    cliente, desde = ver_como_de(request)
    try:
        with db.connection(request.app.state.cfg) as conn:
            ctx["opciones"] = ver_como.opciones(conn, real)
            if simulado and cliente and cliente not in nombres:
                nombres = {**nombres, **regional.nombres_clientes(conn, [cliente])}
    except Exception:  # noqa: BLE001 — sin selector antes que sin página
        logger.exception("portal: no se pudieron calcular las opciones de «ver como»")
        ctx["opciones"] = []
    if simulado:
        ctx["simulando"] = ver_como.etiqueta(simulado, nombres.get(cliente, cliente) if cliente else None)
        ctx["restante_min"] = _restante_min(desde)
    return ctx


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
    return {"nav": items, "csrf": csrf_para(request), "ver_como": _ver_como_ctx(request, alcance, nombres)}


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
            return RedirectResponse(_con_aviso("/portal/regional", request), status_code=303)
        elif alcance.clientes:
            solicitado = sorted(alcance.clientes)[0]
        elif alcance.ve_operativo:
            return RedirectResponse(_con_aviso("/portal/operativo", request), status_code=303)
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
            conversion = pais.conversion_comercial(conn, cliente, dias=dias)
            tiempos = pais.tiempo_por_etapa(conn, cliente, dias=dias)
            filas = oportunidades_repo.listar(conn, cliente, dias=dias)
    except Exception:
        # Una ADB caída no debe mostrar un traceback ni un portal con ceros falsos.
        logger.exception("portal: no se pudieron leer los KPIs de %s", cliente)
        return _sin_datos(request)

    navegacion = _nav(request, alcance, f"pais:{cliente}", nombres)
    return pagina(
        request, "portal_pais.html",
        **navegacion,
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
        conversion=conversion,
        g_conversion=graficos.embudo([("Detectadas", conversion["detectadas"])]
                                     + [(e["estado"].capitalize(), e["conteo"]) for e in conversion["etapas"]]),
        g_tiempos=graficos.barras([(t["etapa"].capitalize(), t["dias_promedio"]) for t in tiempos]),
        tiempos=tiempos,
        oportunidades_lista=[{**f, "siguientes": pipeline.siguientes(f["estado"]),
                              "exige_motivo": {e: pipeline.exige_motivo(f["estado"], e)
                                               for e in pipeline.siguientes(f["estado"])}} for f in filas],
        puede_cambiar=alcance.puede_cambiar_estado(cliente),
        # Bajo «ver como» se muestran los mismos controles que vería esa persona, pero deshabilitados.
        solo_vista=getattr(alcance, "simulado", None) is not None,
        aviso=_aviso(request, navegacion["ver_como"]),
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

    navegacion = _nav(request, alcance, "operativo", nombres)
    return pagina(
        request, "portal_operativo.html",
        **navegacion,
        aviso=_aviso(request, navegacion["ver_como"]),
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
    navegacion = _nav(request, alcance, "regional", nombres)
    return pagina(
        request, "portal_regional.html",
        **navegacion,
        aviso=_aviso(request, navegacion["ver_como"]),
        dias=dias, rangos=_RANGOS, paises=paises,
        g_paises=graficos.barras([(p["nombre"], p["conteo"]) for p in paises if p["conteo"] > 0]),
    )


@router.post("/portal/oportunidad/{proceso_gid}/estado")
def cambiar_estado(
    request: Request, proceso_gid: str,
    cliente: str = Form(""), estado: str = Form(""), actualizado_en: str = Form(""), csrf: str = Form(""),
    motivo: str = Form(""), monto_ofertado: str = Form(""), competidor: str = Form(""), dias: str = Form(""),
):
    """Mueve una oportunidad en el pipeline comercial. PRG: siempre redirige a la vista de país."""
    cfg = request.app.state.cfg
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    rechazo = verificar_post(request, csrf, "estado", limitador=request.app.state.limitador_estado)
    if rechazo is not None:
        return rechazo
    if getattr(alcance, "simulado", None):
        # «Ver como» es de solo lectura: ni se consulta la base (el servicio también lo rechaza).
        destino = "/portal?" + urlencode({"cliente": cliente, "dias": _rango(dias), "aviso": "simulacion"})
        return RedirectResponse(destino, status_code=303)
    if not alcance.puede_cambiar_estado(cliente):
        # Ajeno, inexistente o de solo lectura: el mismo 404; el ajeno además se audita.
        if cliente and not alcance.puede_ver(cliente):
            _auditar_fuera_de_alcance(cfg, alcance, cliente)
        return feedback.no_encontrado(request)
    try:
        monto = _monto(monto_ofertado)
        with db.connection(cfg) as conn:
            resultado = (oportunidades.INVALIDO if estado not in pipeline.ESTADOS else
                         oportunidades.cambiar_estado(conn, alcance, cliente, proceso_gid, estado, actualizado_en,
                                                      motivo=motivo, monto=monto, competidor=competidor))
    except ValueError:
        resultado = oportunidades.INVALIDO
    except Exception:
        logger.exception("portal: no se pudo cambiar el estado de una oportunidad")
        return _sin_datos(request)
    if resultado == oportunidades.NO_ENCONTRADO:
        return feedback.no_encontrado(request)
    aviso = {"ok": "ok", "conflicto": "conflicto"}.get(resultado, "invalido")
    destino = "/portal?" + urlencode({"cliente": cliente, "dias": _rango(dias), "aviso": aviso})
    return RedirectResponse(destino, status_code=303)


@router.post("/portal/ver-como")
def elegir_vista(request: Request, vista: str = Form(""), csrf: str = Form("")):
    """Inicia o termina «ver como» (E10). `vista`: ``""`` (volver a la propia), ``rol`` o ``rol|cliente``.

    Se autoriza con el alcance REAL y `ver_como` valida `vista` contra las opciones calculadas en el
    servidor (nada de texto libre). PRG: siempre a `/portal`, que decide la vista según el alcance
    efectivo (así no se aterriza en una página que esa vista no puede ver)."""
    cfg = request.app.state.cfg
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    rechazo = verificar_post(request, csrf, "vercomo", limitador=request.app.state.limitador_estado)
    if rechazo is not None:
        return rechazo
    real = alcance_real_de(request)
    if not _puede_ver_como(real):
        # Mismo 404 que una ruta inexistente; quien no puede simular y lo intenta queda auditado.
        _auditar_fuera_de_alcance(cfg, real, "", recurso="ver-como")
        return feedback.no_encontrado(request)
    token_hash = sesiones.hash_token(token_de_request(request))
    ip_hash = _ip_hash(cfg, request)
    try:
        with db.connection(cfg) as conn:
            if vista == "":
                ver_como.terminar(conn, real, token_hash, ip_hash)
                aviso = "ver_como_fin"
            elif ver_como.iniciar(conn, real, token_hash, vista, ip_hash):
                aviso = "ver_como_ok"
            else:
                aviso = "ver_como_invalido"
    except Exception:
        logger.exception("portal: no se pudo cambiar la vista de «ver como»")
        return _sin_datos(request)
    return RedirectResponse("/portal?" + urlencode({"aviso": aviso}), status_code=303)
