"""Tests del rasterizado acotado del triaje (control de costo de tokens).

Contexto: en la validación del 2026-08-03 el triaje enviaba el PDF COMPLETO de
los pliegos escaneados a Haiku. Con ~66% del corpus escaneado, eso convirtió al
triaje en el mayor consumidor de tokens del pipeline. Estos tests fijan el
comportamiento nuevo: solo N páginas, rasterizadas y acotadas en tamaño.
"""
import base64

import fitz  # pymupdf
import pytest

from src.drilldown.pdf import MAX_LADO_PX, PdfCorruptoError, rasterizar_paginas
from src.drilldown.triage import _build_user_content


@pytest.fixture
def pdf_20_paginas(tmp_path):
    """PDF de 20 páginas tamaño carta con algo de texto."""
    path = tmp_path / "pliego.pdf"
    doc = fitz.open()
    for i in range(20):
        page = doc.new_page(width=612, height=792)  # Letter
        page.insert_text((72, 100), f"Pagina {i} — objeto de la contratacion")
    doc.save(str(path))
    doc.close()
    return str(path)


class TestRasterizarPaginas:
    def test_respeta_el_limite_de_paginas(self, pdf_20_paginas):
        assert len(rasterizar_paginas(pdf_20_paginas, 5)) == 5

    def test_no_excede_el_total_del_documento(self, pdf_20_paginas):
        # pedir más páginas de las que hay no debe reventar
        assert len(rasterizar_paginas(pdf_20_paginas, 999)) == 20

    def test_cero_paginas_devuelve_vacio(self, pdf_20_paginas):
        assert rasterizar_paginas(pdf_20_paginas, 0) == []

    def test_devuelve_jpeg_base64_valido(self, pdf_20_paginas):
        img = rasterizar_paginas(pdf_20_paginas, 1)[0]
        crudo = base64.standard_b64decode(img)
        assert crudo[:2] == b"\xff\xd8"  # cabecera JPEG (SOI)
        assert "\n" not in img

    def test_nunca_excede_el_lado_objetivo(self, pdf_20_paginas):
        """Tope de costo: por encima de MAX_LADO_PX el servidor reescala igual."""
        img = rasterizar_paginas(pdf_20_paginas, 1)[0]
        pix = fitz.Pixmap(base64.standard_b64decode(img))
        assert max(pix.width, pix.height) <= MAX_LADO_PX

    def test_alcanza_el_lado_objetivo(self, pdf_20_paginas):
        """Regresión del bug de 72 DPI: una carta (612x792 PUNTOS) se renderizaba
        a 612x792 px porque el zoom se topaba contra los puntos, no los píxeles.
        Debe llegar al lado objetivo, no quedarse en el tamaño de la página."""
        img = rasterizar_paginas(pdf_20_paginas, 1)[0]
        pix = fitz.Pixmap(base64.standard_b64decode(img))
        assert max(pix.width, pix.height) == MAX_LADO_PX

    def test_pdf_corrupto_lanza_error(self, tmp_path):
        malo = tmp_path / "malo.pdf"
        malo.write_bytes(b"no soy un pdf")
        with pytest.raises(PdfCorruptoError):
            rasterizar_paginas(str(malo), 3)


class TestContenidoDelTriaje:
    def test_escaneado_manda_imagenes_no_el_pdf(self, pdf_20_paginas):
        content = _build_user_content(
            "ctx", texto=None, pdf_path=pdf_20_paginas, scanned=True, raster_pages=3
        )
        tipos = [b["type"] for b in content]
        assert tipos.count("image") == 3
        # REGRESIÓN: jamás volver a mandar el PDF completo en el triaje
        assert "document" not in tipos

    def test_nativo_manda_solo_texto(self, pdf_20_paginas):
        content = _build_user_content(
            "ctx", texto="servidor rack firewall", pdf_path=pdf_20_paginas, scanned=False
        )
        assert [b["type"] for b in content] == ["text"]
        assert "servidor rack" in content[0]["text"]

    def test_raster_pages_controla_el_costo(self, pdf_20_paginas):
        pocas = _build_user_content("c", texto=None, pdf_path=pdf_20_paginas,
                                    scanned=True, raster_pages=2)
        muchas = _build_user_content("c", texto=None, pdf_path=pdf_20_paginas,
                                     scanned=True, raster_pages=8)
        n_pocas = sum(1 for b in pocas if b["type"] == "image")
        n_muchas = sum(1 for b in muchas if b["type"] == "image")
        assert (n_pocas, n_muchas) == (2, 8)

    def test_el_texto_declara_cuantas_paginas_van(self, pdf_20_paginas):
        content = _build_user_content("c", texto=None, pdf_path=pdf_20_paginas,
                                      scanned=True, raster_pages=4)
        texto = content[-1]["text"]
        assert "4 pagina" in texto
