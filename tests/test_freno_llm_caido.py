"""No publicar resultados cuando no hubo clasificación LLM."""
from types import SimpleNamespace

import pytest

import src.main as main
from src.classifier.engine import ResultadoClasificacion
from src.llm.contrato import LLMError


@pytest.mark.parametrize("previos", [0, 2])
@pytest.mark.parametrize("drilldown", [False, True])
@pytest.mark.parametrize("clientes,caidos", [(1, 1), (2, 1), (1, 0)])
@pytest.mark.parametrize("lote_unico", [False, True])
def test_pipeline(monkeypatch, tmp_path, drilldown, clientes, caidos, lote_unico, previos):
    cfg = SimpleNamespace(data_dir=str(tmp_path), clients_dir="clients", enable_drilldown=drilldown)
    profiles = [SimpleNamespace(client_id=str(i), client_name=str(i), portal_id="comprasal_sv")
                for i in range(clientes)]
    async def sesion(*a):
        return True
    async def extraer(*a):
        return [object()]
    def clasificar(procesos, config, profile):
        if int(profile.client_id) >= caidos:
            return []
        resultado = ResultadoClasificacion(1, "oci")
        error = LLMError("caído", proveedor="oci")
        resultado.registrar_fallo(error)
        if lote_unico:
            error.resultado_clasificacion = resultado
            raise error
        return resultado
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    monkeypatch.setattr(main, "load_all_active_profiles", lambda *a: profiles)
    monkeypatch.setattr(main, "is_session_valid", sesion)
    monkeypatch.setattr(main, "extract_recent_procesos", extraer)
    monkeypatch.setattr(main, "classify_procesos", clasificar)
    monkeypatch.setattr(main, "_consecutive_errors", previos)
    monkeypatch.setattr(main, "_preparar_estado", lambda *a: None)
    monkeypatch.setattr("src.memoria.service.fallos_consecutivos", lambda *a: 0)
    monkeypatch.setattr("src.drilldown.pipeline.limpiar_pliegos", lambda *a: None)
    ops, breaker, estados, registros, guardados, reportes = [], [], [], [], [], []
    monkeypatch.setattr("src.notifier.ops_alert.send_ops_alert", lambda *a: ops.append(a))
    monkeypatch.setattr(main, "send_circuit_breaker_alert", lambda *a: breaker.append(a))
    monkeypatch.setattr(main, "_save_last_run", lambda *a, **kw: estados.append(kw))
    monkeypatch.setattr(main, "_registrar_ejecucion", lambda *a, **kw: None)
    monkeypatch.setattr(main, "_registrar_clasificados", lambda c, ident, r: registros.append(ident))
    monkeypatch.setattr(main, "save_run", lambda **kw: guardados.append(kw["client_id"]))
    monkeypatch.setattr(main, "send_report", lambda **kw: reportes.append(kw["client_profile"].client_id))
    main.run_pipeline()
    sanos = [str(i) for i in range(caidos, clientes)]
    assert reportes == sanos
    assert registros == guardados == sanos
    assert estados[-1]["success"] is (caidos != clientes)
    assert main._consecutive_errors == (previos + 1 if caidos == clientes else 0)
    assert not breaker
    assert len(ops) == caidos + (1 if caidos == clientes and previos == 2 else 0)


@pytest.mark.parametrize("legado", [False, True])
def test_etapas_persisten_caida_y_omiten_reporte(monkeypatch, tmp_path, legado):
    import json

    cfg = SimpleNamespace(data_dir=str(tmp_path), clients_dir="clients")
    profiles = [SimpleNamespace(client_id=i, client_name=i, portal_id="comprasal_sv")
                for i in ("caido", "sano")]
    async def sesion(*a):
        return True
    async def extraer(*a):
        return [object()]
    def clasificar(config, profile, procesos):
        if profile.client_id == "sano":
            return []
        resultado = ResultadoClasificacion(1, "oci")
        resultado.registrar_fallo(LLMError("caído", proveedor="oci"))
        return resultado
    monkeypatch.setattr(main, "load_all_active_profiles", lambda *a: profiles)
    monkeypatch.setattr(main, "is_session_valid", sesion)
    monkeypatch.setattr(main, "extract_recent_procesos", extraer)
    monkeypatch.setattr(main, "_clasificar_con_alerta", clasificar)
    monkeypatch.setattr(main, "_preparar_estado", lambda *a: None)
    registros, guardados, reportes = [], [], []
    monkeypatch.setattr(main, "_registrar_clasificados", lambda c, ident, r: registros.append(ident))
    monkeypatch.setattr(main, "save_run", lambda **kw: guardados.append(kw["client_id"]))
    monkeypatch.setattr(main, "send_report", lambda **kw: reportes.append(kw["client_profile"].client_id))
    monkeypatch.setattr(main, "_save_last_run", lambda *a, **kw: None)
    monkeypatch.setattr("src.drilldown.pipeline.limpiar_pliegos", lambda *a: None)
    monkeypatch.setattr("src.memoria.service.cerrar", lambda *a: None)
    main._stage_scrape(cfg)
    path = tmp_path / "stage_handoff.json"
    manifiesto = json.loads(path.read_text())
    assert manifiesto["clientes"]["caido"]["llm_caido"] is True
    assert manifiesto["clientes"]["sano"]["llm_caido"] is False
    assert registros == ["sano"]
    if legado:
        for datos in manifiesto["clientes"].values():
            datos.pop("llm_caido")
        path.write_text(json.dumps(manifiesto))
    main._stage_analyze(cfg)
    assert reportes == guardados == (["caido", "sano"] if legado else ["sano"])
    assert not path.exists()
