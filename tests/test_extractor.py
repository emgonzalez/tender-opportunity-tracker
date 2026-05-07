"""
Tests del extractor de COMPRASAL.
Usa datos mock del DOM para no requerir conexión real.
"""
from datetime import datetime, timezone, timedelta

import pytest

from src.scraper.extractor import _parse_fecha, _parse_proceso_raw
from src.scraper.models import Proceso

TZ = timezone(timedelta(hours=-6))


class TestParseFecha:
    def test_formato_dd_mm_yyyy(self):
        dt = _parse_fecha("15/03/2025")
        assert dt is not None
        assert dt.day == 15
        assert dt.month == 3
        assert dt.year == 2025

    def test_formato_dd_mm_yyyy_hhmm(self):
        dt = _parse_fecha("15/03/2025 09:30")
        assert dt is not None
        assert dt.hour == 9
        assert dt.minute == 30

    def test_formato_yyyy_mm_dd(self):
        dt = _parse_fecha("2025-03-15")
        assert dt is not None
        assert dt.year == 2025

    def test_fecha_invalida_retorna_none(self):
        dt = _parse_fecha("esto no es fecha")
        assert dt is None

    def test_fecha_vacia_retorna_none(self):
        dt = _parse_fecha("   ")
        assert dt is None


class TestParseProcesoRaw:
    def test_tabla_tres_columnas(self):
        raw = {
            "estrategia": "tabla",
            "texto_completo": "Min. Hacienda\tCompra de laptops\t15/03/2025",
            "href": "/proceso/12345",
            "celdas": ["Min. Hacienda", "Compra de 20 laptops Dell para oficinas", "15/03/2025"],
        }
        proceso = _parse_proceso_raw(raw)
        assert proceso is not None
        assert proceso.institucion == "Min. Hacienda"
        assert "laptops" in proceso.descripcion.lower()
        assert proceso.url_directa == "https://www.comprasal.gob.sv/proceso/12345"

    def test_href_absoluto_no_modifica(self):
        raw = {
            "estrategia": "tabla",
            "texto_completo": "texto",
            "href": "https://comprasal.gob.sv/proceso/99",
            "celdas": ["Inst", "Desc", "01/01/2025"],
        }
        proceso = _parse_proceso_raw(raw)
        assert proceso.url_directa == "https://comprasal.gob.sv/proceso/99"

    def test_fila_vacia_retorna_none(self):
        raw = {
            "estrategia": "tabla",
            "texto_completo": "   ",
            "href": "",
            "celdas": [],
        }
        proceso = _parse_proceso_raw(raw)
        assert proceso is None

    def test_fallback_texto_completo(self):
        raw = {
            "estrategia": "card",
            "texto_completo": "Ministerio de Salud - Compra de equipos informáticos",
            "href": "",
            "celdas": [],
        }
        proceso = _parse_proceso_raw(raw)
        assert proceso is not None
        assert proceso.institucion == "Desconocida"
        assert "informáticos" in proceso.descripcion or "Ministerio" in proceso.descripcion


class TestProcesoModel:
    def test_proceso_clasificado_from_proceso(self):
        from src.scraper.models import ProcesoClasificado
        p = Proceso(
            institucion="Inst A",
            descripcion="Desc",
            fecha_publicacion=datetime.now(TZ),
            url_directa="https://example.com",
        )
        pc = ProcesoClasificado.from_proceso(p)
        assert pc.institucion == p.institucion
        assert pc.es_relevante is False
        assert pc.confianza == 0.0
