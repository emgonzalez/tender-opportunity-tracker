"""Tests de src.memoria.backfill_v2 — orquestación del backfill (§3.3 Fase 3).

No usa Oracle real: cada test monkeypatchea los escritores v2 (ya probados
contra Oracle real en tests/integration/test_oracle_local.py) y verifica que
este módulo lee las filas de v1 y llama a esos escritores con los argumentos
correctos — la garantía de "reutiliza los mismos escritores, no una copia
paralela del SQL".
"""
from unittest.mock import MagicMock

import pytest

from src.memoria import backfill_v2


@pytest.fixture
def mock_cursor():
    c = MagicMock()
    c.fetchall = MagicMock(return_value=[])
    return c


@pytest.fixture
def mock_conn(mock_cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=mock_cursor)
    return conn


class TestBackfillProcesos:
    def test_llama_upsert_y_set_estado_por_fila(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [
            ("REF-001", "cli_a", "Ministerio X", "Título", "infraestructura", "Licitación",
             1000.0, "2026-01-01", "2026-02-01", "https://u", "ANALIZADO", "GO", 0.9, "NATIVO", None),
        ]
        upsert = MagicMock()
        set_estado = MagicMock()
        monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert)
        monkeypatch.setattr("src.memoria.repo.set_estado_v2", set_estado)

        total = backfill_v2.backfill_procesos(mock_conn)

        assert total == 1
        upsert.assert_called_once()
        args, kwargs = upsert.call_args.args, upsert.call_args.kwargs
        assert args[1].numero_referencia == "REF-001"
        assert args[1].institucion == "Ministerio X"
        assert args[1].monto == 1000.0
        assert args[2] == "cli_a"
        assert kwargs["estado"] == "ANALIZADO"

        set_estado.assert_called_once()
        args, kwargs = set_estado.call_args.args, set_estado.call_args.kwargs
        assert args[1] == "REF-001"
        assert args[2] == "cli_a"
        assert args[3] == "ANALIZADO"
        assert kwargs["triaje"] == "GO"
        assert kwargs["formato"] == "NATIVO"

    def test_una_fila_rota_no_frena_las_demas(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [
            ("REF-ROTA", "cli_a", None, None, None, None, None, None, None, None,
             "DESCUBIERTO", None, None, None, None),
            ("REF-OK", "cli_a", "M", "T", "c", "m", None, None, None, None,
             "DESCUBIERTO", None, None, None, None),
        ]
        llamadas = []
        monkeypatch.setattr(
            "src.memoria.repo.upsert_proceso_v2",
            lambda conn, ns, cid, **kw: (
                (_ for _ in ()).throw(RuntimeError("boom")) if ns.numero_referencia == "REF-ROTA"
                else llamadas.append(ns.numero_referencia)
            ),
        )
        monkeypatch.setattr("src.memoria.repo.set_estado_v2", MagicMock())

        total = backfill_v2.backfill_procesos(mock_conn)

        assert total == 1, "la fila rota no debe contarse, pero tampoco debe frenar a la siguiente"
        assert llamadas == ["REF-OK"]

    def test_sin_filas_devuelve_cero(self, mock_conn, mock_cursor):
        assert backfill_v2.backfill_procesos(mock_conn) == 0


class TestBackfillClasificacion:
    def _jsonl(self, tmp_path, cliente, procesos):
        import json
        from datetime import datetime

        from src.history import _serialize
        from src.scraper.models import MarcaRecomendada, ProcesoClasificado

        d = tmp_path / "history"
        d.mkdir()
        objs = [
            ProcesoClasificado(
                institucion="M", descripcion="D", fecha_publicacion=datetime(2026, 1, 1),
                url_directa="https://u", numero_referencia=ref, es_relevante=True,
                categoria_detectada="infraestructura", tags=["Servidores"],
                marcas_recomendadas=[MarcaRecomendada(marca="HPE", razon="r")],
            )
            for ref in procesos
        ]
        linea = {"procesos": [_serialize(o) for o in objs]}
        (d / f"{cliente}.jsonl").write_text(json.dumps(linea) + "\n")

    def test_reaplica_tags_a_los_existentes_y_no_crea_ausentes(
        self, mock_conn, mock_cursor, monkeypatch, tmp_path
    ):
        self._jsonl(tmp_path, "cli_a", ["REF-001", "REF-NUEVO"])
        mock_cursor.fetchall.return_value = [("cli_a", "REF-001"), ("cli_a", "REF-SIN-JSONL")]
        upsert = MagicMock()
        monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert)

        total = backfill_v2.backfill_clasificacion(mock_conn, str(tmp_path))

        assert total == 1
        upsert.assert_called_once()
        proceso, cliente = upsert.call_args.args[1], upsert.call_args.args[2]
        assert proceso.numero_referencia == "REF-001"
        assert proceso.tags == ["Servidores"]
        assert proceso.categoria_detectada == "infraestructura"
        assert cliente == "cli_a"

    def test_sin_filas_no_escribe(self, mock_conn, monkeypatch, tmp_path):
        upsert = MagicMock()
        monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert)
        assert backfill_v2.backfill_clasificacion(mock_conn, str(tmp_path)) == 0
        upsert.assert_not_called()


class TestBackfillAnalisis:
    def test_toma_cliente_id_de_metadata_via_join(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [
            ("REF-001", {"items": [], "resumen": "resumen x"}, "modelo-x", "cli_a", 500.0),
        ]
        save_analisis = MagicMock()
        monkeypatch.setattr("src.memoria.repo.save_analisis_v2", save_analisis)

        total = backfill_v2.backfill_analisis(mock_conn)

        assert total == 1
        args = save_analisis.call_args.args
        assert args[1].proceso_uuid == "REF-001"
        assert args[1].matriz.resumen == "resumen x"
        assert args[1].modelo_analisis == "modelo-x"
        assert args[1].monto_estimado == 500.0
        assert args[2] == "cli_a"

        sql = mock_cursor.execute.call_args[0][0]
        assert "JOIN licitaciones_metadata" in sql

    def test_matriz_riesgos_como_texto_se_parsea(self, mock_conn, mock_cursor, monkeypatch):
        import json as _json

        mock_cursor.fetchall.return_value = [
            ("REF-001", _json.dumps({"items": [], "resumen": "r"}), "m", "cli_a", None),
        ]
        save_analisis = MagicMock()
        monkeypatch.setattr("src.memoria.repo.save_analisis_v2", save_analisis)

        backfill_v2.backfill_analisis(mock_conn)

        assert save_analisis.call_args.args[1].matriz.resumen == "r"

    def test_una_fila_rota_no_frena_las_demas(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [
            ("REF-ROTA", None, "m", "cli_a", None),
            ("REF-OK", None, "m", "cli_a", None),
        ]
        llamadas = []
        monkeypatch.setattr(
            "src.memoria.repo.save_analisis_v2",
            lambda conn, r, cid: (
                (_ for _ in ()).throw(RuntimeError("boom")) if r.proceso_uuid == "REF-ROTA"
                else llamadas.append(r.proceso_uuid)
            ),
        )
        total = backfill_v2.backfill_analisis(mock_conn)
        assert total == 1
        assert llamadas == ["REF-OK"]


class TestBackfillChunks:
    def test_agrupa_por_proceso_y_copia(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [
            ("REF-001", 0, 1, "texto 0", "emb0"),
            ("REF-001", 1, 1, "texto 1", "emb1"),
            ("REF-002", 0, None, "texto x", "embx"),
        ]
        copiar = MagicMock(side_effect=lambda conn, pid, chunks: len(chunks))
        monkeypatch.setattr("src.memoria.vectorizer.copiar_chunks_v2", copiar)

        total = backfill_v2.backfill_chunks(mock_conn)

        assert total == 3
        assert copiar.call_count == 2
        llamadas = {c.args[1]: c.args[2] for c in copiar.call_args_list}
        assert llamadas["REF-001"] == [(0, 1, "texto 0", "emb0"), (1, 1, "texto 1", "emb1")]
        assert llamadas["REF-002"] == [(0, None, "texto x", "embx")]

    def test_sin_chunks_devuelve_cero(self, mock_conn, mock_cursor):
        assert backfill_v2.backfill_chunks(mock_conn) == 0


class TestBackfillReportadas:
    def test_llama_upsert_reportada_v2_por_fila(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [("REF-001", "cli_a"), ("REF-002", "cli_b")]
        upsert = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_reportada_v2", upsert)

        total = backfill_v2.backfill_reportadas(mock_conn)

        assert total == 2
        primeras = {c.args[1]: c.args[2].numero_referencia for c in upsert.call_args_list}
        assert primeras == {"cli_a": "REF-001", "cli_b": "REF-002"}


class TestBackfillFeedback:
    def test_llama_upsert_voto_v2_con_motivos_como_lista(self, mock_conn, mock_cursor, monkeypatch):
        import json as _json

        mock_cursor.fetchall.return_value = [
            ("REF-001", "cli_a", "dest1", "IRRELEVANTE", _json.dumps(["fuera_de_rubro"]), "nota"),
        ]
        upsert = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_voto_v2", upsert)

        total = backfill_v2.backfill_feedback(mock_conn)

        assert total == 1
        kwargs = upsert.call_args.kwargs
        assert kwargs["proceso_id"] == "REF-001"
        assert kwargs["cliente_id"] == "cli_a"
        assert kwargs["destinatario_id"] == "dest1"
        assert kwargs["motivos"] == ["fuera_de_rubro"]
        assert kwargs["comentario"] == "nota"

    def test_voto_invalido_no_frena_las_demas(self, mock_conn, mock_cursor):
        """Usa el `upsert_voto_v2` REAL (no mockeado): la fila con voto
        desconocido dispara su `ValueError` genuino, y debe saltarse sin
        frenar la siguiente."""
        mock_cursor.fetchall.return_value = [
            ("REF-ROTA", "cli_a", "d1", "TAL_VEZ", "[]", ""),
            ("REF-OK", "cli_a", "d2", "RELEVANTE", "[]", ""),
        ]
        total = backfill_v2.backfill_feedback(mock_conn)
        assert total == 1, "la fila con voto inválido (ValueError real) debe saltarse, no frenar"


class TestBackfillOportunidades:
    def test_una_llamada_por_cliente_distinto(self, mock_conn, mock_cursor, monkeypatch):
        mock_cursor.fetchall.return_value = [("cli_a",), ("cli_b",)]
        prepoblar = MagicMock(return_value=3)
        monkeypatch.setattr("src.mercado.repo.prepoblar_oportunidad_estado_v2", prepoblar)

        total = backfill_v2.backfill_oportunidades(mock_conn)

        assert total == 6
        clientes_llamados = {c.args[1] for c in prepoblar.call_args_list}
        assert clientes_llamados == {"cli_a", "cli_b"}


class TestBackfillV2Orquestacion:
    def test_dry_run_no_escribe_nada(self, monkeypatch):
        import types

        conteos_fetchone = iter([(10,), (3,), (7,)])
        cursor = MagicMock()
        cursor.fetchone.side_effect = lambda: next(conteos_fetchone)

        class _Conexion:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return cursor

        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _Conexion())
        pasos = ["backfill_procesos", "backfill_clasificacion", "backfill_analisis", "backfill_chunks",
                 "backfill_reportadas", "backfill_feedback", "backfill_oportunidades"]
        for paso in pasos:
            monkeypatch.setattr(
                backfill_v2, paso, MagicMock(side_effect=AssertionError("no debe llamarse"))
            )

        resultado = backfill_v2.backfill_v2(types.SimpleNamespace(), dry_run=True)

        assert resultado == {"procesos": 10, "analisis": 3, "chunks": 7}

    def test_corre_los_pasos_en_orden_y_comitea_por_paso(self, monkeypatch):
        import types

        orden = []
        commits = []

        class _Conexion:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def commit(self):
                commits.append(len(orden))

        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _Conexion())

        pasos = ["backfill_procesos", "backfill_clasificacion", "backfill_analisis", "backfill_chunks",
                 "backfill_reportadas", "backfill_feedback", "backfill_oportunidades"]
        for paso in pasos:
            monkeypatch.setattr(
                backfill_v2, paso,
                lambda conn, *a, _paso=paso: (orden.append(_paso), 1)[1],
            )

        resultado = backfill_v2.backfill_v2(types.SimpleNamespace(data_dir="/x"))

        assert orden == pasos, "cada paso debe correr en el orden que exigen las FK de v2"
        assert len(commits) == len(pasos), "cada paso comitea por separado"
        assert resultado == {
            "procesos": 1, "clasificacion": 1, "analisis": 1, "chunks": 1,
            "reportadas": 1, "feedback": 1, "oportunidades": 1,
        }


class TestVerificarConteos:
    def test_compara_cada_par_v1_v2(self, mock_conn, mock_cursor):
        conteos = iter([10, 10, 5, 5, 3, 2, 1, 1, 4, 4, 2, 0])
        mock_cursor.fetchone = MagicMock(side_effect=lambda: (next(conteos),))

        resultado = backfill_v2.verificar_conteos(mock_conn)

        assert resultado["proceso"] == (10, 10)
        assert resultado["cliente_proceso"] == (5, 5)
        assert resultado["analisis"] == (3, 2)
        assert resultado["chunk"] == (1, 1)
        assert resultado["reportada"] == (4, 4)
        assert resultado["feedback"] == (2, 0)
