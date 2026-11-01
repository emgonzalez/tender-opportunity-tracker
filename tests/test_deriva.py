from src.aprendizaje import deriva


def test_distribucion_normaliza_volumen():
    assert deriva.distribucion(["Oracle java"], ["Oracle java"] * 8)["distancia"] == 0
    assert deriva.distribucion(["Oracle java"], ["Cognos licencias"])["distancia"] == 1


def test_reporte_desviaciones_y_ausencia_no_es_cero():
    vacio = deriva.resumir({}, [], [], {}, [])
    assert vacio["precision_estimada_votos"] is None
    assert vacio["cobertura_vecinos"] is None
    resultado = deriva.resumir({"IRRELEVANTE": 20}, [[{"veredicto": "por_confirmar"}] * 20],
                              [{"vecinos": []}] * 20, {"oracle": 2},
                              [("java oracle", True)] * 20 + [("cognos ibm", False)] * 20)
    assert len(resultado["desviaciones"]) == 4
    assert resultado["marcas_pocos_ejemplos"] == {"oracle": 2}


def test_adb_caida_no_alerta(monkeypatch):
    def caida(*args):
        raise RuntimeError("DSN inválido")
    monkeypatch.setattr(deriva.repo, "_todos", caida)
    assert deriva.ejecutar(None, "demo", aplicar=True, cfg=object())["ejecutado"] is False


def test_alerta_solo_desviacion_y_aplicar(monkeypatch):
    avisos = []
    def filas(conn, sql, binds):
        return [("IRRELEVANTE", 20)] if "FROM feedback" in sql else []
    monkeypatch.setattr(deriva.repo, "_todos", filas)
    monkeypatch.setattr(deriva.alerts, "notify", lambda *a, **kw: avisos.append(kw) or ["falso"])
    assert deriva.ejecutar(None, "demo", cfg=object())["alertas"] == []
    assert not avisos
    assert deriva.ejecutar(None, "demo", aplicar=True, cfg=object())["alertas"] == ["falso"]
    assert len(avisos) == 1


def test_sin_desviacion_no_notifica_aunque_aplicar(monkeypatch):
    monkeypatch.setattr(deriva.repo, "_todos", lambda *a: [])
    def prohibida(*args, **kwargs):
        raise AssertionError("no debe notificar sin desviación")
    monkeypatch.setattr(deriva.alerts, "notify", prohibida)
    resultado = deriva.ejecutar(None, "demo", aplicar=True, cfg=object())
    assert resultado["ejecutado"] is True
    assert resultado["desviaciones"] == []
    assert resultado["alertas"] == []
