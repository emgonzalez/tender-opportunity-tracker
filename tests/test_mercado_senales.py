"""C7 sin red: filas públicas sintéticas y conexión falsa."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.mercado import repo, senales


def filas(n=10, top=9, directas=3):
    return [dict(id=i, codigo_proceso=f"EJ-{i}", institucion="Institución   Ejemplo",
                 nombre_proceso="Licencias ORACLE" if i % 2 else "oraculo",
                 id_proveedor=1 if i < top else 2, proveedor_nombre=f"Proveedor {1 if i < top else 2}",
                 fecha_adjudicacion=None, directa=i < directas) for i in range(n)]


TERMINOS = [dict(marca_id="oracle", termino="Oracle", tipo="fuerte", activo=1)]


@pytest.mark.parametrize('top,directas,esperado', [(9, 3, True), (8, 3, False), (9, 2, False)])
def test_umbrales(top, directas, esperado):
    s = senales.agregar(filas(top=top, directas=directas), TERMINOS)
    assert s[0]['institucion_norm'] == 'institucion ejemplo'
    assert s[0]['proveedor_unico_de_hecho'] is esperado
    assert s[1]['n_adj'] == 5
    assert s[1]['marca_id'] == 'oracle'


def test_lotes_y_terminos():
    datos = filas()
    for f in datos:
        f['codigo_proceso'] = 'UNICO'
    assert senales.agregar(datos, TERMINOS)[0]['n_directas'] == 1
    assert len(senales.agregar(datos, [dict(TERMINOS[0], tipo='debil')])) == 1
    assert len(senales.agregar(datos, [dict(TERMINOS[0], activo=0)])) == 1
    datos[0]['nombre_proceso'] = 'Mantenimiento JÁVA'
    s = senales.agregar(datos, [dict(TERMINOS[0], termino='java')])
    assert s[1]['n_adj'] == 1


class Conexion:
    def __init__(self, falla=False):
        self.falla = falla
        self.rows = []
        self.snapshot = []
        self.commits = self.rollbacks = 0
        self.sql = []

    def cursor(self):
        return self

    def execute(self, sql, binds):
        self.sql.append(sql)
        if self.falla:
            raise RuntimeError('DSN inválido')
        if sql.lstrip().startswith('DELETE'):
            self.snapshot = self.rows.copy()
            self.rows = [r for r in self.rows if (r['cliente_id'], r['version']) !=
                         (binds['cliente_id'], binds['version'])]
        elif sql.lstrip().startswith('INSERT'):
            self.rows.append(binds)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rows = self.snapshot.copy()
        self.rollbacks += 1

    def close(self):
        pass


def test_idempotencia_y_bytes():
    conn = Conexion()
    s = senales.agregar(filas(), TERMINOS)
    s[0]['proveedor_top'] = 'á' * 300
    assert repo.guardar_senales(conn, 'ejemplo', s, 1) == 2
    assert repo.guardar_senales(conn, 'ejemplo', s, 1) == 2
    assert len(conn.rows) == 2
    assert len(conn.rows[0]['proveedor_top'].encode()) == 300
    assert repo.guardar_senales(conn, 'otro', s, 1) == 2
    assert repo.guardar_senales(conn, 'ejemplo', [], 1) == 0
    assert len(conn.rows) == 2 and conn.commits == 4


def test_adb_caida_y_rollback(monkeypatch, caplog):
    from src.aprendizaje import repo as aprendizaje
    monkeypatch.setattr(aprendizaje, 'listar_terminos_activos', lambda *a: TERMINOS)
    conn = Conexion(falla=True)
    assert repo.calcular_senales(conn, 'ejemplo') == []
    assert repo.guardar_senales(conn, 'ejemplo', [], 1) == 0
    assert conn.rollbacks == 1
    assert 'DSN' not in caplog.text


def test_calculo_solo_select(monkeypatch):
    from src.aprendizaje import repo as aprendizaje
    monkeypatch.setattr(aprendizaje, 'listar_terminos_activos', lambda *a: TERMINOS)

    class Lectura(Conexion):
        def __iter__(self):
            claves = ('id', 'codigo_proceso', 'institucion', 'nombre_proceso', 'id_proveedor',
                      'proveedor_nombre', 'fecha_adjudicacion', 'directa')
            return iter(tuple(f[k] for k in claves) + (1,) for f in filas())

    conn = Lectura()
    assert len(repo.calcular_senales(conn, 'ejemplo')) == 2
    assert all(s.lstrip().startswith('SELECT') for s in conn.sql)
    assert conn.commits == conn.rollbacks == 0


def test_cli_dry_run(monkeypatch, capsys):
    from src.memoria import db
    from src.mercado import __main__ as cli
    conn = Conexion()

    @contextmanager
    def conexion(cfg):
        yield conn

    monkeypatch.setattr(db, 'connection', conexion)
    monkeypatch.setattr(cli, 'get_config', lambda: SimpleNamespace())
    monkeypatch.setattr(repo, 'calcular_senales', lambda *a, **k: senales.agregar(filas(), TERMINOS))
    monkeypatch.setattr(repo, 'guardar_senales', lambda *a, **k: pytest.fail('dry-run escribió'))
    monkeypatch.setattr('sys.argv', ['mercado', 'senales', '--cliente', 'ejemplo'])
    cli.main()
    assert 'Dry-run' in capsys.readouterr().out
    assert conn.sql == []


def test_prefiltro_conservador():
    assert 'oracle' in senales.patron_lexico(TERMINOS)
    assert senales.patron_lexico([dict(TERMINOS[0], termino='x' * 600)]) == '.'
    assert senales.patron_lexico([dict(TERMINOS[0], termino='品牌')]) == '.'


def test_fallo_despues_de_borrar_revierte():
    class FalloInsert(Conexion):
        def execute(self, sql, binds):
            if sql.lstrip().startswith('INSERT'):
                raise RuntimeError('fallo simulado')
            super().execute(sql, binds)

    conn = FalloInsert()
    conn.rows = [dict(cliente_id='ejemplo', version=1, tipo='institucion', n_adj=99)]
    anterior = conn.rows.copy()
    assert repo.guardar_senales(conn, 'ejemplo', senales.agregar(filas(), TERMINOS), 1) == 0
    assert conn.rows == anterior and conn.rollbacks == 1 and conn.commits == 0


def test_cli_fallo_calculo_no_borra(monkeypatch):
    from src.aprendizaje import repo as aprendizaje
    from src.memoria import db
    conn = Conexion(falla=True)

    @contextmanager
    def conexion(cfg):
        yield conn

    monkeypatch.setattr(db, 'connection', conexion)
    monkeypatch.setattr(aprendizaje, 'listar_terminos_activos', lambda *a: TERMINOS)
    monkeypatch.setattr(repo, 'guardar_senales', lambda *a, **k: pytest.fail('fallo de lectura borró señales'))
    senales.ejecutar(SimpleNamespace(), 'ejemplo', aplicar=True)
