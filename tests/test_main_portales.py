"""Bucle por portal de `src.main` (docs/arquitectura/portales.md §5, §6, §11 fila A2b).

Sin red: el extractor de cada portal es un doble inyectado por `cargar_extractor`, y la
clasificación/notificación se reemplaza por un registro de llamadas.
"""
import dataclasses
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

import src.main as main
from src import history, portales
from src.classifier.engine import _procesos_to_dicts
from src.portales.contrato import BloqueoAcceso, ContratoRoto, ErrorPortal, PortalNoDisponible
from src.scraper.models import Proceso, ProcesoClasificado


def _cfg(tmp_path, **overrides):
    base = {
        "clients_dir": "clients",
        "enable_drilldown": False,
        "last_run_path": str(tmp_path / "last_run.json"),
        "data_dir": str(tmp_path),
        "app_version": "test-sha",
        "enable_memoria": False,
        "days_lookback": 1,
        "comprasal_email": "bot@example.com",
        "comprasal_password": "x",
        "portales_deshabilitados": frozenset(),
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _cliente(client_id, portal_id="comprasal_sv"):
    return types.SimpleNamespace(client_id=client_id, client_name=client_id.upper(), portal_id=portal_id)


def _proceso(ref, portal_id="comprasal_sv", **kw):
    return Proceso(institucion="Inst", descripcion=f"Proceso {ref}",
                   fecha_publicacion=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
                   url_directa=f"https://example.com/{ref}", numero_referencia=ref, portal_id=portal_id, **kw)


@pytest.fixture
def sicop_integrado(monkeypatch):
    monkeypatch.setitem(portales.PORTALES, "sicop_cr",
                        dataclasses.replace(portales.PORTALES["sicop_cr"], integrado=True))


class Corrida:
    """Arma `run_pipeline` con dobles y registra lo que pasó."""

    def __init__(self, monkeypatch, tmp_path, clientes, extractores, *, sesion=True, restaurar=False, **cfg):
        monkeypatch.setenv("ESTADO_FUENTE", "archivo")
        self.cfg = _cfg(tmp_path, **cfg)
        self.extraidos, self.clasificados = [], []
        self.ops, self.breaker, self.sesion_alertas, self.ejecuciones = [], [], [], []

        def cargar(portal):
            async def extraer(config, p, desde):
                assert desde.tzinfo is not None
                self.extraidos.append(p.portal_id)
                resultado = extractores[p.portal_id]
                if isinstance(resultado, BaseException):
                    raise resultado
                return resultado
            return extraer

        async def sesion_valida(config):
            return sesion

        monkeypatch.setattr(main, "_consecutive_errors", 0)
        monkeypatch.setattr(main, "get_config", lambda: self.cfg)
        monkeypatch.setattr(main, "load_all_active_profiles", lambda d: clientes)
        monkeypatch.setattr(main, "is_session_valid", sesion_valida)
        monkeypatch.setattr(main, "_try_restore_session", lambda c: restaurar)
        monkeypatch.setattr(main.portales, "cargar_extractor", cargar)
        monkeypatch.setattr(main, "_classify_and_notify",
                            lambda config, cliente, procesos, *a, **kw:
                            self.clasificados.append((cliente.client_id, [p.proceso_gid for p in procesos])) or 0)
        monkeypatch.setattr(main, "_preparar_estado", lambda c: None)
        monkeypatch.setattr("src.memoria.service.fallos_consecutivos", lambda c: 0)
        monkeypatch.setattr(main, "_registrar_ejecucion",
                            lambda c, ts, success, **kw: self.ejecuciones.append((success, kw)))
        monkeypatch.setattr(main, "send_session_expired_alert", lambda c, cl: self.sesion_alertas.append(cl.client_id))
        monkeypatch.setattr(main, "send_circuit_breaker_alert",
                            lambda c, cl, n, motivo: self.breaker.append((cl.client_id, n, motivo)))
        monkeypatch.setattr("src.notifier.ops_alert.send_ops_alert", lambda c, asunto, cuerpo: self.ops.append(asunto))

    def correr(self):
        main.run_pipeline()
        return json.loads(open(self.cfg.last_run_path).read())


# ── Config ────────────────────────────────────────────────────────────────────────────────────────────

class TestConfig:
    def _limpio(self, monkeypatch):
        for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "PORTALES_DESHABILITADOS", "SICOP_PAUSA_S",
                    "SICOP_PAGE_SIZE", "SICOP_MAX_PAGINAS", "SICOP_MAX_DETALLES", "SICOP_CACHE_DIAS",
                    "SICOP_HEADLESS", "HEADLESS"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("SMTP_USER", "smtp@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "x")

    def test_config_sin_comprasal_no_lanza(self, monkeypatch):
        from src.config import Config
        self._limpio(monkeypatch)
        cfg = Config()
        assert cfg.comprasal_email == "" and cfg.comprasal_password == ""
        assert cfg.portales_deshabilitados == frozenset()

    def test_defaults_de_sicop(self, monkeypatch):
        from src.config import Config
        self._limpio(monkeypatch)
        cfg = Config()
        assert (cfg.sicop_pausa_s, cfg.sicop_page_size, cfg.sicop_max_paginas,
                cfg.sicop_max_detalles, cfg.sicop_cache_dias, cfg.sicop_headless) == (1.0, 50, 20, 300, 3, True)

    def test_correcciones_y_lista_de_deshabilitados(self, monkeypatch):
        from src.config import Config
        self._limpio(monkeypatch)
        monkeypatch.setenv("PORTALES_DESHABILITADOS", " sicop_cr, ,COMPRASAL_SV ")
        monkeypatch.setenv("SICOP_PAUSA_S", "0.1")
        monkeypatch.setenv("SICOP_PAGE_SIZE", "37")
        monkeypatch.setenv("SICOP_MAX_PAGINAS", "basura")
        monkeypatch.setenv("HEADLESS", "false")
        cfg = Config()
        assert cfg.portales_deshabilitados == frozenset({"sicop_cr", "comprasal_sv"})
        assert cfg.sicop_pausa_s == 0.5
        assert cfg.sicop_page_size == 50
        assert cfg.sicop_max_paginas == 20
        assert cfg.sicop_headless is False  # hereda HEADLESS
        monkeypatch.setenv("SICOP_PAGE_SIZE", "20")
        monkeypatch.setenv("SICOP_HEADLESS", "true")
        cfg = Config()
        assert cfg.sicop_page_size == 20 and cfg.sicop_headless is True


# ── Extractor real de COMPRASAL por el registro ──────────────────────────────────────────────────────

def test_cargar_extractor_de_comprasal_es_el_adaptador_real():
    pytest.importorskip("playwright", reason="Playwright no instalado (sí lo está en Docker)")
    from src.scraper import extractor
    fn = portales.cargar_extractor(portales.PORTALES["comprasal_sv"])
    assert fn is extractor.extraer_comprasal


# ── Aislamiento de fallos (§5.2, §5.3) ───────────────────────────────────────────────────────────────

def test_sicop_bloqueado_no_impide_comprasal(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    c = Corrida(monkeypatch, tmp_path, clientes, {
        "comprasal_sv": [_proceso("96-2026-P0011")],
        "sicop_cr": BloqueoAcceso("sicop_cr", "HTML con 200", detalle="<html>No fue posible acceder"),
    })
    last_run = c.correr()

    assert c.extraidos == ["comprasal_sv", "sicop_cr"]  # orden del registro
    assert c.clasificados == [("sv", ["comprasal_sv:96-2026-P0011"])]
    assert history.fallos_portal(c.cfg, "comprasal_sv") == 0
    assert history.fallos_portal(c.cfg, "sicop_cr") == 1
    assert main._consecutive_errors == 0
    assert c.ops == ["SICOP: contrato roto"]
    assert c.sesion_alertas == [] and c.breaker == []  # nunca correo al cliente por un contrato roto
    assert last_run["success"] is True
    assert last_run["error"].startswith("PARCIAL: sicop_cr: BloqueoAcceso")
    assert last_run["portales"]["comprasal_sv"] == {"ok": True, "procesos": 1, "error": None}
    assert last_run["portales"]["sicop_cr"]["ok"] is False
    success, kw = c.ejecuciones[-1]
    assert success is True and kw["error"].startswith("PARCIAL:") and kw["clientes_procesados"] == 1


def test_sesion_invalida_de_comprasal_no_impide_sicop(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    c = Corrida(monkeypatch, tmp_path, clientes, {
        "comprasal_sv": [_proceso("1-2026-P0001")],
        "sicop_cr": [_proceso("2026LD-000364-0015499999", "sicop_cr")],
    }, sesion=False, restaurar=False)
    last_run = c.correr()

    assert c.extraidos == ["sicop_cr"]
    assert c.sesion_alertas == ["sv"]
    assert c.clasificados == [("cr", ["sicop_cr:2026LD-000364-0015499999"])]
    assert last_run["success"] is True and main._consecutive_errors == 0


def test_credenciales_faltantes_solo_tumban_comprasal(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    c = Corrida(monkeypatch, tmp_path, clientes, {
        "comprasal_sv": [_proceso("1-2026-P0001")],
        "sicop_cr": [_proceso("2026LD-000364-0015499999", "sicop_cr")],
    }, comprasal_email="", comprasal_password="")
    last_run = c.correr()

    assert c.extraidos == ["sicop_cr"]
    assert c.sesion_alertas == ["sv"]
    assert "CredencialesFaltantes" in last_run["portales"]["comprasal_sv"]["error"]
    assert "COMPRASAL_EMAIL" in last_run["portales"]["comprasal_sv"]["error"]


def test_solo_sicop_sin_variables_de_comprasal_corre(monkeypatch, tmp_path, sicop_integrado):
    cfg_sin = {"comprasal_email": "", "comprasal_password": ""}
    c = Corrida(monkeypatch, tmp_path, [_cliente("cr", "sicop_cr")],
                {"sicop_cr": [_proceso("2026LD-000364-0015499999", "sicop_cr")]}, sesion=False, **cfg_sin)
    last_run = c.correr()
    assert c.clasificados == [("cr", ["sicop_cr:2026LD-000364-0015499999"])]
    assert c.sesion_alertas == [] and last_run["success"] is True and "error" not in last_run


def test_todos_fallan_es_success_false_como_hoy(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    c = Corrida(monkeypatch, tmp_path, clientes, {
        "comprasal_sv": RuntimeError("timeout"),
        "sicop_cr": PortalNoDisponible("sicop_cr", "5xx"),
    })
    last_run = c.correr()
    assert last_run["success"] is False
    assert main._consecutive_errors == 1
    assert c.ejecuciones[-1][0] is False
    assert "comprasal_sv: RuntimeError: timeout" in c.ejecuciones[-1][1]["error"]


def test_un_solo_portal_fallido_conserva_el_mensaje_de_hoy(monkeypatch, tmp_path):
    c = Corrida(monkeypatch, tmp_path, [_cliente("sv")], {"comprasal_sv": []}, sesion=False)
    c.correr()
    assert c.ejecuciones[-1] == (False, {"error": main.ERROR_SESION_COMPRASAL})
    assert c.sesion_alertas == ["sv"]


def test_breaker_por_portal_en_el_tercer_fallo(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    for corrida in range(1, 4):
        c = Corrida(monkeypatch, tmp_path, clientes, {
            "comprasal_sv": [_proceso("1-2026-P0001")],
            "sicop_cr": PortalNoDisponible("sicop_cr", "5xx tras reintentos"),
        })
        c.correr()
        assert history.fallos_portal(c.cfg, "sicop_cr") == corrida
        assert main._consecutive_errors == 0  # el global no se mueve si otro portal funciona
        if corrida < 3:
            assert c.breaker == [] and c.ops == []
    assert c.breaker == [("cr", 3, "[SICOP] 5xx tras reintentos")]
    assert c.ops == ["SICOP: 3 fallos consecutivos"]

    # Se recupera: el contador vuelve a 0.
    c = Corrida(monkeypatch, tmp_path, clientes, {
        "comprasal_sv": [], "sicop_cr": [_proceso("2026LD-000364-0015499999", "sicop_cr")]})
    c.correr()
    assert history.fallos_portal(c.cfg, "sicop_cr") == 0


def test_contrato_roto_alerta_en_el_primero_y_en_el_umbral(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    asuntos = []
    for _ in range(4):
        c = Corrida(monkeypatch, tmp_path, clientes, {
            "comprasal_sv": [], "sicop_cr": ContratoRoto("sicop_cr", "422")})
        c.correr()
        asuntos.append(list(c.ops))
    assert asuntos == [["SICOP: contrato roto"], [], ["SICOP: contrato roto"], []]


def test_breaker_global_no_duplica_el_del_portal(monkeypatch, tmp_path):
    monkeypatch.setattr("src.memoria.service.fallos_consecutivos", lambda c: 2)
    (tmp_path / "portal_estado.json").write_text(json.dumps({"comprasal_sv": {"fallos_consecutivos": 2}}))
    c = Corrida(monkeypatch, tmp_path, [_cliente("sv")], {"comprasal_sv": RuntimeError("caído")})
    monkeypatch.setattr("src.memoria.service.fallos_consecutivos", lambda c: 2)
    c.correr()
    assert main._consecutive_errors == 3
    assert c.breaker == [("sv", 3, "[COMPRASAL] caído")]


def test_portales_deshabilitados(monkeypatch, tmp_path, sicop_integrado):
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    c = Corrida(monkeypatch, tmp_path, clientes, {
        "comprasal_sv": [_proceso("1-2026-P0001")], "sicop_cr": RuntimeError("no debía llamarse")},
        portales_deshabilitados=frozenset({"sicop_cr"}))
    last_run = c.correr()
    assert c.extraidos == ["comprasal_sv"]
    assert set(last_run["portales"]) == {"comprasal_sv"}
    assert last_run["clientes_procesados"] == 1


def test_login_en_otro_portal_no_implementado(monkeypatch):
    portal = dataclasses.replace(portales.PORTALES["sicop_cr"], requiere_login=True)
    with pytest.raises(ErrorPortal, match="login no implementado"):
        main._asegurar_sesion(types.SimpleNamespace(), portal)


def test_desde_en_la_zona_del_portal():
    cfg = types.SimpleNamespace(days_lookback=2)
    desde = main._desde(cfg, portales.PORTALES["sicop_cr"])
    assert str(desde.tzinfo) == "America/Costa_Rica"
    assert abs((datetime.now(timezone.utc) - desde) - timedelta(days=2)) < timedelta(minutes=1)


# ── Contrato del extractor e invariante I1 (§2.2, §4) ────────────────────────────────────────────────

class TestInvariante:
    def test_proceso_de_otro_portal_es_contrato_roto(self):
        with pytest.raises(ContratoRoto, match="otro portal"):
            main._verificar_procesos([_proceso("A"), _proceso("B", "sicop_cr")], portales.PORTALES["comprasal_sv"])

    def test_filas_invalidas_se_descartan_y_todas_invalidas_rompen(self):
        portal = portales.PORTALES["comprasal_sv"]
        ingenua = _proceso("C")
        ingenua.fecha_publicacion = datetime(2026, 11, 20, 9, 0)
        assert main._verificar_procesos([_proceso("A"), _proceso(""), ingenua], portal) == [_proceso("A")]
        with pytest.raises(ContratoRoto):
            main._verificar_procesos([ingenua], portal)
        assert main._verificar_procesos([], portal) == []

    def test_drilldown_rechaza_mezcla_de_portales(self):
        with pytest.raises(AssertionError, match="I1"):
            main._run_clients_with_drilldown(types.SimpleNamespace(), [], [_proceso("A"), _proceso("B", "sicop_cr")],
                                             2, datetime.now(timezone.utc))

    def test_procesar_portal_rechaza_procesos_ajenos(self):
        with pytest.raises(AssertionError, match="I1"):
            main._procesar_portal(types.SimpleNamespace(), portales.PORTALES["sicop_cr"], [],
                                  [_proceso("A")], datetime.now(timezone.utc), set())


# ── Prompt del clasificador (§3) ─────────────────────────────────────────────────────────────────────

def test_prompt_de_comprasal_byte_a_byte_igual():
    p = _proceso("96-2026-P0011", forma_contratacion="Licitación")
    assert json.dumps(_procesos_to_dicts([p]), ensure_ascii=False) == (
        '[{"referencia": "96-2026-P0011", "institucion": "Inst", "descripcion": "Proceso 96-2026-P0011", '
        '"metodo": "Licitación", "fecha": "2026-11-20 09:00"}]')


def test_prompt_de_sicop_agrega_modalidad_monto_y_detalle():
    p = _proceso("2026LD-000364-0015499999", "sicop_cr", modalidad_codigo="LD", monto=12_500_000.0,
                 moneda="CRC", detalle="Servidor rack · Switch 48p")
    d = _procesos_to_dicts([p])[0]
    assert d["modalidad"] == "LD"
    assert d["monto"] == "CRC 12,500,000"
    assert d["detalle"] == "Servidor rack · Switch 48p"
    mezcla = _proceso("X", "sicop_cr", montos_por_moneda={"CRC": 1e6, "USD": 2e3})
    assert _procesos_to_dicts([mezcla])[0]["monto"] == "CRC 1,000,000 + USD 2,000"


# ── Etapas scrape/analyze con un manifiesto por portal (§5.4) ────────────────────────────────────────

def test_etapas_un_manifiesto_por_portal_y_lectura_del_heredado(monkeypatch, tmp_path, sicop_integrado):
    from src.drilldown import pipeline

    monkeypatch.setenv("ESTADO_FUENTE", "archivo")
    cfg = _cfg(tmp_path)
    clientes = [_cliente("sv"), _cliente("cr", "sicop_cr")]
    extractores = {"comprasal_sv": [_proceso("1-2026-P0001")],
                   "sicop_cr": [_proceso("2026LD-000364-0015499999", "sicop_cr")]}

    def cargar(portal):
        async def extraer(config, p, desde):
            return extractores[p.portal_id]
        return extraer

    async def sesion(config):
        return True

    def clasificar(config, profile, procesos, caidos):
        out = []
        for p in procesos:
            c = ProcesoClasificado.from_proceso(p)
            c.es_relevante = True
            out.append(c)
        return out

    descargas, reportes = [], []

    async def descargar(relevantes, config):
        descargas.append({p.portal_id for p in relevantes})
        return {}

    monkeypatch.setattr(main, "load_all_active_profiles", lambda d: clientes)
    monkeypatch.setattr(main, "is_session_valid", sesion)
    monkeypatch.setattr(main.portales, "cargar_extractor", cargar)
    monkeypatch.setattr(main, "_clasificar_para_reporte", clasificar)
    monkeypatch.setattr(main, "_preparar_estado", lambda c: None)
    monkeypatch.setattr(main, "_registrar_clasificados", lambda *a: None)
    monkeypatch.setattr(pipeline, "descargar_pliegos", descargar)
    monkeypatch.setattr(pipeline, "limpiar_pliegos", lambda *a: None)
    monkeypatch.setattr(main, "save_run", lambda **kw: None)
    monkeypatch.setattr(main, "send_report",
                        lambda **kw: reportes.append((kw["client_profile"].client_id,
                                                      [p.portal_id for p in kw["procesos_clasificados"]])))
    monkeypatch.setattr("src.memoria.service.cerrar", lambda *a: None)

    main._stage_scrape(cfg)
    assert descargas == [{"comprasal_sv"}, {"sicop_cr"}]  # I1: nunca se mezclan portales
    for pid in ("comprasal_sv", "sicop_cr"):
        m = json.loads((tmp_path / f"stage_handoff_{pid}.json").read_text())
        assert m["portal_id"] == pid
    assert not (tmp_path / "stage_handoff.json").exists()

    # Un manifiesto heredado (sin sufijo, sin portal_id en las líneas) se lee como COMPRASAL.
    viejo = {"run_timestamp": datetime.now(timezone.utc).isoformat(), "total_revisados": 1, "pdf_paths": {},
             "clientes": {"sv": {"clasificados": [{
                 "numero_referencia": "9-2026-P0009", "institucion": "Inst", "descripcion": "viejo",
                 "fecha_publicacion": "2026-11-19T09:00:00-06:00", "es_relevante": False}]}}}
    (tmp_path / "stage_handoff.json").write_text(json.dumps(viejo))

    main._stage_analyze(cfg)
    assert reportes == [("sv", ["comprasal_sv"]), ("sv", ["comprasal_sv"]), ("cr", ["sicop_cr"])]
    assert not list(tmp_path.glob("stage_handoff*.json"))
    last_run = json.loads((tmp_path / "last_run.json").read_text())
    assert last_run["success"] is True and set(last_run["portales"]) == {"comprasal_sv", "sicop_cr"}


def test_analyze_un_manifiesto_roto_no_frena_los_demas(monkeypatch, tmp_path):
    from src.drilldown import pipeline

    monkeypatch.setenv("ESTADO_FUENTE", "archivo")
    cfg = _cfg(tmp_path)
    (tmp_path / "stage_handoff.json").write_text("{no es json")
    (tmp_path / "stage_handoff_comprasal_sv.json").write_text(json.dumps(
        {"portal_id": "comprasal_sv", "run_timestamp": datetime.now(timezone.utc).isoformat(),
         "total_revisados": 4, "clientes": {}, "pdf_paths": {}}))
    monkeypatch.setattr(main, "load_all_active_profiles", lambda d: [])
    monkeypatch.setattr(pipeline, "limpiar_pliegos", lambda *a: None)
    monkeypatch.setattr("src.memoria.service.cerrar", lambda *a: None)
    main._stage_analyze(cfg)
    assert not list(tmp_path.glob("stage_handoff*.json"))
    last_run = json.loads((tmp_path / "last_run.json").read_text())
    assert last_run["procesos_revisados"] == 4 and last_run["success"] is True


def test_scrape_con_todos_los_portales_caidos_no_deja_manifiesto(monkeypatch, tmp_path):
    monkeypatch.setenv("ESTADO_FUENTE", "archivo")
    cfg = _cfg(tmp_path)
    alertas = []

    async def sesion(config):
        return False

    monkeypatch.setattr(main, "load_all_active_profiles", lambda d: [_cliente("sv")])
    monkeypatch.setattr(main, "is_session_valid", sesion)
    monkeypatch.setattr(main, "_try_restore_session", lambda c: False)
    monkeypatch.setattr(main, "_preparar_estado", lambda c: None)
    monkeypatch.setattr(main, "send_session_expired_alert", lambda c, cl: alertas.append(cl.client_id))
    main._stage_scrape(cfg)
    assert alertas == ["sv"]
    assert not list(tmp_path.glob("stage_handoff*.json"))
    assert json.loads((tmp_path / "last_run.json").read_text())["success"] is False
    assert history.fallos_portal(cfg, "comprasal_sv") == 1
