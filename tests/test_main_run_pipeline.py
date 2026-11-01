"""Tests de `src.main.run_pipeline` — el circuit breaker real (plan del portal, §9).

`run_pipeline` no tenía ningún test hasta ahora (función grande, con muchas
dependencias de I/O). Estos dos tests cubren específicamente lo que se acaba
de arreglar, no la función entera:

1. Que el "fallo persistido" (`service.fallos_consecutivos`, historial en la
   ADB) efectivamente se sume al contador de proceso al arrancar.
2. **El caso que antes era imposible de reproducir**: con `_consecutive_errors`
   en 0 (proceso recién arrancado, como pasa cada `docker run --rm`), el
   circuit breaker YA NO depende de que este mismo proceso vea 3 fallos — un
   solo fallo en esta corrida, sumado a 2 ya persistidos de corridas
   anteriores, alcanza el umbral y dispara la alerta. Con el código viejo esto
   nunca ocurría: `_consecutive_errors` siempre nacía en 0.

Todo el resto de `run_pipeline` (sesión, extracción, clasificación) se mockea:
no es el objeto de este test.
"""
import types

import src.main as main


def _cfg(**overrides):
    base = {
        "clients_dir": "clients",
        "enable_drilldown": False,
        "last_run_path": "/tmp/amalia-test-last-run.json",
        "data_dir": "/tmp",
        "app_version": "test-sha",
        "enable_memoria": True,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _cliente(client_id="cli", portal_id="comprasal_sv"):
    return types.SimpleNamespace(client_id=client_id, portal_id=portal_id)


async def _sesion_valida(cfg):
    return True


async def _sin_procesos(cfg):
    return []


def test_hidrata_desde_el_historial_persistido_y_registra_el_exito(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_consecutive_errors", 0)
    cfg = _cfg(last_run_path=str(tmp_path / "last_run.json"), data_dir=str(tmp_path))
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    monkeypatch.setattr(main, "load_all_active_profiles", lambda d: [_cliente()])
    monkeypatch.setattr(main, "is_session_valid", _sesion_valida)
    monkeypatch.setattr(main, "extract_recent_procesos", _sin_procesos)
    monkeypatch.setattr(main, "_classify_and_notify", lambda *a, **kw: 0)

    llamado_fallos = []
    monkeypatch.setattr(
        "src.memoria.service.fallos_consecutivos",
        lambda c: llamado_fallos.append(1) or 2,
    )
    registradas = []
    monkeypatch.setattr(
        "src.memoria.service.registrar_ejecucion",
        lambda c, ts, success, **kw: registradas.append((success, kw)),
    )

    main.run_pipeline()

    assert llamado_fallos == [1], "debía consultar el historial persistido al arrancar"
    assert len(registradas) == 1
    success, kw = registradas[0]
    assert success is True
    assert kw["procesos_revisados"] == 0
    assert kw["clientes_procesados"] == 1
    # Tras un éxito, el contador de proceso se resetea (mismo comportamiento de siempre).
    assert main._consecutive_errors == 0


def test_el_circuit_breaker_dispara_con_un_solo_fallo_de_proceso_mas_el_historial(
    monkeypatch, tmp_path
):
    """La regresión que el fix cierra: antes, `_consecutive_errors` siempre
    nacía en 0 en cada `docker run --rm` y el breaker (umbral 3) nunca podía
    dispararse con un solo fallo. Con el historial persistido devolviendo 2,
    un solo fallo de ESTA corrida ya alcanza el umbral.
    """
    monkeypatch.setattr(main, "_consecutive_errors", 0)
    cfg = _cfg(last_run_path=str(tmp_path / "last_run.json"), data_dir=str(tmp_path))
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    monkeypatch.setattr(main, "load_all_active_profiles", lambda d: [_cliente()])
    monkeypatch.setattr(main, "is_session_valid", _sesion_valida)

    async def _explota(cfg_):
        raise RuntimeError("fallo simulado de esta corrida")

    monkeypatch.setattr(main, "extract_recent_procesos", _explota)
    monkeypatch.setattr("src.memoria.service.fallos_consecutivos", lambda c: 2)
    registradas = []
    monkeypatch.setattr(
        "src.memoria.service.registrar_ejecucion",
        lambda c, ts, success, **kw: registradas.append((success, kw)),
    )
    alertas = []
    monkeypatch.setattr(
        main, "send_circuit_breaker_alert",
        lambda cfg_, cliente, n, motivo: alertas.append((cliente.client_id, n)),
    )

    main.run_pipeline()

    assert main._consecutive_errors == 3, (
        "2 persistidos + 1 de esta corrida = 3: exactamente el umbral"
    )
    assert alertas == [("cli", 3)], "el circuit breaker debía dispararse en ESTA corrida"
    assert registradas and registradas[0][0] is False


class TestPortales:
    """Un cliente declara su portal; el pipeline extrae una vez por portal integrado."""

    def test_agrupa_por_portal(self):
        a, b, c = _cliente("a"), _cliente("b", "sicop_cr"), _cliente("c")
        assert main.agrupar_por_portal([a, b, c]) == {"comprasal_sv": [a, c], "sicop_cr": [b]}

    def test_los_portales_sin_extractor_se_omiten_con_aviso(self, caplog):
        sv, cr, raro = _cliente("sv"), _cliente("cr", "sicop_cr"), _cliente("x", "no_existe")
        with caplog.at_level("WARNING"):
            integrados = main._agrupar_integrados([sv, cr, raro])
        assert integrados == {"comprasal_sv": [sv]}
        assert "sicop_cr" in caplog.text and "sin extractor" in caplog.text
        assert "portal desconocido" in caplog.text

    def test_etapas_con_un_solo_portal_integrado(self, monkeypatch):
        sv, cr = _cliente("sv"), _cliente("cr", "sicop_cr")
        assert main._un_solo_portal([sv, cr]) == [sv]
        monkeypatch.setitem(main.portales.PORTALES, "sicop_cr",
                            main.portales.Portal("sicop_cr", "SICOP", "CR", "America/Costa_Rica",
                                                 "CRC", "https://x", integrado=True))
        import pytest
        with pytest.raises(RuntimeError, match="un solo portal"):
            main._un_solo_portal([sv, cr])

    def test_un_cliente_de_costa_rica_no_dispara_alerta_de_sesion_ni_extrae(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "_consecutive_errors", 0)
        cfg = _cfg(last_run_path=str(tmp_path / "last_run.json"), data_dir=str(tmp_path))
        monkeypatch.setattr(main, "get_config", lambda: cfg)
        monkeypatch.setattr(main, "load_all_active_profiles",
                            lambda d: [_cliente("sv"), _cliente("cr", "sicop_cr")])
        llamadas = {"extrae": 0, "alerta": [], "clasifica": []}

        async def sesion(cfg):
            return True

        async def extraer(cfg):
            llamadas["extrae"] += 1
            return []

        monkeypatch.setattr(main, "is_session_valid", sesion)
        monkeypatch.setattr(main, "extract_recent_procesos", extraer)
        monkeypatch.setattr(main, "send_session_expired_alert", lambda c, cl: llamadas["alerta"].append(cl))
        monkeypatch.setattr(main, "_classify_and_notify",
                            lambda config, cliente, *a, **kw: llamadas["clasifica"].append(cliente.client_id) or 0)
        monkeypatch.setattr("src.memoria.service.fallos_consecutivos", lambda c: 0)
        monkeypatch.setattr("src.memoria.service.registrar_ejecucion", lambda *a, **kw: None)

        main.run_pipeline()

        assert llamadas == {"extrae": 1, "alerta": [], "clasifica": ["sv"]}


def test_reconciliacion_off_no_consulta_capas(monkeypatch):
    def prohibida(*args, **kwargs):
        raise AssertionError("off debe evitar la capa")

    monkeypatch.setattr("src.drilldown.reconciliar.reconciliar", prohibida)
    main._reconciliar_marcas(_cfg(marca_veredicto="off"), _cliente(), [], {})


def test_reconciliacion_fallida_no_impide_reporte(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    cfg = _cfg(marca_veredicto="sombra", data_dir=str(tmp_path))
    profile = types.SimpleNamespace(client_id="cli", client_name="Ejemplo")
    eventos = []

    def fallo(*args, **kwargs):
        eventos.append("reconciliar")
        raise RuntimeError("capa no disponible")

    monkeypatch.setattr("src.drilldown.reconciliar.reconciliar", fallo)
    monkeypatch.setattr(main, "_registrar_clasificados", lambda *a: None)
    monkeypatch.setattr(main, "save_run", lambda **kw: eventos.append("guardar"))
    monkeypatch.setattr(main, "send_report", lambda **kw: eventos.append("correo"))
    assert main._classify_and_notify(cfg, profile, [], 0, datetime.now(timezone.utc)) == 0
    assert eventos == ["reconciliar", "guardar", "correo"]


def test_reconcilia_despues_del_drilldown_antes_del_historial_y_correo(monkeypatch, tmp_path):
    import json
    from datetime import datetime, timezone

    from src.drilldown import pipeline

    cfg = _cfg(marca_veredicto="sombra", data_dir=str(tmp_path))
    profile = _cliente()
    proceso = types.SimpleNamespace(es_relevante=True, numero_referencia="TEST-1")
    eventos = []
    resultados = {"TEST-1": types.SimpleNamespace(estado="NO_ANALIZADO")}

    async def descargar(*args):
        return {"TEST-1": "falso.pdf"}

    def analizar(*args):
        eventos.append("analizar")
        return resultados

    def reconciliar(clasificados, analisis, config, perfil):
        assert clasificados == [proceso] and analisis is resultados
        assert perfil is profile
        eventos.append("reconciliar")

    monkeypatch.setattr(main, "classify_procesos", lambda *a: [proceso])
    monkeypatch.setattr(main, "_registrar_clasificados", lambda *a: None)
    monkeypatch.setattr(pipeline, "descargar_pliegos", descargar)
    monkeypatch.setattr(pipeline, "procesar_drilldown", analizar)
    monkeypatch.setattr(pipeline, "limpiar_pliegos", lambda *a: None)
    monkeypatch.setattr("src.drilldown.reconciliar.reconciliar", reconciliar)
    monkeypatch.setattr(main, "save_run", lambda **kw: eventos.append("guardar"))
    monkeypatch.setattr(main, "send_report", lambda **kw: eventos.append("correo"))
    timestamp = datetime.now(timezone.utc)
    main._run_clients_with_drilldown(cfg, [profile], [proceso], 1, timestamp)
    assert eventos == ["analizar", "reconciliar", "guardar", "correo"]

    eventos.clear()
    manifiesto = {"run_timestamp": timestamp.isoformat(), "total_revisados": 1,
                  "pdf_paths": {"TEST-1": "falso.pdf"},
                  "clientes": {"cli": {"clasificados": [{}]}}}
    (tmp_path / "stage_handoff.json").write_text(json.dumps(manifiesto))
    monkeypatch.setattr("src.history._deserialize", lambda d: proceso)
    monkeypatch.setattr(main, "load_all_active_profiles", lambda *a: [profile])
    monkeypatch.setattr(main, "_marcar_notificados", lambda *a: None)
    monkeypatch.setattr(main, "_save_last_run", lambda *a, **kw: None)
    monkeypatch.setattr("src.memoria.service.cerrar", lambda *a: None)
    main._stage_analyze(cfg)
    assert eventos == ["analizar", "reconciliar", "guardar", "correo"]
