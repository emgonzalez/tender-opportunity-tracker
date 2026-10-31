"""Arreglos previos a SESION_FUENTE=adb: consumidores, sobrescritura con sesión vieja, aviso sin secreto."""
import logging
import os

import pytest

from src.config import Config
from src.persistencia import sesion
from tests.test_sesion_adb import SECRETO_SESION, FakeAlmacen, _ruta, _sube, entorno  # noqa: F401


@pytest.fixture(autouse=True)
def _reinicia_estado_del_modulo():
    sesion._aviso_secreto_emitido = False
    sesion._ultimo_intento_materializar = 0.0
    yield
    sesion._aviso_secreto_emitido = False
    sesion._ultimo_intento_materializar = 0.0


class SoloLee(FakeAlmacen):
    """La ADB lee pero no escribe."""
    escribe = False

    def escribir(self, portal_id, contenido, version_esperada):
        if not self.escribe:
            raise ConnectionError("ORA-00000: solo lectura")
        return super().escribir(portal_id, contenido, version_esperada)


def test_escenario_del_bug_subida_fallida_no_se_pisa(entorno):  # noqa: F811
    cfg, _, _ = entorno
    alm = SoloLee()
    # la ADB tiene una sesión vieja (v3), ya materializada
    vieja = FakeAlmacen()
    _sube(cfg, vieja, '{"cookies": ["vieja"]}')
    alm.fila = (vieja.fila[0], 3)
    os.remove(_ruta(cfg))
    os.remove(_ruta(cfg) + ".meta")
    assert sesion.preparar_sesion(cfg, alm) is True
    # el login produce una sesión nueva y la subida falla
    with open(_ruta(cfg), "w") as f:
        f.write(SECRETO_SESION)
    assert sesion.devolver_sesion(cfg, alm) is False
    # la siguiente corrida no debe pisarla con la vieja de la ADB
    assert sesion.preparar_sesion(cfg, alm) is False
    assert open(_ruta(cfg)).read() == SECRETO_SESION
    # y cuando la ADB vuelve a escribir, el reintento sube la sesión nueva
    alm.escribe = True
    assert sesion.preparar_sesion(cfg, alm) is False
    assert alm.fila[1] == 4
    assert open(_ruta(cfg)).read() == SECRETO_SESION


def test_consumidor_directo_de_storage_state_path_obtiene_la_sesion(entorno, monkeypatch):  # noqa: F811
    cfg, data, _ = entorno
    alm = FakeAlmacen()
    _sube(cfg, alm)
    os.remove(_ruta(cfg))
    os.remove(_ruta(cfg) + ".meta")
    monkeypatch.setattr(sesion, "AlmacenOracle", lambda c: alm)
    config = Config()
    config.data_dir = str(data)
    config.identidad_secret = cfg.identidad_secret
    ruta = config.storage_state_path
    assert open(ruta).read() == SECRETO_SESION
    lecturas = []
    alm.leer = lambda p: lecturas.append(p)  # ya materializada: no se toca la ADB
    assert config.storage_state_path == ruta and lecturas == []


def test_storage_state_path_con_adb_caida_devuelve_la_ruta_sin_levantar(entorno):  # noqa: F811
    cfg, data, _ = entorno
    config = Config()
    config.data_dir = str(data)
    config.identidad_secret = cfg.identidad_secret
    config.adb_dsn, config.adb_user, config.adb_password = "dsn-que-no-existe-invalido", "u", "p"
    ruta = config.storage_state_path
    assert ruta == _ruta(cfg) and not os.path.exists(ruta)


def test_sin_identidad_secret_un_solo_error(entorno, caplog):  # noqa: F811
    cfg, _, _ = entorno
    cfg.identidad_secret = ""
    alm = FakeAlmacen()
    with caplog.at_level(logging.INFO):
        assert sesion.preparar_sesion(cfg, alm) is False
        assert _sube(cfg, alm) is False
        assert sesion.preparar_sesion(cfg, alm) is False
    errores = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errores) == 1 and "IDENTIDAD_SECRET" in errores[0].getMessage()
    assert "SUPER-SECRETO" not in caplog.text and alm.escrituras == 0


def test_con_archivo_no_hay_error_ni_cambio(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("SESION_FUENTE", raising=False)
    config = Config()
    config.data_dir = str(tmp_path)
    config.identidad_secret = ""
    assert config.storage_state_path == os.path.join(str(tmp_path), "storage_state.json")
    assert sesion.preparar_sesion(config) is False and sesion.devolver_sesion(config) is False
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
