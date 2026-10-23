"""Puntos de unión entre P1 (historial), P3 (texto limpio) y P4 (competible).

Cada paso se probó por separado; aquí se verifica que la señal y el texto limpio
atraviesan los puntos donde antes se perdían: filas NOTIFICADO, reimportación,
carga del historial y el JSON parseado del modelo.
"""
from types import SimpleNamespace

from src import history, main
from src.drilldown import analysis
from src.memoria import reimportar
from src.scraper.models import MatrizRiesgos, ResultadoAnalisis, RiesgoItem


def _analizado(uuid="4303-2026-P0361") -> ResultadoAnalisis:
    return ResultadoAnalisis(
        proceso_uuid=uuid, estado="ANALIZADO", formato="NATIVO", triaje="GO",
        competible=False, motivo_bloqueo="proveedor_unico",
        matriz=MatrizRiesgos(
            items=[RiesgoItem("legal", "Proveedor único", "alta", "art. 41")],
            resumen="Contratación directa con proveedor único.",
        ),
    )


def test_load_analisis_conserva_competible_y_motivo(tmp_path):
    history.save_analisis("c", _analizado(), str(tmp_path))
    out = history.load_analisis("c", ["4303-2026-P0361"], str(tmp_path))
    res = out["4303-2026-P0361"]
    assert res.competible is False
    assert res.motivo_bloqueo == "proveedor_unico"
    assert res.matriz.items[0].categoria == "legal"


def test_la_fila_notificado_conserva_la_senal(tmp_path, monkeypatch):
    guardadas = []
    monkeypatch.setattr(
        "src.memoria.service.guardar_estado", lambda cfg, cid, r: guardadas.append(r)
    )
    main._marcar_notificados(SimpleNamespace(), "c", {"4303-2026-P0361": _analizado()})
    assert len(guardadas) == 1
    assert guardadas[0].estado == "NOTIFICADO"
    assert guardadas[0].competible is False
    assert guardadas[0].motivo_bloqueo == "proveedor_unico"


def test_reimportar_no_descarta_la_senal():
    res = reimportar._deserializar_resultado_analisis(
        {"proceso_uuid": "p", "estado": "ANALIZADO", "competible": False,
         "motivo_bloqueo": "marca_especifica"}
    )
    assert res.competible is False and res.motivo_bloqueo == "marca_especifica"
    # una línea anterior a P4, o con un valor que no es bool, queda en «desconocido»
    assert reimportar._deserializar_resultado_analisis({"competible": "no"}).competible is None
    assert reimportar._deserializar_resultado_analisis({}).motivo_bloqueo == ""


def test_el_json_del_modelo_pasa_por_el_normalizador_de_texto():
    crudo = {
        "resumen": "especßicas condiciones",
        "items": [{"clausula_ref": "LEY DE COMPRAS PÓBLICAS", "severidad": "alta"}],
        "n": 3,
    }
    limpio = analysis._limpiar_texto_modelo(crudo)
    assert limpio["resumen"] == "específicas condiciones"
    assert limpio["items"][0]["clausula_ref"] == "LEY DE COMPRAS PÚBLICAS"
    assert limpio["items"][0]["severidad"] == "alta"
    assert limpio["n"] == 3
    assert analysis._limpiar_texto_modelo(limpio) == limpio  # idempotente
