"""Lectura C2 con PDFs sintéticos y proveedor falso; sin servicios externos."""
import json
from types import SimpleNamespace

import fitz
import pytest

from src.drilldown import lectura, pdf
from src.llm.contrato import BloqueImagen, BloqueTexto, Respuesta, Uso


@pytest.fixture
def pliego(tmp_path):
    ruta = tmp_path / "pliego.pdf"
    with fitz.open() as doc:
        for n in range(1, 31):
            pagina = doc.new_page(width=100, height=130)
            pagina.insert_text((5, 20), "carta de autorización del fabricante" if n == 23 else f"Página {n}",
                                fontsize=4)
        doc.save(str(ruta))
    return str(ruta)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(lectura.quota, "cabe_lectura", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(lectura.quota, "cobrar_lectura", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(lectura.llm, "modelo_de", lambda *a: "modelo-falso")
    return SimpleNamespace(intel_c2="activo", llm_temperatura=0, lectura_tiempo_max_s=120)


def test_selector_ve_pagina_23_en_lotes_de_diez(pliego, cfg, monkeypatch):
    pedidos = []

    def completar(_, pedido):
        pedidos.append(pedido)
        numeros = [int(b.texto.split()[2]) for b in pedido.contenido if isinstance(b, BloqueTexto)]
        return Respuesta(json.dumps({"paginas": [{"n": 23, "tipo": "carta"}]
                                    if 23 in numeros else []}), Uso(1, 1), "modelo-falso", "falso")

    monkeypatch.setattr(lectura.llm, "completar", completar)
    assert lectura.seleccionar_paginas(pliego, 30, cfg, {}) == [1, 23]
    assert len(pedidos) == 3
    assert all(sum(isinstance(b, BloqueImagen) for b in p.contenido) <= 10 for p in pedidos)
    assert all(p.esquema == lectura.ESQUEMA_SELECTOR and p.temperatura == 0 for p in pedidos)


def test_selector_falla_devuelve_vacio(pliego, cfg, monkeypatch):
    def fallar(*args):
        raise ValueError("proveedor no disponible")
    monkeypatch.setattr(lectura.llm, "completar", fallar)
    assert lectura.seleccionar_paginas(pliego, 30, cfg, {}) == []


def test_selector_rechaza_paginas_alucinadas(pliego, cfg, monkeypatch):
    monkeypatch.setattr(lectura.llm, "completar", lambda *a: Respuesta(
        '{"paginas":[{"n":23,"tipo":"carta"}]}', Uso(1, 1), "modelo-falso", "falso"))
    assert lectura.seleccionar_paginas(pliego, 30, cfg, {}) == []


def test_transcripcion_prioriza_y_limita_lotes(pliego, cfg, monkeypatch):
    lotes = []

    def completar(_, pedido):
        numeros = [int(b.texto.split()[2]) for b in pedido.contenido if isinstance(b, BloqueTexto)]
        lotes.append(numeros)
        assert pedido.etapa == "analisis" and pedido.temperatura == 0
        return Respuesta(json.dumps({"paginas": [{"n": n, "texto": f"literal {n}"}
                                                  for n in numeros]}), Uso(1, 1), "modelo-falso", "falso")

    monkeypatch.setattr(lectura.llm, "completar", completar)
    salida = lectura.transcribir_paginas(pliego, [23, 1, 2, 3, 4], cfg)
    assert lotes == [[23, 1, 2], [3, 4]]
    assert [p["pagina"] for p in salida] == [23, 1, 2, 3, 4]
    assert all(p["fuente"] == "vision_llm" for p in salida)


def test_transcripcion_respeta_tope_60_y_tiempo(cfg, monkeypatch):
    monkeypatch.setattr(lectura.pdf, "page_count", lambda _: 100)
    monkeypatch.setattr(lectura, "_contenido", lambda *a: [])
    lotes = []

    def llamar(_, pedido, **kw):
        lotes.append(kw["imagenes"])
        # Cada lote debe contener 3; el contenido se simuló sin rásters.
        offset = 3 * (len(lotes) - 1)
        return [{"n": n, "texto": "literal"} for n in range(offset + 1, offset + 4)], "falso"

    monkeypatch.setattr(lectura, "_llamar", llamar)
    assert len(lectura.transcribir_paginas("falso.pdf", list(range(1, 101)), cfg)) == 60
    cfg.lectura_tiempo_max_s = 0
    assert lectura.transcribir_paginas("falso.pdf", [1, 2], cfg) == []


def test_raster_indices_ordenados_una_pagina_a_la_vez(pliego, monkeypatch):
    original = pdf.fitz.open
    cargadas = []

    class Documento:
        def __init__(self, ruta):
            self.doc = original(ruta)
            self.page_count = self.doc.page_count

        def load_page(self, n):
            cargadas.append(n)
            return self.doc.load_page(n)

        def close(self):
            self.doc.close()

    monkeypatch.setattr(pdf.fitz, "open", Documento)
    assert len(pdf.rasterizar_paginas(pliego, indices=[22, 0, 22], max_lado_px=100)) == 2
    assert cargadas == [22, 0]
    with pytest.raises(ValueError):
        pdf.rasterizar_paginas(pliego, indices=[30])


@pytest.mark.parametrize("fallo,esperadas", [(lectura._PresupuestoAgotado, 3), (ValueError, 0), (TimeoutError, 0)])
def test_transcripcion_conserva_lote_solo_si_agota_presupuesto(cfg, monkeypatch, fallo, esperadas):
    monkeypatch.setattr(lectura.pdf, "page_count", lambda _: 6)
    monkeypatch.setattr(lectura, "_contenido", lambda *a: [])
    llamadas = []

    def llamar(acotada, pedido, **kw):
        llamadas.append(acotada)
        if len(llamadas) > 1:
            raise fallo("detener")
        return [{"n": n, "texto": "literal"} for n in [1, 2, 3]], "falso"

    monkeypatch.setattr(lectura, "_llamar", llamar)
    salida = lectura.transcribir_paginas("falso.pdf", [1, 2, 3, 4, 5, 6], cfg)
    assert len(salida) == esperadas
    assert llamadas[0] is not cfg
    assert 0 < llamadas[0].llm_timeout_s <= 120
    assert not hasattr(cfg, "llm_timeout_s")


def test_timeout_disminuye_tras_rasterizar(cfg, monkeypatch):
    monkeypatch.setattr(lectura.pdf, "page_count", lambda _: 6)
    monkeypatch.setattr(lectura, "_contenido", lambda *a: [])
    reloj = iter([0, 1, 20, 21, 119.5])
    monkeypatch.setattr(lectura.time, "monotonic", lambda: next(reloj))
    timeouts = []

    def llamar(acotada, pedido, **kw):
        timeouts.append(acotada.llm_timeout_s)
        return [{"n": n, "texto": "literal"} for n in [1, 2, 3]], "falso"

    monkeypatch.setattr(lectura, "_llamar", llamar)
    salida = lectura.transcribir_paginas("falso.pdf", [1, 2, 3, 4, 5, 6], cfg)
    assert len(salida) == 3
    assert timeouts == [100]
