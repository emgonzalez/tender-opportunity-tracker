"""Tests para src.memoria.rag — verificación de SQL (sin usar Oracle real).

Modelado sobre tests/test_repo_sql.py: valida que las funciones emiten SQL
seguro (bindings, no interpolación de valores) y con las palabras clave
esperadas — no contra Oracle real (eso lo cubre
tests/integration/test_oracle_local.py).

Hasta la Fase 4 (switch del esquema v2) no existía ningún test unitario
mockeado de este módulo, solo integración — este archivo lo cierra.
"""
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_cursor():
    cursor = MagicMock()
    cursor.fetchone = MagicMock(return_value=None)
    cursor.fetchall = MagicMock(return_value=[])
    cursor.rowcount = 1
    return cursor


@pytest.fixture
def mock_conn(mock_cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=mock_cursor)
    return conn


@pytest.fixture
def cfg():
    return types.SimpleNamespace(embedding_model_name="MULTILINGUAL_E5_SMALL", precedentes_max=3)


class TestBuscarChunksV2:
    """Espejo v2 de buscar_chunks (plan del portal §3.7 punto 4)."""

    def test_emite_select_a_chunk_con_proceso_gid(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        rag.buscar_chunks_v2(mock_conn, "REF-001", "pregunta", cfg)

        sql, binds = mock_cursor.execute.call_args[0]
        assert "FROM   chunk" in sql or "FROM chunk" in sql
        assert "licitaciones_chunks" not in sql
        assert binds["pgid"] == "comprasal_sv:REF-001"

    def test_preserva_allowlist_de_modelo(self, mock_conn, mock_cursor):
        from src.memoria import rag

        cfg_malicioso = types.SimpleNamespace(
            embedding_model_name="X); DROP TABLE chunk; --",
        )
        with pytest.raises(ValueError):
            rag.buscar_chunks_v2(mock_conn, "REF-001", "pregunta", cfg_malicioso)

    def test_devuelve_textos_de_los_chunks(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        mock_cursor.fetchall = MagicMock(return_value=[("chunk uno",), ("chunk dos",)])

        resultado = rag.buscar_chunks_v2(mock_conn, "REF-001", "pregunta", cfg)

        assert resultado == ["chunk uno", "chunk dos"]

    def test_anti_injection_en_proceso_id(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        malicious_ref = "REF-001'; DROP TABLE chunk; --"
        rag.buscar_chunks_v2(mock_conn, malicious_ref, "pregunta", cfg)

        sql, binds = mock_cursor.execute.call_args[0]
        assert malicious_ref not in sql, f"SQL injection detectada en: {sql}"
        assert binds["pgid"] == f"comprasal_sv:{malicious_ref}"


class TestBuscarPrecedentesV2:
    """Espejo v2 de buscar_precedentes (plan del portal §3.7 punto 4)."""

    def test_emite_join_por_cliente_proceso_no_licitaciones_metadata(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        rag.buscar_precedentes_v2(mock_conn, "pregunta", cfg, cliente_id="client_1")

        sql, binds = mock_cursor.execute.call_args[0]
        assert "FROM   chunk c" in sql or "FROM chunk c" in sql
        assert "JOIN   cliente_proceso cp" in sql or "JOIN cliente_proceso cp" in sql
        assert "cp.cliente_id = :cliente_id" in sql
        assert "licitaciones_chunks" not in sql
        assert "licitaciones_metadata" not in sql
        assert binds["cliente_id"] == "client_1"

    def test_condiciones_opcionales_usan_bind_no_concatenacion(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        rag.buscar_precedentes_v2(
            mock_conn, "pregunta", cfg, cliente_id="client_1",
            excluir_proceso_id="REF-001", institucion="Ministerio X",
        )

        sql, binds = mock_cursor.execute.call_args[0]
        assert "p.numero_referencia <> :excluir" in sql
        assert "UPPER(p.institucion) LIKE UPPER(:institucion)" in sql
        assert binds["excluir"] == "REF-001"
        assert binds["institucion"] == "Ministerio X"

    def test_preserva_allowlist_de_modelo(self, mock_conn, mock_cursor):
        from src.memoria import rag

        cfg_malicioso = types.SimpleNamespace(
            embedding_model_name="X USING (SELECT 1 FROM dual) AS data; DROP TABLE chunk; --",
        )
        with pytest.raises(ValueError):
            rag.buscar_precedentes_v2(mock_conn, "pregunta", cfg_malicioso, cliente_id="client_1")

    def test_anti_injection_en_parametros_opcionales(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        malicious_ref = "REF-001'; DROP TABLE chunk; --"
        rag.buscar_precedentes_v2(
            mock_conn, "pregunta", cfg, cliente_id="client_1", excluir_proceso_id=malicious_ref,
        )

        sql, binds = mock_cursor.execute.call_args[0]
        assert malicious_ref not in sql, f"SQL injection detectada en: {sql}"
        assert binds["excluir"] == malicious_ref

    def test_dict_de_retorno_mismas_claves_que_v1(self, mock_conn, mock_cursor, cfg):
        """Fija el contrato de forma: service.obtener_precedentes no debe
        tener que cambiar su mapeo a Precedente según qué versión respondió."""
        from datetime import datetime

        from src.memoria import rag

        mock_cursor.fetchall = MagicMock(return_value=[
            ("REF-001", "Ministerio X", "Título", datetime(2026, 1, 1), "https://comprasal/x",
             "texto del chunk", 0.12),
        ])

        resultado = rag.buscar_precedentes_v2(mock_conn, "pregunta", cfg, cliente_id="client_1")

        assert len(resultado) == 1
        claves_v2 = set(resultado[0].keys())
        claves_v1 = {"proceso_id", "institucion", "titulo", "fecha_pub", "url_comprasal",
                     "chunk_text", "distancia"}
        assert claves_v2 == claves_v1
        assert resultado[0]["proceso_id"] == "REF-001"
        assert resultado[0]["url_comprasal"] == "https://comprasal/x"
        assert resultado[0]["chunk_text"] == "texto del chunk"

    def test_dedup_se_queda_con_la_menor_distancia_por_proceso(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        mock_cursor.fetchall = MagicMock(return_value=[
            ("REF-001", "Inst", "T1", None, "url1", "chunk peor", 0.50),
            ("REF-001", "Inst", "T1", None, "url1", "chunk mejor", 0.10),
            ("REF-002", "Inst", "T2", None, "url2", "otro chunk", 0.30),
        ])

        resultado = rag.buscar_precedentes_v2(mock_conn, "pregunta", cfg, cliente_id="client_1")

        assert len(resultado) == 2
        por_proceso = {r["proceso_id"]: r for r in resultado}
        assert por_proceso["REF-001"]["chunk_text"] == "chunk mejor"
        assert por_proceso["REF-001"]["distancia"] == 0.10
        # ordenado por distancia ascendente
        assert resultado[0]["proceso_id"] == "REF-001"

    def test_distancia_max_descarta_precedentes_lejanos(self, mock_conn, mock_cursor, cfg):
        from src.memoria import rag

        mock_cursor.fetchall = MagicMock(return_value=[
            ("REF-001", "Inst", "T1", None, "url1", "cerca", 0.10),
            ("REF-002", "Inst", "T2", None, "url2", "lejos", 0.90),
        ])

        resultado = rag.buscar_precedentes_v2(
            mock_conn, "pregunta", cfg, cliente_id="client_1", distancia_max=0.5,
        )

        assert len(resultado) == 1
        assert resultado[0]["proceso_id"] == "REF-001"


class TestTendenciasV2:
    """Espejo v2 de tendencias — requiere cliente_id (cambio de firma
    deliberado, ver docstring de rag.tendencias_v2)."""

    def test_emite_join_por_cliente_proceso_y_filtra_por_cliente(self, mock_conn, mock_cursor):
        from src.memoria import rag

        rag.tendencias_v2(mock_conn, "client_1", dias=30)

        sql, binds = mock_cursor.execute.call_args[0]
        assert "cliente_proceso" in sql
        assert "cp.cliente_id = :cliente_id" in sql
        assert "licitaciones_metadata" not in sql
        assert binds["cliente_id"] == "client_1"
        assert binds["dias"] == 30

    def test_mismas_claves_de_retorno_que_v1(self, mock_conn, mock_cursor):
        from src.memoria import rag

        mock_cursor.fetchall = MagicMock(return_value=[
            ("Ministerio X", "infraestructura", 5, 2),
        ])

        resultado = rag.tendencias_v2(mock_conn, "client_1")

        assert resultado == [{
            "institucion": "Ministerio X",
            "categoria": "infraestructura",
            "conteo": 5,
            "conteo_go": 2,
        }]

    def test_conteo_go_none_se_normaliza_a_cero(self, mock_conn, mock_cursor):
        from src.memoria import rag

        mock_cursor.fetchall = MagicMock(return_value=[("Ministerio X", "infra", 3, None)])

        resultado = rag.tendencias_v2(mock_conn, "client_1")

        assert resultado[0]["conteo_go"] == 0
