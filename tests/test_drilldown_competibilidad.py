"""Tests de P4: señal estructurada `competible`/`motivo_bloqueo` y triaje consultable.

Los textos de las matrices son los de la corrida real del 2026-10-22
(`*.analisis.jsonl` de multicomputos_dev), recortados: CEPA 4303-2026-P0361
(proveedor único nombrado), BCR 99-2027-P0072 (marca específica VMware, fuera
del portafolio) y 96-2026-P0187 (soporte de switches Dell por subasta inversa,
el caso ordinario). Se conservan los escapes rotos que de verdad emite el
modelo (`espec\\u00ifico`, `PÓBLICAS`) porque la heurística tiene que
sobrevivirlos.
"""
import dataclasses
import json
import types

import fitz  # pymupdf
import pytest

from src import history
from src.drilldown import analysis, pipeline
from src.llm.contrato import Respuesta, Uso
from src.scraper.models import (
    MOTIVO_DIRECTA_NOMINADA,
    MOTIVO_MARCA_ESPECIFICA,
    MOTIVO_PROVEEDOR_UNICO,
    MOTIVOS_BLOQUEO,
    MatrizRiesgos,
    Precedente,
    Proceso,
    ResultadoAnalisis,
    RiesgoItem,
    UsoModelo,
)

PORTAFOLIO = ("HPE", "Nutanix", "Citrix", "Fortinet", "Oracle", "Veritas")


def _cepa() -> MatrizRiesgos:
    return MatrizRiesgos(
        resumen=(
            "El proceso de contratación directa con proveedor único para el servicio de "
            "mantenimiento y soporte técnico del Sistema Integrado para la Gestión "
            "Automatizada de Archivos de CEPA presenta riesgos relacionados con la "
            "exclusividad del proveedor y la falta de competencia."
        ),
        items=[
            RiesgoItem(
                "legal", "La contratación directa con proveedor único puede limitar la competencia.",
                "alta",
                "El método de contratación utilizado en la presente invitación corresponde a "
                "Contratación Directa con Proveedor Único de acuerdo con literal b) del art. 41 "
                "de la Ley de Compras Públicas.",
            ),
            RiesgoItem(
                "tecnico", "El servicio está a cargo de un desarrollador y titular de los derechos "
                "patrimoniales específicos.", "media",
                "Contratar el Servicio de mantenimiento y soporte técnico del Sistema integrado "
                "para la gestión automatizada de archivos de CEPA, con el consultor Alfredo "
                "Antonio Martínez Magaña, en su calidad de desarrollador",
            ),
        ],
    )


def _bcr() -> MatrizRiesgos:
    return MatrizRiesgos(
        resumen=(
            "El proceso de contratación es una Contratación Directa por Marca Específica para "
            "la renovación de licenciamiento VMware para el Banco Central de Reserva de El "
            "Salvador. No se declara un monto estimado para la contratación."
        ),
        items=[
            RiesgoItem("legal", "Se rige por la LCP, normativas especßicas.", "media",
                       "Los documentos contractuales y su ejecución se regirán por la LEY DE "
                       "COMPRAS PÓBLICAS"),
            RiesgoItem("tecnico", "Renovación de licenciamiento VMware.", "alta",
                       "El Banco Central de Reserva requiere contratar la “Renovación de "
                       "Licenciamiento Vmware” para el año 2027"),
        ],
    )


def _dell_ordinario() -> MatrizRiesgos:
    return MatrizRiesgos(
        resumen=(
            "El proceso de contratación es relevante para Multicómputos El Salvador ya que "
            "involucra servicios de soporte técnico para switches DELL."
        ),
        items=[
            RiesgoItem("financiero", "Garantía de cumplimiento del 10%.", "media",
                       "El importe de Garantía de Cumplimiento es del DIEZ POR CIENTO (10%)"),
            RiesgoItem("tecnico", "Técnico certificado en Dell EMC SD-WAN.", "media",
                       "Tener un Service Desk o NOC ... 24x7"),
        ],
    )


# ── Vocabulario ──────────────────────────────────────────────────────────────

def test_vocabulario_cerrado_y_pequeno():
    assert set(MOTIVOS_BLOQUEO) == {
        MOTIVO_PROVEEDOR_UNICO, MOTIVO_MARCA_ESPECIFICA, MOTIVO_DIRECTA_NOMINADA}
    for clave, etiqueta in MOTIVOS_BLOQUEO.items():
        assert clave.isascii() and clave == clave.lower() and " " not in clave
        assert etiqueta  # texto legible para el correo


# ── Heurística: casos reales ─────────────────────────────────────────────────

class TestEvaluarCompetibilidad:
    def test_cepa_proveedor_unico_no_es_competible(self):
        competible, motivo, evidencia = analysis.evaluar_competibilidad(
            _cepa(), "Contratación directa", PORTAFOLIO)
        assert competible is False
        assert motivo == MOTIVO_PROVEEDOR_UNICO
        assert "Proveedor Único" in evidencia or "proveedor único" in evidencia

    def test_cepa_sin_forma_ni_portafolio_igual_se_bloquea(self):
        """El proveedor único no depende del portafolio: basta el texto."""
        assert analysis.evaluar_competibilidad(_cepa())[:2] == (False, MOTIVO_PROVEEDOR_UNICO)

    def test_bcr_marca_especifica_sin_portafolio_es_desconocido(self):
        competible, motivo, evidencia = analysis.evaluar_competibilidad(
            _bcr(), "Contratación directa")
        assert competible is None
        assert motivo == MOTIVO_MARCA_ESPECIFICA
        assert "Marca Específica" in evidencia

    def test_bcr_marca_fuera_del_portafolio_no_es_competible(self):
        assert analysis.evaluar_competibilidad(
            _bcr(), "Contratación directa", PORTAFOLIO)[:2] == (False, MOTIVO_MARCA_ESPECIFICA)

    def test_bcr_marca_dentro_del_portafolio_si_es_competible(self):
        assert analysis.evaluar_competibilidad(
            _bcr(), "Contratación directa", PORTAFOLIO + ("VMware",)) == (True, "", "")

    def test_marca_del_portafolio_se_busca_como_palabra(self):
        """`HP` no debe casar dentro de `HPE`, ni `Oracle` dentro de otra palabra."""
        m = MatrizRiesgos(resumen="Contratación directa por marca específica HPE Aruba.")
        assert analysis.evaluar_competibilidad(m, "", ("HP",))[0] is False
        assert analysis.evaluar_competibilidad(m, "", ("HPE",))[0] is True

    def test_proceso_ordinario_es_competible(self):
        assert analysis.evaluar_competibilidad(
            _dell_ordinario(), "Subasta Inversa", PORTAFOLIO) == (True, "", "")

    def test_forma_competitiva_de_comprasal_manda_sobre_el_texto(self):
        """Un pliego de subasta que cite «proveedor único» de la ley no es un bloqueo."""
        assert analysis.evaluar_competibilidad(
            _cepa(), "Subasta Inversa", PORTAFOLIO) == (True, "", "")

    def test_proveedor_unico_sin_contratacion_directa_no_bloquea(self):
        m = MatrizRiesgos(resumen="Se exige que el proveedor único responsable del soporte ...")
        assert analysis.evaluar_competibilidad(m)[:2] == (True, "")

    def test_contratacion_directa_nominada(self):
        m = MatrizRiesgos(
            resumen="Contratación directa para soporte del sistema.",
            items=[RiesgoItem("legal", "d", "alta",
                              "con el consultor Juan Pérez, titular de los derechos de autor")],
        )
        assert analysis.evaluar_competibilidad(m)[:2] == (False, MOTIVO_DIRECTA_NOMINADA)

    def test_contratacion_directa_sin_bloqueo_reconocible_es_competible(self):
        m = MatrizRiesgos(resumen="Contratación directa por urgencia para reparar el UPS.")
        assert analysis.evaluar_competibilidad(m) == (True, "", "")

    def test_tolera_escapes_rotos_y_mayusculas(self):
        m = MatrizRiesgos(resumen="CONTRATACI\\u00d3N DIRECTA POR MARCA ESPEC\\u00ifICA de Cisco")
        assert analysis.evaluar_competibilidad(m, "", PORTAFOLIO)[:2] == (
            False, MOTIVO_MARCA_ESPECIFICA)

    def test_sin_matriz_es_desconocido(self):
        assert analysis.evaluar_competibilidad(None, "Contratación directa") == (None, "", "")


class TestAplicarCompetibilidad:
    def test_escribe_campos_y_anexos_y_es_idempotente(self):
        res = ResultadoAnalisis(proceso_uuid="4303-2026-P0361", estado="ANALIZADO",
                                matriz=_cepa(), anexos={"monto_estimado_clausula": "x"})
        analysis.aplicar_competibilidad(res, "Contratación directa", PORTAFOLIO)
        primera = dataclasses.asdict(res)
        analysis.aplicar_competibilidad(res, "Contratación directa", PORTAFOLIO)
        assert dataclasses.asdict(res) == primera
        assert res.competible is False and res.no_competible
        assert res.motivo_bloqueo == MOTIVO_PROVEEDOR_UNICO
        assert res.anexos["monto_estimado_clausula"] == "x"  # no pisa lo que había
        comp = res.anexos["competibilidad"]
        assert comp["competible"] is False
        assert comp["motivo_bloqueo"] == MOTIVO_PROVEEDOR_UNICO
        assert comp["evidencia"]
        json.dumps(res.anexos)  # viaja tal cual a licitaciones_analisis.anexos

    @pytest.mark.parametrize("estado", [
        "DESCARTADO_TRIAJE", "DESCARTADO_REGEX", "ERROR_LLM", "REQUIERE_REVISION_MANUAL",
        "NOTIFICADO"])
    def test_estados_no_analizados_quedan_desconocidos(self, estado):
        res = ResultadoAnalisis(proceso_uuid="P", estado=estado, matriz=_cepa())
        analysis.aplicar_competibilidad(res, "Contratación directa")
        assert res.competible is None and res.motivo_bloqueo == "" and res.anexos == {}


def test_analizar_pliego_cepa_sale_no_competible(monkeypatch, tmp_path):
    """Integración: la señal sale de `analizar_pliego` sin un parámetro nuevo."""
    data = {"resumen": _cepa().resumen,
            "items": [dataclasses.asdict(i) for i in _cepa().items],
            "monto_estimado": None, "monto_estimado_clausula": ""}

    def fake(cfg, pedido, *, cliente=None):
        return Respuesta(texto=json.dumps(data, ensure_ascii=False),
                         uso=Uso(input_tokens=1, output_tokens=1), modelo="x",
                         proveedor="anthropic")

    monkeypatch.setattr(analysis.llm, "completar", fake)
    res = analysis.analizar_pliego(
        str(tmp_path / "p.pdf"), types.SimpleNamespace(llm_provider="anthropic"),
        "ctx", "4303-2026-P0361", formato="NATIVO", n_paginas=3)
    assert res.estado == "ANALIZADO"
    assert (res.competible, res.motivo_bloqueo) == (False, MOTIVO_PROVEEDOR_UNICO)


# ── Triaje consultable ───────────────────────────────────────────────────────

class TestTriajeConsultable:
    def test_descartado_por_triaje(self):
        assert ResultadoAnalisis("P", "DESCARTADO_TRIAJE", triaje="NO-GO").descartado_por_triaje
        assert ResultadoAnalisis("P", "NOTIFICADO", triaje="NO-GO").descartado_por_triaje
        assert not ResultadoAnalisis("P", "ANALIZADO", triaje="GO").descartado_por_triaje
        assert not ResultadoAnalisis("P", "DESCARTADO_REGEX").descartado_por_triaje

    def test_descartados_por_triaje_del_lote(self):
        resultados = {
            "8352-2026-P0114": ResultadoAnalisis("8352-2026-P0114", "DESCARTADO_TRIAJE",
                                                 triaje="NO-GO"),
            "1500-2026-P0077": ResultadoAnalisis("1500-2026-P0077", "DESCARTADO_TRIAJE",
                                                 triaje="NO-GO"),
            "96-2026-P0187": ResultadoAnalisis("96-2026-P0187", "ANALIZADO", triaje="GO"),
            "roto": None,
        }
        assert pipeline.descartados_por_triaje(resultados) == {
            "8352-2026-P0114", "1500-2026-P0077"}
        assert pipeline.descartados_por_triaje({}) == set()
        assert pipeline.descartados_por_triaje(None) == set()


# ── Pipeline: forma de COMPRASAL + portafolio ────────────────────────────────

def _cfg(tmp_path):
    return types.SimpleNamespace(
        data_dir=str(tmp_path), budget_path=str(tmp_path / "gasto.json"),
        pliego_max_pages=100, triage_pages=15, triage_raster_pages=5,
        daily_budget_usd=5.0, analisis_tokens_por_pagina=3000,
        drilldown_tmp_dir=str(tmp_path / "tmp"), model_triage="claude-haiku-4-5",
        model_analysis="claude-sonnet-5", anthropic_api_key="x", enable_memoria=False,
    )


def _pdf(tmp_path, texto="Licenciamiento de servidor y firewall para el data center."):
    path = tmp_path / "p.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), texto)
    doc.save(str(path))
    doc.close()
    return str(path)


def _uso():
    return UsoModelo(modelo="claude-haiku-4-5", input_tokens=10, output_tokens=5, llamadas=1)


@pytest.mark.parametrize("forma, marcas, esperado", [
    ("Contratación directa", PORTAFOLIO, (False, MOTIVO_MARCA_ESPECIFICA)),
    ("Contratación directa", PORTAFOLIO + ("VMware",), (True, "")),
    ("Contratación directa", None, (None, MOTIVO_MARCA_ESPECIFICA)),
    ("Licitación competitiva", PORTAFOLIO, (True, "")),
])
def test_pipeline_reevalua_con_forma_y_portafolio(tmp_path, monkeypatch, forma, marcas, esperado):
    monkeypatch.setattr(pipeline.triage, "triage",
                        lambda cfg, ctx, **kw: ("GO", 0.9, "ok", _uso()))

    # Firma FIJA, igual que los dobles de test_pipeline.py: el pipeline no
    # debe pasarle a `analizar_pliego` ningún kwarg nuevo.
    def fake_analysis(pdf_path, cfg, ctx, uuid, formato="", client=None):
        return analysis.aplicar_competibilidad(ResultadoAnalisis(
            proceso_uuid=uuid, estado="ANALIZADO", formato=formato, matriz=_bcr(),
            modelo_analisis="test", uso_analisis=_uso()))

    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", fake_analysis)
    proceso = Proceso(institucion="BCR", descripcion="Renovación de Licenciamiento Vmware",
                      fecha_publicacion=None, url_directa="u",
                      numero_referencia="99-2027-P0072", forma_contratacion=forma)
    cfg = _cfg(tmp_path)

    res = pipeline.analizar_pliego_proceso(
        proceso, _pdf(tmp_path), cfg, "ctx", "cli", ["servidor"], [], marcas_cliente=marcas)

    assert (res.competible, res.motivo_bloqueo) == esperado
    # Lo persistido en el JSONL lleva la señal ya reevaluada.
    with open(history._analisis_path("cli", cfg.data_dir), encoding="utf-8") as f:
        ultima = json.loads(f.read().splitlines()[-1])
    assert (ultima["competible"], ultima["motivo_bloqueo"]) == esperado
    assert ultima["anexos"]["competibilidad"]["competible"] == esperado[0]


def test_pipeline_no_go_queda_consultable_y_sin_competibilidad(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.triage, "triage",
                        lambda cfg, ctx, **kw: ("NO-GO", 0.95, "tóners", _uso()))
    proceso = Proceso(institucion="Municipio", descripcion="TINTAS Y TONERS",
                      fecha_publicacion=None, url_directa="u",
                      numero_referencia="8352-2026-P0114")
    res = pipeline.analizar_pliego_proceso(
        proceso, _pdf(tmp_path), _cfg(tmp_path), "ctx", "cli", ["servidor"], [],
        marcas_cliente=PORTAFOLIO)
    assert res.estado == "DESCARTADO_TRIAJE" and res.descartado_por_triaje
    assert res.competible is None and res.motivo_bloqueo == ""


def test_marcas_del_portafolio():
    marca = types.SimpleNamespace
    profile = types.SimpleNamespace(catalog=types.SimpleNamespace(categories=[
        types.SimpleNamespace(brands=[marca(name="HPE"), marca(name="")]),
        types.SimpleNamespace(brands=[]),
        types.SimpleNamespace(brands=[marca(name="Fortinet")]),
    ]))
    assert pipeline.marcas_del_portafolio(profile) == ("HPE", "Fortinet")


# ── Compatibilidad hacia atrás y round-trip ──────────────────────────────────

LINEA_PRE_P4 = {
    # Forma exacta de una línea real de la corrida del 2026-10-22 (antes de P4).
    "proceso_uuid": "4303-2026-P0361", "estado": "ANALIZADO", "formato": "NATIVO",
    "triaje": "GO", "triaje_confianza": 0.9,
    "matriz": {"items": [dataclasses.asdict(i) for i in _cepa().items],
               "resumen": _cepa().resumen},
    "anexos": {}, "monto_estimado": None, "modelo_analisis": "x", "error": "",
    "generado_en": "2026-10-22T13:00:00+00:00", "n_paginas": 7, "tamano_bytes": 123,
    "uso_triaje": {"modelo": "h", "input_tokens": 1, "output_tokens": 2, "llamadas": 1},
    "uso_analisis": None, "precedentes": [],
}


class TestDeserializacion:
    def test_linea_antigua_queda_desconocida(self):
        res = ResultadoAnalisis.desde_dict(LINEA_PRE_P4)
        assert res.competible is None and res.motivo_bloqueo == ""
        assert res.matriz.items[0].clausula_ref.startswith("El método de contratación")
        assert res.uso_triaje.output_tokens == 2 and res.uso_analisis is None
        assert res.n_paginas == 7

    def test_linea_antigua_se_puede_reevaluar(self):
        res = analysis.aplicar_competibilidad(ResultadoAnalisis.desde_dict(LINEA_PRE_P4))
        assert (res.competible, res.motivo_bloqueo) == (False, MOTIVO_PROVEEDOR_UNICO)

    def test_constructor_sin_los_campos_nuevos(self):
        """Quien construye ResultadoAnalisis sin conocer P4 (p. ej. main.py al
        marcar NOTIFICADO) sigue funcionando: los campos tienen default."""
        res = ResultadoAnalisis(proceso_uuid="P", estado="NOTIFICADO")
        assert res.competible is None and res.motivo_bloqueo == ""

    def test_round_trip_asdict(self):
        original = analysis.aplicar_competibilidad(ResultadoAnalisis(
            proceso_uuid="99-2027-P0072", estado="ANALIZADO", formato="NATIVO",
            triaje="GO", triaje_confianza=0.8, matriz=_bcr(), monto_estimado=10.5,
            uso_analisis=UsoModelo("s", 10, 20, 1),
            precedentes=[Precedente(proceso_id="X", distancia=0.3)],
        ), "Contratación directa", PORTAFOLIO)
        linea = json.loads(json.dumps(dataclasses.asdict(original), ensure_ascii=False))
        assert ResultadoAnalisis.desde_dict(linea) == original

    def test_round_trip_por_el_jsonl_real(self, tmp_path):
        original = analysis.aplicar_competibilidad(ResultadoAnalisis(
            proceso_uuid="4303-2026-P0361", estado="ANALIZADO", matriz=_cepa()))
        history.save_analisis("cli", original, str(tmp_path))
        with open(history._analisis_path("cli", str(tmp_path)), encoding="utf-8") as f:
            leido = ResultadoAnalisis.desde_dict(json.loads(f.readline()))
        assert leido == original and leido.no_competible

    def test_claves_desconocidas_y_tipos_sucios(self):
        d = dict(LINEA_PRE_P4, competible="false", motivo_bloqueo=None, campo_futuro=1)
        res = ResultadoAnalisis.desde_dict(d)
        assert res.competible is None  # solo un bool real cuenta
        assert res.motivo_bloqueo == ""

    def test_dict_vacio(self):
        res = ResultadoAnalisis.desde_dict({})
        assert res.proceso_uuid == "" and res.matriz is None and res.competible is None
