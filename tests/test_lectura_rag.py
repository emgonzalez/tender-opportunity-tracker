"""Búsqueda dirigida híbrida sin red ni Oracle real."""
from unittest.mock import MagicMock

from src.memoria.rag import buscar_en_pliego


def test_sql_exacto_y_lexico_sin_tildes():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchall.side_effect = [
        [(0, 1, "Objeto genérico"), (1, 23, "Carta de autorización del fabricante. Proveedor único")],
        [(0, 1, "Objeto genérico", 0.1), (1, 23, "Carta de autorización del fabricante", 0.2)],
    ]
    referencia = "portal:REF';SELECT"
    resultado = buscar_en_pliego(conn, referencia, ["autorización"], 2)
    assert resultado[0]["pagina"] == 23
    llamadas = cursor.execute.call_args_list
    for llamada in llamadas:
        sql, binds = llamada.args
        assert "WHERE proceso_gid = :p" in sql
        assert referencia not in sql
        assert binds["p"] == referencia
        assert "APPROX" not in sql
    sql, binds = llamadas[1].args
    assert "VECTOR_DISTANCE" in sql
    assert "VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :q AS data)" in sql
    assert binds["q"] == "query: autorización"
    assert len(resultado) <= 2


def test_busqueda_caida_y_limite():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("DSN inválido")
    assert buscar_en_pliego(conn, "portal:REF", ["marca"], 4) == []
    assert buscar_en_pliego(conn, "portal:REF", ["marca"], 0) == []
