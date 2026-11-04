"""Aislamiento de decisiones históricas, sin servicios externos."""
import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import evaluar_marca, metricas_capas
from src.aprendizaje import repo
from src.drilldown import replay


@pytest.mark.parametrize("variante", ["activa", "sombra", "replay"])
def test_repo_valida_y_aisla_variantes(variante):
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None
    repo.guardar_decision(conn, "demo", "demo:1", "C0", variante=variante)
    assert conn.cursor.return_value.execute.call_args.args[1]["variante"] == variante
    repo.buscar_decision_por_hash(conn, "hash", "C0", variante=variante)
    assert conn.cursor.return_value.execute.call_args.args[1]["variante"] == variante
    with pytest.raises(ValueError):
        repo.buscar_decision_por_hash(conn, "hash", "C0", variante="otra")


def test_solo_replay_escribe_variante_replay():
    for ruta in Path("src").rglob("*.py"):
        for nodo in ast.walk(ast.parse(ruta.read_text())):
            if isinstance(nodo, ast.Call):
                for kw in nodo.keywords:
                    if kw.arg == "variante" and isinstance(kw.value, ast.Constant) and kw.value.value == "replay":
                        assert ruta == Path("src/drilldown/replay.py")


@pytest.mark.parametrize("modulo", [metricas_capas, evaluar_marca])
@pytest.mark.parametrize("opciones,filtro", [({}, "variante <> 'replay'"),
    ({"incluir_replay": True}, "1=1"), ({"solo_replay": True}, "variante = 'replay'")])
def test_lectores_filtran_replay(modulo, opciones, filtro, monkeypatch):
    consultas = []
    def leer(conn, sql, *args):
        consultas.append(sql)
        return []
    monkeypatch.setattr(modulo, "leer", leer)
    resultado = (modulo.informe(None, **opciones) if modulo is metricas_capas
                 else modulo.informe(None, "demo", "C0", **opciones))
    assert filtro in next(sql for sql in consultas if "FROM decision_capa" in sql)
    if modulo is evaluar_marca and opciones.get("solo_replay"):
        assert set(resultado["metricas"]) == {"replay"}
        assert not resultado["promocion"]["puede_promover"]


def test_reintento_sin_duplicar_decisiones():
    conn = MagicMock()
    estado = dict(resultados={"REF": {"nuevo": {}}}, capas=["c0", "c2"], llamadas={}, firma="demo")
    filas = [dict(referencia="REF", gid="demo:REF")]
    conectar = MagicMock(return_value=conn)
    replay.persistir_decisiones(estado, filas, "demo", conectar)
    assert conn.__enter__.return_value.commit.call_count == 2
    replay.persistir_decisiones(estado, filas, "demo", conectar)
    assert conn.__enter__.return_value.commit.call_count == 2
    assert estado["decisiones_guardadas"] == ["REF:c0", "REF:c2"]


def test_fallo_commit_se_reintenta_sin_repetir_exitos():
    conn = MagicMock()
    conn.__enter__.return_value.commit.side_effect = [None, ConnectionError("DSN inválido de prueba"), None]
    estado = dict(resultados={"REF": {"nuevo": {}}}, capas=["c0", "c2"], llamadas={}, firma="demo")
    filas = [dict(referencia="REF", gid="demo:REF")]
    conectar = MagicMock(return_value=conn)
    replay.persistir_decisiones(estado, filas, "demo", conectar)
    assert estado["fallos_decision"] == 1
    assert estado["decisiones_guardadas"] == ["REF:c0"]
    replay.persistir_decisiones(estado, filas, "demo", conectar)
    assert conn.__enter__.return_value.commit.call_count == 3
    assert estado["fallos_decision"] == 0
    assert estado["decisiones_guardadas"] == ["REF:c0", "REF:c2"]


@pytest.mark.parametrize("modulo", [metricas_capas, evaluar_marca])
def test_opciones_replay_son_excluyentes(modulo):
    with pytest.raises(SystemExit) as exc:
        modulo.main(["--cliente", "demo", "--incluir-replay", "--solo-replay"] if modulo is evaluar_marca
                    else ["--incluir-replay", "--solo-replay"])
    assert exc.value.code == 2
