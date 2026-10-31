"""Fase 4 de persistencia-adb: cuota y libro LLM con la ADB como fuente (fake cursor, sin Oracle).

La ADB caída se simula con un DSN inválido / una conexión que lanza; jamás con una contraseña mala (ORA-28000
bloquea el usuario real).
"""
import contextlib
import json
import types
from datetime import date

import pytest

from src.drilldown import quota
from src.llm import registro
from src.scraper.models import UsoModelo


class FakeADB:
    """Una ADB mínima en memoria: `cuota_gasto` y `llm_llamada`."""

    def __init__(self):
        self.cuota: dict[tuple, float] = {}
        self.llamadas: list[dict] = []
        self.caida = False
        self.ejecuciones: list[str] = []
        self.carrera_pendiente = False

    @contextlib.contextmanager
    def connection(self, cfg):
        if self.caida:
            raise ConnectionError("DPY-6005: no se puede conectar (DSN inválido)")
        yield types.SimpleNamespace(cursor=lambda: _Cursor(self))


class _Cursor:
    def __init__(self, adb):
        self.adb, self.rowcount, self._fila = adb, 0, None

    def execute(self, sql, binds=None):
        a = self.adb
        a.ejecuciones.append(" ".join(sql.split()))
        if "UPDATE cuota_gasto" in sql:
            k = (binds["dia"], binds["modelo"])
            self.rowcount = 1 if k in a.cuota else 0
            if k in a.cuota:
                a.cuota[k] += binds["gasto"]  # el único camino de escritura: gasto = gasto + :x
        elif "INSERT INTO cuota_gasto" in sql:
            if a.carrera_pendiente:
                a.carrera_pendiente = False
                a.cuota[(binds["dia"], binds["modelo"])] = 0.25  # otro proceso ganó la carrera
                raise Exception("ORA-00001: unique constraint violated")
            a.cuota[(binds["dia"], binds["modelo"])] = binds["gasto"]
        elif "SUM(gasto_usd)" in sql:
            self._fila = (sum(v for (d, _), v in a.cuota.items() if d == binds["dia"]),)
        elif "SELECT COUNT(*) FROM llm_llamada" in sql:
            n = sum(1 for f in a.llamadas if all(f[k] == binds[k] for k in registro._CLAVES_EXISTE))
            self._fila = (n,)
        elif "INSERT INTO llm_llamada" in sql:
            a.llamadas.append(dict(binds))

    def fetchone(self):
        return self._fila

    def close(self):
        pass


@pytest.fixture
def adb(monkeypatch):
    fake = FakeADB()
    monkeypatch.setattr("src.memoria.db.connection", fake.connection)
    quota.reiniciar_estado()
    registro.reiniciar_estado()
    yield fake
    quota.reiniciar_estado()
    registro.reiniciar_estado()


def _cfg(tmp_path, budget=1.00, **kw):
    base = dict(
        daily_budget_usd=budget,
        budget_path=str(tmp_path / "gasto.json"),
        data_dir=str(tmp_path),
        adb_dsn="dsn-invalido",
        adb_password="x",
        llm_registro_path=str(tmp_path / "llm_consumo.jsonl"),
        llm_registro_habilitado=True,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _uso(inp=1_000_000, modelo="claude-haiku-4-5"):
    return UsoModelo(modelo=modelo, input_tokens=inp, output_tokens=0, llamadas=1)


def _modo(monkeypatch, valor):
    monkeypatch.setenv("CUOTA_FUENTE", valor)


class TestCuotaAdb:
    def test_archivo_no_toca_la_adb(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "archivo")
        cfg = _cfg(tmp_path)
        quota.cobrar(cfg, _uso())
        assert adb.ejecuciones == []
        assert quota.gasto_del_dia(cfg) == pytest.approx(1.0)

    def test_incremento_atomico_en_sql(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        cfg = _cfg(tmp_path, budget=10)
        quota.cobrar(cfg, _uso(1_000_000))
        quota.cobrar(cfg, _uso(500_000))
        # la primera crea la fila, la segunda solo hace UPDATE ... SET gasto_usd = gasto_usd + :gasto
        assert sum(1 for e in adb.ejecuciones if e.startswith("INSERT INTO cuota_gasto")) == 1
        assert sum(1 for e in adb.ejecuciones if "SET gasto_usd = gasto_usd + :gasto" in e) == 2
        assert adb.cuota[(date.today(), "claude-haiku-4-5")] == pytest.approx(1.5)

    def test_carrera_en_el_insert_reintenta_el_update(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        cfg = _cfg(tmp_path, budget=10)
        adb.carrera_pendiente = True
        quota.cobrar(cfg, _uso(1_000_000))
        assert adb.cuota[(date.today(), "claude-haiku-4-5")] == pytest.approx(1.25)

    def test_adb_caida_falla_cerrado_con_la_cache(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        adb.caida = True
        cfg = _cfg(tmp_path, budget=1.00)
        quota.cobrar(cfg, _uso(900_000))  # $0.90, solo queda en el archivo
        assert quota.gasto_del_dia(cfg) == pytest.approx(0.9)
        assert quota.cabe(cfg, 0.05) is True
        assert quota.cabe(cfg, 0.20) is False  # el tope sigue aplicando sin ADB

    def test_tope_respetado_tras_reinicio_con_la_cache_perdida(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        adb.cuota[(date.today(), "claude-haiku-4-5")] = 0.95  # lo gastó una corrida anterior
        cfg = _cfg(tmp_path, budget=1.00)  # sin gasto_diario.json
        assert quota.cabe(cfg, 0.10) is False
        assert json.load(open(cfg.budget_path))["gasto_usd"] == pytest.approx(0.95)  # caché refrescada
        adb.caida = True  # y si ahora se cae, la caché sigue frenando
        quota.reiniciar_estado()
        assert quota.cabe(cfg, 0.10) is False

    def test_sin_adb_configurada_no_intenta_conectar(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        cfg = _cfg(tmp_path, adb_dsn="")
        quota.cobrar(cfg, _uso())
        assert adb.ejecuciones == []
        assert quota.gasto_del_dia(cfg) == pytest.approx(1.0)

    def test_sombra_manda_el_archivo_y_loguea_la_diferencia_una_vez(self, adb, tmp_path, monkeypatch, caplog):
        _modo(monkeypatch, "sombra")
        cfg = _cfg(tmp_path, budget=10)
        adb.cuota[(date.today(), "otro")] = 3.0  # la ADB sabe algo que el archivo no
        with caplog.at_level("WARNING", logger="src.drilldown.quota"):
            assert quota.gasto_del_dia(cfg) == 0.0  # manda el archivo
            quota.gasto_del_dia(cfg)
            quota.gasto_del_dia(cfg)
        avisos = [r for r in caplog.records if "sombra" in r.getMessage()]
        assert len(avisos) == 1
        assert "3.0000" in avisos[0].getMessage()

    def test_sombra_sin_diferencia_no_loguea(self, adb, tmp_path, monkeypatch, caplog):
        _modo(monkeypatch, "sombra")
        cfg = _cfg(tmp_path, budget=10)
        with caplog.at_level("WARNING", logger="src.drilldown.quota"):
            quota.cobrar(cfg, _uso())
            quota.gasto_del_dia(cfg)
        assert not [r for r in caplog.records if "sombra" in r.getMessage()]


def _pedido():
    return types.SimpleNamespace(etapa="triaje", max_tokens=100, sistema="s", contenido=[])


def _resp():
    return types.SimpleNamespace(
        modelo="claude-haiku-4-5", proveedor="anthropic", texto="ok",
        uso=types.SimpleNamespace(input_tokens=10, output_tokens=5), uso_crudo=None, truncada=False,
    )


def _spool(tmp_path):
    return tmp_path / "llm_consumo.jsonl"


class TestLibroLlm:
    def test_con_adb_arriba_no_escribe_el_jsonl(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        registro.registrar(_cfg(tmp_path), _pedido(), respuesta=_resp(), latencia_ms=5)
        assert len(adb.llamadas) == 1
        assert not _spool(tmp_path).exists()

    def test_adb_caida_deja_la_linea_en_el_spool_y_no_lanza(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        adb.caida = True
        registro.registrar(_cfg(tmp_path), _pedido(), respuesta=_resp(), latencia_ms=5)
        lineas = _spool(tmp_path).read_text().splitlines()
        assert len(lineas) == 1 and json.loads(lineas[0])["etapa"] == "triaje"

    def test_spool_se_vacia_sin_duplicar_en_la_corrida_siguiente(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        cfg = _cfg(tmp_path)
        adb.caida = True
        registro.registrar(cfg, _pedido(), respuesta=_resp(), latencia_ms=5)
        registro.registrar(cfg, _pedido(), respuesta=_resp(), latencia_ms=6)
        # "corrida siguiente": proceso nuevo (estado en blanco) con la ADB arriba
        adb.caida = False
        registro.reiniciar_estado()
        registro.registrar(cfg, _pedido(), respuesta=_resp(), latencia_ms=7)
        assert len(adb.llamadas) == 3
        assert not _spool(tmp_path).exists()

    def test_reenvio_de_lo_ya_escrito_no_duplica(self, adb, tmp_path, monkeypatch):
        """Un corte a mitad del vaciado deja en el spool líneas que la ADB ya recibió."""
        _modo(monkeypatch, "adb")
        cfg = _cfg(tmp_path)
        registro.registrar(cfg, _pedido(), respuesta=_resp(), latencia_ms=5)
        ya = adb.llamadas[0]
        _spool(tmp_path).write_text(json.dumps({
            "ts": ya["ts"].isoformat(), "etapa": ya["etapa"], "modelo": ya["modelo"],
            "latencia_ms": ya["latencia_ms"],
            "uso": {"input_tokens": ya["input_tokens"], "output_tokens": ya["output_tokens"]},
        }) + "\n")
        registro.reiniciar_estado()
        registro.registrar(cfg, _pedido(), respuesta=_resp(), latencia_ms=11)
        assert len(adb.llamadas) == 2  # solo la nueva
        assert not _spool(tmp_path).exists()

    def test_linea_ilegible_del_spool_no_traba_la_cola(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "adb")
        _spool(tmp_path).write_text('{"sin": "ts"}\n')
        registro.registrar(_cfg(tmp_path), _pedido(), respuesta=_resp(), latencia_ms=5)
        assert len(adb.llamadas) == 1
        assert not _spool(tmp_path).exists()

    def test_archivo_sigue_escribiendo_el_jsonl(self, adb, tmp_path, monkeypatch):
        _modo(monkeypatch, "archivo")
        registro.registrar(_cfg(tmp_path), _pedido(), respuesta=_resp(), latencia_ms=5)
        assert _spool(tmp_path).exists()
        assert adb.llamadas == []


class TestComparar:
    def test_leer_adb_da_filas_con_la_forma_del_jsonl(self, adb, tmp_path, monkeypatch):
        from scripts import comparar_llm

        cfg = _cfg(tmp_path)
        linea = registro._linea(cfg, _pedido(), proveedor="anthropic", modelo="m", respuesta=_resp(),
                                error=None, latencia_ms=5)
        valores = registro._binds_adb(linea)

        class Cur:
            def execute(self, sql, binds=None):
                pass

            def fetchall(self):
                return [tuple(valores.get(c) for c in comparar_llm._COLUMNAS_ADB)]

            def close(self):
                pass

        @contextlib.contextmanager
        def conn(_cfg):
            yield types.SimpleNamespace(cursor=lambda: Cur())

        monkeypatch.setattr("src.memoria.db.connection", conn)
        filas, diag = comparar_llm.leer_adb(cfg)
        assert diag["lineas"] == 1 and filas[0]["uso"]["input_tokens"] == 10
        assert comparar_llm.agregar(filas, diag)["grupos"]["anthropic"]["n"] == 1

    def test_leer_adb_caida_devuelve_none(self, adb, tmp_path):
        from scripts import comparar_llm

        adb.caida = True
        assert comparar_llm.leer_adb(_cfg(tmp_path)) is None


class TestSombraAvisaSoloSiLaDiferenciaCambia:
    def test_diferencia_constante_se_avisa_una_vez_y_el_cambio_otra(self, caplog):
        from datetime import date as _d

        quota.reiniciar_estado()
        dia = _d(2026, 10, 30)
        with caplog.at_level("WARNING", logger="src.drilldown.quota"):
            for local, adb in ((0.3186, 0.0242), (0.3273, 0.0329), (0.5106, 0.2162)):  # siempre ≈ $0.2944 de más
                quota._comparar_sombra(dia, local, adb)
            assert len([r for r in caplog.records if "gasto[sombra]" in r.getMessage()]) == 1
            quota._comparar_sombra(dia, 0.60, 0.20)  # un cobro que la ADB no vio: la diferencia se movió
            avisos = [r.getMessage() for r in caplog.records if "gasto[sombra]" in r.getMessage()]
        assert len(avisos) == 2 and "pasó de" in avisos[1]
        quota.reiniciar_estado()
