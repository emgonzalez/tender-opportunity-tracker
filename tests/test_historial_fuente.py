"""Fase 3 de persistencia-adb: estado de corrida (`ESTADO_FUENTE`: archivo | sombra | adb), sin Oracle real.

La ADB se simula con un `FakeDB` en memoria (cursor mínimo + los escritores de `memoria.repo` parchados). Las
pruebas de «ADB caída» usan un DSN inválido (nunca una contraseña mala: ORA-28000 bloquea la cuenta).
"""
import contextlib
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src import history, main
from src.memoria import reimportar
from src.scraper.models import MatrizRiesgos, ProcesoClasificado, ResultadoAnalisis, RiesgoItem

CID = "multicomputos_sv"


def _proceso(ref="REF-1", relevante=True):
    return ProcesoClasificado(
        institucion="Ministerio X", descripcion="Servidores", fecha_publicacion=datetime.now(timezone.utc),
        url_directa="https://x", numero_referencia=ref, es_relevante=relevante,
        categoria_detectada="infraestructura", razon="r", match_tecnico=0.9,
    )


def _res(ref="REF-1", estado="ANALIZADO", con_matriz=True):
    m = MatrizRiesgos(items=[RiesgoItem("financiero", "multa", "alta", "5.1")], resumen="ok") if con_matriz else None
    return ResultadoAnalisis(proceso_uuid=ref, estado=estado, formato="NATIVO", triaje="GO",
                             triaje_confianza=0.9, matriz=m, modelo_analisis="m")


class FakeDB:
    """ADB en memoria: clasificados, estados, matrices y ejecuciones detrás de un cursor mínimo."""

    def __init__(self):
        self.falla = False
        self.clasificados = {}   # (cid, ref) -> ProcesoClasificado
        self.estados = {}        # (cid, ref) -> estado
        self.matrices = {}       # (cid, ref) -> ResultadoAnalisis
        self.ejecuciones = []    # dicts
        self.escrituras = 0

    @contextlib.contextmanager
    def connection(self, cfg):
        if self.falla:
            raise RuntimeError("ADB caída")
        yield self

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db, self._res = db, []

    def execute(self, sql, binds=None):
        s = " ".join(sql.split())
        b = binds or {}
        if s.startswith("SELECT COUNT(*) FROM ejecucion"):
            n = sum(1 for e in self.db.ejecuciones if e["ts"] == b["ts"] and e["success"] == bool(b["s"]))
            self._res = [(n,)]
        elif s.startswith("SELECT run_timestamp, success"):
            ult = self.db.ejecuciones[-1:] if self.db.ejecuciones else []
            self._res = [(e["ts"], int(e["success"]), e["procesos_revisados"], e["clientes_procesados"],
                          e["version"]) for e in ult]
        elif s.startswith("SELECT procesos_revisados FROM ejecucion"):
            ok = [e for e in self.db.ejecuciones if e["success"]]
            self._res = [(ok[-1]["procesos_revisados"],)] if ok else []
        elif s.startswith("SELECT p.numero_referencia"):
            self._res = [
                (p.numero_referencia, p.institucion, p.descripcion, "", "", p.fecha_publicacion, p.url_directa,
                 None, None, int(p.es_relevante), p.categoria_detectada, p.razon, p.match_tecnico, 0, 0.5,
                 json.dumps(p.tags), "[]", datetime(2026, 10, 30, 7, 0))
                for (c, _), p in self.db.clasificados.items() if c == b["cid"]
            ]
        elif s.startswith("SELECT cp.proceso_gid"):
            filas = []
            for (c, ref), est in self.db.estados.items():
                if c != b["cid"] or est not in ("ANALIZADO", "NOTIFICADO", "DESCARTADO_TRIAJE"):
                    continue
                r = self.db.matrices.get((c, ref))
                matriz = json.dumps({"items": [vars(i) for i in r.matriz.items], "resumen": ""}) if r else None
                filas.append((f"comprasal_sv:{ref}", est, "GO", 0.9, "NATIVO", matriz, "m", None))
            self._res = filas
        else:  # pragma: no cover
            raise AssertionError(f"SQL no previsto: {s}")

    def fetchall(self):
        return list(self._res)

    def fetchone(self):
        return self._res[0] if self._res else None

    def close(self):
        pass


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    cfg = SimpleNamespace(adb_dsn="dsn", adb_password="x", data_dir=str(data), clients_dir=str(tmp_path / "c"),
                          enable_memoria=True, enable_esquema_v2=True, app_version="v-test",
                          last_run_path=str(data / "last_run.json"))
    db = FakeDB()
    monkeypatch.delenv("ESTADO_FUENTE", raising=False)
    monkeypatch.setattr(history, "_cfg", lambda: cfg)
    monkeypatch.setattr("src.memoria.db.connection", db.connection)
    monkeypatch.setattr("src.memoria.service._portal", lambda c, cid: "comprasal_sv")

    def upsert(conn, proceso, cid, estado="DESCUBIERTO", portal_id="x"):
        db.escrituras += 1
        db.clasificados[(cid, proceso.numero_referencia)] = proceso
        db.estados.setdefault((cid, proceso.numero_referencia), estado)

    def save_analisis(conn, r, cid, portal_id="x"):
        db.matrices[(cid, r.proceso_uuid)] = r

    def set_estado(conn, ref, cid, estado, **kw):
        db.estados[(cid, ref)] = estado

    def get_estado(conn, cid, ref, portal_id="x"):
        return db.estados.get((cid, ref))

    def insertar(conn, ts, success, procesos_revisados=0, clientes_procesados=0, version="", error=None):
        db.ejecuciones.append(dict(ts=ts, success=success, procesos_revisados=procesos_revisados,
                                   clientes_procesados=clientes_procesados, version=version, error=error))

    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert)
    monkeypatch.setattr("src.memoria.repo.save_analisis_v2", save_analisis)
    monkeypatch.setattr("src.memoria.repo.set_estado_v2", set_estado)
    monkeypatch.setattr("src.memoria.repo.get_estado_v2", get_estado)
    monkeypatch.setattr("src.memoria.repo.insertar_ejecucion", insertar)
    history.reiniciar_estado()
    yield SimpleNamespace(cfg=cfg, db=db, data=data, mp=monkeypatch)
    history.reiniciar_estado()


def _run(env, ref="REF-1", cid=CID):
    history.save_run(cid, datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc), [_proceso(ref)], 10, str(env.data))


def _archivos(env):
    h = env.data / "history"
    return sorted(str(p.relative_to(h)) for p in h.rglob("*.jsonl")) if h.exists() else []


# ── archivo (default): idéntico a hoy ────────────────────────────────────────

def test_archivo_no_toca_la_adb_y_escribe_el_jsonl(entorno):
    entorno.db.falla = True
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    assert _archivos(entorno) == [f"{CID}.analisis.jsonl", f"{CID}.jsonl"]
    assert history.load_last_run(CID, str(entorno.data))["procesos"][0].numero_referencia == "REF-1"
    assert history.analisis_estado(CID, "REF-1", str(entorno.data)) == "ANALIZADO"
    assert entorno.db.escrituras == 0


def test_valor_desconocido_vale_archivo(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adbb")
    _run(entorno)
    assert f"{CID}.jsonl" in _archivos(entorno)


def test_last_run_se_escribe_atomico_y_con_la_forma_de_siempre(entorno):
    main._save_last_run(entorno.cfg, success=True, procesos_count=5, clients_processed=1)
    ruta = entorno.data / "last_run.json"
    assert json.loads(ruta.read_text())["procesos_revisados"] == 5
    assert ruta.read_text().startswith('{\n  "timestamp"')  # indent=2, como antes
    assert [p.name for p in entorno.data.iterdir()] == ["last_run.json"]  # ningún .tmp
    assert entorno.db.ejecuciones == []


def test_last_run_fallido_a_mitad_deja_el_anterior(entorno, monkeypatch):
    main._save_last_run(entorno.cfg, success=True, procesos_count=5, clients_processed=1)
    monkeypatch.setattr("src.persistencia.cache.os.replace", lambda *a: (_ for _ in ()).throw(OSError("corte")))
    with pytest.raises(OSError):
        main._save_last_run(entorno.cfg, success=False, procesos_count=0, clients_processed=0)
    assert json.loads((entorno.data / "last_run.json").read_text())["success"] is True
    assert [p.name for p in entorno.data.iterdir()] == ["last_run.json"]


# ── adb ──────────────────────────────────────────────────────────────────────

def test_adb_escribe_en_la_adb_y_no_deja_jsonl(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    assert (CID, "REF-1") in entorno.db.clasificados
    assert entorno.db.estados[(CID, "REF-1")] == "ANALIZADO"
    assert _archivos(entorno) == []


def test_adb_caida_encola_en_el_spool_y_la_corrida_no_falla(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    assert _archivos(entorno) == [f"spool/analisis.{CID}.jsonl", f"spool/run.{CID}.jsonl"]
    # con la ADB caída el reporte (resend) sigue saliendo desde el spool
    assert history.load_last_run(CID, str(entorno.data))["procesos"][0].numero_referencia == "REF-1"
    assert history.analisis_estado(CID, "REF-1", str(entorno.data)) == "ANALIZADO"


def test_spool_se_vacia_en_la_corrida_siguiente_sin_duplicar(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    _run(entorno)
    _run(entorno, "REF-2")
    history.save_analisis(CID, _res(), str(entorno.data))
    entorno.db.falla = False
    r = history.drenar_spool(entorno.cfg)
    assert r == {f"run.{CID}": (2, 0), f"analisis.{CID}": (1, 0)}
    assert _archivos(entorno) == []
    assert set(entorno.db.clasificados) == {(CID, "REF-1"), (CID, "REF-2")}
    assert history.drenar_spool(entorno.cfg) == {}  # segunda vez: nada que reenviar


def test_reenvio_repetido_es_idempotente(entorno):
    """Un corte tras escribir y antes de retirar del spool reenvía lo mismo: los MERGE no duplican."""
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    history.save_analisis(CID, _res(), str(entorno.data))
    copia = (entorno.data / "history" / "spool" / f"analisis.{CID}.jsonl").read_text()
    entorno.db.falla = False
    history.drenar_spool(entorno.cfg)
    (entorno.data / "history" / "spool" / f"analisis.{CID}.jsonl").write_text(copia)
    history.drenar_spool(entorno.cfg)
    assert len(entorno.db.estados) == 1 and len(entorno.db.matrices) == 1


def test_con_pendientes_lo_nuevo_va_detras_y_no_se_pisa_el_estado(entorno):
    """ANALIZADO quedó en el spool; NOTIFICADO llega con la ADB ya arriba: se vacía primero y gana NOTIFICADO."""
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    entorno.db.falla = False
    history.save_analisis(CID, _res(estado="NOTIFICADO", con_matriz=False), str(entorno.data))
    assert entorno.db.estados[(CID, "REF-1")] == "NOTIFICADO"
    assert _archivos(entorno) == [f"spool/run.{CID}.jsonl"]  # la cola de corridas se vacía al inicio de la siguiente


def test_ultima_linea_gana_en_el_spool_y_tolera_lineas_corruptas(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    history.save_analisis(CID, _res(), str(entorno.data))
    history.save_analisis(CID, _res(estado="NOTIFICADO", con_matriz=False), str(entorno.data))
    ruta = entorno.data / "history" / "spool" / f"analisis.{CID}.jsonl"
    ruta.write_text(ruta.read_text() + "{no es json\n")
    assert history.analisis_estado(CID, "REF-1", str(entorno.data)) == "NOTIFICADO"
    assert history.load_analisis(CID, ["REF-1"], str(entorno.data))["REF-1"].matriz is not None


def test_adb_prevalece_sobre_el_jsonl_viejo_y_este_sirve_de_respaldo(entorno):
    d = str(entorno.data)
    history.save_analisis(CID, _res(estado="ANALIZADO"), d)          # historial viejo, escrito en modo archivo
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.estados[(CID, "REF-1")] = "NOTIFICADO"
    assert history.analisis_estado(CID, "REF-1", d) == "NOTIFICADO"  # manda la ADB
    entorno.db.falla = True
    assert history.analisis_estado(CID, "REF-1", d) == "ANALIZADO"   # ADB caída: el archivo viejo


def test_historial_viejo_con_lineas_corruptas_se_lee_en_adb(entorno):
    d = str(entorno.data)
    _run(entorno)
    ruta = entorno.data / "history" / f"{CID}.jsonl"
    ruta.write_text("basura\n" + ruta.read_text())
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    assert history.load_last_run(CID, d)["procesos"][0].numero_referencia == "REF-1"


def test_load_analisis_de_la_adb_completa_lo_que_falta(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    out = history.load_analisis(CID, ["REF-1", "REF-9"], str(entorno.data))
    assert list(out) == ["REF-1"] and out["REF-1"].matriz.items[0].severidad == "alta"


def test_load_last_run_de_la_adb(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    _run(entorno)
    entorno.db.ejecuciones.append(dict(ts=datetime(2026, 10, 30), success=True, procesos_revisados=40,
                                       clientes_procesados=1, version="v", error=None))
    ult = history.load_last_run(CID, str(entorno.data))
    assert ult["total_revisados"] == 40 and ult["procesos"][0].es_relevante


def test_id_viejo_resuelve_al_canonico_en_el_spool(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    _run(entorno, cid="multicomputos")
    assert _archivos(entorno) == [f"spool/run.{CID}.jsonl"]
    assert history.load_last_run("multicomputos", str(entorno.data)) is not None


def test_sin_flags_de_memoria_todo_va_al_spool(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.cfg.enable_esquema_v2 = False
    _run(entorno)
    assert _archivos(entorno) == [f"spool/run.{CID}.jsonl"] and entorno.db.escrituras == 0


def test_dsn_invalido_real_no_rompe_y_deja_spool(entorno, tmp_path):
    """Sin parchear la conexión: un DSN inválido (jamás una contraseña mala) cae al spool."""
    pytest.importorskip("oracledb")
    entorno.mp.undo()
    entorno.mp.delenv("ESTADO_FUENTE", raising=False)
    cfg = SimpleNamespace(adb_dsn="@@ no es un dsn @@", adb_password="x", adb_user="u", data_dir=str(tmp_path / "d"),
                          clients_dir=str(tmp_path / "c"), enable_memoria=True, enable_esquema_v2=True)
    import src.memoria.db as dbmod
    dbmod._pool = None
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("ESTADO_FUENTE", "adb")
        mp.setattr(history, "_cfg", lambda: cfg)
        mp.setattr("src.memoria.service._portal", lambda c, cid: "comprasal_sv")
        history.save_run(CID, datetime(2026, 10, 30, tzinfo=timezone.utc), [_proceso()], 1, cfg.data_dir)
    finally:
        mp.undo()
        dbmod._pool = None
    assert (tmp_path / "d" / "history" / "spool" / f"run.{CID}.jsonl").exists()


# ── sombra ───────────────────────────────────────────────────────────────────

def test_sombra_escribe_ambos_y_manda_el_archivo(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "sombra")
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    assert _archivos(entorno) == [f"{CID}.analisis.jsonl", f"{CID}.jsonl"]
    assert (CID, "REF-1") in entorno.db.clasificados
    entorno.db.estados[(CID, "REF-1")] = "NOTIFICADO"  # la ADB discrepa; manda el archivo
    assert history.analisis_estado(CID, "REF-1", str(entorno.data)) == "ANALIZADO"


def test_sombra_loguea_la_diferencia_una_sola_vez(entorno, caplog):
    entorno.mp.setenv("ESTADO_FUENTE", "sombra")
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    entorno.db.estados[(CID, "REF-1")] = "NOTIFICADO"
    with caplog.at_level(logging.INFO, logger="src.history"):
        for _ in range(3):
            history.analisis_estado(CID, "REF-1", str(entorno.data))
    assert sum("difiere" in r.message for r in caplog.records) == 1


def test_sombra_sin_diferencias_no_loguea(entorno, caplog):
    entorno.mp.setenv("ESTADO_FUENTE", "sombra")
    _run(entorno)
    with caplog.at_level(logging.INFO, logger="src.history"):
        history.load_last_run(CID, str(entorno.data))
    assert not [r for r in caplog.records if "difiere" in r.message]


def test_sombra_con_adb_caida_no_rompe(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "sombra")
    entorno.db.falla = True
    _run(entorno)
    history.save_analisis(CID, _res(), str(entorno.data))
    assert history.load_last_run(CID, str(entorno.data)) is not None
    assert history.analisis_estado(CID, "REF-1", str(entorno.data)) == "ANALIZADO"


# ── last_run ─────────────────────────────────────────────────────────────────

def test_last_run_adb_escribe_la_fila_y_el_archivo(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    ts = datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)
    main._save_last_run(entorno.cfg, True, 7, 2, run_timestamp=ts)
    assert (entorno.data / "last_run.json").exists()
    assert entorno.db.ejecuciones[0]["procesos_revisados"] == 7 and entorno.db.ejecuciones[0]["ts"] == ts


def test_last_run_adb_caida_no_falla_y_se_reenvia_una_vez(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    ts = datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)
    entorno.db.falla = True
    main._save_last_run(entorno.cfg, True, 7, 2, run_timestamp=ts)
    assert json.loads((entorno.data / "last_run.json").read_text())["procesos_revisados"] == 7
    assert _archivos(entorno) == ["spool/ejecucion.jsonl"]
    entorno.db.falla = False
    history.drenar_spool(entorno.cfg)
    history.drenar_spool(entorno.cfg)
    assert len(entorno.db.ejecuciones) == 1


def test_leer_last_run_adb_primero_y_refresca_la_cache(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.ejecuciones.append(dict(ts=datetime(2026, 10, 30, 7, 0), success=True, procesos_revisados=3,
                                       clientes_procesados=1, version="v9", error=None))
    assert history.leer_last_run(entorno.cfg)["version"] == "v9"
    assert json.loads((entorno.data / "last_run.json").read_text())["procesos_revisados"] == 3


def test_leer_last_run_con_adb_caida_usa_la_cache(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    (entorno.data / "last_run.json").write_text(json.dumps({"success": True, "version": "cache"}))
    entorno.db.falla = True
    assert history.leer_last_run(entorno.cfg)["version"] == "cache"


def test_preparar_estado_nunca_propaga(entorno, monkeypatch):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    monkeypatch.setattr(history, "drenar_spool", lambda cfg: (_ for _ in ()).throw(RuntimeError("x")))
    main._preparar_estado(entorno.cfg)


def test_registrar_ejecucion_no_se_duplica_en_adb(entorno, monkeypatch):
    llamadas = []
    monkeypatch.setattr("src.memoria.service.registrar_ejecucion", lambda *a, **k: llamadas.append(1))
    main._registrar_ejecucion(entorno.cfg, datetime.now(), success=True)
    assert llamadas == [1]  # archivo: como siempre
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    main._registrar_ejecucion(entorno.cfg, datetime.now(), success=True)
    assert llamadas == [1]  # adb: ya lo escribió _save_last_run


# ── reimportar --vaciar-spool ────────────────────────────────────────────────

def test_reimportar_vacia_el_spool_aun_sin_el_flag(entorno):
    entorno.mp.setenv("ESTADO_FUENTE", "adb")
    entorno.db.falla = True
    _run(entorno)
    entorno.db.falla = False
    entorno.mp.delenv("ESTADO_FUENTE")
    assert reimportar.vaciar_spool(entorno.cfg, dry_run=True) == {f"run.{CID}": (0, 1)}
    assert reimportar.vaciar_spool(entorno.cfg) == {f"run.{CID}": (1, 0)}
    assert (CID, "REF-1") in entorno.db.clasificados and _archivos(entorno) == []
