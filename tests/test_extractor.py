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

from src.scraper.extractor import (  # noqa: E402
    _item_to_proceso,
    _parse_fecha_api,
    _tipo_actividad_de,
)
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


def _actividad(nombre: str, tipo_id: int = 3, tipo_nombre: str = "Giro") -> dict:
    """Una entrada de `actividades[]` tal como la devuelve la API real."""
    return {"a": {"nombre": nombre, "id_tipo_actividad": tipo_id, "ae": {"nombre": tipo_nombre}}}


class TestItemToProceso:
    def _item(self, **overrides) -> dict:
        base = {
            "id": 12345,
            "codigo_proceso": "96-2026-P0011",
            "institucion": "  Banco de Desarrollo de El Salvador  ",
            "nombre_proceso": "  Adquisición de servidores rack  ",
            "forma_contratacion": "Licitación Pública",
            "actividades": [_actividad("Infraestructura de TI")],
            "fecha_publicacion": "2026-03-30T22:34:44.000Z",
            # NO incluye "fecha_limite": la API real no trae esa clave en el
            # listado (verificado en vivo). Un item que sí la trajera no
            # debería cambiar nada — ver test_ignora_fecha_limite_si_llegara.
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
        assert p.tipo_actividad == "Infraestructura de TI"

    def test_construye_url_directa_con_el_id(self):
        p = _item_to_proceso(self._item(id=999))
        assert p.url_directa == "https://www.comprasal.gob.sv/pac-procesos/etapas/999"

    def test_fecha_publicacion_se_parsea(self):
        p = _item_to_proceso(self._item())
        assert p.fecha_publicacion is not None

    def test_fecha_limite_siempre_none_en_el_listado(self):
        """La API de listado no la trae; se enriquece después, por proceso,
        solo para relevantes (ver drilldown/locator.py::obtener_fecha_limite).
        """
        assert _item_to_proceso(self._item()).fecha_limite is None

    def test_ignora_fecha_limite_si_llegara(self):
        """Si algún día la API agregara la clave, no debe usarse desde acá:
        el contrato es que fecha_limite SIEMPRE sale del detalle, no del
        listado — así no hay dos fuentes de verdad silenciosamente distintas.
        """
        item = self._item(fecha_limite="2026-04-15T22:00:00.000Z")
        assert _item_to_proceso(item).fecha_limite is None

    def test_tolera_fecha_publicacion_null(self):
        # Regresión: COMPRASAL devuelve null en algunos procesos (fix histórico).
        p = _item_to_proceso(self._item(fecha_publicacion=None))
        assert p.fecha_publicacion is None

    def test_institucion_por_defecto_si_falta(self):
        item = self._item()
        del item["institucion"]
        assert _item_to_proceso(item).institucion == "Desconocida"

    def test_campos_opcionales_ausentes_quedan_vacios(self):
        item = self._item()
        for k in ("codigo_proceso", "nombre_proceso", "forma_contratacion", "actividades"):
            del item[k]
        p = _item_to_proceso(item)
        assert p.numero_referencia == ""
        assert p.descripcion == ""
        assert p.forma_contratacion == ""
        assert p.tipo_actividad == ""


class TestTipoActividadDe:
    """`actividades[]` es una lista de 1 a 8 clasificaciones económicas por
    proceso, sin ninguna marcada como "principal" (verificado contra las 9
    modalidades vigentes de COMPRASAL). `_tipo_actividad_de` las une todas.
    """

    def test_una_sola_actividad(self):
        item = {"actividades": [_actividad("Reparación de equipo electrónico")]}
        assert _tipo_actividad_de(item) == "Reparación de equipo electrónico"

    def test_varias_actividades_se_unen_en_orden(self):
        nombres = [
            "Reparación de equipo electrónico",
            "Reparación de maquinaria",
            "Reparación de equipos n.c.p.",
        ]
        item = {"actividades": [_actividad(n) for n in nombres]}
        assert _tipo_actividad_de(item) == "; ".join(nombres)

    def test_actividades_duplicadas_no_se_repiten(self):
        item = {"actividades": [_actividad("Bienes de TI"), _actividad("Bienes de TI")]}
        assert _tipo_actividad_de(item) == "Bienes de TI"

    def test_actividades_ausente_es_cadena_vacia(self):
        assert _tipo_actividad_de({}) == ""

    def test_actividades_vacia_es_cadena_vacia(self):
        assert _tipo_actividad_de({"actividades": []}) == ""

    def test_entrada_malformada_se_tolera(self):
        """Un elemento sin 'a', o que no sea dict, no debe romper el join."""
        item = {"actividades": [None, {}, {"a": None}, _actividad("Bienes de TI")]}
        assert _tipo_actividad_de(item) == "Bienes de TI"

    def test_nunca_excede_120_caracteres(self):
        """Límite real de licitaciones_metadata.categoria (VARCHAR2(120)):
        sin este recorte, un proceso con muchas actividades largas rompería
        el UPDATE/MERGE de repo.py con ORA-12899.
        """
        item = {"actividades": [_actividad(f"Actividad número {i} bastante larga") for i in range(8)]}
        assert len(_tipo_actividad_de(item)) <= 120


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


# ── Reintento del goto a /proveedor-paac ─────────────────────────────────────

class _PaginaFalsa:
    def __init__(self, fallos):
        self.fallos = fallos
        self.cerrada = False

    async def goto(self, *_a, **_k):
        if self.fallos:
            self.fallos -= 1
            from playwright.async_api import TimeoutError as PT
            raise PT("Page.goto: Timeout")

    async def evaluate(self, *_a):
        return "jwt"

    async def close(self):
        self.cerrada = True


class _ContextoFalso:
    def __init__(self, fallos):
        self.paginas = []
        self.fallos = fallos

    async def new_page(self):
        p = _PaginaFalsa(1 if self.fallos > len(self.paginas) else 0)
        self.paginas.append(p)
        return p


def test_goto_reintenta_tras_timeout(monkeypatch):
    import asyncio

    from src.scraper import extractor

    async def _sin_espera(_s):
        return None

    monkeypatch.setattr(extractor.asyncio, "sleep", _sin_espera)
    ctx = _ContextoFalso(fallos=2)
    page, token = asyncio.run(extractor._get_token_and_page(ctx))
    assert token == "jwt" and len(ctx.paginas) == 3
    assert ctx.paginas[0].cerrada and ctx.paginas[1].cerrada and page is ctx.paginas[2]


def test_goto_se_rinde_tras_los_intentos(monkeypatch):
    import asyncio

    import pytest
    from playwright.async_api import TimeoutError as PT

    from src.scraper import extractor

    async def _sin_espera(_s):
        return None

    monkeypatch.setattr(extractor.asyncio, "sleep", _sin_espera)
    ctx = _ContextoFalso(fallos=99)
    with pytest.raises(PT):
        asyncio.run(extractor._get_token_and_page(ctx))
    assert len(ctx.paginas) == extractor.GOTO_INTENTOS
