"""Tests del locator de pliegos (API REST de COMPRASAL). Sin red.

Cubre la lógica pura: extracción del id, puntuación y selección del documento.
Payloads modelados sobre respuestas reales de
`GET /api/v1/compra/proceso/{id}/documentos`.

`TestObtenerFechaLimite` cubre además `obtener_fecha_limite`, que SÍ es
async — se prueba con `asyncio.run()` dentro de una función de test sync
(no con `async def test_...`) a propósito: el repo no tiene `pytest-asyncio`
todavía (ver el comentario en `pyproject.toml`, "agregarlo el día que lo
haya, no antes"), y esto no lo necesita.
"""
import asyncio
import types

from src.drilldown import locator
from src.drilldown.locator import (
    _es_etapa_de_cierre_de_ofertas,
    _parse_fecha_etapa,
    mejor_documento,
    proceso_id_desde_url,
    puntuar_documento,
)


def _doc(nombre, ruta=None, visible=True):
    return {
        "id": 1,
        "nombre_documento": nombre,
        "ruta_documento": ruta if ruta is not None else f"documentos_hito/{nombre}-123.pdf",
        "visibilidad_proveedor": visible,
    }


class TestProcesoIdDesdeUrl:
    def test_extrae_id(self):
        assert proceso_id_desde_url("https://www.comprasal.gob.sv/pac-procesos/etapas/137496") == "137496"

    def test_url_sin_id(self):
        assert proceso_id_desde_url("https://www.comprasal.gob.sv/otra/cosa") is None

    def test_url_vacia(self):
        assert proceso_id_desde_url("") is None


class TestPuntuarDocumento:
    def test_bases_gana_a_convocatoria(self):
        assert puntuar_documento(_doc("BASES DE LICITACION 001")) > \
               puntuar_documento(_doc("CONVOCATORIA DE PROCESO 001"))

    def test_tolera_tildes(self):
        # "TÉRMINOS DE REFERENCIA" debe puntuar como pliego pese a la tilde
        assert puntuar_documento(_doc("TÉRMINOS DE REFERENCIA")) >= 90

    def test_descarta_no_pdf(self):
        assert puntuar_documento(_doc("BASES", ruta="documentos_hito/bases.docx")) == -1

    def test_descarta_ruta_vacia(self):
        assert puntuar_documento(_doc("BASES", ruta="")) == -1

    def test_pdf_desconocido_es_ultimo_recurso(self):
        p = puntuar_documento(_doc("ARCHIVO SIN PALABRA CLAVE"))
        assert 0 < p < 30


class TestMejorDocumento:
    def _payload(self, *docs):
        return {"documentos": [{"id": 66, "nombre": "Etapa", "documentos": list(docs)}],
                "empleado_asignado": False, "participantes": []}

    def test_elige_el_de_mayor_prioridad(self):
        p = self._payload(
            _doc("CONVOCATORIA DE PROCESO X"),
            _doc("BASES DE LICITACION X"),
            _doc("NOTA DE CONFERENCIA INFORMATIVA"),
        )
        assert "BASES" in mejor_documento(p)["nombre_documento"]

    def test_ignora_no_visibles_para_proveedor(self):
        p = self._payload(
            _doc("BASES DE LICITACION X", visible=False),
            _doc("CONVOCATORIA DE PROCESO X"),
        )
        assert "CONVOCATORIA" in mejor_documento(p)["nombre_documento"]

    def test_aplana_varias_etapas(self):
        p = {"documentos": [
            {"id": 1, "documentos": [_doc("CONVOCATORIA A")]},
            {"id": 2, "documentos": [_doc("PLIEGO DE CONDICIONES B")]},
        ]}
        assert "PLIEGO" in mejor_documento(p)["nombre_documento"]

    def test_sin_documentos_devuelve_none(self):
        assert mejor_documento({"documentos": [{"id": 1, "documentos": []}]}) is None

    def test_todos_no_pdf_devuelve_none(self):
        p = self._payload(_doc("BASES", ruta="documentos_hito/x.docx"))
        assert mejor_documento(p) is None

    def test_payload_inesperado_no_revienta(self):
        assert mejor_documento({}) is None
        assert mejor_documento({"documentos": "no-es-lista"}) is None
        assert mejor_documento({"documentos": [None, 5]}) is None


class TestEsEtapaDeCierreDeOfertas:
    """Nombres reales, capturados en vivo de un proceso de cada una de las 9
    modalidades vigentes de COMPRASAL (contra `/api/v1/compra/proceso/{id}`).
    """

    def test_nombres_reales_que_deben_matchear(self):
        deben_matchear = [
            "Recepción de ofertas",                                  # Contratación directa, Comparación de precios, Licitación, Convenios
            "Recepción de ofertas técnicas",                         # Subasta Inversa
            "Recepción de propuesta técnica y económica",            # Consultoría calidad y costo
            "Recepción de propuesta técnica y ecónomica",            # Consultoría menor costo (con la errata real del portal)
            "Recepción de CV de consultores y propuesta económica",  # Consultoría consultor individual
            "Recepción de propuesta",                                # Consultoría fuente única
        ]
        for nombre in deben_matchear:
            assert _es_etapa_de_cierre_de_ofertas(nombre), f"debía matchear: {nombre!r}"

    def test_nombres_reales_que_no_deben_matchear(self):
        no_deben_matchear = [
            "Recepción de consultas",
            "Publicación de convocatoria en COMPRASAL",
            "Apertura de ofertas",
            "Evaluación de ofertas",
            "Adjudicación",
            "Realización de subasta",
        ]
        for nombre in no_deben_matchear:
            assert not _es_etapa_de_cierre_de_ofertas(nombre), f"NO debía matchear: {nombre!r}"

    def test_tolera_none_y_vacio(self):
        assert not _es_etapa_de_cierre_de_ofertas(None)
        assert not _es_etapa_de_cierre_de_ofertas("")


class TestParseFechaEtapa:
    def test_formato_real_de_la_api(self):
        dt = _parse_fecha_etapa("2026-10-07T18:00:00.000Z")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 10, 7)
        assert dt.hour == 12  # 18:00 UTC → 12:00 El Salvador (UTC-6)

    def test_none_y_vacio_devuelven_none(self):
        assert _parse_fecha_etapa(None) is None
        assert _parse_fecha_etapa("") is None

    def test_formato_invalido_no_revienta(self):
        assert _parse_fecha_etapa("no-es-una-fecha") is None


class TestObtenerFechaLimite:
    """`/api/v1/procesos/proveedores` (listado) no trae esta fecha — verificado
    en vivo. Sale de `/api/v1/compra/proceso/{id}` → `EtapaPorProcesos`.
    """

    def _proceso(self, url_directa="https://www.comprasal.gob.sv/pac-procesos/etapas/140428"):
        return types.SimpleNamespace(numero_referencia="X", url_directa=url_directa)

    def _con_detalle(self, monkeypatch, etapas, status_ok=True):
        async def fake_token(page):
            return "tok"

        async def fake_api_get(page, token, path):
            if not status_ok:
                return None
            return {"EtapaPorProcesos": etapas}

        monkeypatch.setattr(locator, "_token", fake_token)
        monkeypatch.setattr(locator, "_api_get", fake_api_get)

    def test_encuentra_la_etapa_de_cierre(self, monkeypatch):
        self._con_detalle(monkeypatch, [
            {"nombre": "Publicación de convocatoria en COMPRASAL",
             "fecha_hora_fin": "2026-09-26T01:40:22.000Z"},
            {"nombre": "Recepción de ofertas", "fecha_hora_fin": "2026-10-07T18:00:00.000Z"},
            {"nombre": "Adjudicación", "fecha_hora_fin": None},
        ])
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 10, 7)

    def test_dos_etapas_candidatas_toma_la_mas_tardia(self, monkeypatch):
        """Cubre el caso —no visto en las 9 modalidades probadas, pero posible
        si el portal agrega una nueva— de más de una etapa de cierre: se
        queda con la que de verdad cierra la ventana para ofertar.
        """
        self._con_detalle(monkeypatch, [
            {"nombre": "Recepción de ofertas técnicas", "fecha_hora_fin": "2026-10-01T00:00:00.000Z"},
            {"nombre": "Recepción de ofertas económicas", "fecha_hora_fin": "2026-10-07T18:00:00.000Z"},
        ])
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is not None and dt.day == 7

    def test_sin_id_en_la_url_devuelve_none(self, monkeypatch):
        dt = asyncio.run(locator.obtener_fecha_limite(
            page=None, proceso=self._proceso(url_directa="https://x/otra-cosa"), cfg=None))
        assert dt is None

    def test_sin_token_devuelve_none(self, monkeypatch):
        async def sin_token(page):
            return None
        monkeypatch.setattr(locator, "_token", sin_token)
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is None

    def test_api_no_200_devuelve_none(self, monkeypatch):
        self._con_detalle(monkeypatch, [], status_ok=False)
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is None

    def test_sin_etapa_de_cierre_reconocible_devuelve_none(self, monkeypatch):
        self._con_detalle(monkeypatch, [
            {"nombre": "Publicación de convocatoria en COMPRASAL", "fecha_hora_fin": "2026-09-26T01:40:22.000Z"},
        ])
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is None

    def test_payload_sin_etapaporprocesos_no_revienta(self, monkeypatch):
        async def fake_token(page):
            return "tok"

        async def fake_api_get(page, token, path):
            return {"algo": "distinto"}

        monkeypatch.setattr(locator, "_token", fake_token)
        monkeypatch.setattr(locator, "_api_get", fake_api_get)
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is None

    def test_respuesta_envuelta_en_data(self, monkeypatch):
        """Algunos wrappers de la API anidan el contenido bajo "data" (ya visto
        en `find_pliego_pdf_url`); esta función debe tolerar ambas formas.
        """
        async def fake_token(page):
            return "tok"

        async def fake_api_get(page, token, path):
            return {"data": {"EtapaPorProcesos": [
                {"nombre": "Recepción de ofertas", "fecha_hora_fin": "2026-10-07T18:00:00.000Z"},
            ]}}

        monkeypatch.setattr(locator, "_token", fake_token)
        monkeypatch.setattr(locator, "_api_get", fake_api_get)
        dt = asyncio.run(locator.obtener_fecha_limite(page=None, proceso=self._proceso(), cfg=None))
        assert dt is not None and dt.day == 7
