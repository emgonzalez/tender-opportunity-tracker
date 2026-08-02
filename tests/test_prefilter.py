"""Tests para src.drilldown.prefilter — fase 2 track A."""

import pytest

from src.drilldown.prefilter import passes_prefilter, _normalize_signals


class TestNormalizeSignals:
    """Tests para el helper _normalize_signals."""

    def test_flatten_comma_separated(self):
        """Debe aplanar y normalizar signals separadas por coma."""
        signals = ["servidor, server, rack"]
        result = _normalize_signals(signals)
        assert result == ["servidor", "server", "rack"]

    def test_strip_whitespace_and_lowercase(self):
        """Debe eliminar espacios y convertir a minúsculas."""
        signals = ["  SERVIDOR  ", "  Server  "]
        result = _normalize_signals(signals)
        assert result == ["servidor", "server"]

    def test_discard_empty_tokens(self):
        """Debe descartar tokens vacíos después de strip."""
        signals = ["servidor, , , server"]
        result = _normalize_signals(signals)
        assert result == ["servidor", "server"]

    def test_empty_list_returns_empty(self):
        """Lista vacía debe devolver lista vacía."""
        result = _normalize_signals([])
        assert result == []

    def test_multiple_signal_strings(self):
        """Múltiples strings con commas deben aplanarse."""
        signals = ["servidor, server", "almacenamiento, storage"]
        result = _normalize_signals(signals)
        assert result == ["servidor", "server", "almacenamiento", "storage"]


class TestPassesPrefilter:
    """Tests para la función principal passes_prefilter."""

    def test_text_with_inclusion_and_no_exclusion(self):
        """Texto con inclusión sin exclusión debe pasar."""
        text = "se requiere un servidor rack con firewall"
        inclusion = ["servidor", "firewall"]
        exclusion = ["alimentos"]
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_text_with_exclusion_fails(self):
        """Texto con exclusión debe fallar."""
        text = "suministro de alimentos y bebidas"
        inclusion = ["servidor", "firewall"]
        exclusion = ["alimentos"]
        assert passes_prefilter(text, inclusion, exclusion) is False

    def test_no_inclusion_signals_passes_by_default(self):
        """Sin inclusiones configuradas, debe pasar por defecto."""
        text = "texto sin señales"
        inclusion = []
        exclusion = ["alimentos"]
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_no_inclusion_match_fails(self):
        """Sin coincidencia de inclusión, debe fallar."""
        text = "texto sobre construcción"
        inclusion = ["servidor", "firewall"]
        exclusion = []
        assert passes_prefilter(text, inclusion, exclusion) is False

    def test_multi_word_inclusion_signal(self):
        """Multi-palabra en signal debe usar substring matching."""
        text = "Se requiere un data center con almacenamiento en red"
        inclusion = ["almacenamiento en red", "servidor"]
        exclusion = []
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_case_insensitive_matching(self):
        """Matching debe ser case-insensitive."""
        text = "Se requiere un SERVIDOR RACK para el DATA CENTER"
        inclusion = ["servidor", "rack"]
        exclusion = []
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_word_boundaries_for_single_word(self):
        """Single-word tokens deben respetar word boundaries."""
        text = "servidor1 y server"
        inclusion = ["servidor"]  # debe buscar "servidor" como palabra completa
        exclusion = []
        # "servidor1" contiene "servidor" pero no es una palabra completa separada
        # Por lo tanto solo "server" no coincide exactamente
        result = passes_prefilter(text, inclusion, exclusion)
        # Esperamos False porque "servidor" no se encuentra como palabra completa
        # (solo aparece como parte de "servidor1")
        assert result is False

    def test_word_boundaries_with_actual_match(self):
        """Word boundaries deben coincidir con palabras completas."""
        text = "servidor y servidor1"
        inclusion = ["servidor"]
        exclusion = []
        # "servidor" aparece como palabra completa antes de " y"
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_both_inclusion_and_exclusion_match(self):
        """Si hay inclusión pero también exclusión, debe fallar (exclusión prioritaria)."""
        text = "servidor de alimentos"
        inclusion = ["servidor"]
        exclusion = ["alimentos"]
        # La exclusión tiene prioridad
        assert passes_prefilter(text, inclusion, exclusion) is False

    def test_comma_separated_single_string(self):
        """Señal con commas en un solo string debe funcionar."""
        text = "Se requiere un servidor rack de almacenamiento"
        inclusion = ["servidor, server, rack"]
        exclusion = []
        # "servidor" está en el texto
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_empty_text_with_inclusions_fails(self):
        """Texto vacío sin señales de inclusión debe fallar."""
        text = ""
        inclusion = ["servidor"]
        exclusion = []
        assert passes_prefilter(text, inclusion, exclusion) is False

    def test_empty_text_no_inclusions_passes(self):
        """Texto vacío sin inclusiones configuradas debe pasar."""
        text = ""
        inclusion = []
        exclusion = []
        assert passes_prefilter(text, inclusion, exclusion) is True

    def test_multiple_exclusions_any_match_fails(self):
        """Cualquier match en exclusiones debe fallar."""
        text = "servidor bebidas"
        inclusion = ["servidor"]
        exclusion = ["alimentos", "bebidas", "medicamentos"]
        assert passes_prefilter(text, inclusion, exclusion) is False
