"""Tests de src.mercado.identidad — sin red.

`ID_PROVEEDOR_MULTICOMPUTOS` es un hecho verificado en vivo (2026-09-27,
contra `/api/v1/proveedores` con sesión real), no un valor inventado: estos
tests fijan el contrato de `es_nuestro`, no el número en sí.
"""
from src.mercado.identidad import ID_PROVEEDOR_MULTICOMPUTOS, es_nuestro, id_proveedor_de


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
