"""Herencia de organización y paridad con los perfiles públicos de HEAD, sin red."""
import json
import logging
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_clientes_cli import FakeADB

from src.classifier import client_profile as perfiles
from src.clientes import __main__ as cli
from src.clientes import repo

CLIENTS = Path(__file__).resolve().parents[1] / "clients"


@pytest.fixture(autouse=True)
def _fuente_archivo(monkeypatch):
    monkeypatch.setenv("CLIENTES_FUENTE", "archivo")
    perfiles.reiniciar_estado()
    yield
    perfiles.reiniciar_estado()


@pytest.mark.parametrize("pais", ["sv", "cr"])
def test_perfil_identico_a_head(pais):
    cid = f"multicomputos_{pais}"
    esperado = json.loads(Path(__file__).with_name(f"{cid}_perfil_head.json").read_text())
    assert asdict(perfiles.load_client_profile(cid, str(CLIENTS))) == esperado


def test_base_no_es_cliente():
    assert perfiles._ids_candidatos(str(CLIENTS)) == ["multicomputos_cr", "multicomputos_sv"]
    assert [p.client_id for p in perfiles.load_all_active_profiles(str(CLIENTS))] == ["multicomputos_sv"]


def test_merge_profundo_listas_y_sin_mutacion(tmp_path):
    (tmp_path / "base").mkdir()
    base = {"catalog": {"a": {"uno": 1, "dos": 2}, "lista": [1, 2]}, "client": {"active": False}}
    (tmp_path / "base" / "org.yaml").write_text(yaml.safe_dump(base))
    hijo = {"client": {"id": "ejemplo", "extends": "org", "active": True},
            "catalog": {"a": {"dos": 3}, "lista": []}}
    resuelto = perfiles.resolver_perfil(hijo, str(tmp_path))
    assert resuelto == {"catalog": {"a": {"uno": 1, "dos": 3}, "lista": []},
                        "client": {"id": "ejemplo", "active": True}}
    assert hijo["client"]["extends"] == "org"
    assert yaml.safe_load((tmp_path / "base" / "org.yaml").read_text()) == base
    assert perfiles.resolver_perfil(base, str(tmp_path)) == base


@pytest.mark.parametrize("valor", [None, 12, [], {}, "", True])
def test_extends_debe_ser_string(valor, tmp_path):
    with pytest.raises(ValueError, match="client.extends debe ser una cadena"):
        perfiles.resolver_perfil({"client": {"extends": valor}}, str(tmp_path))


def test_base_inexistente(tmp_path):
    with pytest.raises(FileNotFoundError, match="Base de client.extends no encontrada"):
        perfiles.resolver_perfil({"client": {"extends": "ausente"}}, str(tmp_path))


def test_ciclo_y_cadena_de_bases(tmp_path):
    (tmp_path / "base").mkdir()
    for nombre, padre in (("a", "b"), ("b", "a")):
        (tmp_path / "base" / f"{nombre}.yaml").write_text(yaml.safe_dump({"client": {"extends": padre}}))
    with pytest.raises(ValueError, match="Ciclo en client.extends: a → b → a"):
        perfiles.resolver_perfil({"client": {"extends": "a"}}, str(tmp_path))
    (tmp_path / "base" / "b.yaml").write_text("catalog: {dato: heredado}\n")
    assert perfiles.resolver_perfil({"client": {"extends": "a"}}, str(tmp_path))["catalog"] == {"dato": "heredado"}


@pytest.mark.parametrize("modo", ["adb", "sombra"])
@pytest.mark.parametrize("pais", ["sv", "cr"])
def test_fuentes_reciben_perfil_completo_sin_diferencias(monkeypatch, caplog, modo, pais):
    cid = f"multicomputos_{pais}"
    completo, _ = perfiles._leer_yaml(cid, str(CLIENTS))
    monkeypatch.setenv("CLIENTES_FUENTE", modo)
    monkeypatch.setattr(perfiles, "_cfg", lambda: None)
    monkeypatch.setattr(perfiles, "_leer_adb", lambda cfg: {cid: completo})
    with caplog.at_level(logging.INFO):
        datos, _ = perfiles._datos_perfil(cid, str(CLIENTS))
    assert datos == completo
    assert "extends" not in datos["client"]
    assert not perfiles._diferencias_avisadas
    assert "difiere" not in caplog.text


def test_trainer_escribe_solo_hijo(tmp_path):
    import shutil

    shutil.copytree(CLIENTS, tmp_path / "clients")
    carpeta = tmp_path / "clients"
    base = (carpeta / "base" / "multicomputos.yaml").read_bytes()
    cr = (carpeta / "multicomputos_cr.yaml").read_bytes()
    assert perfiles.save_intelligence("multicomputos_sv", str(carpeta), "Perfil sintético", 3) == "archivo"
    hijo = yaml.safe_load((carpeta / "multicomputos_sv.yaml").read_text())
    assert hijo["client"]["extends"] == "multicomputos"
    assert "catalog" not in hijo
    assert perfiles.load_client_profile("multicomputos_sv", str(carpeta)).intelligence.profile == "Perfil sintético"
    assert (carpeta / "base" / "multicomputos.yaml").read_bytes() == base
    assert (carpeta / "multicomputos_cr.yaml").read_bytes() == cr


def test_importar_exportar_resuelto_sin_herencia(tmp_path):
    adb = FakeADB()
    cid = "multicomputos_cr"
    adb.clientes[cid] = {"org_id": "multicomputos", "nombre": "Ejemplo", "activo": False}
    archivo = CLIENTS / f"{cid}.yaml"
    args = SimpleNamespace(cliente=cid, archivo=str(archivo), aplicar=True)
    cli._cmd_importar(adb, args)
    guardado = adb.activos(cid)[0]["contenido"]
    assert "extends" not in guardado["client"]
    assert "catalog" in guardado
    salida = tmp_path / "exportado.yaml"
    cli._cmd_exportar(adb, SimpleNamespace(cliente=cid, salida=str(salida)))
    assert yaml.safe_load(salida.read_text()) == guardado
    assert repo.importar(adb, cid, yaml.safe_load(salida.read_text()), "prueba") is None


def test_adb_caida_recurre_a_yaml_resuelto(monkeypatch, tmp_path):
    from src.memoria import db

    cfg = SimpleNamespace(adb_dsn="dsn-invalido", adb_password="valor-sintetico", data_dir=str(tmp_path))

    def caida(config):
        assert config.adb_dsn == "dsn-invalido"
        raise ConnectionError("DSN inválido simulado")

    monkeypatch.setenv("CLIENTES_FUENTE", "adb")
    monkeypatch.setattr(perfiles, "_cfg", lambda: cfg)
    monkeypatch.setattr(db, "connection", caida)
    esperado = json.loads(Path(__file__).with_name("multicomputos_sv_perfil_head.json").read_text())
    assert asdict(perfiles.load_client_profile("multicomputos_sv", str(CLIENTS))) == esperado
