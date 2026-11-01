"""La selección de C2 no se filtra a otros pedidos ni impide el fallback."""
from src.drilldown import pdf
from src.llm.contrato import BloqueDocumento, BloqueImagen, Pedido, paginas_documento


def test_seleccion_acotada_al_contexto(monkeypatch):
    monkeypatch.setattr(pdf, "rasterizar_paginas", lambda *a, **kw: ["imagen"])
    documento = BloqueDocumento("sintetico.pdf", paginas=30, escaneado=True)
    with paginas_documento(documento.ruta, [1, 23]):
        pedido = Pedido("analisis", [documento])
        assert sum(isinstance(b, BloqueImagen) for b in pedido.contenido) == 2
        assert pedido.contenido[2].texto == "Página original 23"
        otro = BloqueDocumento("otro.pdf")
        assert Pedido("analisis", [otro]).contenido == [otro]
    assert Pedido("analisis", [documento]).contenido == [documento]


def test_raster_fallido_conserva_documento(monkeypatch):
    monkeypatch.setattr(pdf, "rasterizar_paginas", lambda *a, **kw: [])
    documento = BloqueDocumento("sintetico.pdf", paginas=30, escaneado=True)
    with paginas_documento(documento.ruta, [1, 23]):
        assert Pedido("analisis", [documento]).contenido == [documento]
