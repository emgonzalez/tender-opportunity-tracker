"""Cassettes sintéticos: las marcas exigen términos completos y citas comprobables."""
import pytest

from src.classifier.marcas import (
    detectar_por_diccionario,
    marcas_ajenas_en,
    normalizar,
    validar_evidencia,
)


@pytest.fixture
def terminos():
    return [{"marca_id": marca, "termino": texto, "tipo": tipo, "estado": "semilla", "activo": True}
            for marca, texto, tipo in [
                ("oracle", "Oracle", "fuerte"), ("oracle", "Sun", "fuerte"),
                ("oracle", "Crystal Ball", "fuerte"),
                ("oracle", "migración de Oracle a", "excluye"),
                ("ibm", "Cognos", "ajena"), ("sap", "Crystal Reports", "ajena"),
            ]]


@pytest.mark.parametrize(("texto", "esperado"), [
    ("Renovación y soporte de licencias COGNOS", "DESCARTADA"),
    ("Renovación licenciamiento de productos Oracle", "CONFIRMADA"),
    ("servidores Sun V880", "CONFIRMADA"),
    ("migración de Oracle a PostgreSQL", "DUDOSA"),
    ("Crystal Ball", "CONFIRMADA"), ("Crystal Reports", "DESCARTADA"),
    ("Crystal", "DUDOSA"), ("oraculo", "DUDOSA"), ("OracleCloud", "DUDOSA"),
    ("nóminas", "DUDOSA"), ("Claude Pro", "DUDOSA"), ("ArcGIS", "DUDOSA"),
])
def test_diccionario(texto, esperado, terminos):
    assert detectar_por_diccionario(texto, terminos, "oracle") == esperado


def test_citas(terminos):
    fuentes = {"titulo": "Renovación de ORACLE", "institucion": "Entidad de ejemplo"}
    assert validar_evidencia("renovacion de Oracle", fuentes, terminos)
    assert not validar_evidencia("Oracle", {"titulo": "nóminas"}, terminos)
    assert not validar_evidencia("Renovación", fuentes, terminos)
    assert not validar_evidencia("", fuentes, terminos)
    assert not validar_evidencia("Oracle", {"titulo": "Ora", "institucion": "cle"}, terminos)


def test_estado_y_ajenas(terminos):
    assert marcas_ajenas_en("Licencias Cognos", terminos)[0]["marca"] == "ibm"
    for estado, activo in [("propuesto", True), ("rechazado", True), ("activo", False)]:
        assert detectar_por_diccionario("Oracle", [
            {"marca_id": "oracle", "termino": "Oracle", "tipo": "fuerte", "estado": estado, "activo": activo}
        ], "oracle") == "DUDOSA"


def test_normalizacion():
    assert normalizar("  RENOVACIÓN\nOracle ") == "renovacion oracle"
