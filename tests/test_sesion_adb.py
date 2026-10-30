"""Fase 5 de persistencia-adb: sesión de COMPRASAL cifrada en la ADB (SESION_FUENTE), sin Oracle real."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from src.persistencia import sesion
from src.persistencia.cifrado import cifrar

SECRETO_SESION = '{"cookies": [{"name": "JSESSION", "value": "SUPER-SECRETO-123"}]}'


class FakeAlmacen:
    def __init__(self):
        self.fila = None  # (bytes, version)
        self.cae = False
        self.escrituras = 0

    def leer(self, portal_id):
        if self.cae:
            raise ConnectionError("DPY-6005: DSN inválido")
        return self.fila

    def escribir(self, portal_id, contenido, version_esperada):
        if self.cae:
            raise ConnectionError("DPY-6005: DSN inválido")
        actual = self.fila[1] if self.fila else 0
        if actual != version_esperada:
            raise sesion.Conflicto()
        self.fila = (contenido, actual + 1)
        self.escrituras += 1
        return actual + 1


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SESION_TMP_DIR", str(tmp))
    monkeypatch.setenv("SESION_FUENTE", "adb")
    cfg = SimpleNamespace(data_dir=str(data), identidad_secret="s3creto-de-pruebas")
    return cfg, data, tmp


def _ruta(cfg):
    return sesion.ruta_sesion(cfg.data_dir)


def _sube(cfg, almacen, texto=SECRETO_SESION):
    ruta = _ruta(cfg)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w") as f:
        f.write(texto)
    return sesion.devolver_sesion(cfg, almacen)


def test_ida_y_vuelta_cifrado(entorno):
    cfg, data, tmp = entorno
    alm = FakeAlmacen()
    assert _sube(cfg, alm) is True
    blob, version = alm.fila
    assert version == 1 and b"SUPER-SECRETO" not in blob and b"JSESSION" not in blob
    os.remove(_ruta(cfg))
    os.remove(_ruta(cfg) + ".meta")
    assert sesion.preparar_sesion(cfg, alm) is True
    assert open(_ruta(cfg)).read() == SECRETO_SESION
    assert oct(os.stat(_ruta(cfg)).st_mode & 0o777) == "0o600"


def test_no_se_escribe_plaintext_en_disco_persistente(entorno):
    cfg, data, tmp = entorno
    alm = FakeAlmacen()
    _sube(cfg, alm)
    assert os.listdir(data) == []
    assert str(_ruta(cfg)).startswith(str(tmp))


def test_devolver_solo_si_cambio(entorno):
    cfg, _, _ = entorno
    alm = FakeAlmacen()
    _sube(cfg, alm)
    assert sesion.devolver_sesion(cfg, alm) is False
    assert alm.escrituras == 1
    _sube(cfg, alm, '{"cookies": []}')
    assert alm.escrituras == 2 and alm.fila[1] == 2


def test_preparar_no_pisa_lo_local_si_es_igual_o_mas_nuevo(entorno):
    cfg, _, _ = entorno
    alm = FakeAlmacen()
    _sube(cfg, alm)
    assert sesion.preparar_sesion(cfg, alm) is False


def test_conflicto_de_version_gana_la_sesion_nueva(entorno):
    cfg, _, _ = entorno
    alm = FakeAlmacen()
    _sube(cfg, alm)  # v1 local
    # otra VM sube v2 mientras tanto
    alm.fila = (alm.fila[0], 2)
    _sube(cfg, alm, '{"cookies": ["nueva"]}')
    assert alm.fila[1] == 3
    with pytest.raises(sesion.Conflicto):
        alm.escribir("comprasal_sv", b"x", 1)


def test_adb_caida_no_levanta_y_conserva_lo_local(entorno, caplog):
    cfg, _, _ = entorno
    alm = FakeAlmacen()
    alm.cae = True
    assert sesion.preparar_sesion(cfg, alm) is False
    assert _sube(cfg, alm) is False  # no sube, no levanta
    assert os.path.exists(_ruta(cfg))  # el archivo local queda para esta corrida
    assert "SUPER-SECRETO" not in caplog.text


def test_dsn_invalido_real_cae_sin_levantar(entorno, monkeypatch):
    """AlmacenOracle con un DSN inválido (nunca contraseña mala: ORA-28000 bloquea el usuario)."""
    cfg, _, _ = entorno
    cfg.adb_dsn, cfg.adb_user, cfg.adb_password = "dsn-que-no-existe-invalido", "u", "p"
    assert sesion.preparar_sesion(cfg) is False
    assert sesion.devolver_sesion(cfg) is False


def test_blob_con_otra_clave_o_contexto_se_ignora(entorno):
    cfg, _, _ = entorno
    alm = FakeAlmacen()
    alm.fila = (cifrar("otra-clave", SECRETO_SESION.encode(), "sesion_portal_compras/comprasal_sv").encode(), 1)
    assert sesion.preparar_sesion(cfg, alm) is False
    assert not os.path.exists(_ruta(cfg))
    # mismo secreto pero contexto distinto (p. ej. un blob de MFA) tampoco descifra
    alm.fila = (cifrar(cfg.identidad_secret, SECRETO_SESION.encode(), "mfa").encode(), 1)
    assert sesion.preparar_sesion(cfg, alm) is False


def test_con_archivo_nada_cambia(tmp_path, monkeypatch):
    monkeypatch.delenv("SESION_FUENTE", raising=False)
    cfg = SimpleNamespace(data_dir=str(tmp_path), identidad_secret="x")
    assert sesion.ruta_sesion(str(tmp_path)) == os.path.join(str(tmp_path), "storage_state.json")
    alm = FakeAlmacen()
    (tmp_path / "storage_state.json").write_text(SECRETO_SESION)
    assert sesion.preparar_sesion(cfg, alm) is False
    assert sesion.devolver_sesion(cfg, alm) is False
    assert alm.fila is None and alm.escrituras == 0
    assert os.listdir(tmp_path) == ["storage_state.json"]


def test_fuente_desconocida_equivale_a_archivo(monkeypatch, tmp_path):
    monkeypatch.setenv("SESION_FUENTE", "adbb")
    assert sesion.ruta_sesion(str(tmp_path)) == os.path.join(str(tmp_path), "storage_state.json")


def test_is_session_valid_con_archivo_sin_sesion(tmp_path, monkeypatch):
    from src.auth.session import is_session_valid
    monkeypatch.delenv("SESION_FUENTE", raising=False)
    cfg = SimpleNamespace(data_dir=str(tmp_path), identidad_secret="")
    assert asyncio.run(is_session_valid(cfg)) is False
