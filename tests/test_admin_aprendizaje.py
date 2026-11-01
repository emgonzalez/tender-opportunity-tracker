"""Administración de aprendizaje con sesión, conexión y repositorios falsos."""

import pytest
from test_admin_clientes import (
    ADMIN_ORG,
    GERENTE,
    ORIGEN,
    SIMULADO,
    _no_existe,
    _post,
    _web,
)
from test_admin_clientes import mundo as mundo

from src.web import admin_aprendizaje as aprendizaje

BASE = aprendizaje.BASE


@pytest.fixture
def entorno(monkeypatch, mundo):
    monkeypatch.setattr(aprendizaje, '_marcas', lambda *a: ['oracle'])
    mundo.filas = []
    mundo.escrituras = []
    monkeypatch.setattr(aprendizaje.repo, '_todos', lambda *a: mundo.filas)
    monkeypatch.setattr(aprendizaje.repo, '_uno', lambda *a: ('Título público',))
    monkeypatch.setattr(aprendizaje.repo, 'agregar_ejemplo',
                        lambda *a, **k: mundo.escrituras.append((a, k)))
    monkeypatch.setattr(aprendizaje.repo, 'proponer_termino',
                        lambda *a, **k: mundo.escrituras.append((a, k)))
    return mundo


@pytest.mark.parametrize('seccion', ['labels', 'terms', 'calibration', 'explore'])
def test_vistas(monkeypatch, entorno, seccion):
    c = _web(monkeypatch, ADMIN_ORG)
    assert c.get(BASE).status_code == 200
    assert c.get(BASE + '/acme/' + seccion).status_code == 200
    assert c.get(BASE + '/ajeno/' + seccion).text == _no_existe()


def test_acceso(monkeypatch, entorno):
    assert _web(monkeypatch, GERENTE).get(BASE).text == _no_existe()
    assert _web(monkeypatch, ADMIN_ORG, con_sesion=False).get(BASE).status_code == 303


@pytest.mark.parametrize('headers,token', [({}, 'malo'), (ORIGEN, 'malo'),
                                          ({'origin': 'https://otro.example.com'}, None)])
def test_csrf_origin(monkeypatch, entorno, headers, token):
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + '/acme/labels', headers=headers, token=token)
    assert r.status_code == 403
    assert not entorno.escrituras


def test_simulado(monkeypatch, entorno):
    r = _post(_web(monkeypatch, SIMULADO), BASE + '/acme/labels')
    assert r.status_code == 303 and 'simulacion' in r.headers['location']
    assert not entorno.escrituras


def test_etiquetar(monkeypatch, entorno):
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + '/acme/labels', proceso_gid='demo:1',
              marca_id='oracle', etiqueta='positivo', split='critico', evidencia='Texto público')
    assert r.status_code == 303 and 'admin_ok' in r.headers['location']
    args, kw = entorno.escrituras[0]
    assert args[4:6] == ('humana', 'Título público')
    assert kw['split'] == 'critico' and kw['creado_por'] == 'admin@example.com'
    assert entorno.auditados[-1][0] == 'EJEMPLO_ETIQUETADO'


@pytest.mark.parametrize('cambios', [{'marca_id': 'ajena'}, {'etiqueta': 'otra'}, {'split': 'otro'},
                                    {'evidencia': 'á' * 501}, {'evidencia': 'x\x00'}])
def test_validacion_etiqueta(monkeypatch, entorno, cambios):
    datos = dict(proceso_gid='demo:1', marca_id='oracle', etiqueta='positivo', split='prueba')
    datos.update(cambios)
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + '/acme/labels', **datos)
    assert 'admin_invalido' in r.headers['location'] and not entorno.escrituras


@pytest.mark.parametrize('termino', ['crystal', ' CRYSTAL ', 'a\n', 'á' * 61, ''])
def test_validacion_termino(monkeypatch, entorno, termino):
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + '/acme/terms', marca_id='oracle',
              termino=termino, tipo='fuerte')
    assert 'admin_invalido' in r.headers['location'] and not entorno.escrituras


def test_proponer(monkeypatch, entorno):
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + '/acme/terms', marca_id='oracle',
              termino='Oracle Database', tipo='fuerte')
    assert 'admin_ok' in r.headers['location']
    assert entorno.auditados[-1][0] == 'TERMINO_PROPUESTO'


@pytest.mark.parametrize('seccion,operacion', [('labels', 'deactivate'), ('terms', 'activate'),
                                             ('terms', 'reject'), ('calibration', 'promote')])
def test_recurso_ajeno(monkeypatch, entorno, seccion, operacion):
    monkeypatch.setattr(aprendizaje.repo, '_uno', lambda *a: None)
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + f'/acme/{seccion}/123/{operacion}')
    assert r.status_code == 404 and r.text == _no_existe()
    assert not entorno.auditados


@pytest.mark.parametrize('operacion', ['promote', 'archive'])
def test_calibracion_confirmacion(monkeypatch, entorno, operacion):
    llamadas = []
    monkeypatch.setattr(aprendizaje.repo, 'promover', lambda *a, **k: llamadas.append(a))
    monkeypatch.setattr(aprendizaje.repo, 'archivar', lambda *a, **k: llamadas.append(a))
    c = _web(monkeypatch, ADMIN_ORG)
    url = BASE + '/acme/calibration/1/' + operacion
    assert 'admin_invalido' in _post(c, url).headers['location']
    assert not llamadas
    assert 'admin_ok' in _post(c, url, confirmar='si').headers['location']
    assert len(llamadas) == 1


def test_paginacion_y_sql():
    capturado = []
    original = aprendizaje.repo._todos
    try:
        aprendizaje.repo._todos = lambda conn, sql, binds: capturado.append((sql, binds)) or [
            (i, 'oracle', 'Título', 'positivo', 'humana', 'prueba', 1) for i in range(26)]
        filas, siguiente = aprendizaje._listar(None, 'demo', 'labels', {'fuente': 'humana'}, 2)
        assert len(filas) == 25 and siguiente
        assert capturado[0][1]['offset'] == 25
        assert capturado[0][1]['fuente'] == 'humana'
        assert 'OFFSET :offset' in capturado[0][0]
    finally:
        aprendizaje.repo._todos = original


def test_adb_caida(monkeypatch, entorno):
    def falla(*a):
        raise RuntimeError('indisponible')
    monkeypatch.setattr(aprendizaje.servicio, 'listar', falla)
    assert _web(monkeypatch, ADMIN_ORG).get(BASE).status_code == 503


def test_texto_autoescape(monkeypatch, entorno):
    entorno.filas = [(1, 'oracle', '<script>demo</script>', 'positivo', 'humana', 'critico', 1)]
    r = _web(monkeypatch, ADMIN_ORG).get(BASE + '/acme/labels')
    assert '&lt;script&gt;demo&lt;/script&gt;' in r.text
    assert '<script>demo</script>' not in r.text


@pytest.mark.parametrize('seccion,operacion,funcion,accion', [
    ('labels', 'deactivate', 'desactivar_ejemplo', 'EJEMPLO_DESACTIVADO'),
    ('terms', 'activate', 'activar_termino', 'TERMINO_ACTIVADO'),
    ('terms', 'reject', 'rechazar_termino', 'TERMINO_RECHAZADO'),
])
def test_transiciones_auditadas(monkeypatch, entorno, seccion, operacion, funcion, accion):
    llamadas = []
    def cambiar(*args, **kw):
        llamadas.append((args, kw))
        return True
    monkeypatch.setattr(aprendizaje.repo, funcion, cambiar)
    r = _post(_web(monkeypatch, ADMIN_ORG), BASE + f'/acme/{seccion}/1/{operacion}')
    assert 'admin_ok' in r.headers['location'] and len(llamadas) == 1
    assert entorno.auditados[-1][0] == accion


def test_exploracion_sql(monkeypatch):
    capturado = []
    monkeypatch.setattr(aprendizaje.repo, '_todos',
                        lambda conn, sql, binds: capturado.append((sql, binds)) or [])
    aprendizaje._listar(None, 'demo', 'explore', {'marca_id': 'oracle'}, 1)
    sql, binds = capturado[0]
    assert 'JSON_TABLE' in sql and 'JSON_SERIALIZE(cp.marcas RETURNING CLOB)' in sql
    assert "j.veredicto = 'por_confirmar'" in sql
    assert 'cp.cliente_id = :cliente_id' in sql and binds['cliente_id'] == 'demo'


def test_limite_intentos(monkeypatch, entorno):
    c = _web(monkeypatch, ADMIN_ORG)
    from src.web.limitador import Limitador
    c.app.state.limitador_estado = Limitador(maximo=1, ventana_seg=600)
    assert _post(c, BASE + '/acme/labels').status_code == 303
    assert _post(c, BASE + '/acme/labels').status_code == 429
