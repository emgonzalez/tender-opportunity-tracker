"""src.web.graficos — SVG de los KPIs del portal, generado en el servidor.

Cada función devuelve `Markup` listo para insertar en Jinja. Las marcas llevan
`data-tip` (texto del tooltip); `static/charts/*.js` solo agrega el hover. Sin JS el
gráfico se ve igual, y cada marca lleva además un `<title>` para lectores de pantalla.
Colores: variables CSS definidas en `templates/portal_base.html` (claro/oscuro).
"""
from __future__ import annotations

from markupsafe import Markup, escape

_ANCHO = 560


def _num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def sparkline(serie: list[int], ancho: int = 120, alto: int = 32) -> Markup:
    """Línea de tendencia sin ejes. Con menos de 2 puntos no hay tendencia que dibujar."""
    if len(serie) < 2:
        return Markup("")
    maximo, minimo = max(serie), min(serie)
    rango = (maximo - minimo) or 1
    paso = (ancho - 4) / (len(serie) - 1)
    pts = [(2 + i * paso, alto - 3 - (v - minimo) / rango * (alto - 6)) for i, v in enumerate(serie)]
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return Markup(
        f'<svg class="spark" viewBox="0 0 {ancho} {alto}" width="{ancho}" height="{alto}" '
        f'role="img" aria-label="Tendencia diaria, últimos {len(serie)} días">'
        f'<polyline points="{d}" fill="none" stroke="var(--serie)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--serie)" stroke="var(--superficie)" stroke-width="2"/></svg>'
    )


def barras(filas: list[tuple[str, int]], colores: list[str] | None = None) -> Markup:
    """Barras horizontales (etiqueta, valor) en el orden recibido. Vacío → cadena vacía."""
    if not filas:
        return Markup("")
    etiqueta_w, alto_fila, barra_h = 230, 28, 12
    max_w = _ANCHO - etiqueta_w - 60
    maximo = max(v for _, v in filas) or 1
    alto = alto_fila * len(filas)
    partes = [f'<svg class="grafico bar" viewBox="0 0 {_ANCHO} {alto}" role="img" '
              f'aria-label="Gráfico de barras horizontales">']
    for i, (nombre, valor) in enumerate(filas):
        y = i * alto_fila
        recorte = nombre if len(nombre) <= 34 else nombre[:33] + "…"
        w = max(valor / maximo * max_w, 2 if valor else 0)
        color = colores[i] if colores else "var(--serie)"
        tip = f"{nombre}: {_num(valor)}"
        partes.append(
            f'<g class="marca" data-tip="{escape(tip)}"><title>{escape(tip)}</title>'
            f'<rect x="0" y="{y}" width="{_ANCHO}" height="{alto_fila}" fill="transparent"/>'
            f'<text x="0" y="{y + 18}" class="etq">{escape(recorte)}</text>'
            f'<rect x="{etiqueta_w}" y="{y + 8}" width="{w:.1f}" height="{barra_h}" rx="3" fill="{color}"/>'
            f'<text x="{etiqueta_w + w + 8:.1f}" y="{y + 18}" class="val">{_num(valor)}</text></g>'
        )
    partes.append("</svg>")
    return Markup("".join(partes))


def embudo(etapas: list[tuple[str, int]]) -> Markup:
    """Embudo del camino feliz: ancho proporcional al primer conteo; cada etapa dice
    su conversión contra la anterior. Rampa de un solo tono (ordinal)."""
    if not etapas or not any(v for _, v in etapas):
        return Markup("")
    alto_fila, etiqueta_w = 30, 150
    max_w = _ANCHO - etiqueta_w - 110
    base = max(v for _, v in etapas) or 1
    alto = alto_fila * len(etapas)
    partes = [f'<svg class="grafico funnel" viewBox="0 0 {_ANCHO} {alto}" role="img" '
              f'aria-label="Embudo del pipeline">']
    previo = None
    for i, (nombre, valor) in enumerate(etapas):
        y = i * alto_fila
        w = max(valor / base * max_w, 2 if valor else 0)
        conv = f" · {round(100 * valor / previo)}% de la etapa anterior" if previo else ""
        tip = f"{nombre}: {_num(valor)}{conv}"
        extra = f"{round(100 * valor / previo)}%" if previo else ""
        partes.append(
            f'<g class="marca" data-tip="{escape(tip)}"><title>{escape(tip)}</title>'
            f'<rect x="0" y="{y}" width="{_ANCHO}" height="{alto_fila}" fill="transparent"/>'
            f'<text x="0" y="{y + 19}" class="etq">{escape(nombre.replace("_", " ").capitalize())}</text>'
            f'<rect x="{etiqueta_w}" y="{y + 5}" width="{w:.1f}" height="{alto_fila - 12}" rx="3" '
            f'fill="var(--rampa-{min(i, 7)})"/>'
            f'<text x="{etiqueta_w + w + 8:.1f}" y="{y + 19}" class="val">{_num(valor)}'
            f'<tspan class="conv" dx="8">{extra}</tspan></text></g>'
        )
        previo = valor or None
    partes.append("</svg>")
    return Markup("".join(partes))


def medidor(valor: float, tope: float, unidad: str = "USD") -> Markup:
    """Barra de uso valor/tope. Color semántico por consumo: <70 % bueno, <100 % aviso,
    ≥100 % crítico. Sin tope (≤ 0) no hay proporción que mostrar → cadena vacía."""
    if tope <= 0:
        return Markup("")
    pct = valor / tope * 100
    color = "var(--bueno)" if pct < 70 else "var(--aviso)" if pct < 100 else "var(--critico)"
    ancho_total, alto = _ANCHO - 20, 14
    w = min(pct, 100) / 100 * ancho_total
    if valor > 0:
        w = max(w, 2)
    tip = f"{valor:.2f} {unidad} de {tope:.2f} {unidad} ({pct:.1f}%)"
    return Markup(
        f'<svg class="grafico medidor" viewBox="0 0 {_ANCHO} {alto + 4}" role="img" '
        f'aria-label="{escape(tip)}">'
        f'<g class="marca" data-tip="{escape(tip)}"><title>{escape(tip)}</title>'
        f'<rect x="0" y="2" width="{ancho_total}" height="{alto}" rx="7" fill="var(--borde)"/>'
        f'<rect x="0" y="2" width="{w:.1f}" height="{alto}" rx="7" fill="{color}"/></g>'
        f'<text x="{ancho_total + 8}" y="{alto}" class="val">{pct:.0f}%</text></svg>'
    )


def linea(puntos: list[tuple[str, float | None, str]], unidad: str = "") -> Markup:
    """Serie temporal (etiqueta, valor|None, texto del tooltip) con ejes mínimos: el
    máximo en el eje Y y la primera/última etiqueta en el X. Los valores None dejan
    hueco. Con menos de 2 puntos con valor no hay línea que dibujar → vacío."""
    validos = [(i, v) for i, (_, v, _) in enumerate(puntos) if v is not None]
    if len(validos) < 2:
        return Markup("")
    izq, der, alto, base = 44, 12, 140, 22
    ancho_util, alto_util = _ANCHO - izq - der, alto - base - 8
    maximo = max(v for _, v in validos) or 1
    paso = ancho_util / (len(puntos) - 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        return izq + i * paso, 8 + alto_util - v / maximo * alto_util

    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in validos))
    partes = [
        f'<svg class="grafico linea" viewBox="0 0 {_ANCHO} {alto}" role="img" '
        f'aria-label="Serie de los últimos {len(puntos)} días">',
        f'<line x1="{izq}" y1="{8 + alto_util}" x2="{_ANCHO - der}" y2="{8 + alto_util}" '
        f'stroke="var(--borde)"/>',
        f'<text x="{izq - 6}" y="12" class="etq" text-anchor="end">{_num(round(maximo))}{escape(unidad)}</text>',
        f'<text x="{izq - 6}" y="{8 + alto_util}" class="etq" text-anchor="end">0</text>',
        f'<text x="{izq}" y="{alto - 4}" class="etq">{escape(puntos[0][0])}</text>',
        f'<text x="{_ANCHO - der}" y="{alto - 4}" class="etq" text-anchor="end">{escape(puntos[-1][0])}</text>',
        f'<polyline points="{d}" fill="none" stroke="var(--serie)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>',
    ]
    for i, v in validos:
        x, y = xy(i, v)
        tip = puntos[i][2]
        partes.append(
            f'<g class="marca" data-tip="{escape(tip)}"><title>{escape(tip)}</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="transparent"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--serie)" '
            f'stroke="var(--superficie)" stroke-width="2"/></g>'
        )
    partes.append("</svg>")
    return Markup("".join(partes))


# ---------------------------------------------------------------- E11: vista de país
_DIAS_ABR = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_DIAS_LARGO = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_URGENCIA_VAR = {"rojo": "var(--critico)", "ambar": "var(--aviso)", "verde": "var(--bueno)"}
_COLORES_SEGMENTO = {"critico", "aviso", "bueno", "serie", "muted", "borde"}


def _href_seguro(href: str | None) -> str | None:
    """Solo enlaces internos: `?…` o `/portal…`. Cualquier otro (javascript:, http:…) se descarta."""
    if isinstance(href, str) and (href.startswith("?") or href.startswith("/portal")):
        return href
    return None


def barras_dias(dias: list[dict], altura: int = 150) -> Markup:
    """Calendario de cierres: una columna por día con barra proporcional a `n`.
    Cada día: {"fecha": date, "n": int, "urgencia": 'rojo'|'ambar'|'verde'|None,
    "href": str|None, "activo": bool}. Sin días → cadena vacía."""
    if not dias:
        return Markup("")
    cuentas = [max(int(d.get("n") or 0), 0) for d in dias]
    maximo = max(cuentas) or 1
    ancho, alto_barra = 28, max(int(altura), 40)
    cols = []
    for d, n in zip(dias, cuentas):
        f = d["fecha"]
        nombre = _DIAS_LARGO[f.weekday()]
        abr = _DIAS_ABR[f.weekday()]
        if n == 0:
            tip = f"Sin cierres el {nombre} {f.day}"
        else:
            tip = f"{n} cierran el {nombre} {f.day}" if n != 1 else f"1 cierra el {nombre} {f.day}"
        color = _URGENCIA_VAR.get(d.get("urgencia") or "", "var(--serie)")
        h = max(n / maximo * (alto_barra - 16), 3) if n else 0
        barra = ""
        if n:
            barra = (f'<text x="{ancho / 2:.0f}" y="{alto_barra - h - 4:.1f}" text-anchor="middle" class="val">{n}</text>'
                     f'<rect x="4" y="{alto_barra - h:.1f}" width="{ancho - 8}" height="{h:.1f}" rx="3" fill="{color}"/>')
        svg = (f'<svg viewBox="0 0 {ancho} {alto_barra}" width="{ancho}" height="{alto_barra}" aria-hidden="true">'
               f'<rect x="0" y="{alto_barra - 1}" width="{ancho}" height="1" fill="var(--borde)"/>{barra}</svg>')
        activo = bool(d.get("activo"))
        clase = "dia activo" if activo else "dia"
        etiqueta = f'<span class="etq-dia">{escape(abr)}<br>{f.day}</span>'
        href = _href_seguro(d.get("href"))
        atributos = f'class="{clase}" data-tip="{escape(tip)}" title="{escape(tip)}"'
        if activo:
            atributos += ' aria-current="true"'
        if href and n:
            cols.append(f'<a {atributos} href="{escape(href)}" aria-label="{escape(tip)}">{svg}{etiqueta}</a>')
        else:
            cols.append(f'<div {atributos}>{svg}{etiqueta}</div>')
    resumen = f"Cierres de los próximos {len(dias)} días: {sum(cuentas)} en total"
    return Markup(f'<div class="cal-barras" role="img" aria-label="{escape(resumen)}">{"".join(cols)}</div>')


def barra_apilada(segmentos: list[tuple[str, int, str]]) -> Markup:
    """Barra horizontal apilada (etiqueta, valor, color_var). Ancho proporcional; ignora
    valores ≤ 0; total 0 → cadena vacía. `color_var` fuera de la lista cerrada → 'muted'."""
    validos = [(str(e), int(v), c if c in _COLORES_SEGMENTO else "muted") for e, v, c in segmentos if v and v > 0]
    total = sum(v for _, v, _ in validos)
    if total <= 0:
        return Markup("")
    partes = []
    for etiqueta, valor, color in validos:
        pct = valor / total * 100
        tip = f"{etiqueta}: {_num(valor)} ({pct:.0f}%)"
        dentro = f"{etiqueta} {_num(valor)}" if pct >= 18 else (_num(valor) if pct >= 8 else "")
        partes.append(
            f'<span data-tip="{escape(tip)}" title="{escape(tip)}" '
            f'style="width:{pct:.2f}%;background:var(--{color})">{escape(dentro)}</span>'
        )
    resumen = "; ".join(f"{e}: {_num(v)}" for e, v, _ in validos)
    return Markup(f'<div class="barra-apilada" role="img" aria-label="{escape(resumen)}">{"".join(partes)}</div>')


def linea_doble(serie_a: list[tuple[str, float]], serie_b: list[tuple[str, float]],
                etiqueta_a: str, etiqueta_b: str) -> Markup:
    """Dos series (etiqueta_x, valor) comparadas: a en gris, b en navy. Series vacías → ''."""
    if not serie_a and not serie_b:
        return Markup("")
    izq, der, alto, base = 44, 12, 160, 22
    ancho_util, alto_util = _ANCHO - izq - der, alto - base - 8
    maximo = max([v for _, v in serie_a] + [v for _, v in serie_b] + [0]) or 1
    n = max(len(serie_a), len(serie_b))
    paso = ancho_util / (n - 1) if n > 1 else 0

    def xy(i: int, v: float) -> tuple[float, float]:
        x = izq + (i * paso if n > 1 else ancho_util / 2)
        return x, 8 + alto_util - max(v, 0) / maximo * alto_util

    resumen = f"Comparación: {etiqueta_a} contra {etiqueta_b}"
    partes = [f'<svg class="grafico linea-doble" viewBox="0 0 {_ANCHO} {alto}" role="img" '
              f'aria-label="{escape(resumen)}">']
    for k in range(4):  # guías: 0, 1/3, 2/3 y máximo
        y = 8 + alto_util - k / 3 * alto_util
        partes.append(f'<line x1="{izq}" y1="{y:.1f}" x2="{_ANCHO - der}" y2="{y:.1f}" stroke="var(--borde)"/>'
                      f'<text x="{izq - 6}" y="{y + 4:.1f}" class="etq" text-anchor="end">{_num(round(maximo * k / 3))}</text>')
    for serie, nombre, color in ((serie_a, etiqueta_a, "var(--muted)"), (serie_b, etiqueta_b, "var(--serie)")):
        if not serie:
            continue
        pts = [xy(i, v) for i, (_, v) in enumerate(serie)]
        if len(pts) > 1:
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            partes.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2" '
                          f'stroke-linejoin="round" stroke-linecap="round"/>')
        for i, (x, y) in enumerate(pts):
            tip = f"{nombre} · {serie[i][0]}: {_num(round(serie[i][1]))}"
            partes.append(
                f'<g class="marca" data-tip="{escape(tip)}"><title>{escape(tip)}</title>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="transparent"/>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" stroke="var(--superficie)" stroke-width="2"/></g>')
        x, y = pts[-1]
        partes.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" class="val" text-anchor="end">'
                      f'{_num(round(serie[-1][1]))}</text>')
    ref = serie_b or serie_a
    partes.append(f'<text x="{izq}" y="{alto - 4}" class="etq">{escape(ref[0][0])}</text>'
                  f'<text x="{_ANCHO - der}" y="{alto - 4}" class="etq" text-anchor="end">{escape(ref[-1][0])}</text>')
    partes.append("</svg>")
    return Markup("".join(partes))
