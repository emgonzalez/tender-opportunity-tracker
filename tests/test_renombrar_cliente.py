"""scripts/renombrar_cliente.py: orden de los pasos y salvaguardas, con un cursor falso (sin ADB)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("renombrar_cliente", Path(__file__).parent.parent / "scripts" / "renombrar_cliente.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

TODAS = rc._conocidas() - rc.IGNORAR


def _orden(plan):
    return [(a, t) for a, t, _ in plan]


def test_el_padre_se_copia_antes_de_re_apuntar_a_los_hijos_y_se_borra_despues():
    plan = _orden(rc.pasos(TODAS))
    for padre, hijo in rc.PADRES:
        i_copia, i_hijo, i_borra = plan.index(("copiar", padre)), plan.index(("actualizar", hijo)), plan.index(("borrar", padre))
        assert i_copia < i_hijo < i_borra


def test_cliente_se_copia_primero_y_se_borra_al_final():
    plan = _orden(rc.pasos(TODAS))
    assert plan[0] == ("copiar", "CLIENTE") and plan[-1] == ("borrar", "CLIENTE")
    assert plan.index(("actualizar", "MEMBRESIA")) < plan.index(("borrar", "CLIENTE"))
    assert plan.index(("copiar", "CLIENTE")) < plan.index(("copiar", "CLIENTE_PROCESO"))


def test_omite_tablas_que_no_existen():
    plan = _orden(rc.pasos({"CLIENTE", "MEMBRESIA", "CLIENTE_PROCESO", "ANALISIS"}))
    assert ("actualizar", "LICITACIONES_METADATA") not in plan and ("copiar", "OPORTUNIDAD_ESTADO") not in plan
    assert ("actualizar", "ANALISIS") in plan


def test_auditoria_nunca_se_toca():
    assert "AUDITORIA" in rc.IGNORAR
    assert all(t != "AUDITORIA" for _, t, _ in rc.pasos(rc._conocidas()))


class _Cur:
    """Cursor falso: responde a las consultas de metadatos y cuenta con un dict; registra las escrituras."""

    def __init__(self, filas, extra=()):
        self.filas, self.extra, self.escrituras, self._res = filas, list(extra), [], None

    def execute(self, sql, **kw):
        s = " ".join(sql.split())
        if s.startswith(("INSERT", "UPDATE", "DELETE")):
            self.escrituras.append(s)
        elif "FROM user_tab_cols WHERE column_name IN" in s:
            self._res = [(t,) for t in sorted(rc._conocidas() | set(self.extra))]
        elif "FROM user_tables" in s:
            self._res = [(1,)]
        elif s.startswith("SELECT COUNT(*)"):
            n = self.filas.get(kw["v"], 0)
            # la fila de `cliente` es una sola por id; el resto de las tablas tiene `n` filas
            self._res = [(min(n, 1) if s.startswith('SELECT COUNT(*) FROM "CLIENTE" ') else n,)]

    def fetchone(self):
        return self._res[0]

    def fetchall(self):
        return self._res


class _Con:
    def __init__(self, cur):
        self.cur, self.commits, self.rollbacks = cur, 0, 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _salida():
    lineas = []
    return lineas, lineas.append


def test_dry_run_no_escribe():
    con = _Con(_Cur({"multicomputos": 3}))
    lineas, out = _salida()
    assert rc.renombrar(con, "multicomputos", "multicomputos_sv", aplicar=False, out=out)
    assert con.cur.escrituras == [] and con.commits == 0 and "dry-run" in lineas[-1]


def test_aborta_si_el_id_nuevo_ya_existe():
    con = _Con(_Cur({"multicomputos": 3, "multicomputos_sv": 1}))
    lineas, out = _salida()
    assert not rc.renombrar(con, "multicomputos", "multicomputos_sv", aplicar=True, out=out)
    assert con.cur.escrituras == [] and any("ya existe" in x for x in lineas)


def test_aborta_si_el_viejo_no_existe():
    lineas, out = _salida()
    assert not rc.renombrar(_Con(_Cur({})), "multicomputos", "multicomputos_sv", aplicar=True, out=out)
    assert any("no existe" in x for x in lineas)


def test_aborta_ante_una_tabla_con_cliente_que_no_conoce():
    con = _Con(_Cur({"multicomputos": 1}, extra=["TABLA_NUEVA"]))
    lineas, out = _salida()
    assert not rc.renombrar(con, "multicomputos", "multicomputos_sv", aplicar=True, out=out)
    assert con.cur.escrituras == [] and any("TABLA_NUEVA" in x for x in lineas)


def test_ids_iguales():
    assert not rc.renombrar(_Con(_Cur({})), "a", "a", aplicar=True, out=lambda *_: None)


def test_si_los_conteos_no_cuadran_hace_rollback():
    """El cursor falso no mueve filas, así que tras aplicar el id nuevo sigue en 0 y el viejo intacto."""
    con = _Con(_Cur({"multicomputos": 2}))
    lineas, out = _salida()
    assert not rc.renombrar(con, "multicomputos", "multicomputos_sv", aplicar=True, out=out)
    assert con.rollbacks == 1 and con.commits == 0 and any("ROLLBACK" in x for x in lineas)
    assert con.cur.escrituras  # sí intentó escribir; por eso el rollback importa


@pytest.mark.parametrize("sql_inicio", ["INSERT INTO", "UPDATE", "DELETE FROM"])
def test_solo_dml(sql_inicio):
    con = _Con(_Cur({"multicomputos": 1}))
    rc.renombrar(con, "multicomputos", "multicomputos_sv", aplicar=True, out=lambda *_: None)
    assert all(e.startswith(("INSERT INTO", "UPDATE", "DELETE FROM")) for e in con.cur.escrituras)
