"""Tests de src.mercado.identidad — sin red.

`ID_PROVEEDOR_MULTICOMPUTOS` es un hecho verificado en vivo (2026-09-27,
contra `/api/v1/proveedores` con sesión real), no un valor inventado: estos
tests fijan el contrato de `es_nuestro`, no el número en sí.
"""
import pytest

from src.mercado.identidad import (
    ID_PROVEEDOR_MULTICOMPUTOS,
    IDS_PROPIOS,
    es_nuestro,
    es_propio,
    id_proveedor_de,
)


def _item(id_proveedor=None, id_=None):
    proveedor = {}
    if id_proveedor is not None:
        proveedor["id_proveedor"] = id_proveedor
    if id_ is not None:
        proveedor["id"] = id_
    return {"proveedor": proveedor} if proveedor else {"proveedor": {}}


class TestIdProveedorDe:
    def test_usa_id_proveedor_si_esta(self):
        assert id_proveedor_de(_item(id_proveedor=691, id_=691)) == 691

    def test_cae_a_id_si_falta_id_proveedor(self):
        assert id_proveedor_de(_item(id_=691)) == 691

    def test_sin_proveedor_es_none(self):
        assert id_proveedor_de({}) is None

    def test_proveedor_no_es_dict_no_revienta(self):
        assert id_proveedor_de({"proveedor": None}) is None
        assert id_proveedor_de({"proveedor": "algo"}) is None


class TestEsNuestro:
    def test_nuestro_id_da_true(self):
        assert es_nuestro(_item(id_proveedor=ID_PROVEEDOR_MULTICOMPUTOS)) is True

    def test_otro_id_da_false(self):
        assert es_nuestro(_item(id_proveedor=691)) is False

    def test_sin_proveedor_da_false(self):
        assert es_nuestro({}) is False


class TestEsPropio:
    @pytest.mark.parametrize("cedula", ["3102813498", "9000018410"])
    def test_las_dos_cedulas_de_sicop(self, cedula):
        assert es_propio("sicop_cr", cedula) is True

    def test_comprasal_por_texto_y_por_numero(self):
        assert es_propio("comprasal_sv", "20371") is True
        assert es_propio("comprasal_sv", ID_PROVEEDOR_MULTICOMPUTOS) is True

    @pytest.mark.parametrize("valor", [" 3102813498", "3102813498 ", "03102813498", "310281349", "", None, True])
    def test_comparacion_de_texto_exacta(self, valor):
        assert es_propio("sicop_cr", valor) is False

    def test_el_id_es_por_portal(self):
        assert es_propio("comprasal_sv", "3102813498") is False
        assert es_propio("sicop_cr", "20371") is False
        assert es_propio("portal_inexistente", "20371") is False

    def test_mapa_coherente_con_la_constante_de_comprasal(self):
        assert IDS_PROPIOS["comprasal_sv"] == frozenset({str(ID_PROVEEDOR_MULTICOMPUTOS)})
        assert IDS_PROPIOS["sicop_cr"] == frozenset({"3102813498", "9000018410"})
        assert all(isinstance(v, frozenset) for v in IDS_PROPIOS.values())
