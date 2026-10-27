"""Gráficos de la vista de país (E11): escape, enlaces seguros y casos vacíos."""
import re
from datetime import date, timedelta

from src.web.graficos import barra_apilada, barras_dias, linea_doble

MAL = "<script>alert(1)</script>"


def _limpio(html):
    s = str(html)
    assert "<script" not in s
    assert not re.search(r"\son\w+\s*=", s)


def _dias(**cambios):
    base = date(2026, 10, 27)  # martes
    ds = [{"fecha": base + timedelta(days=i), "n": 0, "urgencia": None, "href": None, "activo": False}
          for i in range(14)]
    ds[0].update({"n": 3, "urgencia": "rojo", "href": "?dia=2026-10-27", "activo": True})
    ds[1].update({"n": 1, "urgencia": "verde", "href": "/portal?dia=x"})
    ds[2].update({"n": 2, "urgencia": "ambar", "href": "javascript:alert(1)"})
    ds[3].update(cambios)
    return ds


def test_barras_dias_basico():
    h = str(barras_dias(_dias()))
    assert "3 cierran el martes 27" in h
    assert 'href="?dia=2026-10-27"' in h and 'href="/portal?dia=x"' in h
    assert "javascript:" not in h
    assert "var(--critico)" in h and 'aria-current="true"' in h
    assert h.count('class="dia') == 14
    _limpio(h)


def test_barras_dias_href_peligroso_y_escape():
    h = barras_dias(_dias(n=5, href="https://x.com", urgencia="<script>"))
    assert "https://x.com" not in str(h)
    _limpio(h)
    assert barras_dias([]) == ""


def test_barras_dias_negativos():
    _limpio(barras_dias(_dias(n=-4)))


def test_barra_apilada():
    h = str(barra_apilada([(MAL, 5, "critico"), ("b", 0, "bueno"), ("c", -2, "aviso"),
                           ("d", 5, 'x" onclick="y')]))
    assert "&lt;script&gt;" in h
    assert "var(--critico)" in h and "var(--muted)" in h
    assert "var(--bueno)" not in h
    assert "50%" in h
    _limpio(h)


def test_barra_apilada_vacia():
    assert barra_apilada([]) == ""
    assert barra_apilada([("a", 0, "bueno"), ("b", -1, "serie")]) == ""


def test_linea_doble():
    a = [("1 oct", 3.0), ("2 oct", 5.0)]
    b = [("1 oct", 1.0), ("2 oct", 9.0)]
    h = str(linea_doble(a, b, MAL, "Año <b>"))
    assert "&lt;script&gt;" in h and "var(--muted)" in h and "var(--serie)" in h
    assert h.count("<polyline") == 2
    _limpio(h)


def test_linea_doble_vacia_y_un_punto():
    assert linea_doble([], [], "a", "b") == ""
    h = str(linea_doble([("x", 2.0)], [], "a", "b"))
    assert "<polyline" not in h and "<circle" in h
    _limpio(h)
    _limpio(linea_doble([("x", -5.0)], [("y", 0.0)], "a", "b"))
