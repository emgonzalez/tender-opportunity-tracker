"""Tests para src.mercado.repo — verificación de SQL (sin usar Oracle real).

Mismo patrón que tests/test_repo_sql.py: cursor/conexión mockeados, se
inspecciona el SQL emitido y los binds, no el resultado contra un motor real
(eso lo cubre tests/integration/test_oracle_local.py).
"""
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.mercado import repo


@pytest.fixture
def mock_cursor():
    cursor = MagicMock()
    cursor.fetchone = MagicMock(return_value=None)
    cursor.rowcount = 1
    return cursor


@pytest.fixture
def mock_conn(mock_cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=mock_cursor)
    return conn


def _adjudicacion_item(**overrides):
    base = {
        "id": 504032,
        "monto": 79.2,
        "proceso_compra": {
            "id": 138001,
            "nombre_proceso": "SUMINISTRO DE MEDICAMENTOS",
            "codigo_proceso": "300-2026-P0168",
            "fecha_adjudicacion": "2026-09-25",
        },
        "institucion": {"id": 25, "codigo": "300", "nombre": "Tribunal Supremo Electoral (TSE)"},
        "proveedor": {
            "id": 691, "id_proveedor": 691,
            "nombre": "SERVICIOS TECNICOS MEDICOS, S.A. DE C.V.",
            "nombre_comercial": "S.T.MEDIC, S.A. DE C.V.",
        },
    }
    base.update(overrides)
    return base


class TestParseFecha:
    def test_fecha_corta(self):
        assert repo._parse_fecha("2026-09-25") == date(2026, 9, 25)

    def test_fecha_iso_con_hora_y_z(self):
        assert repo._parse_fecha("2026-10-07T18:00:00.000Z") == date(2026, 10, 7)

    def test_none_y_vacio(self):
        assert repo._parse_fecha(None) is None
        assert repo._parse_fecha("") is None

    def test_formato_invalido_no_revienta(self):
        assert repo._parse_fecha("no-es-una-fecha") is None

    def test_date_ya_parseado_pasa_igual(self):
        d = date(2026, 1, 1)
        assert repo._parse_fecha(d) == d


class TestUpsertAdjudicacionItem:
    def test_emite_merge_con_los_campos_mapeados(self, mock_conn, mock_cursor):
        repo.upsert_adjudicacion_item(mock_conn, _adjudicacion_item(), es_multicomputos=False)

        assert mock_cursor.execute.called
        sql, binds = mock_cursor.execute.call_args[0]
        assert "MERGE INTO adjudicacion_item" in sql
        assert binds["id"] == 504032
        assert binds["codigo_proceso"] == "300-2026-P0168"
        assert binds["nombre_proceso"] == "SUMINISTRO DE MEDICAMENTOS"
        assert binds["id_institucion"] == 25
        assert binds["institucion"] == "Tribunal Supremo Electoral (TSE)"
        assert binds["fecha_adjudicacion"] == date(2026, 9, 25)
        assert binds["monto"] == 79.2
        assert binds["id_proveedor"] == 691
        assert binds["proveedor_nombre"] == "SERVICIOS TECNICOS MEDICOS, S.A. DE C.V."
        assert binds["es_multicomputos"] == 0
        assert '"proceso_compra"' in binds["datos_crudos"]  # el crudo completo viaja como JSON

    def test_es_multicomputos_true_se_traduce_a_1(self, mock_conn, mock_cursor):
        repo.upsert_adjudicacion_item(mock_conn, _adjudicacion_item(), es_multicomputos=True)
        _, binds = mock_cursor.execute.call_args[0]
        assert binds["es_multicomputos"] == 1

    def test_campos_faltantes_no_revientan(self, mock_conn, mock_cursor):
        item = {"id": 1}  # sin proceso_compra/institucion/proveedor
        repo.upsert_adjudicacion_item(mock_conn, item, es_multicomputos=False)
        _, binds = mock_cursor.execute.call_args[0]
        assert binds["codigo_proceso"] == ""
        assert binds["id_institucion"] is None
        assert binds["id_proveedor"] is None

    def test_nunca_interpola_valores_en_el_sql(self, mock_conn, mock_cursor):
        """Regla de seguridad innegociable del repo: todo por bind, nunca en el texto."""
        item = _adjudicacion_item()
        item["proceso_compra"]["codigo_proceso"] = "'; DROP TABLE x; --"
        repo.upsert_adjudicacion_item(mock_conn, item, es_multicomputos=False)
        sql, binds = mock_cursor.execute.call_args[0]
        assert "DROP TABLE" not in sql
        assert binds["codigo_proceso"] == "'; DROP TABLE x; --"


class TestUpsertProcesoParticipado:
    def test_emite_merge_y_nunca_toca_resultado_en_el_update(self, mock_conn, mock_cursor):
        item = {"codigo": "300-2026-P0168", "nombre": "X", "institucion": "TSE",
                "metodo": "Contratación directa", "fecha": "2026-09-01"}
        repo.upsert_proceso_participado(mock_conn, "multicomputos", item)

        sql, binds = mock_cursor.execute.call_args[0]
        assert "MERGE INTO proceso_participado" in sql
        assert binds["cliente_id"] == "multicomputos"
        assert binds["codigo_proceso"] == "300-2026-P0168"
        assert binds["fecha_publicacion"] == date(2026, 9, 1)
        # `resultado` solo aparece en la rama INSERT (con 'PENDIENTE' fijo), no en el UPDATE.
        rama_update = sql.split("WHEN MATCHED")[1].split("WHEN NOT MATCHED")[0]
        assert "resultado" not in rama_update.lower()


class TestResolverResultados:
    def test_ejecuta_dos_updates_y_devuelve_conteos(self, mock_conn, mock_cursor):
        mock_cursor.rowcount = 3
        resultado = repo.resolver_resultados(mock_conn, "multicomputos")

        assert mock_cursor.execute.call_count == 2
        primer_sql = mock_cursor.execute.call_args_list[0][0][0]
        segundo_sql = mock_cursor.execute.call_args_list[1][0][0]
        assert "'GANADO'" in primer_sql
        assert "'PERDIDO'" in segundo_sql
        assert resultado == {"ganados": 3, "perdidos": 3}


class TestContarPendientes:
    def test_devuelve_el_conteo_del_fetchone(self, mock_conn, mock_cursor):
        mock_cursor.fetchone = MagicMock(return_value=(7,))
        assert repo.contar_pendientes(mock_conn, "multicomputos") == 7
        sql, binds = mock_cursor.execute.call_args[0]
        assert "PENDIENTE" in sql
        assert binds["cliente_id"] == "multicomputos"
