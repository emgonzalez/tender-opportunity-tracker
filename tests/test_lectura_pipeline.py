"""C2 integra páginas posteriores a la décima sin alterar off ni sombra."""
import json
from types import SimpleNamespace

import fitz
import pytest

from src import llm
from src.aprendizaje import service as aprendizaje
from src.drilldown import pipeline
from src.llm import proveedor_oci
from src.llm.contrato import BloqueDocumento, BloqueImagen, BloqueTexto, Pedido, Respuesta, Uso
from src.memoria import service as memoria
from src.scraper.models import Proceso, ResultadoAnalisis, UsoModelo


@pytest.fixture
def escaneado_30(tmp_path):
    ruta = tmp_path / "pliego_sintetico.pdf"
    destino = fitz.open()
    try:
        for n in range(1, 31):
            original = fitz.open()
            try:
                hoja = original.new_page(width=300, height=400)
                texto = ("carta de autorización del fabricante" if n == 23 else "Objeto sintético")
                hoja.insert_textbox(fitz.Rect(20, 30, 280, 180), f"Página {n}\n{texto}", fontsize=12)
                imagen = hoja.get_pixmap().tobytes("png")
                pagina = destino.new_page(width=300, height=400)
                pagina.insert_image(pagina.rect, stream=imagen)
            finally:
                original.close()
        destino.save(ruta)
    finally:
        destino.close()
    return str(ruta)


@pytest.mark.parametrize("modo,falla", [("activo", False), ("activo", True),
                                       ("off", False), ("sombra", False)])
def test_pipeline_dirige_analisis_a_pagina_23_y_degrada(tmp_path, monkeypatch, escaneado_30,
                                                       modo, falla):
    cfg = SimpleNamespace(data_dir=str(tmp_path), budget_path=str(tmp_path / "gasto.json"),
                          pliego_max_pages=100, triage_pages=5, triage_raster_pages=5,
                          daily_budget_usd=5, analisis_tokens_por_pagina=3000,
                          model_triage="claude-haiku-4-5", model_analysis="claude-sonnet-5",
                          enable_memoria=modo == "activo" and not falla, intel_c2=modo)
    proceso = Proceso(institucion="Institución sintética", descripcion="Licencias sintéticas",
                      fecha_publicacion=None, url_directa="https://example.com/proceso",
                      numero_referencia="REF-SINTETICA")
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k: ("GO", .99, "ok", UsoModelo()))
    transcritas = [{"pagina": 23, "texto": "carta de autorización del fabricante",
                   "fuente": "vision_llm", "modelo": "fake", "calidad": None}]
    orden_transcripcion = []

    def transcribir(ruta, paginas, cfg):
        orden_transcripcion.extend(paginas)
        return transcritas

    monkeypatch.setattr(pipeline.lectura, "transcribir_paginas", transcribir)
    textos_memoria = []
    guardadas = []
    monkeypatch.setattr(memoria, "persistir_memoria",
                        lambda cfg, proceso, res, texto, cliente: textos_memoria.append(texto))
    monkeypatch.setattr(memoria, "obtener_precedentes", lambda *a: [])
    monkeypatch.setattr(pipeline, "_guardar_lectura",
                        lambda cfg, cliente, referencia, ruta, paginas: guardadas.extend(paginas))
    decisiones = []
    monkeypatch.setattr(aprendizaje, "guardar_decision", lambda *a, **k: decisiones.append(k))
    llamadas = []

    def completar(cfg, pedido):
        llamadas.append(pedido)
        assert pedido.nombre_esquema == "selector_paginas_c2"
        assert sum(isinstance(b, BloqueImagen) for b in pedido.contenido) <= 10
        if falla:
            raise RuntimeError("LLM sintético no disponible")
        tiene_23 = any(isinstance(b, BloqueTexto) and b.texto == "Página física 23 del PDF"
                       for b in pedido.contenido)
        salida = {"paginas": [{"n": 23, "tipo": "carta"}] if tiene_23 else []}
        return Respuesta(json.dumps(salida), Uso(1, 1), "claude-haiku-4-5", "fake")

    monkeypatch.setattr(llm, "completar", completar)
    pedidos_analisis = []

    def analizar(ruta, cfg, contexto, referencia, formato=""):
        pedido = Pedido(etapa="analisis", contenido=[BloqueDocumento(ruta, 30, True)])
        pedidos_analisis.append(pedido)
        return ResultadoAnalisis(proceso_uuid=referencia, estado="ANALIZADO", formato=formato)

    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", analizar)
    resultado = pipeline.analizar_pliego_proceso(proceso, escaneado_30, cfg, "", "sintetico", [], [])
    assert resultado.estado == "ANALIZADO"
    contenido = pedidos_analisis[0].contenido
    if modo == "activo" and not falla:
        assert [b.texto for b in contenido if isinstance(b, BloqueTexto)] == [
            "Página original 1", "Página original 23"]
        assert sum(isinstance(b, BloqueImagen) for b in contenido) == 2
        assert orden_transcripcion[:2] == [1, 23]
        assert sorted(orden_transcripcion) == list(range(1, 31))
        assert guardadas == transcritas
        paginas_memoria = textos_memoria[0].split("\f")
        assert len(paginas_memoria) == 30
        assert paginas_memoria[22] == transcritas[0]["texto"]
        assert all(not texto for n, texto in enumerate(paginas_memoria) if n != 22)
    else:
        assert len(contenido) == 1 and isinstance(contenido[0], BloqueDocumento)
        rasterizados = []

        def rasterizar(ruta, n):
            rasterizados.append(n)
            return ["aW1hZ2Vu"] * n

        monkeypatch.setattr(proveedor_oci, "_pdf",
                            lambda: SimpleNamespace(rasterizar_paginas=rasterizar))
        imagenes = proveedor_oci._documento_como_imagenes(cfg, contenido[0])
        assert rasterizados == [10]
        assert len(imagenes) == 10
        assert not guardadas and not textos_memoria
    assert len(llamadas) == (0 if modo == "off" else 1 if falla else 3)
    if modo == "sombra":
        assert decisiones[0]["variante"] == "sombra"
        assert decisiones[0]["salida"] == {"paginas": [1, 23]}
