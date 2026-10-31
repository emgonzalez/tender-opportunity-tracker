"""`python -m src.clientes` (Fase C del plan de eliminar clients/*.yaml), sin Oracle: ADB falsa inyectada."""
import contextlib
import json

import pytest

from src.classifier.client_profile import hash_contenido
from src.clientes import __main__ as cli
from src.clientes import repo


class FakeADB:
    """`organizacion`, `cliente` y `cliente_config` en memoria detrás de una API mínima de cursor."""

    def __init__(self):
        self.orgs = {"multicomputos"}
        self.clientes = {}   # id -> {"org_id", "nombre", "activo"}
        self.config = []     # dicts: cliente_id, version, contenido, activa, creado_por
        self.transacciones = 0

    @contextlib.contextmanager
    def conectar(self):
        yield self
        self.transacciones += 1

    def cursor(self):
        return FakeCursor(self)

    def activos(self, cid):
        return sorted((f for f in self.config if f["cliente_id"] == cid and f["activa"]), key=lambda f: -f["version"])


class FakeCursor:
    def __init__(self, db):
        self.db, self._res = db, []

    def execute(self, sql, *args, **kw):
        s = " ".join(sql.split())
        c = kw.get("c")
        if s.startswith("SELECT COUNT(*) FROM cliente "):
            self._res = [(1 if c in self.db.clientes else 0,)]
        elif s.startswith("SELECT COUNT(*) FROM organizacion"):
            self._res = [(1 if kw["o"] in self.db.orgs else 0,)]
        elif s.startswith("SELECT c.cliente_id"):
            self._res = [(k, v["org_id"], v["nombre"], v["activo"],
                          max((f["version"] for f in self.db.activos(k)), default=None))
                         for k, v in sorted(self.db.clientes.items())]
        elif s.startswith("SELECT version, JSON_SERIALIZE") and "activa = 1" in s:
            self._res = [(f["version"], json.dumps(f["contenido"])) for f in self.db.activos(c)]
        elif s.startswith("SELECT version, activa, creado_por"):
            self._res = [(f["version"], f["activa"], f["creado_por"], "2026-01-01", json.dumps(f["contenido"]))
                         for f in sorted((f for f in self.db.config if f["cliente_id"] == c),
                                         key=lambda f: -f["version"])]
        elif s.startswith("SELECT JSON_SERIALIZE(contenido RETURNING CLOB) FROM cliente_config WHERE cliente_id = :c AND version"):
            self._res = [(json.dumps(f["contenido"]),) for f in self.db.config
                         if f["cliente_id"] == c and f["version"] == kw["v"]]
        elif s.startswith("SELECT NVL(MAX"):
            self._res = [(max([f["version"] for f in self.db.config if f["cliente_id"] == c] or [0]) + 1,)]
        elif s.startswith("UPDATE cliente_config SET activa = 0"):
            for f in self.db.config:
                if f["cliente_id"] == c:
                    f["activa"] = 0
        elif s.startswith("INSERT INTO cliente_config"):
            self.db.config.append({"cliente_id": c, "version": kw["v"], "contenido": json.loads(kw["j"]),
                                   "activa": 1, "creado_por": kw["p"]})
        elif s.startswith("INSERT INTO cliente "):
            self.db.clientes[c] = {"org_id": kw["o"], "nombre": kw["n"], "activo": 1}
        elif s.startswith("UPDATE cliente SET activo"):
            self.db.clientes[c]["activo"] = kw["a"]
        else:  # pragma: no cover
            raise AssertionError(f"SQL no previsto: {s}")

    def fetchall(self):
        return list(self._res)

    def fetchone(self):
        return self._res[0] if self._res else None

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(var, "x")


@pytest.fixture
def adb():
    return FakeADB()


@pytest.fixture
def correr(adb, capsys):
    def _correr(*argv):
        capsys.readouterr()
        codigo = cli.main(list(argv), conectar=adb.conectar)
        salida = capsys.readouterr()
        return codigo, salida.out, salida.err
    return _correr


def _crear(correr, cid="acme"):
    assert correr("crear", cid, "--nombre", "Acme", "--aplicar")[0] == 0


def _activa(adb, cid="acme"):
    return adb.activos(cid)[0]


def test_crear_dry_run_no_escribe(adb, correr):
    codigo, out, _ = correr("crear", "acme", "--nombre", "Acme")
    assert codigo == 0 and "[dry-run]" in out
    assert adb.clientes == {} and adb.config == []


def test_crear_inserta_cliente_y_version_1(adb, correr):
    _crear(correr)
    assert adb.clientes["acme"] == {"org_id": "multicomputos", "nombre": "Acme", "activo": 1}
    f = _activa(adb)
    assert f["version"] == 1 and f["contenido"]["client"] == {
        "id": "acme", "name": "Acme", "active": True, "portal": "comprasal_sv"}
    assert f["creado_por"].startswith("cli:")


def test_crear_org_configurable_y_validada(adb, correr):
    adb.orgs.add("otra_org")
    assert correr("crear", "acme", "--nombre", "A", "--org", "otra_org", "--aplicar")[0] == 0
    assert adb.clientes["acme"]["org_id"] == "otra_org"
    codigo, _, err = correr("crear", "otro", "--nombre", "O", "--org", "no_existe", "--aplicar")
    assert codigo == 1 and "organización" in err and "otro" not in adb.clientes


def test_crear_duplicado_falla(correr):
    _crear(correr)
    assert correr("crear", "acme", "--nombre", "Acme", "--aplicar")[0] == 1


def test_exportar_importar_sin_cambios(adb, correr, tmp_path):
    _crear(correr)
    archivo = tmp_path / "acme.json"
    assert correr("exportar", "acme", "-o", str(archivo))[0] == 0
    codigo, out, _ = correr("importar", "acme", str(archivo), "--aplicar")
    assert codigo == 0 and "sin cambios" in out
    assert len(adb.config) == 1


def test_importar_con_cambio_crea_version_nueva(adb, correr, tmp_path):
    _crear(correr)
    archivo = tmp_path / "acme.json"
    datos = json.loads(json.dumps(_activa(adb)["contenido"]))
    datos["rules"]["context_note"] = "nota nueva"
    archivo.write_text(json.dumps(datos), encoding="utf-8")

    _, out, _ = correr("importar", "acme", str(archivo))  # dry-run
    assert "[dry-run]" in out and "versión 2" in out and len(adb.config) == 1

    assert correr("importar", "acme", str(archivo), "--aplicar")[0] == 0
    assert [(f["version"], f["activa"]) for f in adb.config] == [(1, 0), (2, 1)]
    assert _activa(adb)["contenido"]["rules"]["context_note"] == "nota nueva"


@pytest.mark.parametrize("contenido", ["{no es json", "[1, 2]", '{"client": {"id": "acme"}}',
                                       '{"client": {"id": "otro", "name": "X"}, "catalog": {}}'])
def test_importar_invalido_se_rechaza_sin_escribir(adb, correr, tmp_path, contenido):
    _crear(correr)
    archivo = tmp_path / "malo.json"
    archivo.write_text(contenido, encoding="utf-8")
    codigo, _, err = correr("importar", "acme", str(archivo), "--aplicar")
    assert codigo == 1 and err.startswith("ERROR")
    assert len(adb.config) == 1


def test_importar_cliente_inexistente_falla(adb, correr, tmp_path):
    archivo = tmp_path / "a.json"
    archivo.write_text(json.dumps(repo.perfil_nuevo("acme", "Acme")), encoding="utf-8")
    assert correr("importar", "acme", str(archivo), "--aplicar")[0] == 1
    assert adb.config == []


def test_desactivar_y_activar_mantienen_las_dos_marcas(adb, correr):
    _crear(correr)
    assert correr("desactivar", "acme")[0] == 0  # dry-run
    assert adb.clientes["acme"]["activo"] == 1 and len(adb.config) == 1

    assert correr("desactivar", "acme", "--aplicar")[0] == 0
    assert adb.clientes["acme"]["activo"] == 0 and _activa(adb)["contenido"]["client"]["active"] is False
    assert len(adb.config) == 2

    assert correr("activar", "acme", "--aplicar")[0] == 0
    assert adb.clientes["acme"]["activo"] == 1 and _activa(adb)["contenido"]["client"]["active"] is True
    assert len(adb.config) == 3

    _, out, _ = correr("activar", "acme", "--aplicar")  # ya activo: ni versión ni divergencia
    assert "sin cambios" in out and len(adb.config) == 3


def test_activar_realinea_si_las_marcas_ya_divergian(adb, correr):
    _crear(correr)
    adb.clientes["acme"]["activo"] = 0  # divergencia heredada: JSON activo, columna apagada
    assert correr("activar", "acme", "--aplicar")[0] == 0
    assert adb.clientes["acme"]["activo"] == 1 and len(adb.config) == 1


def test_importar_sincroniza_la_columna_activo(adb, correr, tmp_path):
    _crear(correr)
    datos = _activa(adb)["contenido"] | {"client": {"id": "acme", "name": "Acme", "active": False}}
    archivo = tmp_path / "a.json"
    archivo.write_text(json.dumps(datos), encoding="utf-8")
    assert correr("importar", "acme", str(archivo), "--aplicar")[0] == 0
    assert adb.clientes["acme"]["activo"] == 0


def test_revertir_crea_version_nueva_y_nunca_borra(adb, correr):
    _crear(correr)
    v1 = json.loads(json.dumps(_activa(adb)["contenido"]))
    correr("desactivar", "acme", "--aplicar")
    assert len(adb.config) == 2

    assert correr("revertir", "acme", "1")[0] == 0  # dry-run
    assert len(adb.config) == 2

    assert correr("revertir", "acme", "1", "--aplicar")[0] == 0
    assert len(adb.config) == 3
    assert _activa(adb)["version"] == 3 and hash_contenido(_activa(adb)["contenido"]) == hash_contenido(v1)
    assert adb.clientes["acme"]["activo"] == 1
    assert [f["version"] for f in adb.config] == [1, 2, 3]


def test_revertir_version_inexistente(correr):
    _crear(correr)
    codigo, _, err = correr("revertir", "acme", "9", "--aplicar")
    assert codigo == 1 and "9" in err


def test_listar_mostrar_versiones(correr):
    _crear(correr)
    assert "acme" in correr("listar")[1]
    out = correr("mostrar", "acme")[1]
    assert "Acme" in out and "versión activa : 1" in out
    assert "ACTIVA" in correr("versiones", "acme")[1]
    assert correr("mostrar", "nadie")[0] == 1


def test_exportar_a_stdout_es_json_del_perfil_activo(adb, correr):
    _crear(correr)
    out = correr("exportar", "acme")[1]
    assert json.loads(out) == _activa(adb)["contenido"]


def test_alias_de_cliente_resuelve_al_canonico(adb, correr):
    _crear(correr, "multicomputos_sv")
    assert correr("mostrar", "multicomputos")[0] == 0


def test_sin_adb_configurada_informa_sin_traza(monkeypatch, capsys):
    monkeypatch.setenv("ADB_DSN", "")
    monkeypatch.setenv("ADB_PASSWORD", "")
    assert cli.main(["listar"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR") and "Traceback" not in err


def test_adb_inalcanzable_no_filtra_el_dsn(monkeypatch, capsys):
    monkeypatch.setenv("ADB_DSN", "host-invalido.example.com:1/nada")
    monkeypatch.setenv("ADB_PASSWORD", "secreto-de-prueba")
    assert cli.main(["listar"]) == 1
    err = capsys.readouterr().err
    assert "host-invalido" not in err and "secreto-de-prueba" not in err
