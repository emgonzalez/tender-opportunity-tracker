"""Evaluación C8 con muestras sintéticas; sin servicios externos."""
import math
from types import SimpleNamespace

import pytest

from scripts import canario_llm, evaluar_marca
from src.aprendizaje import evaluacion as ev


def test_wilson_conocido():
    assert ev.wilson(50, 100) == pytest.approx((0.40383153, 0.59616847))
    assert ev.wilson(0, 0) == (0, 1)
    assert ev.wilson(0, 10)[0] == pytest.approx(0)
    with pytest.raises(ValueError):
        ev.wilson(11, 10)


def test_matriz_kappa_ece():
    matriz = ev.matriz_confusion([True, True, False, False], [True, False, True, False])
    assert matriz == dict(tp=1, fn=1, fp=1, tn=1)
    assert ev.precision_recall_f1(matriz)["f1"] == 0.5
    assert ev.kappa([1, 1, 0, 0], [1, 0, 1, 0]) == 0
    assert ev.kappa([1, 0], [1, 0]) == 1
    assert ev.kappa([], []) is None
    assert ev.ece([0, 1], [False, True]) == 0
    assert ev.ece([0.5, 0.5], [True, False]) == 0
    assert ev.ece([], []) is None
    with pytest.raises(ValueError):
        ev.ece([math.nan], [True])


def test_mcnemar_ips():
    assert ev.mcnemar([True] * 6, [False] * 6)["p"] == pytest.approx(0.03125)
    assert ev.mcnemar([True], [True])["p"] == 1
    m = ev.ips([True, False], [True, True], [0.5, 1])
    assert m["precision"] == pytest.approx(2 / 3)
    assert "wilson" not in m
    for p in (0, -1, 2, math.nan):
        with pytest.raises(ValueError):
            ev.ips([True], [True], [p])


def muestras(n=100):
    return [dict(marca="Oracle", estrato="prueba", real=True, veredicto="confirmada", probabilidad=1.0)
            for _ in range(n)]


def test_puerta_umbral_criticos_y_regresion():
    activa = ev.evaluar(muestras())
    assert ev.puede_promover(activa, activa, [True])["puede_promover"]
    assert not ev.puede_promover(activa, activa, [False])["puede_promover"]
    assert not ev.puede_promover(activa, activa, [])["puede_promover"]
    assert not ev.puede_promover(ev.evaluar(muestras(5)), ev.evaluar(muestras(5)), [True])["puede_promover"]
    filas = muestras()
    filas[0]["veredicto"] = "por_confirmar"
    filas[0]["probabilidad"] = 0.9
    assert not ev.puede_promover(ev.evaluar(filas), activa, [True])["puede_promover"]
    filas[0]["veredicto"] = None
    assert not ev.puede_promover(ev.evaluar(filas), activa, [True])["puede_promover"]
    filas = muestras()
    filas[0]["probabilidad"] = None
    assert not ev.puede_promover(ev.evaluar(filas), activa, [True])["puede_promover"]
    assert not ev.puede_promover({}, {}, [True])["puede_promover"]


def test_entrenamiento_no_promueve():
    filas = muestras()
    for f in filas:
        f["estrato"] = "entrenamiento"
    m = ev.evaluar(filas)
    assert not ev.puede_promover(m, m, [True])["puede_promover"]


def test_canario_sin_red_y_deriva(monkeypatch, capsys):
    def completar(cfg, pedido):
        assert pedido.temperatura == 0
        assert pedido.esquema == canario_llm.ESQUEMA
        return SimpleNamespace(texto='{"veredicto":"' + (
            "confirmada" if "productos Oracle" in pedido.contenido[0].texto else "fuera") + '"}')
    monkeypatch.setattr(canario_llm.llm, "completar", completar)
    assert canario_llm.main([]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert canario_llm.ejecutar(object())["ok"]
    monkeypatch.setattr(canario_llm.llm, "completar", lambda *a: SimpleNamespace(texto="GO"))
    assert not canario_llm.ejecutar(object())["ok"]
    monkeypatch.setattr(canario_llm, "configuracion", lambda: object())
    assert canario_llm.main(["--ejecutar"]) == 1


def test_cli_lectura_dry_run_y_error(monkeypatch, capsys):
    monkeypatch.setattr(evaluar_marca, "configuracion", lambda: (_ for _ in ()).throw(RuntimeError("sensible")))
    assert evaluar_marca.main(["--cliente", "sintetico", "--dry-run"]) == 0
    assert evaluar_marca.main(["--cliente", "sintetico"]) == 2
    assert "sensible" not in capsys.readouterr().err


class Cursor:
    def __init__(self, lotes):
        self.lotes = iter(lotes)

    def execute(self, sql, binds):
        assert sql.startswith("SELECT")
        self.filas = next(self.lotes)

    def fetchall(self):
        return self.filas

    def close(self):
        pass


class Conexion:
    def __init__(self, lotes):
        self.c = Cursor(lotes)

    def cursor(self):
        return self.c


def test_informe_lee_sin_escritura():
    marca = [{"marca_id": "oracle", "veredicto": "confirmada", "score": 1}]
    conn = Conexion([
        [(1, "sintetico:1", "oracle", "positivo", "prueba", "humana", "sintetico_a"),
         (2, "sintetico:2", "oracle", "positivo", "critico", "humana", "sintetico_a")],
        [("sintetico:1", "activa", marca, 1, 1), ("sintetico:1", "sombra", marca, 1, 1),
         ("sintetico:2", "sombra", marca, 1, 1)], []])
    r = evaluar_marca.informe(conn, "sintetico", "C8")
    assert r["metricas"]["sombra"]["oracle/prueba"]["recall"] == 1
    assert not r["promocion"]["puede_promover"]  # Wilson con n=1
    assert r["ips_sombra"]["precision"] == 1


def test_puerta_recall_precision_y_ece():
    activa = ev.evaluar(muestras())
    filas = muestras()
    for f in filas[:3]:
        f["veredicto"] = "fuera"
    assert not ev.puede_promover(ev.evaluar(filas), activa, [True])["puede_promover"]
    filas = muestras()
    for f in filas[:6]:
        f["real"] = False
    assert not ev.puede_promover(ev.evaluar(filas), activa, [True])["puede_promover"]
    filas = muestras()
    for f in filas:
        f["probabilidad"] = 0.9
    assert not ev.puede_promover(ev.evaluar(filas), activa, [True])["puede_promover"]
    corruptas = ev.evaluar(muestras())
    corruptas["oracle/prueba"]["precision"] = math.nan
    assert not ev.puede_promover(corruptas, activa, [True])["puede_promover"]


def test_deduplica_casos_y_kappa_humano():
    marca = [{"marca_id": "oracle", "veredicto": "confirmada", "score": 1}]
    conn = Conexion([
        [(1, "sintetico:1", "oracle", "positivo", "prueba", "humana", "sintetico_a"),
         (2, "sintetico:1", "oracle", "negativo", "prueba", "humana", "sintetico_b")],
        [("sintetico:1", "sombra", marca, 1, 1)], []])
    r = evaluar_marca.informe(conn, "sintetico", "C8")
    assert r["metricas"]["sombra"]["oracle/prueba"]["n"] == 1
    assert r["casos_con_etiquetas_contradictorias"] == 1
    assert not r["promocion"]["puede_promover"]
    assert r["kappa_etiquetadores"]["etiquetador_1/etiquetador_2"]["kappa"] == 0
