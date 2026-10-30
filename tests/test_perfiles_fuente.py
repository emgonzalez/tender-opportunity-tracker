"""Fase 2 de persistencia-adb: perfiles de cliente ADB-first (CLIENTES_FUENTE), sin Oracle real."""
import contextlib
import json
import logging
from types import SimpleNamespace

import pytest
import yaml

from src import portales
from src.classifier import client_profile as cp


def _yaml(cid="acme", nombre="Acme", activo=True, extra=None):
    d = {
        "client": {"id": cid, "name": nombre, "active": activo},
        "catalog": {"categories": [{"id": "x", "label": "X", "items": ["a"]}]},
        "rules": {"context_note": "n"},
    }
    d.update(extra or {})
    return d


class FakeDB:
    """Tabla cliente_config en memoria detrás de una API mínima de cursor."""

    def __init__(self, filas=None, clientes=("acme", "multicomputos_sv")):
        self.config = filas or []  # dicts: cliente_id, version, contenido(dict), activa, creado_por
        self.clientes = set(clientes)
        self.commits = 0
        self.falla = False

    @contextlib.contextmanager
    def connection(self, cfg):
        if self.falla:
            raise RuntimeError("ADB caída")
        yield self
        self.commits += 1

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db, self._res = db, []

    def execute(self, sql, *args, **kw):
        s = " ".join(sql.split())
        c = kw.get("c")
        if s.startswith("SELECT cc.cliente_id"):
            vivas = [f for f in self.db.config if f["activa"]]
            ult = {}
            for f in vivas:
                if f["cliente_id"] not in ult or f["version"] > ult[f["cliente_id"]]["version"]:
                    ult[f["cliente_id"]] = f
            self._res = [(k, json.dumps(v["contenido"])) for k, v in ult.items()]
        elif s.startswith("SELECT version, JSON_SERIALIZE") or s.startswith("SELECT JSON_SERIALIZE(contenido"):
            fs = sorted((f for f in self.db.config if f["cliente_id"] == c and f["activa"]),
                        key=lambda f: -f["version"])
            self._res = [(f["version"], json.dumps(f["contenido"])) if "version," in s
                         else (json.dumps(f["contenido"]),) for f in fs]
        elif s.startswith("SELECT NVL(MAX"):
            self._res = [(max([f["version"] for f in self.db.config if f["cliente_id"] == c] or [0]) + 1,)]
        elif s.startswith("UPDATE cliente_config SET activa = 0"):
            for f in self.db.config:
                if f["cliente_id"] == c:
                    f["activa"] = 0
        elif s.startswith("INSERT INTO cliente_config"):
            self.db.config.append({"cliente_id": c, "version": kw["v"], "contenido": json.loads(kw["j"]),
                                   "activa": 1, "creado_por": kw["p"]})
        elif s.startswith("SELECT COUNT(*) FROM cliente "):
            self._res = [(1 if c in self.db.clientes else 0,)]
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
    clients = tmp_path / "clients"
    clients.mkdir()
    data = tmp_path / "data"
    cfg = SimpleNamespace(adb_dsn="dsn", adb_password="x", data_dir=str(data), clients_dir=str(clients))
    monkeypatch.setattr(cp, "_cfg", lambda: cfg)
    monkeypatch.delenv("CLIENTES_FUENTE", raising=False)
    cp.reiniciar_estado()
    portales._portal_en.cache_clear()
    db = FakeDB()
    monkeypatch.setattr("src.memoria.db.connection", db.connection)

    def escribir(d, nombre=None):
        (clients / f"{nombre or d['client']['id']}.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")

    yield SimpleNamespace(clients=str(clients), data=data, cfg=cfg, db=db, escribir=escribir, mp=monkeypatch)
    cp.reiniciar_estado()


def _fila(cid, contenido, version=1, activa=1):
    return {"cliente_id": cid, "version": version, "contenido": contenido, "activa": activa, "creado_por": "t"}


def test_archivo_por_defecto_no_toca_la_adb(entorno):
    entorno.escribir(_yaml())
    entorno.db.falla = True  # si se tocara, explotaría
    entorno.db.config.append(_fila("acme", _yaml(nombre="De la ADB")))
    assert cp.load_client_profile("acme", entorno.clients).client_name == "Acme"
    assert [p.client_id for p in cp.load_all_active_profiles(entorno.clients)] == ["acme"]
    assert not (entorno.data / "cache").exists()


def test_archivo_sin_yaml_sigue_levantando(entorno):
    with pytest.raises(FileNotFoundError):
        cp.load_client_profile("nadie", entorno.clients)


def test_adb_manda_y_llena_la_cache(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.escribir(_yaml())
    entorno.db.config.append(_fila("acme", _yaml(nombre="De la ADB")))
    assert cp.load_client_profile("acme", entorno.clients).client_name == "De la ADB"
    en_cache = json.loads((entorno.data / "cache" / "clientes" / "acme.json").read_text(encoding="utf-8"))
    assert en_cache["client"]["name"] == "De la ADB"


def test_adb_toma_la_ultima_version_activa(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.db.config += [_fila("acme", _yaml(nombre="v1"), 1, 0), _fila("acme", _yaml(nombre="v2"), 2, 1)]
    assert cp.load_client_profile("acme", entorno.clients).client_name == "v2"


def test_adb_incluye_clientes_que_no_estan_en_el_directorio(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.escribir(_yaml("acme"))
    entorno.db.config.append(_fila("solo_adb", _yaml("solo_adb")))
    ids = [p.client_id for p in cp.load_all_active_profiles(entorno.clients)]
    assert ids == ["acme", "solo_adb"]


def test_adb_caida_usa_la_cache(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.escribir(_yaml())
    entorno.db.config.append(_fila("acme", _yaml(nombre="De la ADB")))
    cp.load_client_profile("acme", entorno.clients)  # llena la caché
    cp.reiniciar_estado()
    entorno.db.falla = True
    assert cp.load_client_profile("acme", entorno.clients).client_name == "De la ADB"


def test_adb_caida_sin_cache_cae_al_yaml_y_la_corrida_sigue(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.escribir(_yaml())
    entorno.db.falla = True
    assert [p.client_name for p in cp.load_all_active_profiles(entorno.clients)] == ["Acme"]


def test_adb_caida_no_se_reintenta_en_cada_lectura(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.escribir(_yaml())
    entorno.db.falla = True
    llamadas = []
    original = entorno.db.connection
    entorno.mp.setattr("src.memoria.db.connection", lambda cfg: (llamadas.append(1), original(cfg))[1])
    for _ in range(3):
        cp.load_client_profile("acme", entorno.clients)
    assert len(llamadas) == 1


def test_adb_con_dsn_invalido_de_verdad(entorno):
    """Sin mocks: un DSN inexistente (la contraseña no importa) no puede tumbar la carga."""
    entorno.mp.undo()
    entorno.mp.setattr(cp, "_cfg", lambda: entorno.cfg)
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.cfg.adb_dsn = "dsn-invalido-sin-host"
    entorno.escribir(_yaml())
    assert cp.load_client_profile("acme", entorno.clients).client_name == "Acme"


def test_adb_sin_ninguna_fuente_levanta_filenotfound(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    with pytest.raises(FileNotFoundError):
        cp.load_client_profile("nadie", entorno.clients)


def test_sombra_usa_el_yaml_y_loguea_la_diferencia_una_vez(entorno, caplog):
    entorno.mp.setenv("CLIENTES_FUENTE", "sombra")
    entorno.escribir(_yaml())
    entorno.db.config.append(_fila("acme", _yaml(nombre="Otro")))
    with caplog.at_level(logging.WARNING, logger=cp.logger.name):
        assert cp.load_client_profile("acme", entorno.clients).client_name == "Acme"
        cp.load_client_profile("acme", entorno.clients)
    avisos = [r.getMessage() for r in caplog.records if "difiere" in r.getMessage()]
    assert len(avisos) == 1 and "client" in avisos[0]
    assert not (entorno.data / "cache").exists()


def test_sombra_sin_diferencias_no_avisa(entorno, caplog):
    entorno.mp.setenv("CLIENTES_FUENTE", "sombra")
    entorno.escribir(_yaml())
    entorno.db.config.append(_fila("acme", _yaml()))
    with caplog.at_level(logging.WARNING, logger=cp.logger.name):
        cp.load_client_profile("acme", entorno.clients)
    assert not [r for r in caplog.records if "difiere" in r.getMessage()]


def test_sombra_con_adb_caida_usa_el_yaml(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "sombra")
    entorno.escribir(_yaml())
    entorno.db.falla = True
    assert cp.load_client_profile("acme", entorno.clients).client_name == "Acme"


def test_alias_de_id_viejo_en_adb_y_en_la_carga(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.db.config.append(_fila("multicomputos", _yaml("multicomputos_sv", "MC")))
    assert cp.load_client_profile("multicomputos", entorno.clients).client_name == "MC"
    assert (entorno.data / "cache" / "clientes" / "multicomputos_sv.json").exists()


def test_alias_canonico_gana_si_conviven(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.db.config += [_fila("multicomputos", _yaml("multicomputos_sv", "viejo")),
                          _fila("multicomputos_sv", _yaml("multicomputos_sv", "nuevo"))]
    assert cp.load_client_profile("multicomputos_sv", entorno.clients).client_name == "nuevo"


def test_portal_de_cliente_desde_la_adb(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.db.config.append(_fila("acme", _yaml(extra={"client": {"id": "acme", "name": "A", "portal": "sicop_cr"}})))
    assert portales.portal_de_cliente(entorno.clients, "acme") == "sicop_cr"
    entorno.db.falla = True
    cp.reiniciar_estado()
    assert portales.portal_de_cliente(entorno.clients, "nadie") == portales.PORTAL_ID_DEFAULT


def test_portal_de_cliente_archivo_intacto(entorno):
    entorno.escribir(_yaml(extra={"client": {"id": "acme", "name": "A", "portal": "sicop_cr"}}))
    assert portales.portal_de_cliente(entorno.clients, "acme") == "sicop_cr"


# ── escritura: registrar_version / trainer / sincronizador ────────────────────

def test_registrar_version_hash_sin_cambios_no_crea_version(entorno):
    entorno.db.config.append(_fila("acme", _yaml()))
    assert cp.registrar_version(entorno.db, "acme", _yaml(), "t") is None
    assert len(entorno.db.config) == 1


def test_registrar_version_con_cambio_apaga_la_anterior(entorno):
    entorno.db.config.append(_fila("acme", _yaml()))
    assert cp.registrar_version(entorno.db, "acme", _yaml(nombre="Nuevo"), "t") == 2
    assert [(f["version"], f["activa"]) for f in entorno.db.config] == [(1, 0), (2, 1)]


def test_registrar_version_primera_vez(entorno):
    assert cp.registrar_version(entorno.db, "acme", _yaml(), "t") == 1


def test_save_intelligence_archivo_no_toca_la_adb(entorno):
    entorno.escribir(_yaml())
    entorno.db.falla = True
    cp.save_intelligence("acme", entorno.clients, "perfil", 3)
    assert yaml.safe_load(open(f"{entorno.clients}/acme.yaml"))["intelligence"]["profile"] == "perfil"


def test_save_intelligence_sube_version_si_la_fuente_no_es_archivo(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "sombra")
    entorno.escribir(_yaml())
    entorno.db.config.append(_fila("acme", _yaml()))
    cp.save_intelligence("acme", entorno.clients, "perfil", 3)
    activa = [f for f in entorno.db.config if f["activa"]]
    assert len(activa) == 1 and activa[0]["version"] == 2
    assert activa[0]["contenido"]["intelligence"]["profile"] == "perfil"


def test_save_intelligence_no_propaga_si_la_adb_falla(entorno):
    entorno.mp.setenv("CLIENTES_FUENTE", "adb")
    entorno.escribir(_yaml())
    entorno.db.falla = True
    cp.save_intelligence("acme", entorno.clients, "perfil", 3)  # no levanta
    assert "perfil" in open(f"{entorno.clients}/acme.yaml", encoding="utf-8").read()


def test_save_intelligence_resuelve_id_viejo(entorno):
    entorno.escribir(_yaml("multicomputos_sv", "MC"))
    cp.save_intelligence("multicomputos", entorno.clients, "p", 1)
    assert "intelligence" in yaml.safe_load(open(f"{entorno.clients}/multicomputos_sv.yaml"))


def test_sincronizar_dry_run_y_aplicar(entorno):
    from scripts import sincronizar_perfiles as sp
    entorno.escribir(_yaml("acme"))
    entorno.escribir(_yaml("huerfano"))
    entorno.db.config.append(_fila("acme", _yaml("acme", "Viejo")))
    assert sp.sincronizar(entorno.db, entorno.clients) == {"acme": "actualizado", "huerfano": "sin_cliente"}
    assert len(entorno.db.config) == 1  # dry-run: nada escrito
    assert sp.sincronizar(entorno.db, entorno.clients, aplicar=True)["acme"] == "actualizado"
    assert len(entorno.db.config) == 2
    assert sp.sincronizar(entorno.db, entorno.clients, aplicar=True) == {"acme": "sin_cambio", "huerfano": "sin_cliente"}
    assert len(entorno.db.config) == 2  # hash sin cambios → sin versión nueva


def test_a_dict_acepta_un_lob_y_el_sql_pide_clob():
    """Regresión 2026-10-30: JSON_SERIALIZE sin RETURNING CLOB falla con ORA-40478 en perfiles de más de 4000 caracteres."""
    from src.classifier import client_profile as cp

    class _Lob:
        def read(self):
            return '{"client": {"id": "x"}}'

    assert cp._a_dict(_Lob()) == {"client": {"id": "x"}}
    assert "RETURNING CLOB" in cp._SQL_ACTIVAS
