"""Alertas de caída total: motor real con lotes dobles, sin red."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.main as main
from src.classifier import engine
from src.llm.contrato import LLMError
from src.scraper.models import Proceso, ProcesoClasificado


@pytest.mark.parametrize("camino", ["normal", "drilldown"])
@pytest.mark.parametrize("fallos", [{1, 2}, {1}, set()])
@pytest.mark.parametrize("alerta_explota", [False, True])
def test_alerta_solo_si_fallan_todos(monkeypatch, camino, fallos, alerta_explota, caplog):
    cfg = SimpleNamespace(intel_clasif="off", app_name="test", data_dir="/sin-io")
    profile = SimpleNamespace(client_id="cliente", client_name="Cliente", portal_id="comprasal_sv")
    procesos = [Proceso(institucion="Inst", descripcion=f"Servidor {i}",
                        fecha_publicacion=datetime.now(timezone.utc), url_directa="https://example.org")
                for i in range(2)]
    monkeypatch.setattr(engine, "get_system_prompt", lambda *a: "prompt")
    monkeypatch.setattr(engine.llm, "proveedor_activo", lambda c: "oci")
    monkeypatch.setattr(engine.llm, "modelo_de", lambda *a: "modelo")
    monkeypatch.setattr(engine, "_batch_size", lambda c: 1)
    monkeypatch.setattr(engine, "filtrar_marcas_por_portafolio", lambda *a: None)
    monkeypatch.setattr(engine, "_aplicar_filtro_ruido", lambda *a: None)
    monkeypatch.setattr("src.drilldown.quota.cobrar", lambda *a: None)
    llamadas = []

    def lote(batch, *args):
        llamadas.append(batch)
        if len(llamadas) in fallos:
            raise LLMError("SDK OCI ausente", proveedor="oci")
        return [ProcesoClasificado.from_proceso(p) for p in batch], engine.UsoModelo(modelo="modelo")

    monkeypatch.setattr(engine, "_classify_batch", lote)
    alertas = []

    def alertar(*args):
        alertas.append(args)
        if alerta_explota:
            raise RuntimeError("SMTP caído")

    monkeypatch.setattr("src.notifier.ops_alert.send_ops_alert", alertar)
    reportes = []
    for nombre in ("_rescatar_semanticos", "_registrar_clasificados", "_reconciliar_marcas", "save_run"):
        monkeypatch.setattr(main, nombre, lambda *a, **kw: None)
    monkeypatch.setattr(main, "send_report", lambda **kw: reportes.append(kw))
    monkeypatch.setattr("src.drilldown.pipeline.limpiar_pliegos", lambda *a: None)
    timestamp = datetime.now(timezone.utc)
    if camino == "normal":
        assert main._classify_and_notify(cfg, profile, procesos, 2, timestamp) == 0
    else:
        assert main._run_clients_with_drilldown(cfg, [profile], procesos, 2, timestamp) == 0
    assert len(llamadas) == 2
    assert "Error al clasificar" not in caplog.text
    assert len(reportes) == (0 if fallos == {1, 2} else 1)
    assert len(alertas) == (1 if fallos == {1, 2} else 0)
    if alertas:
        assert alertas[0][0] is cfg
        assert alertas[0][1] == "LLM caído: 0/2 procesos clasificados"
        assert all(s in alertas[0][2] for s in ("cliente", "oci", "SDK OCI ausente"))
        assert "todos los lotes" in caplog.text


@pytest.mark.parametrize("alerta_explota", [False, True])
def test_lote_unico_alerta_y_conserva_excepcion(monkeypatch, alerta_explota):
    diagnostico = engine.ResultadoClasificacion(1, "oci")
    error = LLMError("primer error", proveedor="oci")
    diagnostico.registrar_fallo(error)
    error.resultado_clasificacion = diagnostico

    def clasificar(*args):
        raise error

    alertas = []

    def alertar(*args):
        alertas.append(args)
        if alerta_explota:
            raise RuntimeError("SMTP caído")

    monkeypatch.setattr(main, "classify_procesos", clasificar)
    monkeypatch.setattr("src.notifier.ops_alert.send_ops_alert", alertar)
    profile = SimpleNamespace(client_id="cliente", client_name="Cliente")
    with pytest.raises(LLMError) as capturado:
        main._clasificar_con_alerta(object(), profile, [object()])
    assert capturado.value is error
    assert len(alertas) == 1
    assert alertas[0][1] == "LLM caído: 0/1 procesos clasificados"


def test_diagnostico_conserva_primer_error_y_no_marca_cero_lotes():
    assert not engine.ResultadoClasificacion(0, "oci").llm_caido
    resultado = engine.ResultadoClasificacion(2, "oci")
    resultado.registrar_fallo(LLMError("primero", proveedor="oci"))
    assert not resultado.llm_caido
    resultado.registrar_fallo(LLMError("segundo", proveedor="oci"))
    assert resultado.llm_caido
    assert resultado.n_fallidos == resultado.n_lotes == 2
    assert "primero" in resultado.primer_error
    assert "segundo" not in resultado.primer_error
