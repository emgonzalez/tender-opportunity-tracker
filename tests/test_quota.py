"""Tests de la guardia de gasto del drill-down (Fase 2, MVP Track A).

El presupuesto pasó de tokens a dólares. Estos tests fijan la propiedad que
motivó el cambio: dos modelos con precios distintos no se pueden sumar en la
misma unidad de tokens sin perder de vista el dinero.
"""
import json
import types
from datetime import date

import fitz  # pymupdf
import pytest

from src.drilldown import quota
from src.scraper.models import UsoModelo


def _cfg(tmp_path, budget=5.00, **kw):
    base = dict(
        daily_budget_usd=budget,
        budget_path=str(tmp_path / "gasto.json"),
        model_triage="claude-haiku-4-5",
        model_analysis="claude-sonnet-5",
        triage_raster_pages=5,
        analisis_tokens_por_pagina=3000,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _uso(modelo, inp, out=0, llamadas=1):
    return UsoModelo(modelo=modelo, input_tokens=inp, output_tokens=out, llamadas=llamadas)


class TestPrecios:
    def test_el_mismo_token_no_cuesta_lo_mismo_en_cada_modelo(self):
        """La razón de ser del cambio de unidad: 1 M de tokens son $1 o $3."""
        assert quota.costo_usd("claude-haiku-4-5", 1_000_000) == pytest.approx(1.00)
        assert quota.costo_usd("claude-sonnet-5", 1_000_000) == pytest.approx(3.00)

    def test_modelo_desconocido_se_cobra_como_el_mas_caro(self):
        """Apuntar MODEL_ANALYSIS a algo nuevo debe apretar la guardia, no soltarla."""
        caro = max(p[0] for p in quota.PRECIOS_USD_MTOK.values())
        assert quota.precio("modelo-que-no-existe")[0] >= caro

    def test_la_salida_se_cobra_mas_cara_que_la_entrada(self):
        assert (quota.costo_usd("claude-sonnet-5", 0, 1_000)
                > quota.costo_usd("claude-sonnet-5", 1_000, 0))


class TestLibroDiario:
    def test_cobra_el_consumo_real_no_una_estimacion(self, tmp_path):
        cfg = _cfg(tmp_path)
        gasto = quota.cobrar(cfg, _uso("claude-haiku-4-5", 12_660, 48))
        esperado = (12_660 * 1.00 + 48 * 5.00) / 1_000_000
        assert gasto == pytest.approx(esperado)
        assert quota.gasto_del_dia(cfg) == pytest.approx(esperado)

    def test_acumula_por_modelo_por_separado(self, tmp_path):
        """Sin el desglose no se puede saber qué etapa se está comiendo el día."""
        cfg = _cfg(tmp_path)
        quota.cobrar(cfg, _uso("claude-haiku-4-5", 10_000))
        quota.cobrar(cfg, _uso("claude-sonnet-5", 10_000))

        libro = json.loads((tmp_path / "gasto.json").read_text())
        assert libro["por_modelo"]["claude-sonnet-5"]["gasto_usd"] == pytest.approx(
            3 * libro["por_modelo"]["claude-haiku-4-5"]["gasto_usd"])
        assert libro["gasto_usd"] == pytest.approx(0.04)

    def test_uso_sin_llamadas_no_cobra(self, tmp_path):
        """Un pliego descartado antes del modelo no gastó nada."""
        cfg = _cfg(tmp_path)
        assert quota.cobrar(cfg, UsoModelo(modelo="claude-haiku-4-5")) == 0.0
        assert quota.cobrar(cfg, None) == 0.0
        assert quota.gasto_del_dia(cfg) == 0.0

    def test_cabe_hasta_agotar_el_tope(self, tmp_path):
        cfg = _cfg(tmp_path, budget=0.02)
        assert quota.cabe(cfg, 0.015) is True
        quota.cobrar(cfg, _uso("claude-haiku-4-5", 15_000))  # $0.015
        assert quota.cabe(cfg, 0.015) is False

    def test_resetea_al_cambiar_de_fecha(self, tmp_path):
        cfg = _cfg(tmp_path)
        quota._guardar_libro(cfg.budget_path,
                             {"fecha": "2000-01-01", "gasto_usd": 999.0, "por_modelo": {}})
        assert quota.gasto_del_dia(cfg) == 0.0

    def test_el_formato_viejo_en_tokens_no_se_lee_como_dolares(self, tmp_path):
        """`{"consumido": 45000}` interpretado como USD sería un tope de $45.000."""
        cfg = _cfg(tmp_path)
        quota._guardar_libro(cfg.budget_path,
                             {"fecha": date.today().isoformat(), "consumido": 45_000})
        assert quota.gasto_del_dia(cfg) == 0.0

    def test_archivo_corrupto_no_revienta(self, tmp_path):
        cfg = _cfg(tmp_path)
        (tmp_path / "gasto.json").write_text("{ no soy json")
        assert quota.gasto_del_dia(cfg) == 0.0
        assert quota.cabe(cfg, 0.01) is True


def _pliego(tmp_path, nombre, *, paginas=1, raster_px=None, pagina_pts=(612, 792),
            texto=""):
    path = tmp_path / nombre
    doc = fitz.open()
    for _ in range(paginas):
        page = doc.new_page(width=pagina_pts[0], height=pagina_pts[1])
        if texto:
            page.insert_text((72, 100), texto)
        if raster_px:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, *raster_px))
            pix.set_rect(pix.irect, (200, 200, 200))
            page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()
    return str(path)


# Página minúscula para los casos multipágina: generar 40 rásters tamaño carta
# cuesta ~125 s de suite. El ráster al doble de los puntos de la página mantiene
# el techo nativo por debajo del objetivo, que es la relación que importa acá.
_MINI_PTS = (100, 130)
_MINI_RASTER = (200, 260)


class TestEstimacionPreFlight:
    def test_el_triaje_escaneado_cuesta_mucho_mas_que_el_nativo(self, tmp_path):
        """Cinco páginas rasterizadas contra un extracto de texto: no es lo mismo,
        y la heurística vieja (n_páginas * 2000) los trataba igual."""
        cfg = _cfg(tmp_path)
        p = _pliego(tmp_path, "scan.pdf", paginas=5, raster_px=(1211, 1568))
        escaneado = quota.costo_triaje_estimado_usd(cfg, p, scanned=True, texto=None)
        nativo = quota.costo_triaje_estimado_usd(cfg, p, scanned=False, texto="x" * 4000)
        assert escaneado > 5 * nativo

    def test_el_triaje_escaneado_no_depende_del_largo_del_pliego(self, tmp_path):
        """Solo se rasterizan triage_raster_pages: un pliego de 40 páginas cuesta
        lo mismo que uno de 5 (es justamente el punto de acotar el triaje)."""
        cfg = _cfg(tmp_path)
        corto = _pliego(tmp_path, "corto.pdf", paginas=5,
                        pagina_pts=_MINI_PTS, raster_px=_MINI_RASTER)
        largo = _pliego(tmp_path, "largo.pdf", paginas=40,
                        pagina_pts=_MINI_PTS, raster_px=_MINI_RASTER)
        assert (quota.costo_triaje_estimado_usd(cfg, corto, scanned=True, texto=None)
                == pytest.approx(
                    quota.costo_triaje_estimado_usd(cfg, largo, scanned=True, texto=None)))

    def test_el_analisis_si_escala_con_las_paginas(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert (quota.costo_analisis_estimado_usd(cfg, 40)
                > 5 * quota.costo_analisis_estimado_usd(cfg, 2))

    def test_el_coeficiente_es_configurable(self, tmp_path):
        """Calibrarlo debe ser cambiar una env var, no editar código."""
        barato = _cfg(tmp_path, analisis_tokens_por_pagina=1000)
        caro = _cfg(tmp_path, analisis_tokens_por_pagina=3000)
        assert (quota.costo_analisis_estimado_usd(caro, 20)
                > 2 * quota.costo_analisis_estimado_usd(barato, 20))

    def test_pdf_ilegible_no_tumba_la_estimacion(self, tmp_path):
        cfg = _cfg(tmp_path)
        malo = tmp_path / "malo.pdf"
        malo.write_bytes(b"no soy un pdf")
        assert quota.costo_triaje_estimado_usd(
            cfg, str(malo), scanned=True, texto=None) > 0
