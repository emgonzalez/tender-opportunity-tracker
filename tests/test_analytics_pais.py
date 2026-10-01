"""Tests para src.analytics.pais — verificación de SQL (sin usar Oracle real).

Modelado sobre tests/test_repo_sql.py y tests/test_rag_sql.py: valida que las
funciones emiten SQL seguro (bindings) con las palabras clave esperadas, y que
la forma del dato que devuelven es la que el stat-tile/chart de
docs/planes/portal-amalia.md §3 necesita — no contra Oracle real (eso lo cubre
tests/integration/test_oracle_local.py).
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_cursor():
    cursor = MagicMock()
    cursor.fetchone = MagicMock(return_value=(0, 0))
    cursor.fetchall = MagicMock(return_value=[])
    return cursor


@pytest.fixture
def mock_conn(mock_cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=mock_cursor)
    return conn


class TestOportunidadesDelMes:
    def test_emite_dos_queries_sobre_cliente_proceso_y_proceso(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchone.return_value = (5, 4)
        pais.oportunidades_del_mes(mock_conn, "client_1")

        assert mock_cursor.execute.call_count == 2
        sql_totales, binds_totales = mock_cursor.execute.call_args_list[0][0]
        sql_serie, binds_serie = mock_cursor.execute.call_args_list[1][0]
        assert "cliente_proceso" in sql_totales
        assert "TRUNC(SYSDATE" in sql_totales
        assert binds_totales["cliente_id"] == "client_1"
        assert "GROUP  BY TRUNC(p.fecha_pub_utc)" in sql_serie
        assert binds_serie["cliente_id"] == "client_1"

    def test_relevancia_incluye_fallback_a_estado(self, mock_conn, mock_cursor):
        """es_relevante/triaje están NULL en producción real (verificado
        2026-10-01 contra multicomputos_dev) — sin este fallback el KPI
        muestra un cero falso. Ver docs/planes/portal-amalia.md §3
        Hallazgo 8."""
        from src.analytics import pais

        pais.oportunidades_del_mes(mock_conn, "client_1")

        sql_totales, _ = mock_cursor.execute.call_args_list[0][0]
        assert "cp.estado IN ('ANALIZADO', 'NOTIFICADO')" in sql_totales
        assert "cp.es_relevante = 1" in sql_totales
        assert "cp.triaje = 'GO'" in sql_totales

    def test_calcula_delta_pct_contra_mes_anterior(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchone.return_value = (10, 5)  # mes_actual=10, mes_anterior=5
        mock_cursor.fetchall.return_value = [(1, 3), (2, 7)]

        resultado = pais.oportunidades_del_mes(mock_conn, "client_1")

        assert resultado["valor"] == 10
        assert resultado["delta_pct"] == 100.0  # +100% (de 5 a 10)
        assert resultado["serie"] == [3, 7]

    def test_sin_mes_anterior_delta_es_none_no_cero(self, mock_conn, mock_cursor):
        """Sin base de comparación, delta_pct debe ser None — no 0 (que
        mentiría diciendo "sin cambio") ni una división por cero."""
        from src.analytics import pais

        mock_cursor.fetchone.return_value = (3, 0)

        resultado = pais.oportunidades_del_mes(mock_conn, "client_1")

        assert resultado["valor"] == 3
        assert resultado["delta_pct"] is None

    def test_valores_none_de_oracle_se_normalizan_a_cero(self, mock_conn, mock_cursor):
        """SUM(CASE...) sobre un result set vacío devuelve NULL, no 0."""
        from src.analytics import pais

        mock_cursor.fetchone.return_value = (None, None)

        resultado = pais.oportunidades_del_mes(mock_conn, "client_1")

        assert resultado["valor"] == 0
        assert resultado["delta_pct"] is None


class TestPctRelevanciaClasificador:
    def test_calcula_porcentaje_y_etiqueta_fuente(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchone.return_value = (40, 10)  # total=40, relevantes=10

        resultado = pais.pct_relevancia_clasificador(mock_conn, "client_1")

        assert resultado["valor"] == 25.0
        assert resultado["total"] == 40
        assert resultado["relevantes"] == 10

    def test_sin_procesos_en_el_periodo_valor_es_none(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchone.return_value = (0, 0)

        resultado = pais.pct_relevancia_clasificador(mock_conn, "client_1")

        assert resultado["valor"] is None

    def test_usa_binds_no_concatenacion(self, mock_conn, mock_cursor):
        from src.analytics import pais

        pais.pct_relevancia_clasificador(mock_conn, "client_1", dias=14)

        sql, binds = mock_cursor.execute.call_args[0]
        assert binds["cliente_id"] == "client_1"
        assert binds["dias"] == 14
        assert ":cliente_id" in sql
        assert ":dias" in sql

    def test_relevancia_incluye_fallback_a_estado(self, mock_conn, mock_cursor):
        """Mismo fallback que oportunidades_del_mes — ver ese test."""
        from src.analytics import pais

        pais.pct_relevancia_clasificador(mock_conn, "client_1")

        sql, _ = mock_cursor.execute.call_args[0]
        assert "cp.estado IN ('ANALIZADO', 'NOTIFICADO')" in sql


class TestInstitucionesTop:
    def test_reusa_stats_por_institucion_v2_no_reescribe_sql(self, mock_conn, monkeypatch):
        """No debe emitir SQL propio: delega en repo.stats_por_institucion_v2."""
        from src.analytics import pais

        llamado = MagicMock(return_value=[
            {"institucion": "Ministerio X", "categoria": "infra", "conteo": 5, "pct_go": 50.0},
        ])
        monkeypatch.setattr("src.memoria.repo.stats_por_institucion_v2", llamado)

        pais.instituciones_top(mock_conn, "client_1", dias=30)

        llamado.assert_called_once_with(mock_conn, "client_1", dias=30)

    def test_reagrega_por_institucion_sola_y_corta_al_limite(self, mock_conn, monkeypatch):
        from src.analytics import pais

        filas = [
            {"institucion": "Ministerio X", "categoria": "infra", "conteo": 3, "pct_go": 0},
            {"institucion": "Ministerio X", "categoria": "redes", "conteo": 2, "pct_go": 0},
            {"institucion": "Ministerio Y", "categoria": "infra", "conteo": 10, "pct_go": 0},
            {"institucion": "Ministerio Z", "categoria": "infra", "conteo": 1, "pct_go": 0},
        ]
        monkeypatch.setattr("src.memoria.repo.stats_por_institucion_v2", MagicMock(return_value=filas))

        resultado = pais.instituciones_top(mock_conn, "client_1", limite=2)

        # Ministerio X = 3+2=5, Ministerio Y=10, Ministerio Z=1 -> top 2: Y, X
        assert resultado == [
            {"institucion": "Ministerio Y", "conteo": 10},
            {"institucion": "Ministerio X", "conteo": 5},
        ]


class TestEmbudoPipeline:
    def test_separa_camino_feliz_de_salidas(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchall.return_value = [
            ("ANALIZADO", 36), ("DESCARTADO_TRIAJE", 1),
        ]

        resultado = pais.embudo_pipeline(mock_conn, "client_1")

        estados_camino = {f["estado"] for f in resultado["camino"]}
        assert "ANALIZADO" in estados_camino
        assert "DESCARTADO_TRIAJE" not in estados_camino
        assert resultado["salidas"] == [{"estado": "DESCARTADO_TRIAJE", "conteo": 1}]

    def test_etapas_del_camino_feliz_sin_filas_quedan_en_cero_explicito(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchall.return_value = [("ANALIZADO", 36)]

        resultado = pais.embudo_pipeline(mock_conn, "client_1")

        por_estado = {f["estado"]: f["conteo"] for f in resultado["camino"]}
        assert por_estado["DESCUBIERTO"] == 0
        assert por_estado["ANALIZADO"] == 36

    def test_salidas_con_cero_no_aparecen(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchall.return_value = [("ANALIZADO", 36)]

        resultado = pais.embudo_pipeline(mock_conn, "client_1")

        assert resultado["salidas"] == []


class TestSeveridadRiesgos:
    def test_orden_descendente_alta_media_baja(self, mock_conn, mock_cursor):
        from src.analytics import pais

        mock_cursor.fetchall.return_value = [("baja", 2), ("alta", 10), ("media", 5)]

        resultado = pais.severidad_riesgos(mock_conn, "client_1")

        assert [f["severidad"] for f in resultado] == ["alta", "media", "baja"]
        assert resultado[0]["conteo"] == 10

    def test_filtra_severidad_null(self, mock_conn, mock_cursor):
        from src.analytics import pais

        pais.severidad_riesgos(mock_conn, "client_1")

        sql, _ = mock_cursor.execute.call_args[0]
        assert "severidad_max IS NOT NULL" in sql

    def test_join_cliente_proceso_para_aislar_por_cliente(self, mock_conn, mock_cursor):
        """analisis no tiene filtro de fecha propio — el JOIN a proceso es lo
        que permite acotar por fecha_pub_utc, y a cliente_proceso lo que
        garantiza que el cliente_id en el WHERE es el dueño real de la fila,
        no solo un bind suelto."""
        from src.analytics import pais

        pais.severidad_riesgos(mock_conn, "client_1")

        sql, binds = mock_cursor.execute.call_args[0]
        assert "cliente_proceso cp" in sql
        assert "a.cliente_id = :cliente_id" in sql
        assert binds["cliente_id"] == "client_1"
