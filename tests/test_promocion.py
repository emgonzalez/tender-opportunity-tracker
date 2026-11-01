"""Puerta real, sin base ni alertas externas."""
from src.aprendizaje import promocion


def preparar(monkeypatch, *, critico=True, buena=True):
    metrica = dict(n=100, faltantes=0, precision=1, recall=1, f1=1,
                   precision_confirmada=1, ece=0, n_calibracion=100, wilson_confirmada=(.96, 1))
    candidata = {'marca_id': 'oracle', 'metricas': {'oracle/prueba': metrica},
                 'parametros': {'predicciones_criticas': {'7': 'positivo' if critico else 'negativo'}}}
    if not buena:
        candidata['metricas'] = {}
    monkeypatch.setattr(promocion.repo, '_todos', lambda *a: [(2,)])
    monkeypatch.setattr(promocion.repo, 'obtener_calibracion', lambda *a: candidata)
    monkeypatch.setattr(promocion.repo, 'calibracion_activa',
                        lambda *a: {'metricas': {'oracle/prueba': metrica}})
    monkeypatch.setattr(promocion.repo, 'listar_ejemplos',
                        lambda *a, **kw: [{'ejemplo_id': 7, 'etiqueta': 'positivo'}])
    cambios = []
    monkeypatch.setattr(promocion.repo, 'promover', lambda *a, **kw: cambios.append(a[1]) or 1)
    monkeypatch.setattr(promocion.alerts, 'notify', lambda *a, **kw: cambios.append('alerta'))
    return cambios


def test_promueve(monkeypatch):
    cambios = preparar(monkeypatch)
    r = promocion.ejecutar(None, 'demo', aplicar=True)
    assert cambios == [2]
    assert r['calibraciones'][0]['archivada'] == 1


def test_dry_run(monkeypatch):
    cambios = preparar(monkeypatch)
    promocion.ejecutar(None, 'demo')
    assert cambios == []


def test_critico_bloquea_alerta(monkeypatch):
    cambios = preparar(monkeypatch, critico=False)
    r = promocion.ejecutar(None, 'demo', aplicar=True, cfg=object())
    assert cambios == ['alerta']
    assert not r['calibraciones'][0]['puede_promover']


def test_metricas_ausentes_bloquean(monkeypatch):
    cambios = preparar(monkeypatch, buena=False)
    promocion.ejecutar(None, 'demo', aplicar=True)
    assert cambios == []
