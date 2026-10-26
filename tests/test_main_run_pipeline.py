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
