"""Borradores sin red ni conexiones reales."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.aprendizaje import borrador
from src.aprendizaje.__main__ import main


@pytest.mark.parametrize('titulo,razon,etiqueta,aviso', [
    ('Renovación COGNOS', '', 'ajena', ''),
    ('Renovación licenciamiento de productos Oracle', '', 'positivo', ''),
    ('Gestión de nóminas', 'Solución empresarial recomendada', 'negativo', ''),
    ('Migración desde Oracle a otra plataforma', '', 'positivo', 'migración fuera'),
    ('Aplicación empresarial', 'Oracle es líder en software empresarial', 'positivo', 'solo en la razón'),
    ('Servicio MySQL', '', 'positivo', ''),
    ('Servicio NetSuite', '', 'positivo', ''),
    ('Servicio oraclex', '', 'negativo', ''),
    ('Power BI', 'Oracle', 'ajena', ''),
    ('MIGRACIÓN DE ORACLE A MySQL', '', 'positivo', 'migración fuera'),
])
def test_reglas(titulo, razon, etiqueta, aviso):
    p = borrador.proponer(titulo, razon)
    assert p['etiqueta'] == etiqueta
    assert aviso in p['avisos']
    assert p['motivo']
    if etiqueta != 'negativo':
        assert p['cita'] in (titulo, razon)
    if 'COGNOS' in titulo:
        assert 'ibm' in p['motivo']


def test_diccionario_activo():
    assert borrador.proponer('Producto Ejemplo', diccionario=[
        dict(marca_id='oracle', termino='Ejemplo', tipo='fuerte', activo=1),
    ])['etiqueta'] == 'positivo'
    assert borrador.proponer('Oracle', diccionario=[])['etiqueta'] == 'negativo'


@contextmanager
def conexion():
    yield object()


def preparar(monkeypatch):
    monkeypatch.setattr(borrador.repo, 'listar_terminos_activos', lambda *a: borrador.terminos())
    monkeypatch.setattr(borrador, 'leer_procesos', lambda *a: [
        dict(proceso_gid='proceso-sintetico', titulo='Soporte Oracle', razon='')])


def test_dry_run(monkeypatch, capsys):
    preparar(monkeypatch)
    def prohibido(*a, **kw):
        pytest.fail('dry-run escribió')
    monkeypatch.setattr(borrador.repo, 'agregar_ejemplo', prohibido)
    assert main(['borrador-oracle'], conectar=conexion) == 0
    out = capsys.readouterr().out
    assert 'BORRADOR' in out and 'positivo=1' in out and 'dry-run' in out


def test_aplicar_idempotente(monkeypatch, capsys):
    preparar(monkeypatch)
    guardados = set()
    def agregar(conn, cid, marca, etiqueta, fuente, titulo, **kw):
        assert (marca, fuente, titulo) == ('oracle', 'semilla', 'Soporte Oracle')
        assert kw['peso'] == 0.3 and kw['split'] == 'entrenamiento'
        assert kw['creado_por'] == 'borrador:oracle'
        assert kw['evidencia'] == titulo
        clave = (cid, kw['proceso_gid'], marca, fuente, etiqueta)
        nuevo = clave not in guardados
        guardados.add(clave)
        return dict(nuevo=nuevo)
    monkeypatch.setattr(borrador.repo, 'agregar_ejemplo', agregar)
    for _ in range(2):
        assert main(['borrador-oracle', '--aplicar'], conectar=conexion) == 0
    assert len(guardados) == 1
    assert '0 insertados' in capsys.readouterr().out


def test_adb_caida(capsys):
    @contextmanager
    def caida():
        raise ConnectionError('DSN inválido sintético')
        yield
    assert main(['borrador-oracle'], conectar=caida) == 1
    err = capsys.readouterr().err
    assert 'ERROR' in err and 'Traceback' not in err and 'DSN' not in err


def test_select_clob_y_binds():
    class Cursor:
        def execute(self, sql, binds):
            assert sql.lstrip().startswith('SELECT')
            assert 'JSON_TABLE' in sql and 'RETURNING CLOB' in sql
            assert 'FETCH FIRST 200' in sql
            assert binds['cliente_id'] == 'cliente-demo'
        def fetchall(self):
            return [('gid', 'Oracle', 'Institución sintética',
                     '[{"marca":"Oracle", "razon":"Soporte", "role":"Principal", "score":80}]',
                     1, 1, 1, 1, 'CLASIFICADO')]
        def close(self):
            self.cerrado = True
    cursor = Cursor()
    conn = SimpleNamespace(cursor=lambda: cursor)
    filas = borrador.leer_procesos(conn, 'cliente-demo', borrador.terminos())
    assert filas[0]['razon'] == 'Soporte'
    assert filas[0]['marcas'][0]['role'] == 'Principal'
    assert cursor.cerrado


def test_consola_omite_contactos():
    assert '@' not in borrador._publico('Soporte persona@example.com teléfono 5555-1234')
    assert '5555' not in borrador._publico('teléfono 5555-1234')
