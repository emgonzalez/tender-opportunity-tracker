"""E14 — restricción por marca del alcance (roles Oracle) y su cláusula SQL única."""
from __future__ import annotations

import pytest

from src.identidad import alcance, politica, repo
from src.memoria import relevancia

ORACLE = ("gerente_marca_oracle", "gerente_regional_comercial_oracle", "director_servicios_oracle")
ACTIVOS = ["sv", "gt", "hn"]


@pytest.fixture
def mundo(monkeypatch):
    datos = {"roles_org": [], "membresias": []}
    monkeypatch.setattr(repo, "obtener_usuario", lambda c, u: {"usuario_id": u, "org_id": "org", "email": "x@y.z",
                                                               "activo": True})
    monkeypatch.setattr(repo, "roles_org", lambda c, u: datos["roles_org"])
    monkeypatch.setattr(repo, "membresias_pais", lambda c, u: datos["membresias"])
    monkeypatch.setattr(repo, "clientes_activos", lambda c, o: ACTIVOS)
    return datos


@pytest.mark.parametrize("rol", ORACLE)
def test_rol_oracle_queda_restringido_a_oracle(mundo, rol):
    mundo["roles_org"] = [rol]
    a = alcance.resolver_alcance(None, "u1")
    assert a.solo_marcas == {"oracle"}
    assert a.clientes == set(ACTIVOS) and not a.cambia_estado and not a.ve_operativo


@pytest.mark.parametrize("otro", ["ceo", "director_estrategia", "director_comercial", "admin_organizacion"])
def test_gana_el_permisivo_si_tiene_otro_rol_de_negocio(mundo, otro):
    mundo["roles_org"] = ["gerente_marca_oracle", otro]
    assert alcance.resolver_alcance(None, "u1").solo_marcas == frozenset()


def test_membresia_de_pais_tambien_levanta_la_restriccion(mundo):
    mundo["roles_org"] = ["director_servicios_oracle"]
    mundo["membresias"] = [("sv", "gerente_pais")]
    assert alcance.resolver_alcance(None, "u1").solo_marcas == frozenset()


def test_un_rol_tecnico_no_levanta_la_restriccion(mundo):
    mundo["roles_org"] = ["gerente_marca_oracle", "operador"]
    assert alcance.resolver_alcance(None, "u1").solo_marcas == {"oracle"}


@pytest.mark.parametrize("rol", ["ceo", "gerente_pais", "equipo_ventas", "director_comercial"])
def test_los_demas_roles_no_tienen_restriccion(mundo, rol):
    if rol in politica.ROLES_PAIS:
        mundo["membresias"] = [("sv", rol)]
    else:
        mundo["roles_org"] = [rol]
    assert alcance.resolver_alcance(None, "u1").solo_marcas == frozenset()


@pytest.mark.parametrize("rol", ORACLE)
def test_ver_como_hereda_la_restriccion(mundo, rol):
    admin = alcance.Alcance(usuario_id="a", org_id="org", roles=frozenset({"admin_plataforma"}),
                            clientes=frozenset(), cambia_estado=frozenset(), ve_operativo=True)
    sim = alcance.alcance_simulado(None, admin, rol)
    mundo["roles_org"] = [rol]
    real = alcance.resolver_alcance(None, "u1")
    assert sim.simulado == rol and sim.solo_marcas == real.solo_marcas == {"oracle"}
    assert (sim.clientes, sim.cambia_estado, sim.ve_operativo) == (real.clientes, real.cambia_estado, real.ve_operativo)


def test_los_roles_restringidos_son_de_la_politica():
    assert set(politica.ROLES_SOLO_MARCA) == set(ORACLE) <= politica.ROLES_ORG_NEGOCIO
    assert set(politica.ROLES_SOLO_MARCA.values()) <= set(relevancia.MARCAS_RESTRINGIBLES)


class TestSqlMarca:
    def test_sin_restriccion_no_filtra(self):
        assert relevancia.sql_marca(frozenset()) == "1 = 1"

    def test_oracle_mira_marcas_y_titulo_con_el_alias_pedido(self):
        sql = relevancia.sql_marca(frozenset({"oracle"}), alias="x")
        assert "JSON_TABLE(x.marcas" in sql and "pm.proceso_gid = x.proceso_gid" in sql
        assert "'^oracle'" in sql and "oracle([^[:alnum:]_]|$)" in sql and "cp." not in sql

    def test_marca_fuera_de_la_lista_cerrada_se_rechaza(self):
        with pytest.raises(ValueError):
            relevancia.sql_marca(frozenset({"oracle'; DROP TABLE x--"}))
