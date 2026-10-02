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
