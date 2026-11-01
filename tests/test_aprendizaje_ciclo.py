"""Ciclo aislado: transacción, presupuesto y CLI sin red."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.aprendizaje import ciclo
from src.aprendizaje.__main__ import main


@pytest.fixture
def entorno(monkeypatch):
    conn = SimpleNamespace(call_timeout=10, rollback=lambda: eventos.append('rollback'))
    eventos = []

    @contextmanager
    def conectar():
        try:
            yield conn
        except Exception:
            eventos.append('rollback')
            raise
        else:
            eventos.append('commit')

    monkeypatch.setattr(ciclo.repo, '_uno', lambda *a: ('demo',))
    for modulo in (ciclo.cosecha, ciclo.minado, ciclo.promocion, ciclo.deriva):
        monkeypatch.setattr(modulo, 'ejecutar', lambda *a, **kw: {'aplicar': kw['aplicar']})
    return conectar, eventos, conn


def test_dry_run_no_entrena(entorno):
    conectar, eventos, conn = entorno
    r = ciclo.ejecutar(conectar, 'demo', entrenador=lambda *a, **kw: pytest.fail('entrenó'))
    assert r['estado'] == 'completado'
    assert 'rollback' in eventos
    assert conn.call_timeout == 10
    assert all(not r['pasos'][p]['aplicar'] for p in ('cosecha', 'minado', 'promocion', 'deriva'))


def test_aplicar_y_entrenador(entorno):
    conectar, eventos, _ = entorno
    r = ciclo.ejecutar(conectar, 'demo', aplicar=True, entrenador=lambda *a, **kw: 'candidata')
    assert r['pasos']['entrenamiento'] == 'candidata'
    assert eventos == ['commit']


def test_rollback_por_fallo(entorno, monkeypatch):
    conectar, eventos, _ = entorno

    def fallo(*a, **kw):
        raise RuntimeError('dato sensible que no debe imprimirse')

    monkeypatch.setattr(ciclo.minado, 'ejecutar', fallo)
    r = ciclo.ejecutar(conectar, 'demo', aplicar=True)
    assert eventos == ['rollback']
    assert r['estado'] == 'no ejecutado o revertido'
    assert 'sensible' not in ciclo.texto(r)


def test_tope(entorno):
    conectar, eventos, _ = entorno
    tiempos = iter([0, 0, 301])
    r = ciclo.ejecutar(conectar, 'demo', reloj=lambda: next(tiempos))
    assert r['motivo'] == 'TimeoutError'
    assert eventos == ['rollback']


def test_adb_caida(capsys):
    @contextmanager
    def conectar():
        raise OSError('DSN inválido')
        yield

    assert main(['ciclo'], conectar=conectar) == 0
    assert 'ADB no disponible' in capsys.readouterr().out
    assert main(['ciclo', '--segundos', '-1'], conectar=conectar) == 1


@pytest.mark.parametrize('argumentos,aplicar', [(['ciclo'], False), (['ciclo', '--aplicar'], True)])
def test_cli_modo(entorno, capsys, argumentos, aplicar):
    conectar, _, _ = entorno
    assert main(argumentos, conectar=conectar) == 0
    assert ('(aplicar)' if aplicar else '(dry-run)') in capsys.readouterr().out


def test_fallo_despues_de_promover_revierte(entorno, monkeypatch):
    conectar, eventos, _ = entorno

    def fallo(*a, **kw):
        raise RuntimeError('reporte fallido')

    monkeypatch.setattr(ciclo.deriva, 'ejecutar', fallo)
    r = ciclo.ejecutar(conectar, 'demo', aplicar=True)
    assert 'promocion' in r['pasos']
    assert eventos == ['rollback']


def test_presupuesto_interrumpe_entrenador(entorno):
    import time

    conectar, eventos, _ = entorno
    r = ciclo.ejecutar(conectar, 'demo', aplicar=True, segundos=.02,
                      entrenador=lambda *a, **kw: time.sleep(.1))
    assert r['motivo'] == 'TimeoutError'
    assert eventos == ['rollback']
