"""`normalizar_texto_modelo` y los escapes truncados de `src/llm/parseo.py` (P3).

Los ejemplos salen de 28 análisis reales (2026-10): cada entrada rota es una
cadena observada tal cual en el correo.
"""
from __future__ import annotations

import pytest

from src.llm.parseo import cargar, normalizar_texto_modelo, reparar_escapes

ROTOS = [
    (r"cláusulas espec\u00ificas", "cláusulas específicas"),
    (r"material espec\u00ifico", "material específico"),
    (r"un plazo l\u00imite estricto", "un plazo límite estricto"),
    (r"y pr\u00fácticas anticompetitivas", "y prácticas anticompetitivas"),
    ("cláusulas especßicas.", "cláusulas específicas."),
    ("requisitos especßicos", "requisitos específicos"),
    ("LEY DE COMPRAS PÓBLICAS, en", "LEY DE COMPRAS PÚBLICAS, en"),
    ("LEY DE COMPRAS PºbLICAS.", "LEY DE COMPRAS PÚBLICAS."),
    ("ESPECIFICACIONES TéCNICAS Y", "ESPECIFICACIONES TÉCNICAS Y"),
    ("la hacienda pÿblica", "la hacienda pública"),
    ("Un¡dad Técn¡ca, dispon¡bilidad", "Unidad Técnica, disponibilidad"),
    ("r€cépción del servicio", "recépción del servicio"),
]

CORRECTOS = [
    "Ley de Compras Públicas",
    "LEY DE COMPRAS PÚBLICAS",
    "ESPECIFICACIONES TÉCNICAS Y CONDICIONES",
    "Unidad Técnica, límite, específicas",
    "https://www.comprasal.gob.sv/u00i/pliego_u00fa.pdf",
    "Monto: $1,234.56 (24x7)",
    "¡Atención! ¿Cuánto?",
    "straße",
    "UCP-2026 ONU Ñandú",
    "C:\\temp\\unidad",
    "",
]


@pytest.mark.parametrize("roto,esperado", ROTOS)
def test_corrige_lo_observado(roto, esperado):
    assert normalizar_texto_modelo(roto) == esperado


@pytest.mark.parametrize("roto,esperado", ROTOS)
def test_idempotente(roto, esperado):
    una = normalizar_texto_modelo(roto)
    assert normalizar_texto_modelo(una) == una


@pytest.mark.parametrize("texto", CORRECTOS)
def test_no_toca_texto_correcto(texto):
    assert normalizar_texto_modelo(texto) == texto


def test_no_texto_pasa_igual():
    assert normalizar_texto_modelo(None) is None


def test_cargar_decodifica_el_escape_truncado_al_parsear():
    crudo = r'{"d": "espec\u00ificas y l\u00imite, pr\u00fácticas"}'
    assert cargar(crudo) == {"d": "específicas y límite, prácticas"}


def test_reparar_escapes_sigue_escapando_otras_barras():
    reparado, vistos = reparar_escapes(r'{"a": "\unidad y espec\u00ificas"}')
    assert cargar(reparado)["a"] == "\\unidad y específicas"
    assert vistos
