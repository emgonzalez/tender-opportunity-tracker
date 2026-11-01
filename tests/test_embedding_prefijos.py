"""Prefijos E5 y migración manual, solo conexiones falsas."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.memoria import rag, revectorizar, vectorizer


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for nombre in ("COMPRASAL_EMAIL", "SMTP_USER"):
        monkeypatch.setenv(nombre, "test@example.com")
    for nombre in ("COMPRASAL_PASSWORD", "SMTP_PASSWORD"):
        monkeypatch.setenv(nombre, "ficticia")


def config(modo="off"):
    return SimpleNamespace(embedding_prefijos=modo, embedding_model_name="MULTILINGUAL_E5_SMALL",
                           chunk_tokens=100, chunk_overlap=0, precedentes_max=3)


@pytest.mark.parametrize("valor,esperado", [(None, "off"), ("on", "on"), (" ON ", "on"), ("activo", "off")])
def test_config(monkeypatch, valor, esperado):
    from src.config import Config
    if valor is None:
        monkeypatch.delenv("EMBEDDING_PREFIJOS", raising=False)
    else:
        monkeypatch.setenv("EMBEDDING_PREFIJOS", valor)
    assert Config().embedding_prefijos == esperado


def test_helper():
    assert vectorizer.con_prefijo(config(), "query", "texto") == "texto"
    assert vectorizer.con_prefijo(config("on"), "query", "texto") == "query: texto"
    assert vectorizer.con_prefijo(config("on"), "passage", "passage: texto") == "passage: texto"
    with pytest.raises(ValueError):
        vectorizer.con_prefijo(config(), "otro", "texto")


@pytest.mark.parametrize("funcion", [rag.buscar_chunks, rag.buscar_chunks_v2,
                                     rag.buscar_precedentes, rag.buscar_precedentes_v2])
def test_consultas_sql_identico_y_binds(funcion):
    llamadas = []
    for modo in (None, "off", "on"):
        cfg = config(modo)
        if modo is None:
            del cfg.embedding_prefijos
        conn = MagicMock()
        conn.cursor.return_value.fetchall.return_value = []
        if "precedentes" in funcion.__name__:
            funcion(conn, "texto", cfg, "cliente_demo")
        else:
            funcion(conn, "REF", "texto", cfg)
        llamadas.append(conn.cursor.return_value.execute.call_args.args)
    assert llamadas[0] == llamadas[1]
    assert llamadas[1][0] == llamadas[2][0]
    assert llamadas[1][1]["pregunta"] == "texto"
    assert llamadas[2][1]["pregunta"] == "query: texto"


@pytest.mark.parametrize("modo", ["off", "on"])
def test_buscar_pliego_conserva_prefijo_historico(modo):
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = []
    rag.buscar_en_pliego(conn, "portal:REF", ["texto"], cfg=config(modo))
    assert conn.cursor.return_value.execute.call_args.args[1]["q"] == "query: texto"


@pytest.mark.parametrize("funcion", [vectorizer.vectorizar_proceso, vectorizer.vectorizar_proceso_v2])
def test_insert_prefija_solo_embedding(monkeypatch, funcion):
    monkeypatch.setattr(vectorizer, "chunk_text", lambda *args: [(0, "texto original")])
    llamadas = []
    for modo in (None, "off", "on"):
        cfg = config(modo)
        if modo is None:
            del cfg.embedding_prefijos
        conn = MagicMock()
        funcion(conn, "REF", "texto original", cfg)
        llamadas.append(conn.cursor.return_value.executemany.call_args.args)
    assert llamadas[0] == llamadas[1]
    esperado = (
        "INSERT INTO licitaciones_chunks (chunk_id, proceso_id, seq, chunk_text, embedding) "
        "VALUES (:cid, :pid, :seq, :txt, VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :txt AS data))"
        if funcion is vectorizer.vectorizar_proceso else
        "INSERT INTO chunk (proceso_gid, seq, pagina, texto, embedding) "
        "VALUES (:gid, :seq, :pagina, :txt, VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :txt AS data))"
    )
    assert llamadas[1][0] == esperado
    assert "USING :embedding_txt AS data" in llamadas[2][0]
    assert llamadas[2][1][0]["txt"] == "texto original"
    assert llamadas[2][1][0]["embedding_txt"] == "passage: texto original"


def test_revectorizar_dry_run(tmp_path):
    conn = MagicMock()
    conn.call_timeout = 100
    conn.cursor.return_value.fetchone.side_effect = [(3,), (4,), (5,)]
    archivo = tmp_path / "checkpoint.json"
    resultado = revectorizar.revectorizar(conn, config(), checkpoint=archivo)
    assert [v["total"] for v in resultado.values()] == [3, 4, 5]
    assert all(c.args[0].startswith("SELECT COUNT(*)") for c in conn.cursor.return_value.execute.call_args_list)
    conn.commit.assert_not_called()
    assert not archivo.exists()
    assert conn.call_timeout == 100


def test_revectorizar_aplicar_y_reanudar(tmp_path):
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = (1,)
    conn.cursor.return_value.fetchall.side_effect = [[("a",)], [], [(2,)], [], [(3,)], []]
    archivo = tmp_path / "checkpoint.json"
    revectorizar.revectorizar(conn, config(), aplicar=True, checkpoint=archivo)
    assert conn.commit.call_count == 3
    updates = [c for c in conn.cursor.return_value.execute.call_args_list if c.args[0].startswith("UPDATE")]
    assert len(updates) == 3
    assert all(c.args[1]["prefijo"] == "passage: " for c in updates)
    conn.reset_mock()
    conn.cursor.return_value.fetchall.side_effect = [[], [], []]
    revectorizar.revectorizar(conn, config(), aplicar=True, checkpoint=archivo)
    selects = [c for c in conn.cursor.return_value.execute.call_args_list if "FETCH FIRST" in c.args[0]]
    assert [c.args[1]["ultimo"] for c in selects] == ["a", 2, 3]
    conn.commit.assert_not_called()


def test_revectorizar_tiempo_agotado(monkeypatch, tmp_path):
    conn = MagicMock()
    monkeypatch.setattr(revectorizar.time, "monotonic", MagicMock(side_effect=[0, 2]))
    with pytest.raises(TimeoutError):
        revectorizar.revectorizar(conn, config(), aplicar=True, segundos=1, checkpoint=tmp_path / "c.json")
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
