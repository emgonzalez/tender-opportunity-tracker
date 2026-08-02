"""Tests de utilidades PDF del drill-down (Fase 2, MVP Track A). Sin red."""
import fitz  # pymupdf
import pytest

from src.drilldown.pdf import (
    page_count, extract_text_first_pages, is_scanned, read_pdf_b64, PdfCorruptoError,
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
