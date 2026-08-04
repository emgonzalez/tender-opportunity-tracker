"""Tests de utilidades PDF del drill-down (Fase 2, MVP Track A). Sin red."""
import fitz  # pymupdf
import pytest

from src.drilldown.pdf import (
    page_count, extract_text_first_pages, is_scanned, read_pdf_b64, PdfCorruptoError,
    tokens_imagen_estimados, MAX_LADO_PX,
)

TEXTO_LARGO = (
    "Servidor HP ProLiant con firewall y almacenamiento para el data center "
    "institucional; incluye licenciamiento y servicios de instalación."
)


@pytest.fixture
def pdf_nativo(tmp_path):
    """PDF de una página con capa de texto (>40 chars)."""
    path = tmp_path / "nativo.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), TEXTO_LARGO)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def pdf_vacio(tmp_path):
    """PDF de una página sin texto (simula escaneado)."""
    path = tmp_path / "vacio.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    return str(path)


def test_page_count(pdf_nativo):
    assert page_count(pdf_nativo) == 1


def test_extract_text_contiene_texto(pdf_nativo):
    texto = extract_text_first_pages(pdf_nativo, 5)
    assert "Servidor" in texto


def test_is_scanned_false_para_nativo(pdf_nativo):
    assert is_scanned(pdf_nativo) is False


def test_is_scanned_true_para_vacio(pdf_vacio):
    assert is_scanned(pdf_vacio) is True


def test_read_pdf_b64_no_vacio_sin_saltos(pdf_nativo):
    b64 = read_pdf_b64(pdf_nativo)
    assert isinstance(b64, str) and len(b64) > 0
    assert "\n" not in b64


def test_pdf_corrupto_lanza_error(tmp_path):
    bad = tmp_path / "malo.pdf"
    bad.write_bytes(b"esto no es un PDF valido")
    with pytest.raises(PdfCorruptoError):
        page_count(str(bad))


def _pdf(tmp_path, nombre, *, raster_px=None, pagina_pts=(612, 792),
         paginas=1, raster_cubre_pagina=True, texto=False):
    """PDF de prueba con control del tamaño de página (PUNTOS) y del ráster (PÍXELES).

    La distinción es el corazón del bug que estos tests cubren: `page.rect` está
    en puntos (una carta son 612x792 sin importar a qué DPI se escaneó), y el
    zoom de render debe salir de los píxeles del ráster embebido, no de ahí.
    """
    path = tmp_path / nombre
    doc = fitz.open()
    for _ in range(paginas):
        page = doc.new_page(width=pagina_pts[0], height=pagina_pts[1])
        if texto:
            page.insert_text((72, 100), "texto vectorial del pliego")
        if raster_px:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, *raster_px))
            pix.set_rect(pix.irect, (200, 200, 200))
            destino = page.rect if raster_cubre_pagina else fitz.Rect(10, 10, 40, 40)
            page.insert_image(destino, pixmap=pix)
    doc.save(str(path))
    doc.close()
    return str(path)


def _lado_largo_render(path):
    """Lado largo en píxeles al que se rasterizaría la primera página."""
    from src.drilldown.pdf import _zoom_de_pagina
    doc = fitz.open(path)
    try:
        page = doc.load_page(0)
        z = _zoom_de_pagina(page, MAX_LADO_PX)
        return round(max(page.rect.width, page.rect.height) * z)
    finally:
        doc.close()


class TestResolucionDeRender:
    """Regresión del bug de rasterizado a 72 DPI (detectado 2026-08-04).

    `zoom = min(1.0, MAX_LADO_PX / lado)` medía `lado` en PUNTOS PDF, así que en
    toda página de tamaño estándar el zoom quedaba clavado en 1.0 y se
    renderizaba a 72 DPI. Medido sobre un pliego real de COMPRASAL: el escaneo
    embebido tenía 155 DPI (2200x1700 px) y se mandaba al modelo a 72 DPI —
    ilegible para texto legal denso.
    """

    def test_escaneo_de_alta_resolucion_se_lleva_al_lado_objetivo(self, tmp_path):
        """El caso del bug: 155 DPI reales deben llegar al modelo, no 72."""
        p = _pdf(tmp_path, "scan155.pdf", raster_px=(1700, 2200))
        assert _lado_largo_render(p) == MAX_LADO_PX

    def test_no_amplia_mas_alla_de_la_resolucion_nativa(self, tmp_path):
        """Un escaneo pobre a 72 DPI no gana detalle por renderizarlo más grande."""
        p = _pdf(tmp_path, "scan72.pdf", raster_px=(612, 792))
        assert _lado_largo_render(p) == 792

    def test_un_logo_no_encoge_la_pagina_entera(self, tmp_path):
        """Sin esta guarda, un logo de 30 px reduciría la página a una miniatura."""
        p = _pdf(tmp_path, "logo.pdf", raster_px=(30, 30),
                 raster_cubre_pagina=False, texto=True)
        assert _lado_largo_render(p) == MAX_LADO_PX

    def test_pagina_sobredimensionada_se_reduce(self, tmp_path):
        """Un plano de 2550x3300 PUNTOS debe bajarse, no costar una fortuna."""
        p = _pdf(tmp_path, "plano.pdf", pagina_pts=(2550, 3300), texto=True)
        assert _lado_largo_render(p) == MAX_LADO_PX

    def test_pagina_sin_raster_usa_el_objetivo(self, tmp_path):
        p = _pdf(tmp_path, "vector.pdf", texto=True)
        assert _lado_largo_render(p) == MAX_LADO_PX

    def test_el_render_real_coincide_con_la_estimacion(self, tmp_path):
        """Si estimador y renderizador divergen, la cuota mide un documento ficticio."""
        import base64
        from src.drilldown.pdf import rasterizar_paginas

        p = _pdf(tmp_path, "scan.pdf", raster_px=(1700, 2200))
        pix = fitz.Pixmap(base64.standard_b64decode(rasterizar_paginas(p, 1)[0]))
        real = int(pix.width * pix.height / 750)
        assert abs(real - tokens_imagen_estimados(p, 1)) <= 5  # redondeo de píxeles


# Página deliberadamente diminuta para los tests multipágina: generar 25 rásters
# tamaño carta cuesta ~70 s de suite. Con el ráster igual a la página en puntos
# el techo nativo manda (zoom=1) y la aritmética sigue siendo la misma.
_MINI_PTS = (100, 130)
_MINI_TOK = int(100 * 130 / 750)


class TestTokensImagenEstimados:
    def test_escaneo_de_alta_resolucion_cuesta_mucho_mas_que_uno_pobre(self, tmp_path):
        """Misma página (612x792 pt), distinto DPI: la razón de calibrar por formato."""
        alta = _pdf(tmp_path, "alta.pdf", raster_px=(1700, 2200))
        pobre = _pdf(tmp_path, "pobre.pdf", raster_px=(612, 792))
        assert tokens_imagen_estimados(alta, 1) > 3 * tokens_imagen_estimados(pobre, 1)

    def test_acotar_paginas_reduce_proporcionalmente(self, tmp_path):
        p = _pdf(tmp_path, "multi.pdf", pagina_pts=_MINI_PTS,
                 raster_px=_MINI_PTS, paginas=25)
        assert tokens_imagen_estimados(p, 5) * 5 == tokens_imagen_estimados(p, None)

    def test_n_none_estima_el_documento_completo(self, tmp_path):
        p = _pdf(tmp_path, "multi.pdf", pagina_pts=_MINI_PTS,
                 raster_px=_MINI_PTS, paginas=10)
        assert tokens_imagen_estimados(p, None) == 10 * _MINI_TOK

    def test_n_mayor_que_el_documento_no_revienta(self, tmp_path):
        p = _pdf(tmp_path, "corto.pdf", pagina_pts=_MINI_PTS,
                 raster_px=_MINI_PTS, paginas=3)
        assert tokens_imagen_estimados(p, 999) == tokens_imagen_estimados(p, None)

    def test_pdf_corrupto_devuelve_cero_sin_excepcion(self, tmp_path):
        """Estimar es una ayuda, no una guardia: nunca debe tumbar el batch."""
        bad = tmp_path / "malo.pdf"
        bad.write_bytes(b"no soy un pdf")
        assert tokens_imagen_estimados(str(bad), 3) == 0
