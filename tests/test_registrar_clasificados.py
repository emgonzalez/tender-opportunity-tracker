"""E11.1 — persistir TODO proceso clasificado (relevante o no) en `cliente_proceso`.

Cubre `src.memoria.service.registrar_clasificados` (flags, «sin clasificar», un
solo commit, idempotencia, errores que no se propagan) y su cableado en
`src/main.py` (las tres rutas lo llaman con todo lo clasificado, antes del
drilldown, y un fallo no tumba la corrida ni el correo). El SQL real del MERGE lo
ejercita `tests/integration/test_oracle_local.py`.
"""
from __future__ import annotations

import json
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src import main
from src.memoria import service
from src.scraper.models import Proceso, ProcesoClasificado

# ── Utilidades ───────────────────────────────────────────────────────────────


def _cfg(**overrides):
    base = {"enable_memoria": True, "enable_esquema_v2": True,
            "clients_dir": "clients", "data_dir": "/tmp"}
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _proceso(ref: str) -> Proceso:
    return Proceso(institucion="Ministerio X", descripcion=f"Proceso {ref}",
                   fecha_publicacion=datetime(2026, 10, 20, 9, 0),
                   url_directa=f"https://comprasal/pac/{ref}", numero_referencia=ref)


def _clasificado(ref: str, relevante: bool) -> ProcesoClasificado:
    pc = ProcesoClasificado.from_proceso(_proceso(ref))
    pc.es_relevante = relevante
    pc.categoria_detectada = "infraestructura" if relevante else "otro"
    pc.razon = "Servidores" if relevante else "Obra civil, fuera del catálogo"
    pc.match_tecnico = 0.9 if relevante else 0.1
    pc.match_categoria = 0.8 if relevante else 0.0
    pc.factibilidad = 0.7 if relevante else 0.2
    return pc


def _sin_clasificar(ref: str) -> ProcesoClasificado:
    """Lo que devuelve engine._parsear cuando el LLM falla: from_proceso puro."""
    return ProcesoClasificado.from_proceso(_proceso(ref))


class _Conexion:
    """Imita `db.connection`: commit al salir bien, rollback si hubo excepción."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.aperturas = 0

    def __call__(self, cfg):
        self.aperturas += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1
        return False


class _TablaClienteProceso:
    """`upsert_proceso_v2` falso con semántica de MERGE por (cliente_id, proceso_gid)."""

    def __init__(self):
        self.filas: dict = {}
        self.llamadas: list = []

    def __call__(self, conn, proceso, cliente_id, estado="DESCUBIERTO", portal_id="comprasal_sv"):
        self.llamadas.append((proceso.numero_referencia, cliente_id, estado, portal_id))
        clave = (cliente_id, f"{portal_id}:{proceso.numero_referencia}")
        fila = self.filas.get(clave)
        if fila is None:
            self.filas[clave] = {"estado": estado, "es_relevante": int(proceso.es_relevante)}
        else:  # WHEN MATCHED: no toca `estado`
            fila["es_relevante"] = int(proceso.es_relevante)


@pytest.fixture
def adb(monkeypatch):
    conexion = _Conexion()
    tabla = _TablaClienteProceso()
    monkeypatch.setattr("src.memoria.db.connection", conexion)
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", tabla)
    monkeypatch.setattr(service, "_portal", lambda cfg, cid: "comprasal_sv")
    return types.SimpleNamespace(conexion=conexion, tabla=tabla)


# ── service.registrar_clasificados ───────────────────────────────────────────


class TestRegistrarClasificados:
    def test_guarda_relevantes_y_no_relevantes_en_un_solo_commit(self, adb):
        lote = [_clasificado("A", True), _clasificado("B", False), _clasificado("C", False)]

        r = service.registrar_clasificados(_cfg(), "cli", lote)

        assert r == {"guardados": 3, "omitidos": 0, "error": False}
        assert adb.conexion.aperturas == 1 and adb.conexion.commits == 1
        assert adb.tabla.filas[("cli", "comprasal_sv:A")] == {"estado": "DESCUBIERTO", "es_relevante": 1}
        assert adb.tabla.filas[("cli", "comprasal_sv:B")] == {"estado": "DESCUBIERTO", "es_relevante": 0}
        # Estado por defecto DESCUBIERTO: nunca DESCARTADO_REGEX (es del prefiltro).
        assert {estado for _, _, estado, _ in adb.tabla.llamadas} == {"DESCUBIERTO"}

    def test_omite_los_sin_clasificar(self, adb):
        lote = [_clasificado("A", True), _sin_clasificar("X"), _sin_clasificar("Y")]

        r = service.registrar_clasificados(_cfg(), "cli", lote)

        assert r == {"guardados": 1, "omitidos": 2, "error": False}
        assert [ref for ref, *_ in adb.tabla.llamadas] == ["A"]

    def test_todo_sin_clasificar_no_abre_conexion(self, adb):
        r = service.registrar_clasificados(_cfg(), "cli", [_sin_clasificar("X")])

        assert r == {"guardados": 0, "omitidos": 1, "error": False}
        assert adb.conexion.aperturas == 0

    def test_idempotente_dos_corridas_no_duplican(self, adb):
        lote = [_clasificado("A", True), _clasificado("B", False)]

        service.registrar_clasificados(_cfg(), "cli", lote)
        service.registrar_clasificados(_cfg(), "cli", lote)

        assert len(adb.tabla.filas) == 2
        assert adb.conexion.commits == 2

    def test_no_pisa_el_estado_de_una_fila_avanzada(self, adb):
        adb.tabla.filas[("cli", "comprasal_sv:A")] = {"estado": "ANALIZADO", "es_relevante": 1}

        service.registrar_clasificados(_cfg(), "cli", [_clasificado("A", True)])

        assert adb.tabla.filas[("cli", "comprasal_sv:A")]["estado"] == "ANALIZADO"

    @pytest.mark.parametrize("flags", [
        {"enable_memoria": False, "enable_esquema_v2": True},
        {"enable_memoria": True, "enable_esquema_v2": False},
        {"enable_memoria": False, "enable_esquema_v2": False},
    ])
    def test_flags_apagados_no_tocan_la_conexion(self, adb, flags):
        r = service.registrar_clasificados(_cfg(**flags), "cli", [_clasificado("A", True)])

        assert r == {"guardados": 0, "omitidos": 0, "error": False}
        assert adb.conexion.aperturas == 0
        assert adb.tabla.llamadas == []

    def test_lista_vacia_es_no_op(self, adb):
        assert service.registrar_clasificados(_cfg(), "cli", []) == {
            "guardados": 0, "omitidos": 0, "error": False}
        assert adb.conexion.aperturas == 0

    def test_error_a_mitad_hace_rollback_y_no_propaga(self, adb, monkeypatch, caplog):
        def upsert(conn, proceso, cliente_id, **kw):
            if proceso.numero_referencia == "B":
                raise RuntimeError("ORA-03113: end-of-file on communication channel")
        monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert)

        r = service.registrar_clasificados(
            _cfg(), "cli", [_clasificado("A", True), _clasificado("B", False)])

        assert r == {"guardados": 0, "omitidos": 0, "error": True}
        assert adb.conexion.rollbacks == 1 and adb.conexion.commits == 0
        assert "ORA-03113" in caplog.text

    def test_error_al_conectar_no_propaga(self, adb, monkeypatch):
        def explota(cfg):
            raise ConnectionError("ADB detenida")
        monkeypatch.setattr("src.memoria.db.connection", explota)

        r = service.registrar_clasificados(_cfg(), "cli", [_clasificado("A", True)])

        assert r["error"] is True and r["guardados"] == 0

    def test_usa_el_upsert_real_con_dos_merge_por_proceso(self, monkeypatch):
        """Sin falsear el repo: cada proceso emite los dos MERGE de upsert_proceso_v2."""
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor

        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _Ctx())
        monkeypatch.setattr(service, "_portal", lambda cfg, cid: "comprasal_sv")

        r = service.registrar_clasificados(
            _cfg(), "cli", [_clasificado("A", True), _clasificado("B", False)])

        assert r["guardados"] == 2
        assert cursor.execute.call_count == 4
        binds_b = cursor.execute.call_args_list[3][0][1]
        assert binds_b["es_relevante"] == 0
        assert binds_b["estado"] == "DESCUBIERTO"
        assert binds_b["clasificado"] == 1


class TestSinClasificar:
    def test_from_proceso_puro_es_sin_clasificar(self):
        assert service.sin_clasificar(_sin_clasificar("X")) is True

    def test_no_relevante_real_no_es_sin_clasificar(self):
        assert service.sin_clasificar(_clasificado("B", False)) is False

    def test_no_relevante_con_scores_en_cero_pero_categoria_no_es_sin_clasificar(self):
        """El parser pone categoria_detectada='otro' aun si el modelo la omite:
        con eso basta para saber que el LLM sí lo evaluó."""
        pc = _sin_clasificar("X")
        pc.categoria_detectada = "otro"
        assert service.sin_clasificar(pc) is False

    def test_solo_con_razon_no_es_sin_clasificar(self):
        pc = _sin_clasificar("X")
        pc.razon = "No aplica"
        assert service.sin_clasificar(pc) is False

    def test_relevante_nunca_se_omite(self):
        pc = _sin_clasificar("X")
        pc.es_relevante = True
        assert service.sin_clasificar(pc) is False


# ── Cableado en src/main.py ──────────────────────────────────────────────────


def _cliente(client_id="cli"):
    return types.SimpleNamespace(client_id=client_id, client_name=client_id.upper(),
                                 portal_id="comprasal_sv")


@pytest.fixture
def eventos(monkeypatch):
    """Registra el orden de: clasificar, registrar, drilldown, guardar run, enviar correo."""
    log: list = []
    lote = [_clasificado("A", True), _clasificado("B", False), _sin_clasificar("C")]

    def clasificar(procesos, config, profile):
        log.append(("clasificar", profile.client_id))
        return list(lote)

    def registrar(config, client_id, clasificados):
        log.append(("registrar", client_id, [c.numero_referencia for c in clasificados]))
        return {"guardados": 2, "omitidos": 1, "error": False}

    monkeypatch.setattr(main, "classify_procesos", clasificar)
    monkeypatch.setattr("src.memoria.service.registrar_clasificados", registrar)
    monkeypatch.setattr(main, "save_run", lambda **kw: log.append(("save_run",)))
    monkeypatch.setattr(main, "send_report", lambda **kw: log.append(
        ("send_report", [c.numero_referencia for c in kw["procesos_clasificados"]])))
    return types.SimpleNamespace(log=log, lote=lote)


def _nombres(log):
    return [e[0] for e in log]


class TestMainLlamaARegistrar:
    def test_classify_and_notify(self, eventos):
        n = main._classify_and_notify(_cfg(), _cliente(), [object()], 3, datetime.now())

        assert n == 1
        assert ("registrar", "cli", ["A", "B", "C"]) in eventos.log
        assert _nombres(eventos.log) == ["clasificar", "registrar", "save_run", "send_report"]
        # El correo recibe lo mismo que antes: todos los clasificados.
        assert ("send_report", ["A", "B", "C"]) in eventos.log

    def test_run_clients_with_drilldown_registra_antes_del_drilldown(self, eventos, monkeypatch):
        from src.drilldown import pipeline

        async def descargar(relevantes, config):
            eventos.log.append(("descargar", [c.numero_referencia for c in relevantes]))
            return {"A": "/tmp/a.pdf"}

        def procesar(relevantes, pdf_paths, config, profile):
            eventos.log.append(("drilldown", [c.numero_referencia for c in relevantes]))
            return {}

        monkeypatch.setattr(pipeline, "descargar_pliegos", descargar)
        monkeypatch.setattr(pipeline, "procesar_drilldown", procesar)
        monkeypatch.setattr(pipeline, "limpiar_pliegos", lambda paths: None)

        total = main._run_clients_with_drilldown(
            _cfg(), [_cliente("c1"), _cliente("c2")], [object()], 3, datetime.now())

        assert total == 2
        assert ("registrar", "c1", ["A", "B", "C"]) in eventos.log
        assert ("registrar", "c2", ["A", "B", "C"]) in eventos.log
        nombres = _nombres(eventos.log)
        assert nombres[:4] == ["clasificar", "registrar", "clasificar", "registrar"]
        assert nombres.index("registrar") < nombres.index("descargar")
        # El drilldown sigue recibiendo SOLO los relevantes.
        assert ("descargar", ["A"]) in eventos.log
        assert ("drilldown", ["A"]) in eventos.log
        assert nombres.count("send_report") == 2

    def test_stage_scrape_registra_y_el_manifiesto_no_cambia(self, eventos, monkeypatch, tmp_path):
        from src.drilldown import pipeline

        async def sesion(cfg):
            return True

        async def extraer(cfg):
            return [object()]

        async def descargar(relevantes, config):
            eventos.log.append(("descargar", [c.numero_referencia for c in relevantes]))
            return {}

        monkeypatch.setattr(main, "load_all_active_profiles", lambda d: [_cliente()])
        monkeypatch.setattr(main, "is_session_valid", sesion)
        monkeypatch.setattr(main, "extract_recent_procesos", extraer)
        monkeypatch.setattr(pipeline, "descargar_pliegos", descargar)

        main._stage_scrape(_cfg(data_dir=str(tmp_path)))

        nombres = _nombres(eventos.log)
        assert nombres == ["clasificar", "registrar", "descargar"]
        assert ("registrar", "cli", ["A", "B", "C"]) in eventos.log
        manifiesto = json.loads((tmp_path / "stage_handoff.json").read_text())
        assert manifiesto["clientes"]["cli"]["relevantes"] == ["A"]
        assert len(manifiesto["clientes"]["cli"]["clasificados"]) == 3

    def test_fallo_de_registrar_no_interrumpe_ni_el_correo(self, eventos, monkeypatch):
        def explota(config, client_id, clasificados):
            raise RuntimeError("bug inesperado")
        monkeypatch.setattr("src.memoria.service.registrar_clasificados", explota)

        n = main._classify_and_notify(_cfg(), _cliente(), [object()], 3, datetime.now())

        assert n == 1
        assert ("send_report", ["A", "B", "C"]) in eventos.log

    def test_fallo_de_registrar_no_interrumpe_el_drilldown(self, eventos, monkeypatch):
        from src.drilldown import pipeline

        def explota(config, client_id, clasificados):
            raise RuntimeError("bug inesperado")

        async def descargar(relevantes, config):
            return {}

        monkeypatch.setattr("src.memoria.service.registrar_clasificados", explota)
        monkeypatch.setattr(pipeline, "descargar_pliegos", descargar)
        monkeypatch.setattr(pipeline, "limpiar_pliegos", lambda paths: None)

        total = main._run_clients_with_drilldown(_cfg(), [_cliente()], [object()], 3, datetime.now())

        assert total == 1
        assert "send_report" in _nombres(eventos.log)

    def test_con_la_adb_caida_la_corrida_envia_el_correo(self, eventos, monkeypatch):
        """Camino real: service.registrar_clasificados con la ADB caída devuelve error=True."""
        monkeypatch.setattr("src.memoria.service.registrar_clasificados", _registrar_real)

        def explota(cfg):
            raise ConnectionError("ADB detenida")
        monkeypatch.setattr("src.memoria.db.connection", explota)
        monkeypatch.setattr(service, "_portal", lambda cfg, cid: "comprasal_sv")

        n = main._classify_and_notify(_cfg(), _cliente(), [object()], 3, datetime.now())

        assert n == 1
        assert ("send_report", ["A", "B", "C"]) in eventos.log


# Referencia a la función real, tomada antes de que el fixture `eventos` la reemplace.
_registrar_real = service.registrar_clasificados


def test_from_proceso_conserva_tipo_actividad():
    """`upsert_proceso_v2` escribe `proceso.categoria_raw` con `tipo_actividad`: no debe perderse al clasificar."""
    from datetime import datetime

    from src.scraper.models import Proceso, ProcesoClasificado

    p = Proceso(institucion="MH", descripcion="x", fecha_publicacion=datetime(2026, 10, 27), url_directa="",
                numero_referencia="1-2026", tipo_actividad="Servicios de TI")
    assert ProcesoClasificado.from_proceso(p).tipo_actividad == "Servicios de TI"
