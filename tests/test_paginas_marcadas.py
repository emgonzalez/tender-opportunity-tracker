"""Páginas físicas para C0, manteniendo el contenido legado con off."""
from types import SimpleNamespace

import fitz
import pytest

from src.drilldown import pdf
from src.llm import proveedor_oci
from src.llm.contrato import BloqueDocumento, Pedido, paginas_documento


@pytest.fixture
def documento(tmp_path):
    ruta = str(tmp_path / "sintetico.pdf")
    doc = fitz.open()
    for n in range(1, 5):
        pagina = doc.new_page()
        pagina.insert_text((72, 72), f"Texto sintetico de pagina {n}")
    doc.save(ruta)
    doc.close()
    return ruta


def test_extract_text_paginado(documento):
    texto = pdf.extract_text_paginado(documento, 10)
    assert [linea for linea in texto.splitlines() if linea.startswith("[[")] == [
        f"[[Página {n}]]" for n in range(1, 5)]
    assert "[[Página 3]]\nTexto sintetico de pagina 3" in texto
    assert pdf.extract_text_paginado(documento, 0) == ""


def test_off_texto_identico(documento):
    legado = pdf.extract_text_first_pages(documento, 3)
    bloque = BloqueDocumento(documento, paginas=3)
    salida = proveedor_oci._documento_como_texto(SimpleNamespace(intel_c0="off"), bloque)
    assert salida == [{"type": "TEXT", "text": legado}]
    assert salida[0]["text"].encode() == legado.encode()


def test_off_imagenes_identicas(monkeypatch):
    llamadas = []

    def raster(ruta, n):
        llamadas.append((ruta, n))
        return ["uno", "dos"]

    monkeypatch.setattr(pdf, "rasterizar_paginas", raster)
    bloque = BloqueDocumento("sintetico.pdf", paginas=2, escaneado=True)
    cfg = SimpleNamespace(intel_c0="off", oci_analisis_raster_pages=10)
    assert proveedor_oci._documento_como_imagenes(cfg, bloque) == [
        {"type": "IMAGE", "image_url": {"url": "data:image/jpeg;base64,uno"}},
        {"type": "IMAGE", "image_url": {"url": "data:image/jpeg;base64,dos"}},
    ]
    assert llamadas == [("sintetico.pdf", 2)]


@pytest.mark.parametrize("modo", ["sombra", "activo"])
def test_texto_activo_y_seleccion_fisica(documento, modo):
    cfg = SimpleNamespace(intel_c0=modo)
    bloque = BloqueDocumento(documento, paginas=4)
    with paginas_documento(documento, [4, 2]):
        contenido = proveedor_oci._documento_como_texto(cfg, bloque)[0]["text"]
    assert contenido.startswith("[[Página 4]]")
    assert "[[Página 2]]" in contenido
    assert "[[Página 1]]" not in contenido


def test_imagenes_con_fisicos_no_contiguos(monkeypatch):
    llamadas = []

    def raster(ruta, *, indices):
        llamadas.append(indices)
        return [str(indices[0])]

    monkeypatch.setattr(pdf, "rasterizar_paginas", raster)
    cfg = SimpleNamespace(intel_c0="activo", oci_analisis_raster_pages=10)
    bloque = BloqueDocumento("sintetico.pdf", paginas=8, escaneado=True)
    with paginas_documento(bloque.ruta, [7, 2]):
        salida = proveedor_oci._documento_como_imagenes(cfg, bloque)
    assert llamadas == [[6], [1]]
    assert [b["text"] for b in salida if b["type"] == "TEXT"] == [
        "Página física 7 del PDF", "Página física 2 del PDF"]
    assert salida[1]["image_url"]["url"].endswith(",6")


def test_fallo_raster_no_desplaza_rotulos(monkeypatch):
    monkeypatch.setattr(pdf, "page_count", lambda _: 3)
    monkeypatch.setattr(pdf, "rasterizar_paginas",
                        lambda ruta, *, indices: [] if indices == [1] else [str(indices[0])])
    cfg = SimpleNamespace(intel_c0="activo", oci_analisis_raster_pages=10)
    salida = proveedor_oci._documento_como_imagenes(
        cfg, BloqueDocumento("sintetico.pdf", paginas=3, escaneado=True))
    assert [b["text"] for b in salida if b["type"] == "TEXT"] == [
        "Página física 1 del PDF", "Página física 3 del PDF"]


def test_pedido_seleccion_conserva_fisicos(monkeypatch):
    monkeypatch.setattr(pdf, "rasterizar_paginas", lambda ruta, *, indices: [str(indices[0])])
    with paginas_documento("sintetico.pdf", [8, 3]):
        pedido = Pedido("analisis", [BloqueDocumento("sintetico.pdf", paginas=10)])
    contenido = proveedor_oci.traducir_contenido(SimpleNamespace(intel_c0="activo"), pedido.contenido)
    assert [b["text"] for b in contenido if b["type"] == "TEXT"] == [
        "Página original 8", "Página original 3"]
