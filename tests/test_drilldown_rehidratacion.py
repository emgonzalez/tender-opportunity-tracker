"""Rehidratación del resultado previo cuando el pliego ya está en estado terminal.

Caso real (DEV): en la 2.ª corrida del mismo día un proceso NOTIFICADO con
`competible=False/proveedor_unico` volvía del salto por estado terminal como un
`ResultadoAnalisis` vacío y caía en «Para decidir». Aquí: sin red, sin LLM, con
referencias ficticias y la frase de bloqueo tal como la emite el análisis.
"""
import types

import fitz  # pymupdf
import pytest

from src import history
from src.drilldown import pipeline, quota
from src.memoria import service
from src.notifier import agrupamiento
from src.scraper.models import (
    MOTIVO_PROVEEDOR_UNICO,
    MatrizRiesgos,
    Proceso,
    ResultadoAnalisis,
    RiesgoItem,
)

REF = "0000-2099-P0001"  # ficticia
PORTAFOLIO = ("HPE", "Nutanix", "Fortinet")


def _cfg(tmp_path):
    return types.SimpleNamespace(
        data_dir=str(tmp_path), budget_path=str(tmp_path / "gasto.json"),
        pliego_max_pages=100, triage_pages=15, triage_raster_pages=5,
        daily_budget_usd=5.0, analisis_tokens_por_pagina=3000,
        drilldown_tmp_dir=str(tmp_path / "tmp"), model_triage="claude-haiku-4-5",
        model_analysis="claude-sonnet-5", anthropic_api_key="x", enable_memoria=False,
    )


def _pdf(tmp_path):
    path = tmp_path / "p.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Licenciamiento de servidor y firewall.")
    doc.save(str(path))
    doc.close()
    return str(path)


def _proceso(forma="Contratación directa"):
    return Proceso(institucion="Institución de prueba", descripcion="Licenciamiento de software",
                   fecha_publicacion=None, url_directa="https://example.com/p/1",
                   numero_referencia=REF, forma_contratacion=forma)


def _matriz():
    return MatrizRiesgos(
        resumen="Renovación de licenciamiento de una plataforma de inteligencia de negocios.",
        items=[RiesgoItem(
            "legal", "La forma de contratación limita la competencia.", "alta",
            "Contratación Directa con Proveedor Único por Marca Específica, de acuerdo con "
            "el art. 41 de la Ley de Compras Públicas.")],
    )


def _sembrar(cfg, *resultados):
    for r in resultados:
        history.save_analisis("cli", r, cfg.data_dir)


@pytest.fixture
def prohibido(monkeypatch):
    """Cualquier cobro de LLM o persistencia en el salto rompe el test."""
    llamadas = []

    def _boom(nombre):
        def f(*a, **kw):
            llamadas.append(nombre)
            raise AssertionError(f"el salto por estado terminal no debe llamar a {nombre}")
        return f

    monkeypatch.setattr(pipeline.triage, "triage", _boom("triage"))
    monkeypatch.setattr(pipeline.analysis, "analizar_pliego", _boom("analizar_pliego"))
    monkeypatch.setattr(quota, "cobrar", _boom("quota.cobrar"))
    monkeypatch.setattr(service, "guardar_estado", _boom("guardar_estado"))
    monkeypatch.setattr(service, "registrar_proceso", _boom("registrar_proceso"))
    monkeypatch.setattr(service, "persistir_memoria", _boom("persistir_memoria"))
    return llamadas


def _correr(cfg, tmp_path, proceso=None, marcas=PORTAFOLIO):
    return pipeline.analizar_pliego_proceso(
        proceso or _proceso(), _pdf(tmp_path), cfg, "ctx", "cli", ["servidor"], [],
        marcas_cliente=marcas)


def _lineas(cfg):
    with open(history._analisis_path("cli", cfg.data_dir), encoding="utf-8") as f:
        return f.read().splitlines()


def test_notificado_con_bloqueo_sigue_no_competible(tmp_path, prohibido):
    cfg = _cfg(tmp_path)
    _sembrar(cfg,
             ResultadoAnalisis(REF, "ANALIZADO", triaje="GO", triaje_confianza=0.9, matriz=_matriz(),
                               competible=False, motivo_bloqueo=MOTIVO_PROVEEDOR_UNICO,
                               monto_estimado=1234.5),
             ResultadoAnalisis(REF, "NOTIFICADO", triaje="GO", competible=False,
                               motivo_bloqueo=MOTIVO_PROVEEDOR_UNICO))
    antes = _lineas(cfg)
    proceso = _proceso()

    res = _correr(cfg, tmp_path, proceso)

    assert res.estado == "NOTIFICADO"
    assert (res.competible, res.motivo_bloqueo) == (False, MOTIVO_PROVEEDOR_UNICO)
    assert res.matriz is not None and res.matriz.items
    assert res.triaje == "GO"
    assert proceso.monto == 1234.5
    assert agrupamiento.grupo_de(95, res) == agrupamiento.GRUPO_NO_COMPETIBLE
    assert _lineas(cfg) == antes and prohibido == []


def test_linea_antigua_sin_competible_se_rederiva(tmp_path, prohibido):
    """Una línea previa a P4 (sin `competible`) se re-deriva con la forma y el portafolio."""
    cfg = _cfg(tmp_path)
    _sembrar(cfg, ResultadoAnalisis(REF, "ANALIZADO", triaje="GO", matriz=_matriz()),
             ResultadoAnalisis(REF, "NOTIFICADO"))

    res = _correr(cfg, tmp_path)

    assert res.estado == "NOTIFICADO"
    assert res.competible is False and res.motivo_bloqueo
    assert agrupamiento.grupo_de(95, res) == agrupamiento.GRUPO_NO_COMPETIBLE


def test_descartado_triaje_conserva_no_go(tmp_path, prohibido):
    cfg = _cfg(tmp_path)
    _sembrar(cfg, ResultadoAnalisis(REF, "DESCARTADO_TRIAJE", triaje="NO-GO", triaje_confianza=0.95,
                                    anexos={"triaje_motivo": "tóners"}))

    res = _correr(cfg, tmp_path)

    assert res.estado == "DESCARTADO_TRIAJE"
    assert res.triaje == "NO-GO" and res.triaje_confianza == 0.95
    assert res.anexos["triaje_motivo"] == "tóners"
    assert agrupamiento.grupo_de(95, res) == agrupamiento.GRUPO_BAJA


def test_revision_manual_conserva_error(tmp_path, prohibido):
    cfg = _cfg(tmp_path)
    _sembrar(cfg, ResultadoAnalisis(REF, "REQUIERE_REVISION_MANUAL", triaje="GO",
                                    triaje_confianza=0.4, error="confianza de triaje baja (0.40)"))

    res = _correr(cfg, tmp_path)

    assert res.estado == "REQUIERE_REVISION_MANUAL"
    assert res.error == "confianza de triaje baja (0.40)"
    assert res.triaje == "GO"


def test_sin_historial_util_queda_vacio(tmp_path, prohibido, monkeypatch):
    """El estado terminal existe (p. ej. en la ADB) pero no hay nada que rehidratar."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(service, "estado_de", lambda cfg, cid, uuid: "NOTIFICADO")

    res = _correr(cfg, tmp_path)

    assert res.estado == "NOTIFICADO" and res.proceso_uuid == REF
    assert res.matriz is None and res.competible is None and res.triaje == "" and res.error == ""


def test_load_analisis_que_falla_no_lanza(tmp_path, prohibido, monkeypatch):
    cfg = _cfg(tmp_path)
    _sembrar(cfg, ResultadoAnalisis(REF, "NOTIFICADO", competible=False,
                                    motivo_bloqueo=MOTIVO_PROVEEDOR_UNICO))

    def roto(*a, **kw):
        raise RuntimeError("historial ilegible")

    monkeypatch.setattr(history, "load_analisis", roto)
    res = _correr(cfg, tmp_path)

    # Cae a la marca NOTIFICADO del JSONL, que ya lleva la señal.
    assert res.estado == "NOTIFICADO" and res.competible is False


def test_adb_caida_cae_al_jsonl(tmp_path, prohibido, monkeypatch):
    """ESTADO_FUENTE=adb con la ADB inalcanzable (DSN inválido): manda el JSONL, sin excepción."""
    cfg = _cfg(tmp_path)
    _sembrar(cfg,
             ResultadoAnalisis(REF, "ANALIZADO", triaje="GO", matriz=_matriz(),
                               competible=False, motivo_bloqueo=MOTIVO_PROVEEDOR_UNICO),
             ResultadoAnalisis(REF, "NOTIFICADO"))
    antes = _lineas(cfg)

    monkeypatch.setenv("ESTADO_FUENTE", "adb")
    monkeypatch.setattr(history, "_cfg", lambda: types.SimpleNamespace(
        adb_dsn="dsn-invalido.example.com:1522/no_existe", adb_password="no-se-usa"))
    intentos = []

    def conexion_invalida(cfg):
        intentos.append(cfg.adb_dsn)
        raise ConnectionError("DPY-4000: cannot connect: DSN inválido")

    from src.memoria import db
    monkeypatch.setattr(db, "connection", conexion_invalida)

    res = _correr(cfg, tmp_path)

    assert intentos, "debió intentar la ADB antes de caer al JSONL"
    assert res.estado == "NOTIFICADO"
    assert (res.competible, res.motivo_bloqueo) == (False, MOTIVO_PROVEEDOR_UNICO)
    assert agrupamiento.grupo_de(95, res) == agrupamiento.GRUPO_NO_COMPETIBLE
    assert _lineas(cfg) == antes and prohibido == []
