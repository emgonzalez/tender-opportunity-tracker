"""Preflight sin Oracle ni red: metadatos y agregados sintéticos."""
import copy
import json

import pytest

from scripts.preflight_diff_adb import comparar, exportar, main, normalizar_check


class Cursor:
    def __init__(self):
        self.consultas = []
        self.cerrado = False

    def execute(self, sql):
        assert sql.startswith("SELECT ")
        self.consultas.append(sql)
        if "FROM user_tables" in sql:
            self.filas = [(t,) for t in ("PEDIDO", "CLIENTE", "BIN$X", "SYS_X", "DR$X", "ISEQ$$X", "DM$X")]
        elif "FROM user_tab_columns" in sql:
            self.filas = [
                ("PEDIDO", "CLIENT_ID", "VARCHAR2", 64, None, None, "N", 64, "B"),
                ("CLIENTE", "CLIENTE_ID", "VARCHAR2", 64, None, None, "N", 64, "B"),
                ("PEDIDO", "DETALLE", "VARCHAR2", 200, None, None, "Y", 200, "B"),
                ("SYS_X", "SECRETO", "VARCHAR2", 64, None, None, "Y", 64, "B"),
            ]
        elif "FROM user_constraints" in sql:
            self.filas = [("CK_PEDIDO", "PEDIDO", "C", '  "CLIENT_ID"  IS\n NOT NULL ', "ENABLED"),
                          ("SYS_C1", "PEDIDO", "C", "1=1", "ENABLED")]
        elif "FROM user_indexes" in sql:
            self.filas = [("IX_VECTOR", "PEDIDO", "VECTOR", "NONUNIQUE", "VALID"),
                          ("DR$X", "PEDIDO", "DOMAIN", "NONUNIQUE", "VALID")]
        elif "FROM user_ind_columns" in sql:
            self.filas = [("IX_VECTOR", "DETALLE", 2, "ASC"), ("IX_VECTOR", "CLIENT_ID", 1, "ASC")]
        elif "COUNT(*)" in sql:
            self.filas = [("cliente_demo", 3), (None, 1)]
        elif sql == 'SELECT "CLIENTE_ID" FROM "CLIENTE"':
            self.filas = [("cliente_demo",)]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.filas

    def close(self):
        self.cerrado = True


class Conexion:
    def __init__(self):
        self.cur = Cursor()

    def cursor(self):
        return self.cur


@pytest.fixture
def datos():
    return exportar(Conexion(), "DEV")


def test_export_solo_metadatos_y_conteos(datos):
    con = Conexion()
    assert exportar(con, "DEV") == datos
    assert con.cur.cerrado
    assert datos["tablas"] == ["CLIENTE", "PEDIDO"]
    assert datos["clientes"] == ["cliente_demo"]
    assert datos["conteos"]["PEDIDO"]["CLIENT_ID"] == [
        {"cliente_id": None, "cantidad": 1}, {"cliente_id": "cliente_demo", "cantidad": 3}]
    assert datos["indices"]["IX_VECTOR"]["tipo"] == "VECTOR"
    assert datos["indices"]["IX_VECTOR"]["columnas"][0]["posicion"] == 1
    assert list(datos["constraints"]) == ["CK_PEDIDO"]
    texto = json.dumps(datos)
    assert "SECRETO" not in texto
    assert "contenido privado de un pedido" not in texto
    # Nunca se consulta DETALLE ni se seleccionan filas completas de negocio.
    negocio = [s for s in con.cur.consultas if "user_" not in s]
    assert len(negocio) == 3
    assert all("COUNT(*)" in s or s == 'SELECT "CLIENTE_ID" FROM "CLIENTE"' for s in negocio)


def test_check_normalizado(datos):
    assert normalizar_check('  "X" IS\n NOT   NULL ') == '"x" is not null'
    assert normalizar_check(None) == ""
    assert datos["constraints"]["CK_PEDIDO"]["check"] == '"client_id" is not null'


def test_iguales_y_vacios(datos):
    assert comparar({}, {}, out=lambda _: None) == 0
    otro = copy.deepcopy(datos)
    otro["etiqueta"] = "PROD"
    otro["conteos"]["PEDIDO"]["CLIENT_ID"][1]["cantidad"] = 99
    assert comparar(datos, otro, out=lambda _: None) == 0


@pytest.mark.parametrize("cambio,esperado", [
    (lambda d: d["tablas"].remove("PEDIDO"), "Tabla PEDIDO: falta en PROD"),
    (lambda d: d["columnas"]["PEDIDO"]["DETALLE"].update(tipo="CLOB"), "Columna de PEDIDO DETALLE: distinto"),
    (lambda d: d["columnas"]["PEDIDO"]["DETALLE"].update(nulable="N"), "Columna de PEDIDO DETALLE: distinto"),
    (lambda d: d["constraints"]["CK_PEDIDO"].update(check="1=1"), "CHECK distinto"),
    (lambda d: d["constraints"].clear(), "Constraint CK_PEDIDO: falta en PROD"),
    (lambda d: d["indices"].clear(), "Índice IX_VECTOR: falta en PROD"),
    (lambda d: d.update(clientes=["cliente_demo_sv"]), "Id de cliente cliente_demo: falta en PROD"),
])
def test_diferencias(datos, cambio, esperado):
    otro = copy.deepcopy(datos)
    cambio(otro)
    lineas = []
    assert comparar(datos, otro, base="DEV", objetivo="PROD", out=lineas.append) == 2
    assert esperado in "\n".join(lineas)


def test_claves_conteos_solo_informativas(datos):
    otro = copy.deepcopy(datos)
    otro["conteos"]["PEDIDO"]["CLIENT_ID"][1]["cliente_id"] = "cliente_demo_sv"
    lineas = []
    assert comparar(datos, otro, base="DEV", objetivo="PROD", out=lineas.append) == 0
    assert any("cliente_demo' solo en DEV" in linea for linea in lineas)
    assert any("cliente_demo_sv' solo en PROD" in linea for linea in lineas)


def test_cli_comparar(tmp_path, capsys):
    rutas = [tmp_path / "a.json", tmp_path / "b.json"]
    for ruta in rutas:
        ruta.write_text("{}", encoding="utf-8")
    assert main(["--comparar", *map(str, rutas), "--base", "DEV", "--objetivo", "PROD"]) == 0
    assert "DEV → PROD" in capsys.readouterr().out


def test_cli_error_no_revela_detalles(tmp_path, capsys):
    assert main(["--comparar", str(tmp_path / "inexistente"), "otro.json"]) == 1
    assert "inexistente" not in capsys.readouterr().err


def test_cli_export_con_conexion_falsa(monkeypatch, capsys):
    from contextlib import contextmanager

    from src import config
    from src.memoria import db

    @contextmanager
    def conexion_falsa(cfg):
        assert cfg is configuracion
        yield Conexion()

    configuracion = object()
    monkeypatch.setattr(config, "Config", lambda: configuracion)
    monkeypatch.setattr(db, "connection", conexion_falsa)
    assert main(["--exportar", "--etiqueta", "DEV"]) == 0
    salida = capsys.readouterr()
    datos = exportar(Conexion(), "DEV")
    assert salida.out == json.dumps(datos, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    assert salida.err == ""
