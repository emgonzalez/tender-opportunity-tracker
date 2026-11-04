"""Detección de texto escaso con PDFs sintéticos, sin red ni rasterización."""

import fitz
import pytest

from src.drilldown.pdf import PdfCorruptoError, is_scanned, paginas_sin_texto


def _pdf(tmp_path, caracteres):
    path = tmp_path / "sintetico.pdf"
    doc = fitz.open()
    try:
        for cantidad in caracteres:
            page = doc.new_page()
            if cantidad:
                page.insert_text((72, 72), "x" * cantidad)
        doc.save(str(path))
    finally:
        doc.close()
    return str(path)


@pytest.mark.parametrize(("caracteres", "esperado"), [
    ([80] * 10, False),
    ([0] * 3 + [80] * 7, True),  # Regla original, aun con solo 30 % vacío.
    ([80] * 3 + [0] * 7, True),
    ([80] * 8 + [0] * 2, False),
    ([80] * 7 + [0] * 3, False),  # El 30 % exacto no basta.
    ([80] * 6 + [0] * 4, True),
    ([80, 0, 80], False),  # Una sola vacía no basta.
    ([80, 0, 0], True),
    ([80], False),
    ([0], True),
    ([39], True),
    ([40], False),
    ([80, 0], False),
    ([0, 80], False),
    ([20, 20], False),  # Suma original, no umbral por página.
    ([0, 0], True),
    ([80] * 10 + [0] * 5, False),  # Fuera de la ventana.
])
def test_clasificacion(tmp_path, caracteres, esperado):
    assert is_scanned(_pdf(tmp_path, caracteres)) is esperado


@pytest.mark.parametrize(("ventana", "esperado"), [
    (10, (2, 4)), (3, (1, 3)), (20, (2, 4)), (0, (0, 0)), (-1, (0, 0)),
])
def test_paginas_sin_texto(tmp_path, ventana, esperado):
    path = _pdf(tmp_path, [40, 39, 80, 0])
    assert paginas_sin_texto(path, ventana) == esperado


def test_sample_pages_conserva_regla_original(tmp_path):
    path = _pdf(tmp_path, [0, 80, 80])
    assert is_scanned(path) is False
    assert is_scanned(path, sample_pages=1) is True


@pytest.mark.parametrize("detectar", [is_scanned, paginas_sin_texto])
def test_corrupto(tmp_path, detectar):
    path = tmp_path / "corrupto.pdf"
    path.write_bytes(b"documento invalido")
    with pytest.raises(PdfCorruptoError):
        detectar(str(path))


def test_debug_incluye_ambas_metricas(tmp_path, caplog):
    path = _pdf(tmp_path, [80] * 3 + [0] * 7)
    with caplog.at_level("DEBUG", logger="src.drilldown.pdf"):
        assert is_scanned(path) is True
    assert "total_chars=240" in caplog.text
    assert "paginas_sin_texto=7 paginas_leidas=10" in caplog.text
