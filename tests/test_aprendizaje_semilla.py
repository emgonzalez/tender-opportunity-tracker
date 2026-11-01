"""Semilla del diccionario y CLI `python -m src.aprendizaje` (sin ADB: conexión y repo simulados)."""
from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from src.aprendizaje import __main__ as cli
from src.aprendizaje import repo, semilla
from src.identidad.politica import AccionNoAuditable

PORTAFOLIO_SV = {"hpe", "oracle", "netsuite", "citrix", "fortinet"}


# ── datos de la semilla ────────────────────────────────────────────────────

def test_la_semilla_es_valida_para_el_ddl():
    terminos = semilla.terminos_semilla()
    assert len(terminos) == len(set(terminos))
    for marca, termino, tipo in terminos:
        assert tipo in repo.TIPOS_TERMINO
        assert termino == termino.strip() and 0 < len(termino.encode("utf-8")) <= 120
        assert marca == marca.lower()


def test_nunca_crystal_solo():
    terminos = {t.lower() for _, t, _ in semilla.terminos_semilla()}
    assert not terminos & semilla.PROHIBIDOS
    assert ("sap", "Crystal Reports", "ajena") in semilla.terminos_semilla()


def test_oracle_trae_fuertes_debiles_y_contrapatrones():
    oracle = semilla.SEMILLA["oracle"]
    assert {"Exadata", "ZDLRA", "Cloud@Customer", "derechos de actualización por procesador"} <= set(oracle["fuerte"])
    assert "migración de oracle a" in oracle["excluye"]
    assert "base de datos" in oracle["debil"]


def test_cognos_y_power_bi_son_ajenas():
    t = semilla.terminos_semilla()
    assert ("ibm", "Cognos", "ajena") in t and ("microsoft", "Power BI", "ajena") in t


def test_filtrar_por_portafolio():
    salida, avisos = semilla.filtrar_por_portafolio(
        [("oracle", "Exadata", "fuerte"), ("ibm", "Cognos", "ajena"), ("vmware", "vSphere", "fuerte"),
         ("oracle", "Oracle", "ajena")],
        {"oracle"},
    )
    assert salida == [("oracle", "Exadata", "fuerte"), ("ibm", "Cognos", "ajena")]
    assert any("vmware" in a for a in avisos) and any("'oracle': ajena del portafolio" in a for a in avisos)


def test_filtrar_sin_portafolio_siembra_todo_y_avisa():
    salida, avisos = semilla.filtrar_por_portafolio(semilla.terminos_semilla(), None)
    assert salida == semilla.terminos_semilla() and avisos


# ── CLI ────────────────────────────────────────────────────────────────────

@pytest.fixture
def adb(monkeypatch):
    """Estado simulado de la ADB: términos existentes y lo que la CLI intentó sembrar."""
    estado = {"existentes": set(), "sembrados": [], "cliente": True}
    monkeypatch.setattr(repo, "existe_cliente", lambda conn, cid: estado["cliente"])
    monkeypatch.setattr(repo, "terminos_existentes", lambda conn, cid: set(estado["existentes"]))
    monkeypatch.setattr(cli, "portafolio", lambda conn, cid: PORTAFOLIO_SV)

    def sembrar(conn, cid, terminos, creado_por=None):
        terminos = list(terminos)
        estado["sembrados"].append((cid, terminos, creado_por))
        estado["existentes"] |= set(terminos)
        return len(terminos)
    monkeypatch.setattr(repo, "sembrar_terminos", sembrar)
    return estado


def _conectar():
    return contextlib.nullcontext(MagicMock())


def test_semilla_es_dry_run_por_defecto(adb, capsys):
    assert cli.main(["semilla"], conectar=_conectar) == 0
    out = capsys.readouterr().out
    assert adb["sembrados"] == [] and "[dry-run]" in out and "multicomputos_sv" in out


def test_semilla_aplicar_es_idempotente(adb, capsys):
    assert cli.main(["semilla", "--aplicar"], conectar=_conectar) == 0
    ((cid, terminos, por),) = adb["sembrados"]
    assert cid == "multicomputos_sv" and por.startswith("cli:")
    assert ("oracle", "Exadata", "fuerte") in terminos and ("ibm", "Cognos", "ajena") in terminos
    capsys.readouterr()

    assert cli.main(["semilla", "--aplicar"], conectar=_conectar) == 0
    assert len(adb["sembrados"]) == 1                     # la segunda vez no hay nada que insertar
    assert "0 por insertar" in capsys.readouterr().out


def test_semilla_solo_inserta_lo_que_falta(adb, capsys):
    adb["existentes"] = {("oracle", "Exadata", "fuerte")}
    cli.main(["semilla", "--aplicar"], conectar=_conectar)
    (_, terminos, _), = adb["sembrados"]
    assert ("oracle", "Exadata", "fuerte") not in terminos
    assert "1 ya existen" in capsys.readouterr().out


def test_semilla_cliente_inexistente(adb, capsys):
    adb["cliente"] = False
    assert cli.main(["semilla", "--cliente", "nadie"], conectar=_conectar) == 1
    assert "no existe" in capsys.readouterr().err


def test_semilla_resuelve_el_alias_del_cliente(adb, capsys):
    cli.main(["semilla", "--cliente", "multicomputos"], conectar=_conectar)
    assert "multicomputos_sv:" in capsys.readouterr().out


def test_accion_no_auditable_falla_sin_escribir(adb, monkeypatch, capsys):
    def sembrar(*a, **k):
        raise AccionNoAuditable("TERMINOS_SEMBRADOS")
    monkeypatch.setattr(repo, "sembrar_terminos", sembrar)
    assert cli.main(["semilla", "--aplicar"], conectar=_conectar) == 1
    assert "ACCIONES_AUDITADAS" in capsys.readouterr().err


def test_adb_caida_no_imprime_traza(capsys):
    def conectar():
        raise ConnectionError("dsn=secreto")
    assert cli.main(["estado"], conectar=conectar) == 1
    err = capsys.readouterr().err
    assert "ConnectionError" in err and "secreto" not in err


def test_estado_solo_lee(monkeypatch, capsys):
    monkeypatch.setattr(repo, "contar_por_tabla", lambda conn, cid: {t: 0 for t in repo.TABLAS})
    monkeypatch.setattr(repo, "contar_terminos_por_estado", lambda conn, cid: {("fuerte", "semilla"): 18})
    conn = MagicMock()
    assert cli.main(["estado", "--cliente", "multicomputos_sv"], conectar=lambda: contextlib.nullcontext(conn)) == 0
    out = capsys.readouterr().out
    assert "ejemplo_marca" in out and "fuerte   semilla    18" in out
    conn.commit.assert_not_called()


def test_portafolio_lee_las_marcas_del_perfil_activo(monkeypatch):
    from src.clientes import repo as clientes_repo
    datos = {"catalog": {"categories": [{"brands": [{"id": "Oracle"}, {"id": "hpe"}]}, {"brands": None}]}}
    monkeypatch.setattr(clientes_repo, "activo", lambda conn, cid: (3, datos))
    assert cli.portafolio(MagicMock(), "x") == {"oracle", "hpe"}
    monkeypatch.setattr(clientes_repo, "activo", lambda conn, cid: None)
    assert cli.portafolio(MagicMock(), "x") is None
