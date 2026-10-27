"""E11: `identidad.repo.usuarios_asignables` — SQL, binds y nombre, contra un cursor falso.

La regla de quién cambia estado vive en `alcance._alcance_desde`; acá se verifica que el SQL la
replique: misma org, activo, cliente activo de la org, rol de país con permiso o rol de org con permiso."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.identidad import politica, repo


@pytest.fixture
def cursor():
    c = MagicMock()
    c.fetchall.return_value = []
    return c


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


def _sql_binds(cursor):
    return cursor.execute.call_args[0]


class TestUsuariosAsignables:
    def test_filtra_org_activo_y_cliente_activo_de_la_org(self, conn, cursor):
        repo.usuarios_asignables(conn, "org1", "sv")
        sql, binds = _sql_binds(cursor)
        assert binds["o"] == "org1" and binds["c"] == "sv"
        assert "u.org_id = :o" in sql and "u.activo = 1" in sql
        assert "c.cliente_id = :c" in sql and "c.org_id = u.org_id" in sql and "c.activo = 1" in sql
        conn.commit.assert_not_called()
        cursor.close.assert_called_once()

    def test_roles_viajan_como_binds_y_replican_la_politica(self, conn, cursor):
        repo.usuarios_asignables(conn, "org1", "sv")
        sql, binds = _sql_binds(cursor)
        pais = {v for k, v in binds.items() if k.startswith("rp")}
        org = {v for k, v in binds.items() if k.startswith("ro")}
        assert pais == {"gerente_pais", "equipo_ventas"} == politica.ROLES_CAMBIAN_ESTADO & politica.ROLES_PAIS
        assert org == {"director_comercial"} == politica.ROLES_CAMBIAN_ESTADO & politica.ROLES_ORG_NEGOCIO
        for rol in pais | org:
            assert f"'{rol}'" not in sql  # nunca interpolados
        # Membresía de país en ESE cliente; el rol de org vale para todos los países activos.
        assert "m.cliente_id = c.cliente_id" in sql and "membresia_org" in sql

    def test_roles_sin_permiso_no_entran(self, conn, cursor):
        repo.usuarios_asignables(conn, "org1", "sv")
        _, binds = _sql_binds(cursor)
        for rol in ("ceo", "director_estrategia", "admin_plataforma", "operador", "gerente_marca_oracle"):
            assert rol not in binds.values()

    def test_nombre_cae_a_la_parte_local_del_correo_y_ordena(self, conn, cursor):
        cursor.fetchall.return_value = [
            ("u3", None, "zeta@x.com"),
            ("u1", "Ana", "ana@x.com"),
            ("u2", "  ", "beto@x.com"),
        ]
        assert repo.usuarios_asignables(conn, "org1", "sv") == [
            {"usuario_id": "u1", "nombre": "Ana"},
            {"usuario_id": "u2", "nombre": "beto"},
            {"usuario_id": "u3", "nombre": "zeta"},
        ]

    def test_sin_filas_lista_vacia(self, conn, cursor):
        assert repo.usuarios_asignables(conn, "org1", "sv") == []

    def test_conjunto_vacio_no_rompe_el_sql(self):
        assert repo._binds_en("x", frozenset()) == ("NULL", {})
        assert repo._binds_en("x", frozenset({"b", "a"})) == (":x0, :x1", {"x0": "a", "x1": "b"})


def test_acciones_nuevas_auditables():
    assert {"RESPONSABLE_ASIGNADO", "DESCARTADO_RECUPERADO"} <= politica.ACCIONES_AUDITADAS
