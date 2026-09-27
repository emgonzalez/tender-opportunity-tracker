"""Tests de src.drilldown.analysis — matriz de riesgos y extracción de monto.

Contexto de `monto_estimado`: se verificó exhaustivamente (contra la API real
de COMPRASAL, tanto pública como autenticada, y contra la UI renderizada) que
COMPRASAL no publica un monto estimado/presupuesto para ningún proceso activo,
en ningún endpoint. La única fuente posible es que el propio pliego lo declare
y el LLM lo extraiga como parte del mismo análisis que ya hace la matriz de
riesgos — de ahí que `_extraer_monto` viva en este módulo y no en uno aparte.

Toda la red está mockeada parcheando `analysis.llm.completar` (mismo patrón
que `tests/test_triage_raster.py`).
"""
import types

import pytest

from src.drilldown import analysis
from src.llm.contrato import Respuesta, Uso


def _fake_completar(texto: str):
    """Fábrica de un doble de `llm.completar` que devuelve `texto` tal cual."""
    def fake(cfg, pedido, *, cliente=None):
        return Respuesta(
            texto=texto, uso=Uso(input_tokens=100, output_tokens=50),
            modelo="x", proveedor="anthropic",
        )
    return fake


class TestExtraerMonto:
    """Unidad: `_extraer_monto` sobre el dict ya parseado del JSON del modelo."""

    def test_numero_directo(self):
        assert analysis._extraer_monto(
            {"monto_estimado": 12345.67, "monto_estimado_clausula": "cita"}
        ) == (12345.67, "cita")

    def test_entero(self):
        assert analysis._extraer_monto({"monto_estimado": 5000}) == (5000.0, "")

    def test_string_con_simbolo_de_moneda_y_comas(self):
        monto, _ = analysis._extraer_monto({"monto_estimado": "$12,345.67"})
        assert monto == pytest.approx(12345.67)

    def test_null_es_no_declarado(self):
        assert analysis._extraer_monto({"monto_estimado": None}) == (None, "")

    def test_ausente_es_no_declarado(self):
        assert analysis._extraer_monto({}) == (None, "")

    def test_string_vacio(self):
        assert analysis._extraer_monto({"monto_estimado": ""}) == (None, "")

    def test_texto_no_numerico_no_revienta(self):
        """El modelo a veces escribe "no aplica" en vez de null: nunca debe lanzar."""
        assert analysis._extraer_monto({"monto_estimado": "no aplica"}) == (None, "")

    def test_bool_nunca_es_un_monto(self):
        """bool es subclase de int en Python: True no debe convertirse en 1.0."""
        assert analysis._extraer_monto({"monto_estimado": True}) == (None, "")

    def test_clausula_sin_monto_se_ignora(self):
        """Sin un monto válido, la cláusula no tiene nada que respaldar."""
        monto, clausula = analysis._extraer_monto(
            {"monto_estimado": None, "monto_estimado_clausula": "texto suelto"}
        )
        assert monto is None
        # La función no fuerza clausula="" cuando falta el monto (queda a cargo
        # del llamador no usarla); lo que importa es que el monto sea None.


class TestAnalizarPliegoConMonto:
    """Integración: analizar_pliego() con la respuesta completa del modelo."""

    def test_monto_declarado_se_propaga_a_resultado_y_anexos(self, monkeypatch, tmp_path):
        texto = (
            '{"resumen": "ok", "items": [], '
            '"monto_estimado": 45000.5, '
            '"monto_estimado_clausula": "El presupuesto estimado es de $45,000.50"}'
        )
        monkeypatch.setattr(analysis.llm, "completar", _fake_completar(texto))

        res = analysis.analizar_pliego(
            str(tmp_path / "pliego.pdf"), types.SimpleNamespace(llm_provider="anthropic"),
            "ctx", "P1", formato="NATIVO", n_paginas=5,
        )

        assert res.estado == "ANALIZADO"
        assert res.monto_estimado == pytest.approx(45000.5)
        assert res.anexos["monto_estimado_clausula"] == "El presupuesto estimado es de $45,000.50"

    def test_monto_no_declarado_deja_resultado_sin_monto_ni_anexos(self, monkeypatch, tmp_path):
        texto = '{"resumen": "ok", "items": [], "monto_estimado": null, "monto_estimado_clausula": ""}'
        monkeypatch.setattr(analysis.llm, "completar", _fake_completar(texto))

        res = analysis.analizar_pliego(
            str(tmp_path / "pliego.pdf"), types.SimpleNamespace(llm_provider="anthropic"),
            "ctx", "P2", formato="NATIVO", n_paginas=5,
        )

        assert res.estado == "ANALIZADO"
        assert res.monto_estimado is None
        assert res.anexos == {}

    def test_modelo_omite_el_campo_por_completo(self, monkeypatch, tmp_path):
        """Compatibilidad hacia atrás: un modelo que no siga la instrucción nueva
        no debe romper el análisis (la matriz de riesgos es lo que no puede perderse).
        """
        texto = '{"resumen": "ok", "items": []}'
        monkeypatch.setattr(analysis.llm, "completar", _fake_completar(texto))

        res = analysis.analizar_pliego(
            str(tmp_path / "pliego.pdf"), types.SimpleNamespace(llm_provider="anthropic"),
            "ctx", "P3", formato="NATIVO", n_paginas=5,
        )

        assert res.estado == "ANALIZADO"
        assert res.monto_estimado is None

    def test_monto_basura_no_tumba_el_analisis(self, monkeypatch, tmp_path):
        """Un monto_estimado ilegible no debe convertir un ANALIZADO en ERROR_LLM."""
        texto = (
            '{"resumen": "ok", "items": [{"categoria": "financiero", '
            '"descripcion": "d", "severidad": "alta", "clausula_ref": "c"}], '
            '"monto_estimado": "no aplica"}'
        )
        monkeypatch.setattr(analysis.llm, "completar", _fake_completar(texto))

        res = analysis.analizar_pliego(
            str(tmp_path / "pliego.pdf"), types.SimpleNamespace(llm_provider="anthropic"),
            "ctx", "P4", formato="NATIVO", n_paginas=5,
        )

        assert res.estado == "ANALIZADO"
        assert res.monto_estimado is None
        assert res.matriz.items[0].categoria == "financiero"
