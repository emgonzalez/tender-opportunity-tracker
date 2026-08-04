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


class TestTokensImagenEstimados:
    """El costo de un pliego escaneado depende del DPI, no solo de las páginas.

    Esta es la corrección de fondo a la heurística `n_paginas * 2000`: dos
    documentos con el mismo número de páginas cuestan muy distinto según a qué
    resolución se escanearon.
    """

    def _pdf(self, tmp_path, nombre, ancho, alto, paginas=1):
        path = tmp_path / nombre
        doc = fitz.open()
        for _ in range(paginas):
            doc.new_page(width=ancho, height=alto)
        doc.save(str(path))
        doc.close()
        return str(path)

    def test_pagina_pequena_no_se_amplia(self, tmp_path):
        """Carta a 72 DPI (612x792): zoom=1, así que son (612*792)/750 tokens."""
        p = self._pdf(tmp_path, "carta.pdf", 612, 792)
        assert tokens_imagen_estimados(p, 1) == int(612 * 792 / 750)

    def test_escaneo_grande_se_acota_al_lado_maximo(self, tmp_path):
        """Carta a 300 DPI (2550x3300) se reescala a <=1568 px de lado largo."""
        p = self._pdf(tmp_path, "scan.pdf", 2550, 3300)
        z = MAX_LADO_PX / 3300
        assert tokens_imagen_estimados(p, 1) == int((2550 * z) * (3300 * z) / 750)

    def test_un_escaneo_cuesta_mucho_mas_que_un_nativo(self, tmp_path):
        """La razón por la que el formato debe entrar en la estimación de cuota."""
        chico = self._pdf(tmp_path, "chico.pdf", 612, 792)
        grande = self._pdf(tmp_path, "grande.pdf", 2550, 3300)
        assert tokens_imagen_estimados(grande, 1) > 3 * tokens_imagen_estimados(chico, 1)

    def test_acotar_paginas_reduce_proporcionalmente(self, tmp_path):
        p = self._pdf(tmp_path, "multi.pdf", 2550, 3300, paginas=25)
        assert tokens_imagen_estimados(p, 5) * 5 == tokens_imagen_estimados(p, None)

    def test_n_none_estima_el_documento_completo(self, tmp_path):
        p = self._pdf(tmp_path, "multi.pdf", 612, 792, paginas=10)
        assert tokens_imagen_estimados(p, None) == 10 * int(612 * 792 / 750)

    def test_n_mayor_que_el_documento_no_revienta(self, tmp_path):
        p = self._pdf(tmp_path, "corto.pdf", 612, 792, paginas=3)
        assert tokens_imagen_estimados(p, 999) == tokens_imagen_estimados(p, None)

    def test_pdf_corrupto_devuelve_cero_sin_excepcion(self, tmp_path):
        """Estimar es una ayuda, no una guardia: nunca debe tumbar el batch."""
        bad = tmp_path / "malo.pdf"
        bad.write_bytes(b"no soy un pdf")
        assert tokens_imagen_estimados(str(bad), 3) == 0
