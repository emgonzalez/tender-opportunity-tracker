"""
Envío de reportes por email usando SMTP (Gmail con App Password).

El reporte diario es un resumen en tres grupos (Para decidir / No competibles /
Baja prioridad, ver la REGLA DE AGRUPAMIENTO más abajo), con parte HTML y parte
text/plain. Del ClientProfile usa el nombre y el catálogo (etiquetas de categoría);
el asunto NO usa `subject_prefix`. Los destinatarios salen de los usuarios del portal (ADB).

Las credenciales SMTP (usuario, contraseña, host) permanecen en .env
ya que son compartidas por todos los clientes.

Los REPORTES salen solo por aquí. Las ALERTAS de operación (sesión expirada,
circuit breaker) delegan en `notifier.alerts`, que las manda por todos los
canales configurados: si el correo está caído, el aviso igual sale por otro lado.
"""
import logging
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as _escapar
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.classifier.client_profile import ClientProfile
from src.config import Config
from src.feedback import service as feedback
from src.notifier import alerts
from src.notifier.agrupamiento import (  # noqa: F401 — reexportados: API pública del correo
    DIAS_CORTOS,
    DIAS_ESTA_SEMANA,
    DIAS_LARGOS,
    DIAS_PRONTO,
    GRUPO_BAJA,
    GRUPO_DECIDIR,
    GRUPO_NO_COMPETIBLE,
    MESES_CORTOS,
    MESES_LARGOS,
    UMBRAL_MATCH_DECIDIR,
    cierre,
    dias_hasta,
    fecha_corta,
    grupo_de,
    match_de,
)
from src.notifier.por_marca import es_de_marca_proceso, marcas_visibles
from src.scraper.models import MOTIVOS_BLOQUEO, ProcesoClasificado, ResultadoAnalisis

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path("/app/templates")


def _get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def resolve_recipients(config: Config, recipients: list[str]) -> tuple[list[str], list[str]]:
    """Punto ÚNICO de resolución de destinatarios.

    Devuelve `(destinatarios_efectivos, destinatarios_originales)`.

    Si `NOTIFICATION_OVERRIDE_TO` está configurado (modo validación), **todo** el
    correo se redirige ahí y los destinatarios reales de `clients/*.yaml` se
    ignoran por completo. Toda función de envío pasa por aquí: no hay forma de
    escribirle a un cliente real con el override activo.
    """
    override = config.override_recipients
    if not override:
        return list(recipients), list(recipients)
    logger.warning(
        "MODO VALIDACIÓN: correo redirigido a %s (destinatarios reales omitidos: %s)",
        ", ".join(override), ", ".join(recipients) or "—",
    )
    return override, list(recipients)


def _componer_mensaje(
    config: Config,
    efectivos: list[str],
    originales: list[str],
    subject: str,
    html_body: str,
    para: list[str] | None = None,
    texto: str | None = None,
) -> MIMEMultipart:
    """Arma el mensaje con una lista efectiva YA resuelta (no vuelve a resolver).

    `para` fija el `To` de un envío individual; el asunto y `X-Original-To` se
    deciden con la lista COMPLETA: que una sola persona reciba el correo no lo
    vuelve "no validación".

    Existe aparte de `_build_message` para el envío por destinatario: ahí la lista
    se resuelve UNA vez y se itera sobre ella. Si cada iteración re-resolviera con
    `[addr]`, el override mandaría los N correos a la misma dirección y perderíamos
    a quién correspondía cada uno.
    """
    msg = MIMEMultipart("alternative")
    # Con override activo, el asunto lo deja explícito para no confundir un
    # correo de prueba con uno real en la bandeja de entrada.
    msg["Subject"] = f"[VALIDACIÓN] {subject}" if efectivos != originales else subject
    msg["From"] = config.notification_from
    msg["To"] = ", ".join(efectivos if para is None else para)
    if efectivos != originales:
        # Trazabilidad: a quién habría llegado en producción.
        msg["X-Original-To"] = ", ".join(originales) or "(sin destinatarios)"
    # En un `alternative` la parte preferida va AL FINAL: texto plano primero (relojes,
    # vistas previas, lectores de solo texto) y HTML después. Las alertas no lo pasan.
    if texto is not None:
        msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _build_message(
    config: Config,
    recipients: list[str],
    subject: str,
    html_body: str,
    texto: str | None = None,
) -> MIMEMultipart:
    efectivos, originales = resolve_recipients(config, recipients)
    return _componer_mensaje(config, efectivos, originales, subject, html_body, texto=texto)


def _send(config: Config, client_id: str, recipients: list[str], msg: MIMEMultipart) -> None:
    efectivos, _ = resolve_recipients(config, recipients)
    if not efectivos:
        logger.warning("[%s] Sin destinatarios; no se envía nada.", client_id)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.smtp_user, efectivos, msg.as_string())
    logger.info(
        "[%s] Email enviado a %s (asunto: '%s').",
        client_id,
        ", ".join(efectivos),
        msg["Subject"],
    )


def _send_individual(
    config: Config,
    client_id: str,
    efectivos: list[str],
    originales: list[str],
    subject: str,
    render,
) -> None:
    """Un mensaje por dirección efectiva, sobre UNA sola conexión SMTP.

    `render(addr)` devuelve `(html, texto)` de esa persona (sus enlaces de
    feedback son suyos); un `str` solo se toma como HTML sin parte de texto. Un fallo con una dirección no debe dejar sin reporte a las demás:
    se registra y se sigue, y el primer error se relanza AL FINAL para que quien
    llama (`main.py`) lo trate igual que antes: log, alerta, circuit breaker.
    """
    errores: list[tuple[str, Exception]] = []
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        for addr in efectivos:
            try:
                cuerpo = render(addr)
                html, texto = cuerpo if isinstance(cuerpo, tuple) else (cuerpo, None)
                msg = _componer_mensaje(
                    config, efectivos, originales, subject, html, para=[addr], texto=texto
                )
                server.sendmail(config.smtp_user, [addr], msg.as_string())
                logger.info("[%s] Email enviado a %s (asunto: '%s').",
                            client_id, addr, msg["Subject"])
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] Falló el envío a %s: %s", client_id, addr, exc)
                errores.append((addr, exc))
    if errores:
        if len(errores) > 1:
            logger.error("[%s] Fallaron %d de %d envíos: %s", client_id, len(errores),
                         len(efectivos), ", ".join(a for a, _ in errores))
        raise errores[0][1]


# ── Correo diario tipo resumen (P5 del rediseño del correo) ─────────────────
#
# REGLA DE AGRUPAMIENTO (explícita y testeable; `grupo_de` la implementa). Sobre
# las relevantes (`es_relevante`), con `ra` = su ResultadoAnalisis si lo hay, en
# este orden de precedencia (la primera que aplica gana):
#   1. NO COMPETIBLES  ← `ra.competible is False` (bloqueo DETECTADO por el
#      drill-down: proveedor único, marca exigida fuera del portafolio, directa
#      nominada). Se muestra la etiqueta de `MOTIVOS_BLOQUEO`.
#   2. BAJA PRIORIDAD  ← `ra.descartado_por_triaje` (NO-GO) o match < UMBRAL_MATCH_DECIDIR.
#   3. PARA DECIDIR    ← el resto. `competible is None` (desconocido, o sin
#      análisis) NO excluye: si trae `motivo_bloqueo` se muestra como «Verificar».
# Orden: «Para decidir» y «No competibles» por fecha de cierre ascendente (sin
# fecha al final) y luego match descendente; «Baja prioridad» por match
# descendente. Los contadores del encabezado son len() de esos tres grupos y
# suman siempre el total de relevantes: nada desaparece en silencio.
# La regla, los umbrales y la urgencia viven en `src.notifier.agrupamiento` (los
# comparte el portal); se reexportan aquí con los mismos nombres de siempre.
# Topes de presentación. Ninguno oculta nada en silencio: «Para decidir» pasado
# el tope sigue visible en formato de una línea, y si «Baja prioridad» pasa el
# suyo, el correo dice cuántas faltan y enlaza a COMPRASAL.
MAX_DETALLADAS = 12
MAX_LINEAS_BAJA = 40
# Gmail recorta el correo a ~102 KB y deja fuera el final (incluidos los botones de
# voto). Si el HTML pasa de este presupuesto se degrada: tarjetas → filas y menos filas
# de baja prioridad, y el correo lo dice. Un día normal (~12 relevantes) pesa ~40 KB.
PRESUPUESTO_HTML = 90_000
LARGO_VEREDICTO = 140
LARGO_TITULO_LINEA = 90
COMPRASAL_URL = "https://www.comprasal.gob.sv/"

_DIAS = DIAS_CORTOS
_DIAS_LARGOS = DIAS_LARGOS
_MESES = MESES_CORTOS
_MESES_LARGOS = MESES_LARGOS
_dias_hasta = dias_hasta
_fecha_corta = fecha_corta
_cierre = cierre


def _recortar(texto: str, largo: int) -> str:
    texto = " ".join((texto or "").split())
    if len(texto) <= largo:
        return texto
    corte = texto[: largo - 1].rsplit(" ", 1)[0].rstrip(" ,;:.—-")
    return corte + "…"


def _institucion_corta(nombre: str) -> str:
    """La sigla entre paréntesis si la trae («… (RNPN)» → «RNPN»); si no, el nombre."""
    m = re.search(r"\(([^()]{2,15})\)\s*$", nombre or "")
    return m.group(1).strip() if m else _recortar(nombre, 40)


def _categoria_legible(slug: str, client_profile) -> str:
    """La etiqueta del catálogo del cliente para un slug («servicios_ti» → su `label`)."""
    if not slug:
        return ""
    catalog = getattr(client_profile, "catalog", None)
    for cat in getattr(catalog, "categories", None) or []:
        if getattr(cat, "id", None) == slug and getattr(cat, "label", ""):
            return cat.label
    return slug.replace("_", " ").capitalize()


def _veredicto(proc: ProcesoClasificado, ra: ResultadoAnalisis | None) -> str:
    """Una línea: la razón del clasificador; sin ella, la 1.ª oración del resumen del pliego.

    Nunca se redacta texto nuevo: solo se recorta lo que ya dijeron los modelos.
    """
    if proc.razon:
        return _recortar(proc.razon, LARGO_VEREDICTO)
    if ra is not None and ra.matriz and ra.matriz.resumen:
        return _recortar(ra.matriz.resumen.strip().split(". ")[0], LARGO_VEREDICTO)
    return ""


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def agrupar(
    relevantes: list[ProcesoClasificado],
    resultados_analisis: dict | None,
    hoy: datetime,
    client_profile=None,
    solo_marcas: frozenset[str] = frozenset(),
) -> dict[str, list[dict]]:
    """Arma las oportunidades del correo y las reparte en los tres grupos, ya ordenadas.

    `solo_marcas` (perfil restringido): el «Portafolio:» de cada tarjeta nombra solo marcas de esa marca;
    la selección de las oportunidades la hace quien llama (`send_report`).

    Devuelve `{GRUPO_DECIDIR: [...], GRUPO_NO_COMPETIBLE: [...], GRUPO_BAJA: [...]}`;
    cada oportunidad es el dict que pintan la plantilla y la parte de texto.
    """
    resultados_analisis = resultados_analisis or {}
    grupos: dict[str, list[dict]] = {GRUPO_DECIDIR: [], GRUPO_NO_COMPETIBLE: [], GRUPO_BAJA: []}
    for proc in relevantes:
        ra = resultados_analisis.get(proc.numero_referencia)
        match = match_de(proc)
        grupo = grupo_de(match, ra)
        dias = _dias_hasta(proc.fecha_limite, hoy)
        cierre, urgencia = _cierre(proc.fecha_limite, dias)
        inst = _institucion_corta(proc.institucion)
        categoria = _categoria_legible(proc.categoria_detectada, client_profile)
        marcas = marcas_visibles([m.marca for m in (proc.marcas_recomendadas or []) if m.marca], solo_marcas)[:2]
        estimado = f"USD {proc.monto:,.0f}" if proc.monto else ""
        meta = " · ".join(x for x in (
            inst, proc.forma_contratacion, proc.numero_referencia, categoria,
            f"match {match}", estimado,
            f"Portafolio: {', '.join(marcas)}" if marcas else "",
        ) if x)
        motivo = MOTIVOS_BLOQUEO.get(ra.motivo_bloqueo, ra.motivo_bloqueo) if ra and ra.motivo_bloqueo else ""
        fecha_txt = f"cierra {_fecha_corta(proc.fecha_limite)}" if proc.fecha_limite else ""
        if grupo == GRUPO_NO_COMPETIBLE:
            detalle = " · ".join(x for x in (motivo or "Bloqueo detectado en el pliego", fecha_txt) if x)
        elif grupo == GRUPO_BAJA:
            senal = "triaje NO-GO" if ra is not None and ra.descartado_por_triaje else ""
            detalle = " · ".join(x for x in (f"match {match}", senal, fecha_txt) if x)
        else:
            detalle = " · ".join(x for x in (cierre, f"match {match}") if x)
        grupos[grupo].append({
            "grupo": grupo,
            "code": proc.numero_referencia,
            "title": proc.descripcion,
            "titulo_corto": _recortar(proc.descripcion, LARGO_TITULO_LINEA),
            "institution": proc.institucion,
            "inst_corta": inst,
            "method": proc.forma_contratacion,
            "category": categoria,
            "marcas": marcas,
            "estimate": estimado,
            "match": match,
            "url": proc.url_directa,
            "fecha_limite": proc.fecha_limite,
            "daysLeft": dias,
            "cierre": cierre,
            "urgencia": urgencia,
            "meta": meta,
            "veredicto": _veredicto(proc, ra),
            # competible desconocido CON motivo: no excluye, pero se avisa.
            "alerta": motivo if grupo == GRUPO_DECIDIR and ra is not None and ra.competible is None else "",
            "motivo": motivo,
            "detalle": detalle,
        })

    def por_cierre(o):
        # Vigentes por fecha ascendente; luego las de plazo vencido; al final, sin fecha.
        d = o["daysLeft"]
        return (2 if d is None else (1 if d < 0 else 0), d if d is not None else 0, -o["match"])

    grupos[GRUPO_DECIDIR].sort(key=por_cierre)
    grupos[GRUPO_NO_COMPETIBLE].sort(key=por_cierre)
    grupos[GRUPO_BAJA].sort(key=lambda o: -o["match"])
    return grupos


def _textos(app: str, grupos: dict, total_revisados: int | None, omitidas: int) -> dict[str, str]:
    """Asunto, preheader, veredicto y contadores. No depende de `subject_prefix` del yaml.

    `total_revisados=None` (perfil restringido): ninguna frase menciona la cifra de procesos revisados.
    """
    decidir = grupos[GRUPO_DECIDIR]
    n_d, n_nc, n_b = len(decidir), len(grupos[GRUPO_NO_COMPETIBLE]), len(grupos[GRUPO_BAJA])
    total = n_d + n_nc + n_b
    semana = sum(1 for o in decidir if o["daysLeft"] is not None and 0 <= o["daysLeft"] <= DIAS_ESTA_SEMANA)
    revisados = _plural(total_revisados, "proceso revisado", "procesos revisados") if total_revisados is not None else ""

    if total == 0 and total_revisados is None:
        return {
            "asunto": f"{app} · Sin oportunidades nuevas",
            "preheader": "Hoy no hay oportunidades nuevas para tu portafolio.",
            "veredicto": "Sin oportunidades nuevas hoy.",
            "contadores": "Hoy ningún proceso de COMPRASAL es relevante para tu portafolio.",
        }
    if total == 0:
        return {
            "asunto": f"{app} · Sin oportunidades nuevas · {total_revisados} revisados",
            "preheader": f"Revisamos {_plural(total_revisados, 'proceso', 'procesos')} en "
                         "COMPRASAL; ninguno encaja con tu portafolio.",
            "veredicto": "Sin oportunidades nuevas hoy.",
            "contadores": f"{revisados} en COMPRASAL; ninguno es relevante para tu portafolio.",
        }

    primera = next((o for o in decidir if o["daysLeft"] is not None and o["daysLeft"] >= 0), None)
    if n_d:
        asunto = f"{app} · {n_d} para decidir"
        if primera:
            asunto += f" · 1.ª cierra {_fecha_corta(primera['fecha_limite'])} ({primera['inst_corta']})"
        # «N cierran esta semana» = las de «Para decidir» que cierran en ≤ DIAS_ESTA_SEMANA.
        asunto += f" · {semana} {'cierra' if semana == 1 else 'cierran'} esta semana"
        if primera:
            f = primera["fecha_limite"]
            veredicto = (f"{_plural(n_d, 'oportunidad', 'oportunidades')} para decidir; la más urgente "
                         f"cierra el {_DIAS_LARGOS[f.weekday()]} {f.day} de {_MESES_LARGOS[f.month - 1]}.")
        else:
            veredicto = (f"{_plural(n_d, 'oportunidad', 'oportunidades')} para decidir; "
                         "ninguna trae fecha de cierre: confírmala en COMPRASAL.")
    else:
        asunto = f"{app} · Nada para decidir hoy · {total} relevantes en revisión rápida"
        veredicto = "Nada para decidir hoy: las relevantes son de baja prioridad o no competibles."

    contadores = ((f"{revisados} → " if revisados else "") + f"{_plural(total, 'relevante', 'relevantes')} = {n_d} para decidir"
                  f" + {_plural(n_nc, 'no competible', 'no competibles')} + {n_b} de baja prioridad. ")
    contadores += (f"Este correo muestra {total - omitidas}; las {omitidas} restantes se indican al final."
                   if omitidas else "Este correo las muestra todas.")
    pre = [x for x in (
        _plural(n_nc, "no competible", "no competibles") if n_nc else "",
        f"{n_b} de baja prioridad" if n_b else "",
        revisados,
    ) if x]
    if not n_d:
        pre.insert(0, "Nada para decidir")
    preheader = " · ".join(pre) + (" · todas en este correo." if not omitidas else ".")
    return {"asunto": asunto, "preheader": preheader, "veredicto": veredicto, "contadores": contadores}


def _pie(app_full_name: str, con_votos: bool) -> str:
    return (
        f"«Para decidir» = match ≥ {UMBRAL_MATCH_DECIDIR}, triaje sin descartar y sin bloqueo "
        "detectado en el pliego; ordenadas por fecha de cierre. «Baja prioridad» = triaje NO-GO "
        f"o match < {UMBRAL_MATCH_DECIDIR}. Se marca en rojo lo que cierra en {DIAS_ESTA_SEMANA} "
        "días o menos. "
        + ("Tus votos en cada oportunidad afinan los próximos reportes. " if con_votos else "")
        + f"Enviado por {app_full_name}: son sugerencias; revisa el pliego en COMPRASAL antes de cotizar."
    )


def _texto_plano(ctx: dict, con_votos) -> str:
    """Parte text/plain del MIME alternative: el mismo contenido esencial que el HTML."""
    out = [ctx["encabezado"], "", ctx["veredicto"], ctx["contadores"], ""]

    def votos(o):
        fb = con_votos(o)
        return [f"   ¿Sirve? Relevante: {fb['relevante']}", f"   Irrelevante: {fb['irrelevante']}"] if fb else []

    if ctx["decidir"] or ctx["decidir_lineas"]:
        out.append(f"PARA DECIDIR ({len(ctx['decidir']) + len(ctx['decidir_lineas'])}) · por fecha de cierre")
        for o in ctx["decidir"]:
            out += ["", f"* {o['cierre']}", f"  {o['title']}", f"  {o['meta']}"]
            if o["alerta"]:
                out.append(f"  Verificar: {o['alerta']}.")
            if o["veredicto"]:
                out.append(f"  {o['veredicto']}")
            out.append(f"  COMPRASAL: {o['url']}")
            out += votos(o)
        for o in ctx["decidir_lineas"]:
            out += ["", f"* {o['inst_corta']} · {o['titulo_corto']} · {o['detalle']}", f"  {o['url']}"]
            out += votos(o)
        out.append("")
    for titulo, clave in (("NO COMPETIBLES", "no_competibles"), ("BAJA PRIORIDAD", "baja")):
        lista = ctx[clave]
        if not lista:
            continue
        n = len(lista) + (ctx["baja_omitidas"] if clave == "baja" else 0)
        out.append(f"{titulo} ({n})" + (f" · {ctx['regla_baja']}" if clave == "baja" else ""))
        for o in lista:
            out += [f"- {o['inst_corta']} · {o['titulo_corto']} · {o['detalle']}", f"  {o['url']}"]
            out += votos(o)
        if clave == "baja" and ctx["baja_omitidas"]:
            out.append(f"Y {ctx['baja_omitidas']} más de baja prioridad que no caben en este correo: "
                       f"búscalas en {COMPRASAL_URL}")
        out.append("")
    if not (ctx["decidir"] or ctx["decidir_lineas"] or ctx["no_competibles"] or ctx["baja"]):
        out += ["Sin oportunidades relevantes en este período.", ""]
    out += ["--", ctx["pie"]]
    return "\n".join(out) + "\n"


def componer_correo(
    config,
    client_profile,
    relevantes: list[ProcesoClasificado],
    total_revisados: int,
    run_timestamp: datetime,
    resultados_analisis: dict | None,
    hoy: datetime,
    solo_marcas: frozenset[str] = frozenset(),
):
    """Arma el correo sin enviarlo: devuelve `(asunto, render)`.

    `solo_marcas` no vacío = perfil restringido (E14): `relevantes` ya viene filtrado por quien llama; aquí
    solo se oculta la cifra de procesos revisados (revelaría el tamaño del universo) y se acota el portafolio.

    `render(con_votos)` → `(html, texto)`; `con_votos(oportunidad)` da los enlaces
    de feedback de esa oportunidad (o None). Lo usan `send_report` y
    `scripts/validate_trackb_email.py`, para que la verificación pinte lo mismo
    que se envía.
    """
    revisados = None if solo_marcas else total_revisados
    grupos = agrupar(relevantes, resultados_analisis, hoy, client_profile, solo_marcas)
    decidir = grupos[GRUPO_DECIDIR]
    baja = grupos[GRUPO_BAJA]
    app = getattr(config, "app_name", "") or "AMALIA"
    app_full_name = getattr(config, "app_full_name", "") or app
    asunto = _textos(app, grupos, revisados, max(0, len(baja) - MAX_LINEAS_BAJA))["asunto"]

    fecha = run_timestamp
    semana = sum(1 for o in decidir if o["daysLeft"] is not None and 0 <= o["daysLeft"] <= DIAS_ESTA_SEMANA)

    def armar(detalladas: int, max_baja: int) -> dict:
        """Contexto de la plantilla con `detalladas` tarjetas y hasta `max_baja` filas de baja prioridad."""
        omitidas = max(0, len(baja) - max_baja)
        textos = _textos(app, grupos, revisados, omitidas)
        return {
            "app_name": app,
            "fecha_corrida": _fecha_corta(fecha),
            "encabezado": (f"{app} · COMPRASAL · {getattr(client_profile, 'client_name', '')} · "
                           f"{_DIAS_LARGOS[fecha.weekday()]} {fecha.day} {_MESES[fecha.month - 1]} {fecha.year}"),
            "titulo": "Licitaciones TI",
            "subtitulo": f"para {getattr(client_profile, 'client_name', '')}".strip(),
            "fecha_larga": f"{_DIAS_LARGOS[fecha.weekday()]} {fecha.day} {_MESES[fecha.month - 1]} {fecha.year}",
            "kpi_revisados": revisados,
            "kpi_oportunidades": len(relevantes),
            "kpi_semana": semana,
            "preheader": textos["preheader"],
            "veredicto": textos["veredicto"],
            "contadores": textos["contadores"],
            "decidir": decidir[:detalladas],
            "decidir_lineas": decidir[detalladas:],
            "no_competibles": grupos[GRUPO_NO_COMPETIBLE],
            "baja": baja[:max_baja],
            "baja_omitidas": omitidas,
            "comprasal_url": COMPRASAL_URL,
            "regla_baja": f"match < {UMBRAL_MATCH_DECIDIR} o triaje NO-GO",
            "umbral_decidir": UMBRAL_MATCH_DECIDIR,
            "dias_urgente": DIAS_ESTA_SEMANA,
            "cliente": getattr(client_profile, "client_name", ""),
            "pie": _pie(app_full_name, feedback.activo(config)),
        }

    template = _get_jinja_env().get_template("report.html")
    # Escalones de degradación: (tarjetas detalladas, filas de baja prioridad).
    escalones = [(MAX_DETALLADAS, MAX_LINEAS_BAJA), (6, MAX_LINEAS_BAJA), (6, 20), (3, 10), (0, 5)]

    def render(con_votos) -> tuple[str, str]:
        """(html, texto) con los enlaces de feedback que devuelva `con_votos(o)`."""
        for detalladas, max_baja in escalones:
            c = armar(detalladas, max_baja)
            for clave in ("decidir", "decidir_lineas", "no_competibles", "baja"):
                c[clave] = [{**o, "feedback": con_votos(o)} for o in c[clave]]
            # La frase «Tus votos…» del pie solo si algún enlace de voto salió en el correo.
            c["con_votos"] = any(o["feedback"] for clave in ("decidir", "decidir_lineas", "no_competibles", "baja")
                                 for o in c[clave])
            html = template.render(**c)
            if len(html.encode("utf-8")) <= PRESUPUESTO_HTML:
                break
        else:
            logger.warning("[%s] El correo pesa %d bytes aun con el mínimo de detalle (presupuesto %d).",
                           getattr(client_profile, "client_id", "?"), len(html.encode("utf-8")), PRESUPUESTO_HTML)
        if (detalladas, max_baja) != escalones[0]:
            logger.warning("[%s] Correo degradado para no pasar de %d bytes: %d tarjetas, %d filas de baja prioridad.",
                           getattr(client_profile, "client_id", "?"), PRESUPUESTO_HTML, detalladas, max_baja)
        logger.info("[%s] Correo: %d bytes de HTML, %d oportunidades (%d para decidir, "
                    "%d no competibles, %d baja prioridad, %d omitidas).",
                    getattr(client_profile, "client_id", "?"), len(html.encode("utf-8")),
                    len(relevantes), len(decidir), len(grupos[GRUPO_NO_COMPETIBLE]), len(baja), c["baja_omitidas"])
        return html, _texto_plano(c, lambda o: o.get("feedback"))

    return asunto, render


def _leer_destinatarios(config: Config, client_id: str) -> list[tuple[str, frozenset[str]]] | None:
    """`[(correo, solo_marcas)]` de los usuarios del portal con acceso al cliente, o `None` si la ADB no respondió.

    Distingue «la ADB falló» (`None`) de «no hay nadie» (`[]`): son dos alertas distintas. Nunca lanza.
    """
    try:
        from src.identidad import repo
        from src.memoria import db
        with db.connection(config) as conn:
            org_id = repo.org_de_cliente(conn, client_id)
            return repo.destinatarios_reporte(conn, org_id, client_id) if org_id else []
    except Exception as exc:  # noqa: BLE001 — la corrida no cae por la ADB: el reporte no sale y se avisa
        logger.error("[%s] No se pudieron leer los destinatarios en la ADB: %s", client_id, exc)
        return None


def _avisar_sin_reporte(config: Config, client_profile: ClientProfile, asunto: str, texto: str) -> list[str]:
    """Alerta a los admin_plataforma de que el reporte de un cliente NO salió. Nunca lanza."""
    app = config.app_name
    html_body = (
        '<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">'
        f'<h2 style="color: #e74c3c;">⚠️ {_escapar(app)}: reporte no enviado</h2>'
        f"<p>{_escapar(texto)}</p>"
        f'<p style="color: #7f8c8d; font-size: 12px;">Generado automáticamente por {_escapar(app)}.</p>'
        "</body></html>"
    )
    return alerts.notify(
        config, f"[{app}] {asunto} — {client_profile.client_name}", texto,
        email_recipients=alerts.admins_plataforma(config), email_html=html_body,
        client_id=client_profile.client_id,
    )


def send_report(
    config: Config,
    client_profile: ClientProfile,
    procesos_clasificados: list[ProcesoClasificado],
    total_revisados: int,
    run_timestamp: datetime,
    resultados_analisis: dict | None = None,
) -> None:
    """Envía el reporte diario (resumen en tres grupos) de un cliente, uno por destinatario.

    Los destinatarios son los usuarios del portal con acceso al cliente (ADB, sin respaldo en el YAML).
    Ya NO sale siempre: si la ADB no responde, o el cliente activo no tiene destinatarios, no se envía
    nada y se alerta a los admin_plataforma (sin lanzar: la corrida sigue). Con destinatarios SIEMPRE sale,
    aunque no haya relevantes (RB-005): el conteo de revisados es la señal de vida.

    Quien tiene un rol restringido a una marca (E14) recibe solo las oportunidades de esa marca, con asunto
    y contadores sobre ellas y sin la cifra de revisados; sin oportunidades el correo sale igual, con 0.
    Se compone un correo por PERFIL (conjunto de marcas) y se envía uno por persona, así los enlaces de
    voto son suyos. Grupos, orden y textos: ver la REGLA DE AGRUPAMIENTO.
    """
    cid = client_profile.client_id
    destinatarios = _leer_destinatarios(config, cid)
    if destinatarios is None:
        _avisar_sin_reporte(
            config, client_profile, "Reporte NO enviado: la ADB no responde",
            f"No se pudo leer en la ADB quién recibe el reporte de {client_profile.client_name}; "
            "el correo de hoy no salió. Revisa la ADB y reenvía con `python -m src.notifier.resend`.")
        return
    if not destinatarios:
        _avisar_sin_reporte(
            config, client_profile, "Reporte NO enviado: cliente sin destinatarios",
            f"El cliente activo {client_profile.client_name} ({cid}) no tiene usuarios con rol de negocio "
            "y acceso a su país: el correo de hoy no salió. Da de alta usuarios con `python -m src.identidad alta`.")
        return

    relevantes = [p for p in procesos_clasificados if p.es_relevante]
    today = datetime.now(run_timestamp.tzinfo)

    # Un perfil por conjunto de marcas, en orden de aparición (los correos ya vienen ordenados).
    perfiles: dict[frozenset[str], list[str]] = {}
    for correo, solo_marcas in destinatarios:
        perfiles.setdefault(solo_marcas, []).append(correo)

    componidos = []
    for solo_marcas, correos in perfiles.items():
        visibles = [p for p in relevantes if es_de_marca_proceso(p, solo_marcas)]
        subject, render = componer_correo(
            config, client_profile, visibles, total_revisados, run_timestamp,
            resultados_analisis, today, solo_marcas=solo_marcas,
        )
        componidos.append((correos, visibles, subject, render))

    # La foto del proceso tiene que existir ANTES de que alguien haga clic. Todas las que algún perfil
    # reciba (la unión), también las de una línea: todas llevan su voto.
    if feedback.activo(config):
        union = {p.numero_referencia: p for _, visibles, _, _ in componidos for p in visibles}
        feedback.registrar_reportadas(config, cid, list(union.values()))

    primer_error: Exception | None = None
    for correos, _visibles, subject, render in componidos:
        # MODO VALIDACIÓN: los efectivos son el override y ningún destinatario real recibe nada;
        # `X-Original-To` conserva los reales DE ESE PERFIL. Sin override, efectivos == originales.
        efectivos, originales = resolve_recipients(config, correos)

        def render_para(addr: str, render=render) -> tuple[str, str]:
            return render(lambda o: feedback.enlaces_para(config, cid, o["code"], addr))

        try:
            _send_individual(config, cid, efectivos, originales, subject, render_para)
        except Exception as exc:  # noqa: BLE001 — un perfil que falla no deja sin reporte a los demás
            primer_error = primer_error or exc
    if primer_error is not None:
        raise primer_error


def send_access_code(config: Config, email: str, codigo: str, ttl_min: int) -> None:
    """Envía el código de acceso al portal (plan de identidad, P7 y T9).

    ÚNICA excepción al modo validación: NO pasa por `resolve_recipients`. Con el
    override activo el operador recibiría los códigos de todos y nadie más podría
    entrar; el reporte diario sigue redirigido. Solo recibe código quien tiene
    cuenta dada de alta (quien llama lo verifica antes).

    El código no se escribe en el log ni en el asunto (T12). Un fallo SMTP se
    propaga: quien llama decide qué responder sin delatar si la cuenta existe.
    """
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #14304d;">{app}: tu código de acceso</h2>
    <p>Usa este código para entrar al portal:</p>
    <p style="font-size: 28px; letter-spacing: 4px; font-family: monospace;"><strong>{codigo}</strong></p>
    <p>Vence en {ttl_min} minutos y sirve una sola vez.
    Si no lo pediste tú, ignora este correo.</p>
    </body></html>
    """
    texto = (
        f"{app}: tu código de acceso es {codigo}\n\n"
        f"Vence en {ttl_min} minutos y sirve una sola vez. "
        "Si no lo pediste tú, ignora este correo."
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{app}] Tu código de acceso"
    msg["From"] = config.notification_from
    msg["To"] = email
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.smtp_user, [email], msg.as_string())
    logger.info("Código de acceso enviado a %s.", email)


def send_session_expired_alert(config: Config, client_profile: ClientProfile) -> list[str]:
    """Notifica a system admins que la sesión de COMPRASAL expiró.

    Devuelve los canales por los que salió. Ya no aborta cuando no hay
    `system_admins_email`: el correo es *un* canal, no el único, y Telegram puede
    estar configurado aunque no haya ninguna dirección. Si no hay ninguno,
    `notify` lo deja dicho en el log.
    """
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">⚠️ {app}: Sesión de COMPRASAL expirada</h2>
    <p>Hola,</p>
    <p>La sesión de acceso a COMPRASAL ha expirado para el cliente <strong>{client_profile.client_name}</strong>
    y necesita ser renovada para que {app} pueda continuar monitoreando licitaciones.</p>
    <h3>Para renovar la sesión:</h3>
    <ol>
      <li>Conéctate al servidor donde corre {app}</li>
      <li>Ejecuta: <code>echo "CODIGO_OTP" | docker compose run -T --rm amalia python -m src.auth.login --manual</code></li>
      <li>{app} retomará su operación normal en la próxima ejecución programada</li>
    </ol>
    <p style="color: #7f8c8d; font-size: 12px;">
      Generado automáticamente por {app} ({config.app_full_name})
    </p>
    </body></html>
    """
    subject = f"[{app}] ⚠️ Acción requerida: Renovar sesión de COMPRASAL — {client_profile.client_name}"
    # Texto plano para Telegram/webhook. Sin Markdown: los guiones bajos del
    # comando harían fallar el envío justo cuando hay algo que reportar.
    texto = (
        f"La sesión de COMPRASAL expiró para el cliente {client_profile.client_name} "
        f"y {app} no puede seguir monitoreando licitaciones.\n\n"
        "Para renovarla, en el servidor:\n"
        '  echo "CODIGO_OTP" | docker compose run -T --rm amalia python -m src.auth.login --manual'
    )
    return alerts.notify(
        config, subject, texto,
        email_recipients=alerts.admins_plataforma(config),
        email_html=html_body,
        client_id=client_profile.client_id,
    )


def send_circuit_breaker_alert(
    config: Config,
    client_profile: ClientProfile,
    error_count: int,
    last_error: str,
) -> list[str]:
    """Notifica a system admins cuando el agente falla repetidamente.

    Este es EL caso que justifica el multi-canal: entre las causas de que la
    corrida falle N veces seguidas está que el propio SMTP esté caído, y
    entonces el aviso por correo tampoco sale.
    """
    app = config.app_name
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #e74c3c;">🚨 {app}: Error crítico</h2>
    <p>{app} ha fallado <strong>{error_count} veces consecutivas</strong> para el cliente
    <strong>{client_profile.client_name}</strong> y ha pausado su operación.</p>
    <h3>Último error:</h3>
    <pre style="background: #f8f8f8; padding: 10px; border-left: 4px solid #e74c3c; font-size: 12px; overflow-x: auto;">
{last_error[:1000]}
    </pre>
    <p>Revisa los logs del contenedor: <code>docker compose logs -f amalia</code></p>
    <p style="color: #7f8c8d; font-size: 12px;">
      Generado automáticamente por {app} ({config.app_full_name})
    </p>
    </body></html>
    """
    subject = f"[{app}] 🚨 Circuit breaker activado ({error_count} errores consecutivos) — {client_profile.client_name}"
    texto = (
        f"{app} falló {error_count} veces consecutivas para el cliente "
        f"{client_profile.client_name} y pausó su operación.\n\n"
        f"Último error:\n{last_error[:1000]}\n\n"
        "Revisar: docker compose logs -f amalia"
    )
    return alerts.notify(
        config, subject, texto,
        email_recipients=alerts.admins_plataforma(config),
        email_html=html_body,
        client_id=client_profile.client_id,
    )
