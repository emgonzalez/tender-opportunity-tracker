"""Chunks de pliegos con ubicación física de la evidencia."""

from types import SimpleNamespace

import pytest

from src.memoria.chunker import chunk_paginas
from src.memoria.vectorizer import vectorizar_proceso_v2


def test_chunks_no_cruzan_paginas_y_conservan_clausulas_breves():
    paginas = [{"pagina": 1, "texto": "Objeto."},
               {"pagina": 23, "texto": "Carta de autorización del fabricante. " * 40}]
    chunks = chunk_paginas(paginas, chunk_tokens=100, overlap=20)
    assert [seq for seq, _, _ in chunks] == list(range(len(chunks)))
    assert chunks[0] == (0, 1, "Objeto.")
    assert all(pagina == 23 and "Objeto" not in texto for _, pagina, texto in chunks[1:])
    assert len(chunks) > 2


@pytest.mark.parametrize("pagina", [0, -1, True, "23"])
def test_chunk_paginas_exige_numero_fisico(pagina):
    with pytest.raises(ValueError):
        chunk_paginas([(pagina, "texto")])


class Cursor:
    rowcount = 0

    def __init__(self):
        self.batches = []

    def execute(self, sql, binds):
        self.delete = (sql, binds)

    def executemany(self, sql, binds):
        self.batches.append((sql, list(binds)))


@pytest.mark.parametrize("explicit", [True, False])
def test_vectorizar_v2_llena_pagina_sin_cambiar_embedding(explicit):
    cursor = Cursor()
    conn = SimpleNamespace(cursor=lambda: cursor)
    cfg = SimpleNamespace(embedding_model_name="MULTILINGUAL_E5_SMALL",
                          chunk_tokens=800, chunk_overlap=100)
    paginas = [{"pagina": 1, "texto": "Objeto."}, {"pagina": 23, "texto": "Carta fabricante."}]
    opciones = {"paginas": paginas} if explicit else {}
    texto = "Objeto.\fCarta fabricante."
    assert vectorizar_proceso_v2(conn, "REF-SINTETICA", texto, cfg, **opciones) == 2
    sql, binds = cursor.batches[0]
    assert "pagina" in sql
    assert "VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :txt AS data)" in sql
    assert [b["pagina"] for b in binds] == ([1, 23] if explicit else [1, 2])
    assert all(not b["txt"].startswith("passage: ") for b in binds)
