"""Rescate C5 sin red ni bases reales."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.classifier.rescate import UMBRAL_SIMILITUD, rescatar


def cfg(modo="activo", prefijos="off"):
    return SimpleNamespace(intel_c5=modo, embedding_prefijos=prefijos,
                           embedding_model_name="MULTILINGUAL_E5_SMALL")


def procesos():
    return [SimpleNamespace(numero_referencia=f"REF-{i}", descripcion=f"Título {i}") for i in range(3)]


def conexion(usados=0):
    conn = MagicMock()
    conn.cursor.return_value.fetchone.side_effect = [("demo",), (usados,)]
    conn.cursor.return_value.fetchall.side_effect = [[("Item", 0.9)], [("Marca", 0.95)], [("Otro", 0.4)]]
    return conn


def inserts(conn):
    return [json.loads(c.args[1]["s"]) for c in conn.cursor.return_value.execute.call_args_list
            if c.args[0].startswith("INSERT")]


def test_off_no_sql():
    conn = conexion()
    assert rescatar(conn, cfg("off"), procesos(), "demo", 3) == []
    conn.cursor.assert_not_called()


def test_umbral_cupo_y_exploracion():
    conn = conexion()
    resultado = rescatar(conn, cfg(), procesos(), "demo", 3)
    assert len(resultado) == 1
    assert resultado[0]["score"] >= UMBRAL_SIMILITUD
    assert resultado[0]["proceso"].numero_referencia == "REF-1"
    assert [s["destino"] for s in inserts(conn)] == ["Por confirmar", "exploracion", "exploracion"]
    conn.commit.assert_not_called()


@pytest.mark.parametrize("modo,usados", [("activo", 1), ("sombra", 0)])
def test_cupo_agotado_o_sombra(modo, usados):
    conn = conexion(usados)
    assert rescatar(conn, cfg(modo), procesos(), "demo", 3) == []
    assert all(s["destino"] == "exploracion" for s in inserts(conn))


@pytest.mark.parametrize("score,elegido", [(UMBRAL_SIMILITUD, True), (UMBRAL_SIMILITUD - .001, False), (float("nan"), False)])
def test_limite_umbral(score, elegido):
    conn = conexion()
    conn.cursor.return_value.fetchall.side_effect = [[("Item", score)]]
    assert bool(rescatar(conn, cfg(), procesos()[:1], "demo", 3)) == elegido


@pytest.mark.parametrize("prefijos", ["off", "on"])
def test_prefijos_sql_aislamiento(prefijos):
    conn = conexion()
    rescatar(conn, cfg(prefijos=prefijos), procesos(), "demo", 3)
    consulta = next(c for c in conn.cursor.return_value.execute.call_args_list if "WITH catalogo" in c.args[0])
    sql, binds = consulta.args
    assert sql.count("c.cliente_id = :c AND c.activa = 1") == 3
    assert binds["titulo"] == ("query: " if prefijos == "on" else "") + "Título 0"
    assert binds["prefijo"] == ("passage: " if prefijos == "on" else "")
    assert "FOR UPDATE" in conn.cursor.return_value.execute.call_args_list[1].args[0]


def test_adb_caida_sin_red():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("DSN inválido: no-es-un-dsn")
    assert rescatar(conn, cfg(), procesos(), "demo", 3) == []


def test_fallo_parcial_revierte_reserva():
    conn = conexion()
    def ejecutar(sql, *args):
        if sql.startswith("INSERT"):
            raise RuntimeError("modelo no disponible")
    conn.cursor.return_value.execute.side_effect = ejecutar
    assert rescatar(conn, cfg(), procesos(), "demo", 3) == []
    assert any("ROLLBACK TO SAVEPOINT" in c.args[0] for c in conn.cursor.return_value.execute.call_args_list)


def test_no_mutar_y_no_rescatar_descarte_determinista():
    conn = conexion()
    datos = procesos()[:1]
    datos[0].descartado_determinista = True
    assert rescatar(conn, cfg(), datos, "demo", 3) == []
    conn.cursor.return_value.fetchall.assert_not_called()
    assert not hasattr(datos[0], "es_relevante")
