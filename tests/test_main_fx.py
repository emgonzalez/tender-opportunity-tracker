import sys
from dataclasses import asdict
from datetime import datetime
from threading import Event, current_thread
from time import monotonic
from types import SimpleNamespace

import pytest

from src import main
from src.memoria import fx_diario, service
from src.memoria.fx_regla import TasaDia
from src.portales.moneda import texto_monto
from src.scraper.models import Proceso, ProcesoClasificado


@pytest.fixture
def entorno(monkeypatch, tmp_path) -> SimpleNamespace:
    cfg = SimpleNamespace(
        data_dir=str(tmp_path), last_run_path=str(tmp_path / "last_run.json"), clients_dir="clients",
        app_version="test", log_level="INFO", enable_drilldown=False,
        fx_auto=True, fx_api_url="https://hacienda.test", enable_memoria=False, enable_esquema_v2=False,
        fx_alerta_dias=5, fx_repetir_alerta_dias=3,
    )
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    monkeypatch.setattr(main, "_asegurar_sesion", lambda *a: None)
    monkeypatch.setattr(main, "_desde", lambda *a: None)
    monkeypatch.setattr(main, "_consecutive_errors", 0)
    monkeypatch.setattr(fx_diario, "_corrida", None)
    return cfg


def _proceso(portal_id: str, moneda: str) -> Proceso:
    return Proceso(
        "Institución", "Compra", datetime.now(main.TZ_ELSALVADOR), "https://portal.test",
        numero_referencia="123", portal_id=portal_id, moneda=moneda, monto=1000,
    )


def _fallo_fx(*args, **kwargs) -> None:
    raise RuntimeError("fallo simulado de FX")


def test_cerrar_fx_limita_espera_en_hilo_daemon(entorno, monkeypatch, caplog) -> None:
    liberar = Event()
    iniciado = Event()
    hilos = []

    def cerrar(config) -> None:
        hilos.append(current_thread())
        iniciado.set()
        liberar.wait(timeout=2)

    monkeypatch.setattr(fx_diario, "cerrar", cerrar)
    try:
        inicio = monotonic()
        main._cerrar_fx(entorno, limite_s=0.01)
        assert monotonic() - inicio < 1
        assert iniciado.wait(timeout=1)
        assert hilos[0].daemon and hilos[0].is_alive()
        assert "superó el límite" in caplog.text
    finally:
        liberar.set()
        if hilos:
            hilos[0].join(timeout=1)


@pytest.mark.parametrize("falla_scrape", [False, True])
def test_cli_scrape_ejecuta_el_cierre_fx_aun_si_falla(entorno, monkeypatch, caplog, falla_scrape) -> None:
    eventos = []

    def scrape(config) -> None:
        eventos.append(("scrape", config))
        if falla_scrape:
            raise RuntimeError("fallo de scrape")

    def cerrar(config) -> None:
        eventos.append(("cerrar", config))
        _fallo_fx()

    monkeypatch.setattr(sys, "argv", ["src.main", "--stage", "scrape"])
    monkeypatch.setattr(main, "_setup_logging", lambda _: None)
    monkeypatch.setattr(service, "ping_con_estado", lambda _: SimpleNamespace(debe_alertar=False))
    monkeypatch.setattr(main, "_stage_scrape", scrape)
    monkeypatch.setattr(fx_diario, "cerrar", cerrar)

    if falla_scrape:
        with pytest.raises(RuntimeError, match="fallo de scrape"):
            main._cli()
    else:
        main._cli()
    assert eventos == [("scrape", entorno), ("cerrar", entorno)]
    assert "Falló el cierre" in caplog.text


def test_fallos_fx_no_rompen_extraccion_ni_corrida(entorno, monkeypatch, caplog) -> None:
    entorno.portales_deshabilitados = {"sicop_cr"}
    clientes = [SimpleNamespace(client_id="sv", portal_id="comprasal_sv"),
                SimpleNamespace(client_id="cr", portal_id="sicop_cr")]
    compra = _proceso("comprasal_sv", "")
    original = asdict(compra)
    opciones = []

    async def extraer(config, portal, desde) -> list[Proceso]:
        assert portal.portal_id == "comprasal_sv"
        return [compra]

    def preparar(config, activos) -> None:
        opciones.extend(p.portal_id for p in activos)
        _fallo_fx()

    monkeypatch.setattr(main, "load_all_active_profiles", lambda _: clientes)
    monkeypatch.setattr(main.portales, "cargar_extractor", lambda _: extraer)
    monkeypatch.setattr(fx_diario, "preparar", preparar)
    monkeypatch.setattr(fx_diario, "enriquecer", _fallo_fx)
    monkeypatch.setattr(fx_diario, "cerrar", _fallo_fx)

    portal = main.portales.obtener_portal("comprasal_sv")
    assert main._extraer_portal(entorno, portal) == [compra]
    vistos = []
    monkeypatch.setattr(main, "_procesar_portal", lambda cfg, p, c, procesos, *a: vistos.extend(procesos) or 0)
    main.run_pipeline()

    assert opciones == ["comprasal_sv"]
    assert vistos == [compra]
    assert asdict(compra) == original
    assert texto_monto(compra) == "USD 1,000"
    assert fx_diario.leer_json(entorno.last_run_path)["success"] is True
    assert "Falló el cierre" in caplog.text


@pytest.mark.parametrize("hay_tasa", [True, False])
def test_crc_llega_al_reporte_en_corrida_y_handoff(entorno, monkeypatch, hay_tasa) -> None:
    p = _proceso("sicop_cr", "CRC")
    cliente = SimpleNamespace(client_id="cr", client_name="Costa Rica", portal_id="sicop_cr")
    eventos = []

    def hacienda(**kwargs) -> TasaDia:
        eventos.append("hacienda")
        if not hay_tasa:
            raise fx_diario.fx_hacienda.ErrorFxHacienda("sin tasa")
        return TasaDia("CRC", p.fecha_publicacion.date(), 500, 480, "Hacienda")

    async def extraer(config, portal, desde) -> list[Proceso]:
        eventos.append("extraer")
        return [p]

    def reportar(**kwargs) -> None:
        proceso = kwargs["procesos_clasificados"][0]
        eventos.append((texto_monto(proceso), proceso.monto_usd))

    monkeypatch.setattr(fx_diario.fx_hacienda, "obtener_ultimo", hacienda)
    monkeypatch.setattr(fx_diario, "send_ops_alert", lambda *a: True)
    monkeypatch.setattr(main, "load_all_active_profiles", lambda _: [cliente])
    monkeypatch.setattr(main.portales, "cargar_extractor", lambda _: extraer)
    monkeypatch.setattr(main, "_rescatar_semanticos", lambda *a: None)
    monkeypatch.setattr(main, "_registrar_clasificados", lambda *a: None)
    monkeypatch.setattr(main, "save_run", lambda **kw: None)
    monkeypatch.setattr(main, "send_report", reportar)
    monkeypatch.setattr(main, "_clasificar_para_reporte",
                        lambda cfg, profile, procesos, caidos: [ProcesoClasificado.from_proceso(procesos[0])])
    monkeypatch.setattr(service, "cerrar", lambda conf: None)

    esperado = ("CRC 1,000 (≈ USD 2)", 2) if hay_tasa else ("CRC 1,000", None)
    main.run_pipeline()
    assert eventos == ["hacienda", "extraer", esperado]

    eventos.clear()
    p.monto_usd = None
    main._stage_scrape(entorno)
    monkeypatch.setattr(fx_diario, "_corrida", None)
    main._stage_analyze(entorno)
    assert eventos == ["hacienda", "extraer", esperado]
