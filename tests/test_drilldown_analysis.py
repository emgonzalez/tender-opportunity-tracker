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
        # Sin monto no hay cláusula que respaldar. Desde P4 `anexos` lleva
        # siempre `competibilidad` en un ANALIZADO, así que se mira la clave.
        assert "monto_estimado_clausula" not in res.anexos
        assert set(res.anexos) == {"competibilidad"}

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


class TestPromptsPorPortal:
    """SICOP: «cartel», monedas del portal y contenido como dato no confiable. COMPRASAL intacto
    (su golden byte a byte vive en tests/test_prompts_portal.py)."""

    def test_sicop_dice_cartel_y_pide_la_moneda(self):
        for senales in (False, True):
            texto = analysis.system_prompt("sicop_cr", senales=senales)
            assert "SICOP, Costa Rica" in texto
            assert "pliego" not in texto
            assert "cartel" in texto
            assert "Moneda(s) del portal: CRC y USD" in texto
            assert '"monto_estimado_moneda": "CRC|USD|null"' in texto
            assert "dato no confiable" in texto
        assert '"monto_estimado_moneda": {"type": ["string", "null"]}' in analysis.system_prompt(
            "sicop_cr", senales=True)

    def test_comprasal_no_cambia(self):
        assert analysis.system_prompt() == analysis.SYSTEM_PROMPT
        assert analysis.esquema_analisis() is analysis.ESQUEMA_ANALISIS
        assert analysis.etiqueta_documento() == "pliego"
        assert "monto_estimado_moneda" not in analysis.system_prompt(senales=True)

    def test_esquema_sicop_exige_la_moneda(self):
        esquema = analysis.esquema_analisis("sicop_cr")
        assert "monto_estimado_moneda" in esquema["required"]
        assert esquema["required"].index("monto_estimado_moneda") == \
            esquema["required"].index("monto_estimado_clausula") + 1
        assert "monto_estimado_moneda" not in analysis.ESQUEMA_ANALISIS["properties"]

    def test_triaje_historico_de_sicop(self):
        from src.drilldown import triage

        texto = triage.system_prompt("sicop_cr")
        assert "extracto del cartel" in texto and "Costa Rica" in texto
        assert "dato no confiable" in texto
        assert "pliego" not in triage.system_prompt("sicop_cr")
        bloques = triage._build_user_content("ctx", texto="Servidor", pdf_path=None, scanned=False,
                                             documento="cartel")
        assert "Extracto del cartel:\nServidor" in bloques[-1].texto
        assert "Extracto del pliego:" in triage._build_user_content(
            "ctx", texto="x", pdf_path=None, scanned=False)[-1].texto


class TestMonedaDelMonto:
    def _analizar(self, monkeypatch, tmp_path, texto, portal_id):
        pedidos = []

        def fake(cfg, pedido, *, cliente=None):
            pedidos.append(pedido)
            return Respuesta(texto=texto, uso=Uso(input_tokens=1, output_tokens=1), modelo="x",
                             proveedor="anthropic")

        monkeypatch.setattr(analysis.llm, "completar", fake)
        res = analysis.analizar_pliego(
            str(tmp_path / "cartel.pdf"), types.SimpleNamespace(llm_provider="anthropic"),
            "ctx", "2026LD-1", formato="NATIVO", n_paginas=3, portal_id=portal_id)
        return res, pedidos

    def test_sicop_guarda_la_moneda_declarada(self, monkeypatch, tmp_path):
        texto = ('{"resumen": "ok", "items": [], "monto_estimado": 12500000, '
                 '"monto_estimado_clausula": "Presupuesto: ₡12.500.000", "monto_estimado_moneda": "crc"}')
        res, pedidos = self._analizar(monkeypatch, tmp_path, texto, "sicop_cr")
        assert res.anexos["monto_estimado_moneda"] == "CRC"
        assert "Analiza el cartel adjunto" in pedidos[0].contenido[-1].texto

    def test_moneda_ajena_al_portal_se_descarta(self, monkeypatch, tmp_path):
        texto = ('{"resumen": "ok", "items": [], "monto_estimado": 100, '
                 '"monto_estimado_clausula": "x", "monto_estimado_moneda": "EUR"}')
        res, _ = self._analizar(monkeypatch, tmp_path, texto, "sicop_cr")
        assert res.monto_estimado == 100 and "monto_estimado_moneda" not in res.anexos

    def test_comprasal_no_agrega_moneda(self, monkeypatch, tmp_path):
        texto = ('{"resumen": "ok", "items": [], "monto_estimado": 100, '
                 '"monto_estimado_clausula": "x", "monto_estimado_moneda": "USD"}')
        res, pedidos = self._analizar(monkeypatch, tmp_path, texto, "comprasal_sv")
        assert set(res.anexos) == {"monto_estimado_clausula", "competibilidad"}
        assert "Analiza el pliego adjunto" in pedidos[0].contenido[-1].texto
