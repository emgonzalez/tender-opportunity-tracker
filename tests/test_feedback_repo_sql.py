"""Verifica el SQL de `src.feedback.repo` sin Oracle real (mismo estilo que test_repo_sql.py)."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.feedback import repo


@pytest.fixture
def cursor():
    c = MagicMock()
    c.fetchone.return_value = None
    return c


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


def _opp(**kw):
    base = dict(
        numero_referencia="96-2026-P0011", institucion="Ministerio X", descripcion="Firewalls",
        categoria_detectada="ciberseguridad", match_tecnico=0.9, match_categoria=0.8,
        factibilidad=0.7, tags=["Firewall"], url_directa="https://comprasal/1",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sql_y_binds(cursor):
    args = cursor.execute.call_args[0]
    return args[0], args[1]


class TestReportada:
    def test_merge_con_binds_y_sin_valores_interpolados(self, conn, cursor):
        repo.upsert_reportada(conn, "multicomputos", _opp(descripcion="x'; DROP TABLE t; --"))
        sql, binds = _sql_y_binds(cursor)

        assert "MERGE INTO licitaciones_reportadas" in sql
        assert "DROP TABLE" not in sql
        assert binds["titulo"] == "x'; DROP TABLE t; --"
        assert json.loads(binds["tags"]) == ["Firewall"]
        cursor.close.assert_called_once()

    def test_recorta_a_las_columnas(self, conn, cursor):
        repo.upsert_reportada(conn, "c", _opp(descripcion="a" * 5000, institucion="b" * 900))
        _, binds = _sql_y_binds(cursor)
        assert len(binds["titulo"]) == 1000 and len(binds["institucion"]) == 400

    def test_reenvio_no_toca_primer_envio(self, conn, cursor):
        repo.upsert_reportada(conn, "c", _opp())
        sql, _ = _sql_y_binds(cursor)
        actualiza = sql.split("WHEN MATCHED")[1].split("WHEN NOT MATCHED")[0]
        assert "primer_envio_en" not in actualiza and "veces_enviada + 1" in actualiza

    def test_lectura(self, conn, cursor):
        cursor.fetchone.return_value = ("Min", "Titulo", "redes", "https://u")
        assert repo.get_reportada(conn, "P-1", "c")["titulo"] == "Titulo"
        assert repo.get_reportada(MagicMock(cursor=MagicMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))), "P", "c") is None


class TestVoto:
    def _voto(self, conn, **kw):
        base = dict(proceso_id="P-1", cliente_id="c", destinatario_id="d", destinatario_email="a@x.com",
                    voto="IRRELEVANTE", motivos=["fuera_de_rubro"], comentario="nota")
        base.update(kw)
        repo.upsert_voto(conn, **base)

    def test_la_clave_incluye_al_destinatario(self, conn, cursor):
        self._voto(conn)
        sql, binds = _sql_y_binds(cursor)
        assert "tgt.destinatario_id = src.destinatario_id" in sql
        assert binds["motivos"] == '["fuera_de_rubro"]'

    def test_relevante_limpia_motivos_y_comentario(self, conn, cursor):
        self._voto(conn, voto="RELEVANTE")
        _, binds = _sql_y_binds(cursor)
        assert binds["motivos"] == "[]" and binds["comentario"] is None

    def test_voto_invalido(self, conn):
        with pytest.raises(ValueError):
            self._voto(conn, voto="TAL VEZ")

    def test_lee_motivos_venga_como_texto_o_lista(self, conn, cursor):
        cursor.fetchone.return_value = ("IRRELEVANTE", '["otro"]', "n")
        assert repo.get_voto(conn, "P", "c", "d")["motivos"] == ["otro"]
        cursor.fetchone.return_value = ("IRRELEVANTE", ["otro"], None)
        assert repo.get_voto(conn, "P", "c", "d") == {"voto": "IRRELEVANTE", "motivos": ["otro"], "comentario": ""}
