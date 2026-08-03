"""Tests del locator de pliegos (API REST de COMPRASAL). Sin red.

Cubre la lógica pura: extracción del id, puntuación y selección del documento.
Payloads modelados sobre respuestas reales de
`GET /api/v1/compra/proceso/{id}/documentos`.
"""
from src.drilldown.locator import (
    proceso_id_desde_url, puntuar_documento, mejor_documento,
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
