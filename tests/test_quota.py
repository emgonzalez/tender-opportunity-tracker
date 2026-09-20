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

    def test_los_modelos_que_el_codigo_usa_de_verdad_tienen_precio(self):
        """El alias con fecha también cuenta.

        `engine.py` y `trainer.py` piden el ID con fecha (`claude-haiku-4-5-20251001`),
        no el alias corto de la tabla. Sin una entrada propia caían en el fallback
        y Haiku se cobraba 5x su precio, cerrando la guardia antes de tiempo.
        """
        from src.classifier.engine import MODEL as MODEL_ENGINE
        from src.classifier.trainer import MODEL as MODEL_TRAINER

        for modelo in {MODEL_ENGINE, MODEL_TRAINER}:
            assert modelo in quota.PRECIOS_USD_MTOK, (
                f"{modelo} no está en PRECIOS_USD_MTOK: se cobraría como el más caro"
            )
            assert quota.precio(modelo) == (1.00, 5.00)

    def test_todo_modelo_por_defecto_tiene_precio(self):
        """La guardia general, de los dos proveedores.

        El test de arriba fija un acoplamiento puntual: los dos `MODEL` que
        importan `engine` y `trainer`. Éste cubre la tabla entera de
        `modelos.DEFAULTS`, que es de donde sale el ID que se le pide al
        proveedor y el que termina en `UsoModelo.modelo`. Es lo que atrapa el
        día que alguien cambie un default —o agregue una etapa— y se olvide de
        la tarifa: `quota.precio()` no falla, devuelve el precio del más caro y
        sesga el presupuesto sin decir nada.
        """
        from src.llm import modelos

        for (proveedor, etapa), modelo in modelos.DEFAULTS.items():
            assert modelo in quota.PRECIOS_USD_MTOK, (
                f"el default de ({proveedor}, {etapa}) es {modelo} y no tiene tarifa: "
                "se cobraría como el más caro"
            )

    def test_oci_cobra_igual_la_entrada_que_la_salida(self):
        """OCI factura por transacción y no distingue prompt de respuesta.

        No es un descuido de la tabla: es la tarifa. Si algún día aparece acá un
        modelo de OCI con salida más cara, es que alguien copió la forma de
        Anthropic sin mirar la factura.
        """
        for modelo, (entrada, salida) in quota.PRECIOS_USD_MTOK.items():
            if modelo.startswith(("meta.", "cohere.")):
                assert entrada == salida, f"{modelo}: OCI no diferencia entrada de salida"

    def test_la_conversion_de_oci_usa_los_5_5_chars_por_token_medidos(self):
        """La Tanda 0 midió 5,5 chars/token; el plan original suponía 4.

        Con ×400 la guardia subestimaba ~37%. El test fija la aritmética
        completa —tarifa × 550 × margen— para que recalibrar sea cambiar una
        constante medida y no redescubrir la cadena entera.
        """
        esperado = 0.0018 * 550 * quota._FACTOR_SEGURIDAD_OCI
        assert quota.precio("meta.llama-4-scout-17b-16e-instruct") == (
            pytest.approx(esperado), pytest.approx(esperado)
        )
        assert quota._CHARS_POR_TOKEN_OCI == 5.5

    def test_ningun_modelo_de_oci_supera_al_precio_desconocido(self):
        """`_PRECIO_DESCONOCIDO` solo protege si de verdad es el techo.

        Su contrato es "un modelo fuera de tabla se cobra como el más caro que
        conozco". Meter en la tabla un modelo más caro que él —Cohere lo sería—
        invierte la guardia en silencio: lo desconocido pasaría a ser lo barato.
        """
        for entrada, salida in quota.PRECIOS_USD_MTOK.values():
            assert entrada <= quota._PRECIO_DESCONOCIDO[0]
            assert salida <= quota._PRECIO_DESCONOCIDO[1]


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


def _cfg_oci(tmp_path, **kw):
    """Config con LLM_PROVIDER=oci. No define `oci_model_*`: el ID sale de DEFAULTS."""
    base = dict(llm_provider="oci", oci_analisis_raster_pages=3)
    base.update(kw)
    return _cfg(tmp_path, **base)


class TestEstimacionPorProveedor:
    """La premisa de `ANALISIS_TOKENS_POR_PAGINA` es "quien rasteriza es
    Anthropic, del lado servidor". Con OCI eso deja de ser cierto: no hay bloque
    `document`, y el pliego se traduce acá a capa de texto o a N imágenes. Estos
    tests fijan que la estimación describa lo que el adaptador **realmente
    manda**, que es la única forma de que una guardia signifique algo.
    """

    def test_la_tarifa_sale_del_modelo_del_proveedor_activo(self, tmp_path):
        """Con OCI el análisis no lo paga Sonnet, y la estimación tiene que saberlo.

        Mismo pliego, misma cantidad de tokens estimados, distinta factura: es
        el mismo argumento que motivó pasar de tokens a dólares.
        """
        assert (quota.costo_analisis_estimado_usd(_cfg_oci(tmp_path), 20)
                < quota.costo_analisis_estimado_usd(_cfg(tmp_path), 20))

    def test_sin_pdf_la_guardia_frena_con_la_estimacion_por_pagina(self, tmp_path):
        """Los kwargs son opcionales; no poder mirar el PDF no afloja la guardia."""
        cfg = _cfg_oci(tmp_path)
        por_pagina = max(20 * cfg.analisis_tokens_por_pagina, 0)
        esperado = quota.costo_usd(
            "meta.llama-4-maverick-17b-128e-instruct-fp8",
            por_pagina + quota._OVERHEAD_PROMPT_TOKENS,
            quota._SALIDA_ANALISIS_TOKENS,
        )
        assert quota.costo_analisis_estimado_usd(cfg, 20) == pytest.approx(esperado)

    def test_oci_nativo_estima_la_capa_de_texto_y_no_las_paginas(self, tmp_path):
        """Para nativos OCI manda texto, que es órdenes de magnitud más barato
        que las ~3.000 tokens/página que cuesta un `document` en Anthropic."""
        cfg = _cfg_oci(tmp_path)
        p = _pliego(tmp_path, "nativo.pdf", paginas=2, texto="objeto de la contratacion")
        nativo = quota.costo_analisis_estimado_usd(cfg, 2, pdf_path=p, escaneado=False)
        assert nativo < quota.costo_analisis_estimado_usd(cfg, 2)

    def test_oci_escaneado_se_acota_a_las_paginas_que_se_rasterizan(self, tmp_path):
        """Un pliego de 40 páginas se analiza viendo `oci_analisis_raster_pages`.

        Es una pérdida real de cobertura y está documentada como tal; lo que no
        puede pasar es que la estimación cobre las 40 que nunca se mandan.
        """
        cfg = _cfg_oci(tmp_path)  # oci_analisis_raster_pages=3
        corto = _pliego(tmp_path, "corto_oci.pdf", paginas=3,
                        pagina_pts=_MINI_PTS, raster_px=_MINI_RASTER)
        largo = _pliego(tmp_path, "largo_oci.pdf", paginas=40,
                        pagina_pts=_MINI_PTS, raster_px=_MINI_RASTER)
        assert (quota.costo_analisis_estimado_usd(cfg, 3, pdf_path=corto, escaneado=True)
                == pytest.approx(
                    quota.costo_analisis_estimado_usd(cfg, 40, pdf_path=largo, escaneado=True)))

    def test_oci_escaneado_carga_la_hipotesis_pesimista_de_facturacion(self, tmp_path):
        """Oracle no documenta cómo cuenta una imagen y el factor en juego es 3,3×.

        Mientras no se cierre mirando Cost Analysis (la Usage API está denegada),
        la guardia asume que factura los caracteres del base64. Si este test
        empieza a molestar, la respuesta es medir, no bajar el factor.
        """
        from src.drilldown import pdf as _pdf

        cfg = _cfg_oci(tmp_path)
        p = _pliego(tmp_path, "scan_oci.pdf", paginas=3,
                    pagina_pts=_MINI_PTS, raster_px=_MINI_RASTER)
        tokens = _pdf.tokens_imagen_estimados(p, 3)
        assert tokens > 0
        esperado = quota.costo_usd(
            "meta.llama-4-maverick-17b-128e-instruct-fp8",
            int(tokens * quota._FACTOR_IMAGEN_B64_OCI) + quota._OVERHEAD_PROMPT_TOKENS,
            quota._SALIDA_ANALISIS_TOKENS,
        )
        assert (quota.costo_analisis_estimado_usd(cfg, 3, pdf_path=p, escaneado=True)
                == pytest.approx(esperado))

    def test_el_triaje_escaneado_tambien_carga_el_factor_de_la_imagen(self, tmp_path):
        """El triaje rasteriza igual con los dos proveedores; lo que cambia es
        la factura. Y el triaje es el gasto mayor en un corpus escaneado."""
        from src.drilldown import pdf as _pdf

        cfg = _cfg_oci(tmp_path)
        p = _pliego(tmp_path, "scan_triaje.pdf", paginas=5,
                    pagina_pts=_MINI_PTS, raster_px=_MINI_RASTER)
        tokens = _pdf.tokens_imagen_estimados(p, 5)
        esperado = quota.costo_usd(
            "meta.llama-4-scout-17b-16e-instruct",
            int(tokens * quota._FACTOR_IMAGEN_B64_OCI) + quota._OVERHEAD_PROMPT_TOKENS,
            quota._SALIDA_TRIAJE_TOKENS,
        )
        assert (quota.costo_triaje_estimado_usd(cfg, p, scanned=True, texto=None)
                == pytest.approx(esperado))

    @pytest.mark.parametrize("escaneado", [True, False])
    def test_un_pdf_ilegible_frena_con_la_estimacion_por_pagina(self, tmp_path, escaneado):
        """Una guardia que no puede estimar debe frenar, no soltarse: se vuelve a
        la estimación por página, que es la más cara de las dos."""
        cfg = _cfg_oci(tmp_path)
        malo = tmp_path / "malo_oci.pdf"
        malo.write_bytes(b"no soy un pdf")
        assert (quota.costo_analisis_estimado_usd(
                    cfg, 20, pdf_path=str(malo), escaneado=escaneado)
                == pytest.approx(quota.costo_analisis_estimado_usd(cfg, 20)))

    def test_las_paginas_que_se_estiman_son_las_que_se_mandan(self):
        """Espejo de los topes del adaptador, duplicados para no importar el SDK.

        Es la misma propiedad que protege `pdf.tokens_imagen_estimados` al usar
        el zoom del renderizador: si el estimador y el emisor no describen el
        mismo documento, la estimación habla de algo que nunca se manda.
        """
        from src.llm import proveedor_oci

        assert quota._RASTER_PAGINAS_OCI == proveedor_oci._RASTER_PAGINAS_DEFAULT
        assert (quota._PAGINAS_TEXTO_SI_DESCONOCIDO_OCI
                == proveedor_oci._PAGINAS_TEXTO_SI_DESCONOCIDO)
