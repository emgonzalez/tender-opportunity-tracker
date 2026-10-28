"""src.web.portal — vistas del portal AMALIA: país (KPIs 1-6), regional (R1, R6) y operativa (11-13).

Plan de identidad, Entrega 4: todo `/portal*` exige sesión y se resuelve contra el `Alcance` de la
persona (`src/identidad/alcance.py`), nunca contra un cliente fijo. Un país fuera del alcance responde
el MISMO 404 que una ruta inexistente (y se audita como `ACCESO_FUERA_DE_ALCANCE`): quien sondea no
distingue «no existe» de «existe pero no es tuyo». Sin la identidad configurada (`IDENTIDAD_SECRET`)
el portal no existe: falla cerrado, no abierto.

«Ver como» (E10): `admin_plataforma` puede ver el portal con el alcance de otro rol. Es de SOLO LECTURA
en tres capas (este módulo, `oportunidades.service` y la plantilla): **toda ruta que escriba debe
rechazar `alcance.simulado`** — el test guardián de `tests/test_web_ver_como.py` avisa si aparece otra.
La única excepción es `POST /portal/tema` (E12): guarda una preferencia en una cookie, no datos.

E11: la vista de país responde «¿qué hago hoy?» (veredicto, calendario de cierres, tablero por estado,
clasificación de AMALIA y votos) y suma dos escrituras: asignar responsable y recuperar un descartado.
Los `?dia=`, `?responsable=` y `?marca=` se validan contra listas cerradas y se ignoran si no calzan.

E11.1: «para decidir» excluye lo vencido (que se resume en `tablero.vencidas`), cada columna del tablero
muestra a lo sumo `_MAX_TARJETAS` tarjetas por urgencia (`n` es el total real, `mas` lo que no cabe), el
filtro ofrece las `_MAX_MARCAS` marcas más frecuentes y la relevancia es la estricta
(`pais.relevancia_estricta`: aptos / revisados).

E13: vista compacta. Cada listado trae 5 visibles y un `*_mas` para «Ver N más» en el lugar; el tablero
lleva lo que necesita el arrastre (`tablero.editable`, `columna.estados`, `tarjeta.siguientes_csv` y
`exige_motivo_json`) sin rutas nuevas: el JS solo rellena y envía el formulario de estado de la tarjeta,
cuyo PRG vuelve al ancla `#op-{gid}`. `resumenes` da la línea de cada acordeón plegado.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from src import portales
from src.analytics import operativo, pais, regional, tablero
from src.classifier import client_profile
from src.feedback import catalogo as feedback_catalogo
from src.identidad import politica as identidad_politica
from src.identidad import repo as identidad_repo
from src.identidad import sesiones, ver_como
from src.memoria import db
from src.mercado import repo as mercado_repo
from src.notifier import agrupamiento
from src.oportunidades import politica as pipeline
from src.oportunidades import service as oportunidades
from src.web import feedback, graficos
from src.web.acceso import _ip_hash, csrf_para, verificar_post
from src.web.plantillas import COOKIE_TEMA, TEMAS, pagina, volver_seguro
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
    # E11: responsable y recuperación de descartados.
    "asignado": ("ok", "Responsable actualizado."),
    "recuperado": ("ok", "Oportunidad recuperada: ahora está en «Nuevo» del tablero."),
    "responsable_invalido": ("error", "Esa persona no puede ser responsable de esta oportunidad "
                                      "(no está activa o no tiene acceso a este país)."),
    "no_recuperable": ("error", "Esa oportunidad no se puede recuperar: ya es relevante o ya se recuperó."),
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
    ctx = {"puede": False, "opciones": [], "simulando": None, "valor": None, "restante_min": None}
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
        ctx["valor"] = f"{simulado}|{cliente}" if cliente else simulado
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


# ── E11: vista de país «qué hago hoy» ───────────────────────────────────────────
#
# Contrato con `analytics.tablero.clasificar` (cada fila): `proceso_gid`, `numero_referencia`, `titulo`,
# `institucion`, `url`, `match`, `fecha_limite`, `marcas`, `monto_usd`, `estado` (None sin
# `oportunidad_estado`), `token`, `responsable_id`, `responsable_nombre`, `actualizado_en`, más lo que
# anota `clasificar`: `grupo`, `competible`, `motivo_bloqueo`, `dias_restantes`, `urgencia`,
# `cierre_texto`. Se leen con `.get()` y alternativas: una clave ausente degrada la tarjeta, no la vista.

_ZONA_DEFECTO = "America/El_Salvador"
_DIAS_CALENDARIO = 14
_DIAS_SIN_MOVER = 7
# E13: cada listado muestra `*_VISIBLES` y guarda hasta `*_EXTRA` más para un «Ver N más» que se despliega
# en el lugar (sin rutas nuevas). El tablero tiene scroll interno: el tope por columna no alarga la página.
_ATENCION_VISIBLES = 5
_ATENCION_EXTRA = 15
_DESCARTADOS_VISIBLES = 5
_DESCARTADOS_EXTRA = 15
_MAX_DESCARTADOS = _DESCARTADOS_VISIBLES + _DESCARTADOS_EXTRA   # lo que se le pide a la base
_DESACUERDOS_VISIBLES = 5
_DESACUERDOS_EXTRA = 10
_MAX_TARJETAS = 25         # por columna del tablero; el resto se resume en «+ N más»
_MAX_MARCAS = 8            # chips de marca del filtro (las más frecuentes)
_MAX_TITULO_PRIMERA = 80
_MAX_TITULO_ATENCION = 90
_POCOS_VOTOS = 10
_COLUMNAS = (
    ("nuevo", "Nuevo", ("NUEVO",)),
    ("evaluando", "Evaluando", ("EVALUANDO",)),
    ("ofertado", "Ofertado", ("OFERTADO",)),
    ("cerradas", "Cerradas", ("GANADO", "PERDIDO", "DESCARTADO")),
)
_GRUPO_DECIDIR, _GRUPO_NO_COMPETIBLE, _GRUPO_BAJA, _GRUPO_DESCARTADO = "decidir", "no_competible", "baja", "descartado"
_GRUPOS_TABLERO = frozenset({_GRUPO_DECIDIR, _GRUPO_NO_COMPETIBLE, _GRUPO_BAJA})
_ACTIVOS = frozenset({"NUEVO", "EVALUANDO", "OFERTADO"})
_SIN_RESPONSABLE = "sin"
_DIAS_LARGOS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES_LARGOS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                 "octubre", "noviembre", "diciembre")


def _zona(cfg, cliente: str) -> ZoneInfo:
    """Zona horaria del portal de compras del cliente; la de El Salvador si no se puede saber."""
    try:
        nombre = portales.obtener_portal(portales.portal_de_cliente(cfg.clients_dir, cliente)).zona_horaria
        return ZoneInfo(nombre)
    except (portales.PortalDesconocido, ZoneInfoNotFoundError, ValueError, TypeError, AttributeError):
        return ZoneInfo(_ZONA_DEFECTO)


def _ahora(zona: ZoneInfo) -> datetime:
    """«Hoy», con zona. Función aparte para que los tests fijen el reloj."""
    return datetime.now(zona)


def _perfil(cfg, cliente: str):
    """Perfil YAML del cliente (marcas del portafolio, motivos de voto); None si no se puede leer."""
    try:
        return client_profile.load_client_profile(cliente, cfg.clients_dir)
    except Exception:  # noqa: BLE001 — sin portafolio la competibilidad queda «desconocida», no se cae
        logger.debug("portal: perfil de %s no legible", cliente, exc_info=True)
        return None


def _marcas_portafolio(perfil) -> tuple[str, ...] | None:
    if perfil is None:
        return None
    return tuple(b.name for c in perfil.catalog.categories for b in c.brands if b.name)


def _local(valor, zona: ZoneInfo) -> datetime | None:
    """Un instante de la base en la zona del cliente. Sin zona se toma como UTC (columnas `*_utc`)."""
    if not isinstance(valor, datetime):
        return None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(zona)


def _instante(f: dict) -> datetime | None:
    """Cuándo cambió de estado por última vez: `actualizado_en`, o el token (su ISO) si no viene."""
    valor = f.get("actualizado_en")
    if isinstance(valor, datetime):
        return valor
    try:
        return datetime.fromisoformat(f.get("token") or "")
    except ValueError:
        return None


def _num_txt(n) -> str:
    return _num(int(round(n)))


def _monto_txt(monto) -> str:
    """Nunca «USD 0»: un monto nulo o cero es un monto que no se estimó."""
    try:
        valor = float(monto)
    except (TypeError, ValueError):
        return ""   # sin monto no se escribe nada en la tarjeta (el KPI lo dice una vez); nunca un 0 inventado
    return f"USD {_num_txt(valor)}" if valor > 0 else ""


def _url_segura(url) -> str | None:
    """Solo enlaces http(s): un `javascript:` escapado sigue siendo ejecutable en un `href`."""
    return url if isinstance(url, str) and url.lower().startswith(("https://", "http://")) else None


def _urgencia_dia(dias: int) -> str | None:
    return agrupamiento.urgencia(dias) or None


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _veredicto(semana: int, sin_responsable: int) -> str:
    if not semana and not sin_responsable:
        return "Nada urgente hoy."
    return (f"Hoy hay {semana} {_plural(semana, 'oportunidad', 'oportunidades')} que "
            f"{_plural(semana, 'cierra', 'cierran')} esta semana y {sin_responsable} sin responsable.")


def _fecha_larga(d: date) -> str:
    return f"{_DIAS_LARGOS[d.weekday()]} {d.day} de {_MESES_LARGOS[d.month - 1]} de {d.year}"


def _fecha_desde(valor, zona: ZoneInfo) -> str | None:
    """«27 de octubre de 2026» para la nota «se guardan desde…»; None si aún no hay descartados guardados."""
    local = _local(valor, zona)
    if local is None:
        return None
    return f"{local.day} de {_MESES_LARGOS[local.month - 1]} de {local.year}"


def _voto_de(v) -> tuple[int, int, str | None]:
    """(relevante, irrelevante, motivo principal) de `votos_por_proceso[gid]`. Solo conteos: nunca quién."""
    if not isinstance(v, dict):
        return 0, 0, None
    motivo = v.get("motivo")
    if motivo is None:
        motivos = v.get("motivos") or []
        primero = motivos[0] if motivos else None
        motivo = primero[0] if isinstance(primero, (tuple, list)) else primero
    return int(v.get("relevante") or 0), int(v.get("irrelevante") or 0), motivo


def _pares(filas, claves=("etiqueta", "motivo", "marca"), conteo=("n", "conteo")) -> list[tuple[str, int]]:
    """Normaliza `[(etq, n)]` o `[{etiqueta|motivo|marca, n|conteo}]` a `[(etq, n)]`."""
    salida = []
    for f in filas or []:
        if isinstance(f, dict):
            etq = next((f[k] for k in claves if f.get(k) is not None), None)
            n = next((f[k] for k in conteo if f.get(k) is not None), 0)
        else:
            etq, n = f[0], f[1]
        if etq is not None:
            salida.append((str(etq), int(n or 0)))
    return salida


def _tarjeta(f: dict, hoy: datetime, voto: tuple[int, int, str | None]) -> dict:
    estado = f.get("estado") or pipeline.ESTADO_INICIAL
    siguientes = pipeline.siguientes(estado)
    desde = _instante(f) if f.get("estado") else None
    dias_en_estado = None
    if desde is not None:
        dias_en_estado = max(0, (hoy - _local(desde, hoy.tzinfo)).days)
    texto = f.get("cierre_texto")
    exige = {e: pipeline.exige_motivo(estado, e) for e in siguientes}
    return {
        "gid": f.get("proceso_gid"),
        "referencia": f.get("numero_referencia") or "",
        "titulo": f.get("titulo") or "",
        "institucion": f.get("institucion") or "",
        "url": _url_segura(f.get("url") or f.get("url_directa")),
        "match": f.get("match"),
        "cierre": {"texto": texto, "urgencia": f.get("urgencia") or None} if texto else None,
        "marcas": [str(m) for m in (f.get("marcas") or [])],
        "monto_txt": _monto_txt(f.get("monto_usd", f.get("monto"))),
        "responsable": f.get("responsable_nombre") or f.get("responsable") or None,
        "responsable_id": f.get("responsable_id") or f.get("responsable_usuario_id") or None,
        "dias_en_estado": dias_en_estado,
        "voto": {"n": voto[1], "motivo": voto[2]} if voto[1] else None,
        "siguientes": siguientes,
        "exige_motivo": exige,
        # E13 (arrastre): lo mismo, serializado para atributos `data-*` (la plantilla los escapa).
        "siguientes_csv": ",".join(siguientes),
        "exige_motivo_json": json.dumps(exige, separators=(",", ":")),
        "token": f.get("token") or "",
        "estado": estado,
        "etiqueta": estado.capitalize(),
        "grupo": f.get("grupo"),
    }


def _recorta(texto, limite: int) -> str:
    """Un título largo, recortado a `limite` caracteres con «…» (nunca a media palabra si se puede)."""
    texto = str(texto or "").strip()
    if len(texto) <= limite:
        return texto
    corte = texto[: limite - 1].rstrip()
    espacio = corte.rfind(" ")
    if espacio >= limite // 2:
        corte = corte[:espacio].rstrip()
    return corte + "…"


def _vencida(f: dict) -> bool:
    d = f.get("dias_restantes")
    return d is not None and d < 0


def _para_decidir(f: dict) -> bool:
    """«Para decidir» (KPI, calendario, atención y veredicto): grupo decidir, sin decisión comercial tomada
    (sin estado, NUEVO o EVALUANDO) y con el cierre aún por delante (o sin fecha)."""
    return (f.get("grupo") == _GRUPO_DECIDIR and f.get("estado") in (None, "NUEVO", "EVALUANDO")
            and not _vencida(f))


def _es_vencida_sin_decidir(f: dict) -> bool:
    """Cierre pasado y sin estado comercial: ya no se puede ofertar. No va al tablero ni a «para decidir»."""
    return f.get("estado") is None and f.get("grupo") in _GRUPOS_TABLERO and _vencida(f)


def _columna_de(f: dict) -> str | None:
    estado = f.get("estado")
    if estado is None:
        if f.get("grupo") not in _GRUPOS_TABLERO or _vencida(f):
            return None  # descartado/pendiente de AMALIA, o vencido sin decidir: no está en el tablero
        return "nuevo"
    return next((clave for clave, _t, estados in _COLUMNAS if estado in estados), None)


def _marcas_frecuentes(filas: list[dict], elegida: str | None) -> list[str]:
    """Las `_MAX_MARCAS` marcas más frecuentes entre las tarjetas (empate: alfabético), más la elegida."""
    conteo = Counter(str(m) for f in filas for m in dict.fromkeys(f.get("marcas") or []))
    marcas = [m for m, _n in sorted(conteo.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_MARCAS]]
    if elegida and elegida not in marcas:
        marcas.append(elegida)
    return marcas


def _relevancia(conn, cliente: str, dias: int) -> tuple[dict, float | None]:
    """`relevancia_estricta` del rango y el delta (en puntos) de `pct_aptos` contra el rango anterior de
    igual longitud (= ventana de 2·dias menos la actual). Sin revisados en alguna de las dos: None."""
    actual = pais.relevancia_estricta(conn, cliente, dias) or {}
    doble = pais.relevancia_estricta(conn, cliente, 2 * dias) or {}
    delta = None
    prev_rev = int(doble.get("revisados") or 0) - int(actual.get("revisados") or 0)
    prev_aptos = int(doble.get("aptos") or 0) - int(actual.get("aptos") or 0)
    if actual.get("pct_aptos") is not None and prev_rev > 0 and prev_aptos >= 0:
        delta = round(float(actual["pct_aptos"]) - 100 * prev_aptos / prev_rev, 1)
    return actual, delta


def _orden_cierre(f: dict):
    d = f.get("dias_restantes")
    return (2 if d is None else (1 if d < 0 else 0), d if d is not None else 0, -(f.get("match") or 0))


def _filtros(query, asignables: list[dict], marcas: set[str], dias_validos: set[date]) -> dict:
    """`?dia=`, `?responsable=` y `?marca=` contra listas cerradas: lo desconocido se ignora (None)."""
    dia = None
    crudo = query.get("dia") or ""
    if len(crudo) == 10:
        try:
            dia = date.fromisoformat(crudo)
        except ValueError:
            dia = None
        if dia not in dias_validos:
            dia = None
    ids = {str(a["usuario_id"]) for a in asignables}
    responsable = query.get("responsable")
    if responsable != _SIN_RESPONSABLE and responsable not in ids:
        responsable = None
    marca = query.get("marca")
    if marca not in marcas:
        marca = None
    return {"dia": dia, "responsable": responsable, "marca": marca}


def _ancla(gid) -> str:
    """`#op-{gid}` con el gid codificado como lo hace `|urlencode` en las plantillas (`id="op-…"`)."""
    return f"#op-{quote(str(gid), safe='/')}" if gid else ""


def _resumen_clasificacion(revisados: int, relevantes: int, aptos: int) -> str:
    if not revisados:
        return "Sin corridas en el rango"
    return f"{_num(revisados)} revisados → {_num(relevantes)} relevantes · {_num(aptos)} aptos para decidir"


def _resumen_equipo(relevante: int, irrelevante: int, desacuerdos: int) -> str:
    if not (relevante or irrelevante or desacuerdos):
        return "Sin votos"
    return (f"{_num(relevante)} relevante · {_num(irrelevante)} irrelevante · "
            f"{_num(desacuerdos)} {_plural(desacuerdos, 'desacuerdo', 'desacuerdos')}")


def _contexto_pais(conn, cfg, alcance, cliente: str, dias: int, hoy: datetime, zona: ZoneInfo, query,
                   editable: bool = False) -> dict:
    """Todo lo que pinta `portal_pais.html` (contrato E13), con UNA conexión.

    `editable`: el tablero admite arrastrar (quien puede cambiar el estado y no está en «ver como»)."""
    perfil = _perfil(cfg, cliente)
    etq_motivo = {m["id"]: m["label"] for m in feedback_catalogo.motivos_de(perfil)}

    rango = tablero.procesos_del_rango(conn, cliente, dias)
    clasificado = tablero.clasificar(rango.get("filas") or [], _marcas_portafolio(perfil), hoy)
    filas = clasificado.get("filas", []) if isinstance(clasificado, dict) else list(clasificado or [])
    gids = [f["proceso_gid"] for f in filas if f.get("proceso_gid")]
    votos = tablero.votos_por_proceso(conn, cliente, gids) if gids else {}
    voto = {g: _voto_de((votos or {}).get(g)) for g in gids}
    asignables = [{"usuario_id": str(a["usuario_id"]), "nombre": a.get("nombre") or str(a["usuario_id"])}
                  for a in identidad_repo.usuarios_asignables(conn, alcance.org_id, cliente)]

    # «Para decidir»: UNA lista para el KPI, el calendario, «requiere atención» y el veredicto.
    decidir = sorted((f for f in filas if _para_decidir(f)), key=_orden_cierre)
    semana_lim = agrupamiento.DIAS_ESTA_SEMANA
    decidir_semana = [f for f in decidir if f.get("dias_restantes") is not None
                      and 0 <= f["dias_restantes"] <= semana_lim]
    sin_resp = [f for f in decidir if not (f.get("responsable_id") or f.get("responsable_usuario_id"))]

    # ── calendario de cierres (14 días desde hoy, solo lo que está «para decidir») ──
    hoy_d = hoy.date()
    ventana = [hoy_d + timedelta(days=i) for i in range(_DIAS_CALENDARIO)]
    cierre_de = {}
    for f in filas:
        local = _local(f.get("fecha_limite"), zona)
        if local is not None:
            cierre_de[f.get("proceso_gid")] = local.date()
    por_dia = Counter(cierre_de.get(f.get("proceso_gid")) for f in decidir)

    # ── tablero (con filtros; tope de tarjetas por columna) ──
    en_tablero = [f for f in filas if _columna_de(f)]
    vencidas = sum(1 for f in filas if _es_vencida_sin_decidir(f))
    marcas_todas = {str(m) for f in en_tablero for m in (f.get("marcas") or [])}
    filtro = _filtros(query, asignables, marcas_todas, set(ventana))

    def pasa(f):
        resp = f.get("responsable_id") or f.get("responsable_usuario_id")
        if filtro["dia"] and cierre_de.get(f.get("proceso_gid")) != filtro["dia"]:
            return False
        if filtro["responsable"] == _SIN_RESPONSABLE and resp:
            return False
        if filtro["responsable"] not in (None, _SIN_RESPONSABLE) and str(resp) != filtro["responsable"]:
            return False
        return not (filtro["marca"] and filtro["marca"] not in (f.get("marcas") or []))

    columnas = []
    for clave, titulo, estados in _COLUMNAS:
        # Lo «para decidir» va primero: el tope de tarjetas no puede dejar sin tarjeta lo que «Requiere atención»
        # señala (si no, el enlace a `#op-…` queda roto y no hay desde dónde moverlo).
        propias = sorted((f for f in en_tablero if _columna_de(f) == clave and pasa(f)),
                         key=lambda f: (f.get("grupo") != _GRUPO_DECIDIR, *_orden_cierre(f)))
        tarjetas = [_tarjeta(f, hoy, voto.get(f.get("proceso_gid"), (0, 0, None)))
                    for f in propias[:_MAX_TARJETAS]]
        columnas.append({"clave": clave, "titulo": titulo, "n": len(propias),
                         "mas": len(propias) - len(tarjetas), "tarjetas": tarjetas, "estados": list(estados)})

    def href_dia(d: date) -> str:
        return "/portal?" + urlencode({"cliente": cliente, "dias": dias, "dia": d.isoformat()})

    cierres = {"svg": graficos.barras_dias([
        {"fecha": d, "n": por_dia.get(d, 0), "urgencia": _urgencia_dia((d - hoy_d).days), "href": href_dia(d),
         "activo": d == filtro["dia"]} for d in ventana], altura=96),
        "dia": filtro["dia"]}

    # ── requiere atención ──
    atencion = []
    for f in decidir_semana:
        d = f["dias_restantes"]
        atencion.append({"chip": "Cierra hoy" if d == 0 else f"Cierra en {d} {_plural(d, 'día', 'días')}",
                         "clase": "rojo", "titulo": _recorta(f.get("titulo"), _MAX_TITULO_ATENCION),
                         "detalle": " · ".join(x for x in (f.get("numero_referencia"), f.get("institucion")) if x),
                         "gid": f.get("proceso_gid")})
    for f in sorted((f for f in filas if f.get("estado") == "EVALUANDO"), key=_orden_cierre):
        desde = _instante(f)
        quieta = (hoy - _local(desde, zona)).days if desde is not None else None
        if quieta is not None and quieta > _DIAS_SIN_MOVER:
            atencion.append({"chip": "Sin moverse", "clase": "ambar",
                             "titulo": _recorta(f.get("titulo"), _MAX_TITULO_ATENCION),
                             "detalle": f"{quieta} días en evaluación", "gid": f.get("proceso_gid")})
    tope_atencion = _ATENCION_VISIBLES + _ATENCION_EXTRA
    if sin_resp:
        # La fila agregada va al final pero nunca se pierde por el tope: le cede su sitio la última por proceso.
        n = len(sin_resp)
        atencion = atencion[:tope_atencion - 1]
        atencion.append({"chip": "Sin responsable", "clase": "ambar",
                         "titulo": f"{n} {_plural(n, 'oportunidad', 'oportunidades')} sin responsable",
                         "detalle": "Asígnalas en el tablero para que alguien las siga.", "gid": None})
    atencion, atencion_mas = atencion[:_ATENCION_VISIBLES], atencion[_ATENCION_VISIBLES:tope_atencion]

    # ── cabecera y KPIs ──
    corrida_db = operativo.estado_ultima_corrida(conn)
    corrida = None
    if corrida_db.get("estado") and corrida_db.get("hace_minutos") is not None:
        corrida = {"hora": (hoy - timedelta(minutes=corrida_db["hace_minutos"])).strftime("%H:%M"),
                   "revisados": corrida_db.get("procesos_revisados") or 0}
    primera = decidir[0] if decidir and decidir[0].get("dias_restantes") is not None else None
    relevancia, delta_pts = _relevancia(conn, cliente, dias)
    revisados = int(relevancia.get("revisados") or 0)
    relevantes = int(relevancia.get("relevantes") or 0)
    del_mes = pais.oportunidades_del_mes(conn, cliente)
    en_juego = tablero.pipeline_en_juego(conn, cliente) or {}
    con_monto = int(en_juego.get("con_monto") or 0)
    monto = en_juego.get("monto_usd")
    estancadas = tablero.estancadas(conn, cliente, dias_sin_mover=_DIAS_SIN_MOVER)
    kpis = {
        "decidir": {"n": len(decidir), "semana": len(decidir_semana),
                    "primera": None if primera is None else {
                        "gid": primera.get("proceso_gid"),
                        "titulo": _recorta(primera.get("titulo"), _MAX_TITULO_PRIMERA),
                        "institucion": primera.get("institucion") or "", "texto": primera.get("cierre_texto"),
                        "dias": primera.get("dias_restantes")}},
        # Sin ningún monto estimado no hay cifra: None (la plantilla pone un guion), nunca un 0 inventado.
        "pipeline": {"monto_usd": float(monto) if con_monto > 0 and monto is not None else None,
                     "activas": int(en_juego.get("activas") or 0), "con_monto": con_monto},
        "relevancia": {"pct_aptos": relevancia.get("pct_aptos"), "aptos": int(relevancia.get("aptos") or 0),
                       "revisados": revisados, "relevantes": relevantes,
                       "pct_relevantes": relevancia.get("pct_relevantes"), "delta_pts": delta_pts,
                       "spark": graficos.sparkline(del_mes["serie"])},
        "estancadas": {"n": estancadas if isinstance(estancadas, int) else len(estancadas or []),
                       "dias": _DIAS_SIN_MOVER},
    }

    # ── clasificación de AMALIA: revisados → relevantes → (decidir · no competibles · baja) + no relevantes ──
    grupos = {"decidir": len(decidir),
              "no_competible": sum(1 for f in filas if f.get("grupo") == _GRUPO_NO_COMPETIBLE),
              "baja": sum(1 for f in filas if f.get("grupo") == _GRUPO_BAJA)}
    no_relevantes = max(revisados - relevantes, 0)
    aptos_n = int(relevancia.get("aptos") or 0)
    guardados = tablero.descartados_del_rango(conn, cliente, dias, limite=_MAX_DESCARTADOS) or {}
    filas_desc = list(guardados.get("filas") or [])[:_MAX_DESCARTADOS]
    total_desc = int(guardados.get("total") or 0)

    def motivo_descarte(f):
        return f.get("motivo_descarte") or f.get("motivo") or "Sin motivo registrado"

    descartados = [{"gid": f.get("proceso_gid"), "referencia": f.get("numero_referencia") or "",
                    "titulo": f.get("titulo") or "", "match": f.get("match"), "motivo": motivo_descarte(f),
                    "token": f.get("token") or ""} for f in filas_desc]

    clasificacion = {
        "revisados": revisados,
        "relevantes": relevantes,
        "aptos": aptos_n,
        "no_relevantes": no_relevantes,
        "grupos": grupos,
        "barra": graficos.barra_apilada([
            # La barra usa solo las cifras del KPI (mismo universo: lo que AMALIA procesó en el rango), no los
            # grupos del tablero (vigentes, sin vencidas, con tope de filas), que no suman lo mismo.
            ("Aptos para decidir", aptos_n, "critico"),
            ("Relevantes, no aptos", max(relevantes - aptos_n, 0), "aviso"),
            ("No relevantes", no_relevantes, "borde"),
        ]),
        "motivos": Counter(motivo_descarte(f) for f in filas
                           if f.get("grupo") == _GRUPO_DESCARTADO and not f.get("estado")).most_common(),
        "descartados": descartados[:_DESCARTADOS_VISIBLES],
        "descartados_mas": descartados[_DESCARTADOS_VISIBLES:],
        "total_descartados": max(total_desc, len(filas_desc)),
        "hay_mas": total_desc > len(filas_desc),
        "desde": _fecha_desde(guardados.get("desde"), zona),
        "truncado": bool(rango.get("truncado")),
    }

    # ── votos de quienes reciben el correo (solo conteos) ──
    resumen = tablero.votos_resumen(conn, cliente, dias) or {}
    desacuerdos = []
    for f in filas:
        rel, irr, mot = voto.get(f.get("proceso_gid"), (0, 0, None))
        if f.get("grupo") == _GRUPO_DECIDIR and irr:
            detalle = f"AMALIA: para decidir · {irr} {_plural(irr, 'voto', 'votos')} Irrelevante"
            if mot:
                detalle += f" ({etq_motivo.get(mot, mot)})"
            desacuerdos.append({"gid": f.get("proceso_gid"), "titulo": f.get("titulo") or "",
                                "match": f.get("match"), "lado": "irrelevante", "detalle": detalle})
        elif f.get("grupo") == _GRUPO_DESCARTADO and rel:
            desacuerdos.append({"gid": f.get("proceso_gid"), "titulo": f.get("titulo") or "",
                                "match": f.get("match"), "lado": "relevante",
                                "detalle": f"AMALIA: descartado · {rel} {_plural(rel, 'voto', 'votos')} Relevante"})
    n_rel, n_irr = int(resumen.get("relevante") or 0), int(resumen.get("irrelevante") or 0)
    votos_ctx = {"relevante": n_rel, "irrelevante": n_irr,
                 "motivos": [(etq_motivo.get(e, e), n) for e, n in _pares(resumen.get("motivos"))],
                 "desacuerdos": desacuerdos[:_DESACUERDOS_VISIBLES],
                 "desacuerdos_mas": desacuerdos[_DESACUERDOS_VISIBLES:_DESACUERDOS_VISIBLES + _DESACUERDOS_EXTRA],
                 "n_desacuerdos": len(desacuerdos), "pocos": n_rel + n_irr < _POCOS_VOTOS}
    for c in columnas:  # el motivo del voto, legible
        for t in c["tarjetas"]:
            if t["voto"] and t["voto"]["motivo"]:
                t["voto"]["motivo"] = etq_motivo.get(t["voto"]["motivo"], t["voto"]["motivo"])

    # ── análisis (lo de antes, sin cambios, + tendencia y marcas) ──
    instituciones = pais.instituciones_top(conn, cliente, dias=dias)
    embudo = pais.embudo_pipeline(conn, cliente, dias=dias)
    severidad = pais.severidad_riesgos(conn, cliente, dias=dias)
    conversion = pais.conversion_comercial(conn, cliente, dias=dias)
    tiempos = pais.tiempo_por_etapa(conn, cliente, dias=dias)
    tendencia = tablero.tendencia_semanal(conn, cliente, semanas=12) or []

    def etq_semana(s):
        valor = s.get("semana")
        return valor.strftime("%d/%m") if hasattr(valor, "strftime") else str(valor)

    analisis = {
        "g_tendencia": graficos.linea_doble(
            [(etq_semana(s), s.get("revisados") or 0) for s in tendencia],
            [(etq_semana(s), s.get("relevantes") or 0) for s in tendencia], "Revisados", "Relevantes"),
        "g_marcas": graficos.barras(_pares(tablero.marcas_top(conn, cliente, dias, limite=5))),
        "g_embudo": graficos.embudo([(e["estado"], e["conteo"]) for e in embudo["camino"]]),
        "g_instituciones": graficos.barras([(i["institucion"], i["conteo"]) for i in instituciones]),
        "g_severidad": graficos.barras(
            [(s["severidad"], s["conteo"]) for s in severidad],
            [_COLOR_SEVERIDAD.get(s["severidad"], "var(--serie)") for s in severidad],
        ),
        "g_conversion": graficos.embudo([("Detectadas", conversion["detectadas"])]
                                        + [(e["estado"].capitalize(), e["conteo"]) for e in conversion["etapas"]]),
        "g_tiempos": graficos.barras([(t["etapa"].capitalize(), t["dias_promedio"]) for t in tiempos]),
        "conversion": conversion,
        "salidas": embudo["salidas"],
        "hay_severidad": bool(severidad),
        "tiempos": tiempos,
        "conflictos": _conflictos(conn, cliente),
    }

    return {
        "cabecera": {"veredicto": _veredicto(len(decidir_semana), len(sin_resp)), "fecha_larga": _fecha_larga(hoy_d),
                     "corrida": corrida},
        "kpis": kpis,
        "cierres": cierres,
        "atencion": atencion,
        "atencion_mas": atencion_mas,
        "tablero": {
            "columnas": columnas,
            "editable": bool(editable),
            "vencidas": {"n": vencidas},
            "asignables": asignables,
            "filtros": {"responsable": filtro["responsable"], "marca": filtro["marca"],
                        "opciones": {"responsables": [{"valor": a["usuario_id"], "etiqueta": a["nombre"]} for a in asignables],
                                     "marcas": _marcas_frecuentes(en_tablero, filtro["marca"])}},
            "truncado": bool(rango.get("truncado")),
        },
        "clasificacion": clasificacion,
        "votos": votos_ctx,
        "analisis": analisis,
        # E13: una línea por acordeón plegado (texto llano: la plantilla lo escapa).
        "resumenes": {
            "clasificacion": _resumen_clasificacion(revisados, relevantes, aptos_n),
            "equipo": _resumen_equipo(n_rel, n_irr, len(desacuerdos)),
            "analisis": "Tendencia, marcas, embudo y conversión",
        },
    }


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

    zona = _zona(cfg, cliente)
    hoy = _ahora(zona)
    puede_cambiar = alcance.puede_cambiar_estado(cliente)
    # Bajo «ver como» se muestran los mismos controles que vería esa persona, pero deshabilitados.
    solo_vista = getattr(alcance, "simulado", None) is not None
    try:
        with db.connection(cfg) as conn:
            nombres = regional.nombres_clientes(conn, sorted(alcance.clientes)) if len(alcance.clientes) > 1 else {}
            ctx = _contexto_pais(conn, cfg, alcance, cliente, dias, hoy, zona, request.query_params,
                                 editable=puede_cambiar and not solo_vista)
    except Exception:
        # Una ADB caída no debe mostrar un traceback ni un portal con ceros falsos.
        logger.exception("portal: no se pudieron leer los KPIs de %s", cliente)
        return _sin_datos(request)

    navegacion = _nav(request, alcance, f"pais:{cliente}", nombres)
    return pagina(
        request, "portal_pais.html",
        **navegacion,
        **ctx,
        cliente=cliente, pais_nombre=nombres.get(cliente, cliente), dias=dias, rangos=_RANGOS,
        puede_cambiar=puede_cambiar,
        solo_vista=solo_vista,
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
    # E13: vuelve a la tarjeta (`id="op-{gid|urlencode}"`), no al principio de la página.
    destino = "/portal?" + urlencode({"cliente": cliente, "dias": _rango(dias), "aviso": aviso}) + _ancla(proceso_gid)
    return RedirectResponse(destino, status_code=303)


def _escritura_e11(request: Request, csrf: str, ambito: str, cliente: str, dias: str, proceso_gid: str,
                   aviso_ok: str, escribir):
    """Esqueleto común de las escrituras de E11 (mismo orden que `cambiar_estado`).

    acceso → CSRF/Origin + límite → «ver como» rechazado SIN tocar la base → permiso → servicio → PRG.
    Lo ajeno (país o proceso) es el 404 de siempre y queda auditado."""
    cfg = request.app.state.cfg
    alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    rechazo = verificar_post(request, csrf, ambito, limitador=request.app.state.limitador_estado)
    if rechazo is not None:
        return rechazo
    if getattr(alcance, "simulado", None):
        destino = "/portal?" + urlencode({"cliente": cliente, "dias": _rango(dias), "aviso": "simulacion"})
        return RedirectResponse(destino, status_code=303)
    if not alcance.puede_cambiar_estado(cliente):
        if cliente and not alcance.puede_ver(cliente):
            _auditar_fuera_de_alcance(cfg, alcance, cliente)
        return feedback.no_encontrado(request)
    ip_hash = _ip_hash(cfg, request)
    try:
        with db.connection(cfg) as conn:
            resultado = escribir(conn, alcance, ip_hash)
    except Exception:
        logger.exception("portal: no se pudo completar %s de una oportunidad", ambito)
        return _sin_datos(request)
    if resultado == oportunidades.NO_ENCONTRADO:
        _auditar_fuera_de_alcance(cfg, alcance, cliente,
                                  recurso=f"{ambito}:{proceso_gid[:100]} cliente={cliente[:50]}")
        return feedback.no_encontrado(request)
    aviso = {
        oportunidades.OK: aviso_ok,
        oportunidades.CONFLICTO: "conflicto",
        oportunidades.SIMULACION: "simulacion",
        oportunidades.RESPONSABLE_INVALIDO: "responsable_invalido",
        oportunidades.NO_RECUPERABLE: "no_recuperable",
        oportunidades.YA_RECUPERADO: "no_recuperable",
    }.get(resultado, "invalido")
    destino = "/portal?" + urlencode({"cliente": cliente, "dias": _rango(dias), "aviso": aviso})
    return RedirectResponse(destino, status_code=303)


@router.post("/portal/oportunidad/{proceso_gid}/responsable")
def asignar_responsable(
    request: Request, proceso_gid: str,
    cliente: str = Form(""), responsable: str = Form(""), actualizado_en: str = Form(""),
    csrf: str = Form(""), dias: str = Form(""),
):
    """Asigna (o quita, con `responsable` vacío) quién sigue una oportunidad. El servicio valida que la
    persona sea asignable en ese cliente: acá no se confía en el `<select>`."""
    responsable_id = (responsable or "").strip()[:64] or None

    def escribir(conn, alcance, ip_hash):
        return oportunidades.asignar_responsable(conn, alcance, cliente, proceso_gid, responsable_id,
                                                 actualizado_en, ip_hash)

    return _escritura_e11(request, csrf, "responsable", cliente, dias, proceso_gid, "asignado", escribir)


@router.post("/portal/oportunidad/{proceso_gid}/recuperar")
def recuperar_descartado(
    request: Request, proceso_gid: str,
    cliente: str = Form(""), actualizado_en: str = Form(""), csrf: str = Form(""), dias: str = Form(""),
):
    """Devuelve al tablero (NUEVO) un proceso que AMALIA descartó; la clasificación original se conserva."""
    def escribir(conn, alcance, ip_hash):
        return oportunidades.recuperar_descartado(conn, alcance, cliente, proceso_gid, actualizado_en, ip_hash)

    return _escritura_e11(request, csrf, "recuperar", cliente, dias, proceso_gid, "recuperado", escribir)


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


_TEMA_MAX_EDAD_SEG = 365 * 86400


@router.post("/portal/tema")
def elegir_tema(request: Request, tema: str = Form(""), csrf: str = Form(""), volver: str = Form("")):
    """Guarda la preferencia Auto · Claro · Oscuro (E12) en una cookie y vuelve a la vista (PRG).

    Mismo orden que las demás escrituras (sesión → CSRF/Origin + límite), pero **es la única ruta POST
    del portal que NO rechaza `alcance.simulado`**: no escribe datos de negocio ni toca la base, solo una
    cookie de este navegador, así que también funciona en «ver como». Por lo mismo no se audita ni exige
    `cambia_estado`. Un `tema` fuera de la lista cerrada se ignora (la cookie no cambia)."""
    _alcance, respuesta = _acceso(request)
    if respuesta is not None:
        return respuesta
    rechazo = verificar_post(request, csrf, "tema", limitador=request.app.state.limitador_estado)
    if rechazo is not None:
        return rechazo
    respuesta = RedirectResponse(volver_seguro(volver), status_code=303)
    if tema in TEMAS:
        # Igual de estricta que la cookie de sesión (`acceso.py`): `__Host-` exige Secure, Path=/ y sin Domain.
        respuesta.set_cookie(COOKIE_TEMA, tema, max_age=_TEMA_MAX_EDAD_SEG, path="/",
                             secure=True, httponly=True, samesite="lax")
    return respuesta
