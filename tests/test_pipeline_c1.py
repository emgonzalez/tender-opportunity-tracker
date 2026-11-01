"""C1 observa sin alterar la salida y respeta el presupuesto."""
from types import SimpleNamespace

import pytest

from src.drilldown import pipeline
from src.memoria import service


@pytest.mark.parametrize("modo,permite,llamadas", [
    ("off", True, 1), ("activo", True, 1), ("sombra", True, 2), ("sombra", False, 1)])
def test_triaje_c1(monkeypatch, modo, permite, llamadas):
    cfg = SimpleNamespace()
    vistos, cobrados, decisiones = [], [], []

    def triaje(*args, **kw):
        contexto = kw.get("contexto_proceso")
        vistos.append(contexto)
        return ("NO-GO" if contexto else "GO", .99, "prueba", contexto)

    monkeypatch.setattr(pipeline.triage, "triage", triaje)
    monkeypatch.setattr(pipeline.quota, "cabe", lambda *args: permite)
    monkeypatch.setattr(pipeline.quota, "costo_triaje_estimado_usd", lambda *a, **k: 1)
    monkeypatch.setattr(pipeline.quota, "cobrar", lambda cfg, uso: cobrados.append(uso))
    from src.aprendizaje import service as aprendizaje
    monkeypatch.setattr(aprendizaje, "guardar_decision", lambda *a, **k: decisiones.append(k))
    resultado = pipeline._triaje_c1(cfg, "cliente", "texto", "pliego", False, {},
                                   "contexto", modo, "cliente", "P1")
    assert len(vistos) == llamadas
    assert resultado[0] == ("NO-GO" if modo == "activo" else "GO")
    assert len(cobrados) == (1 if llamadas == 2 else 0)
    assert len(decisiones) == len(cobrados)


def test_sombra_llm_caido_conserva_base(monkeypatch):
    def triaje(*args, **kw):
        if kw.get("contexto_proceso"):
            raise RuntimeError("LLM no disponible")
        return "GO", .99, "base", None
    monkeypatch.setattr(pipeline.triage, "triage", triaje)
    monkeypatch.setattr(pipeline.quota, "cabe", lambda *args: True)
    monkeypatch.setattr(pipeline.quota, "costo_triaje_estimado_usd", lambda *a, **k: 1)
    assert pipeline._triaje_c1(None, "", "", "", False, {}, "ctx", "sombra", "c", "p")[0] == "GO"


def test_contexto_adb_caida_sin_red(monkeypatch):
    cfg = SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, adb_dsn="dsn-invalido")
    from src.memoria import db
    monkeypatch.setattr(db, "connection", lambda *a: (_ for _ in ()).throw(RuntimeError("caída")))
    assert service.precedentes_compactos(cfg, "cliente", SimpleNamespace()) == []
    assert service.casos_similares(cfg, "cliente", "servidor", ["oracle"]) == []


@pytest.mark.parametrize("modo,disponible", [
    ("off", False), ("activo", False), ("activo", True), ("sombra", True)])
def test_contexto_fallido_y_verificador_caido_no_rompen_pipeline(tmp_path, monkeypatch, modo, disponible):
    import fitz

    from src.scraper.models import ResultadoAnalisis

    cfg = SimpleNamespace(
        data_dir=str(tmp_path), pliego_max_pages=100, triage_pages=1,
        intel_c1=modo, intel_c3=modo, enable_memoria=False)
    ruta = str(tmp_path / "pliego.pdf")
    with fitz.open() as documento:
        pagina = documento.new_page()
        pagina.insert_text((72, 72), "Servidor institucional con soporte Oracle Database")
        documento.save(ruta)
    proceso = SimpleNamespace(numero_referencia="P1", descripcion="Servidor", monto=None)
    vistos, analisis, vecinos = [], [], []
    monkeypatch.setattr(service, "estado_de", lambda *a: None)
    monkeypatch.setattr(service, "registrar_proceso", lambda *a: None)
    monkeypatch.setattr(service, "guardar_estado", lambda *a: None)
    monkeypatch.setattr(service, "precedentes_compactos", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    if disponible:
        monkeypatch.setattr(service, "precedentes_compactos", lambda *a: [])
        monkeypatch.setattr(service, "casos_similares", lambda *a: vecinos.append(a[3]) or [])
    monkeypatch.setattr(pipeline.quota, "cabe", lambda *a: True)
    monkeypatch.setattr(pipeline.quota, "costo_triaje_estimado_usd", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline.quota, "costo_analisis_estimado_usd", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline.quota, "cobrar", lambda *a: None)
    monkeypatch.setattr(pipeline.triage, "triage", lambda *a, **k: (
        vistos.append(k) or ("GO", .99, "correcto", None)))
    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", lambda *a, **k: (
        analisis.append(k) or ResultadoAnalisis(proceso_uuid="P1", estado="ANALIZADO")))
    monkeypatch.setattr(pipeline, "_verificar_c3", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    if modo == "off":
        monkeypatch.setattr(pipeline, "_paginas_verificables", lambda *a: pytest.fail("extracción con off"))
        monkeypatch.setattr(pipeline.contexto, "armar_contexto", lambda *a: pytest.fail("contexto con off"))
    resultado = pipeline.analizar_pliego_proceso(
        proceso, ruta, cfg, "", "cliente", ["servidor"], [],
        marcas_cliente=("Marca de ejemplo",), marca_ids_cliente=("marca_ejemplo",))
    assert resultado.estado == "ANALIZADO"
    assert vecinos == ([("marca_ejemplo",)] if disponible and modo != "off" else [])
    assert ("contexto_proceso" in analisis[0]) == (modo == "activo" and disponible)
    if modo == "sombra" and disponible:
        assert len(vistos) == 2
        assert sum("contexto_proceso" in v for v in vistos) == 1
    else:
        assert ("contexto_proceso" in vistos[0]) == (modo == "activo" and disponible)
