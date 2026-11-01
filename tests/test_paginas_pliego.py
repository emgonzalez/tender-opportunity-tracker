"""Repositorio de páginas sin Oracle real."""
from unittest.mock import MagicMock

from src.aprendizaje.paginas import existe, guardar_paginas, leer_paginas


def test_guardado_idempotente_por_clave():
    class Cursor:
        def __init__(self):
            self.filas = {}

        def executemany(self, sql, lote):
            assert "MERGE INTO pagina_pliego" in sql
            assert "p.proceso_gid = s.proceso_gid AND p.pagina = s.pagina" in sql
            for fila in lote:
                self.filas[fila["gid"], fila["pagina"]] = fila

        def close(self):
            pass

    cursor = Cursor()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pagina = [{"pagina": 23, "texto": "carta del fabricante", "fuente": "vision_llm"}]
    assert guardar_paginas(conn, "portal:REF-SINTETICA", pagina, hash_pdf="a" * 64) == 1
    assert guardar_paginas(conn, "portal:REF-SINTETICA", pagina, hash_pdf="a" * 64) == 1
    assert len(cursor.filas) == 1
    conn.commit.assert_not_called()


def test_adb_no_disponible_degrada():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("DSN inválido")
    assert guardar_paginas(conn, "portal:REF", [{"pagina": 1, "texto": "objeto"}]) == 0
    assert leer_paginas(conn, "portal:REF") == []
    assert existe(conn, "a" * 64) is False


def test_lectura_clob_y_huella():
    conn = MagicMock()
    lob = MagicMock()
    lob.read.return_value = "Texto público sintético"
    conn.cursor.return_value.fetchall.return_value = [(23, lob, "vision_llm", "falso", None, "abc")]
    resultado = leer_paginas(conn, "portal:REF", hash_pdf="abc")
    assert resultado[0]["texto"] == "Texto público sintético"
    assert resultado[0]["pagina"] == 23
    assert conn.cursor.return_value.execute.call_args.args[1]["hash_pdf"] == "abc"


def test_validacion_de_paginas():
    conn = MagicMock()
    assert guardar_paginas(conn, "portal:REF", [{"pagina": 0}, {"pagina": 2, "fuente": "inventada"}]) == 0
    conn.cursor.assert_not_called()
