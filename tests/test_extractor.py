"""
Tests del extractor de COMPRASAL.

COMPRASAL se consume vía su API REST (JSON), no por scraping del DOM: por eso
se prueban `_parse_fecha_api` e `_item_to_proceso` con payloads de la API.
No requiere conexión real.
"""
from datetime import datetime, timedelta, timezone

import pytest

# `src.scraper.extractor` importa Playwright a nivel de módulo. Estos tests solo
# ejercitan funciones puras, pero el import lo arrastra: si Playwright no está
# instalado (entorno local sin Docker), se saltan en vez de romper la colección.
pytest.importorskip("playwright", reason="Playwright no instalado (sí lo está en Docker)")

from src.scraper.extractor import _item_to_proceso, _parse_fecha_api  # noqa: E402
from src.scraper.models import Proceso  # noqa: E402

TZ = timezone(timedelta(hours=-6))  # El Salvador, UTC-6


class TestParseFechaApi:
    def test_iso_utc_se_convierte_a_el_salvador(self):
        # 22:34 UTC → 16:34 en El Salvador (UTC-6)
        dt = _parse_fecha_api("2026-03-30T22:34:44.000Z")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 3, 30)
        assert dt.hour == 16
        assert dt.minute == 34
        assert dt.utcoffset() == timedelta(hours=-6)

    def test_cruce_de_medianoche_retrocede_un_dia(self):
        # 02:00 UTC del día 15 → 20:00 del día 14 en El Salvador
        dt = _parse_fecha_api("2026-03-15T02:00:00.000Z")
        assert dt is not None
        assert dt.day == 14
        assert dt.hour == 20

    def test_sin_milisegundos(self):
        dt = _parse_fecha_api("2026-03-30T22:34:44Z")
        assert dt is not None and dt.year == 2026

    def test_none_retorna_none(self):
        # La API devuelve fecha_publicacion: null en borradores; no debe reventar.
        assert _parse_fecha_api(None) is None

    def test_cadena_vacia_retorna_none(self):
        assert _parse_fecha_api("") is None


class TestItemToProceso:
    def _item(self, **overrides) -> dict:
        base = {
            "id": 12345,
            "codigo_proceso": "96-2026-P0011",
            "institucion": "  Banco de Desarrollo de El Salvador  ",
            "nombre_proceso": "  Adquisición de servidores rack  ",
            "forma_contratacion": "Licitación Pública",
            "tipo_actividad": "Infraestructura",
            "fecha_publicacion": "2026-03-30T22:34:44.000Z",
            "fecha_limite": "2026-04-15T22:00:00.000Z",
        }
        base.update(overrides)
        return base

    def test_mapea_campos_y_hace_strip(self):
        p = _item_to_proceso(self._item())
        assert isinstance(p, Proceso)
        assert p.numero_referencia == "96-2026-P0011"
        assert p.institucion == "Banco de Desarrollo de El Salvador"
        assert p.descripcion == "Adquisición de servidores rack"
        assert p.forma_contratacion == "Licitación Pública"
        assert p.tipo_actividad == "Infraestructura"

    def test_construye_url_directa_con_el_id(self):
        p = _item_to_proceso(self._item(id=999))
        assert p.url_directa == "https://www.comprasal.gob.sv/pac-procesos/etapas/999"

    def test_parsea_ambas_fechas(self):
        p = _item_to_proceso(self._item())
        assert p.fecha_publicacion is not None
        assert p.fecha_limite is not None
        assert p.fecha_limite > p.fecha_publicacion

    def test_tolera_fecha_publicacion_null(self):
        # Regresión: COMPRASAL devuelve null en algunos procesos (fix histórico).
        p = _item_to_proceso(self._item(fecha_publicacion=None))
        assert p.fecha_publicacion is None

    def test_tolera_fecha_limite_ausente(self):
        item = self._item()
        del item["fecha_limite"]
        assert _item_to_proceso(item).fecha_limite is None

    def test_institucion_por_defecto_si_falta(self):
        item = self._item()
        del item["institucion"]
        assert _item_to_proceso(item).institucion == "Desconocida"

    def test_campos_opcionales_ausentes_quedan_vacios(self):
        item = self._item()
        for k in ("codigo_proceso", "nombre_proceso", "forma_contratacion", "tipo_actividad"):
            del item[k]
        p = _item_to_proceso(item)
        assert p.numero_referencia == ""
        assert p.descripcion == ""
        assert p.forma_contratacion == ""
        assert p.tipo_actividad == ""


class TestProcesoModel:
    def test_proceso_clasificado_from_proceso(self):
        from src.scraper.models import ProcesoClasificado
        p = Proceso(
            institucion="Inst A",
            descripcion="Desc",
            fecha_publicacion=datetime.now(TZ),
            url_directa="https://example.com",
            numero_referencia="REF-1",
        )
        pc = ProcesoClasificado.from_proceso(p)
        assert pc.institucion == p.institucion
        assert pc.numero_referencia == p.numero_referencia
        assert pc.es_relevante is False
        assert pc.confianza == 0.0
        assert pc.tags == []
        assert pc.marcas_recomendadas == []
