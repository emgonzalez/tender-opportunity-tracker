"""src/persistencia: selector de fuente, caché atómica, spool y cifrado de blobs."""
import os

import pytest

from src.persistencia import cache, cifrado, fuente
from src.persistencia.spool import Spool

# ── fuente ────────────────────────────────────────────────────────────────────

def test_fuente_por_defecto_es_archivo():
    assert fuente.fuente("clientes", {}) == "archivo"
    assert not fuente.lee_adb("clientes", {}) and not fuente.manda_adb("clientes", {})


@pytest.mark.parametrize("valor,lee,manda", [("archivo", False, False), ("sombra", True, False), ("ADB", True, True)])
def test_fuente_valores(valor, lee, manda):
    env = {"ESTADO_FUENTE": valor}
    assert fuente.lee_adb("estado", env) is lee and fuente.manda_adb("estado", env) is manda


def test_fuente_desconocida_no_mueve_el_estado():
    assert fuente.fuente("cuota", {"CUOTA_FUENTE": "adbb"}) == "archivo"


# ── caché ─────────────────────────────────────────────────────────────────────

def test_escribir_json_roundtrip_y_sin_temporales(tmp_path):
    ruta = tmp_path / "sub" / "x.json"
    cache.escribir_json(ruta, {"a": 1})
    assert cache.leer_json(ruta) == {"a": 1}
    assert [p.name for p in ruta.parent.iterdir()] == ["x.json"]


def test_escritura_fallida_deja_el_archivo_anterior(tmp_path):
    ruta = tmp_path / "x.json"
    cache.escribir_json(ruta, {"v": 1})

    class Roto:
        def __str__(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cache.escribir_json(ruta, {"v": Roto()})
    assert cache.leer_json(ruta) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_leer_json_tolera_ausente_y_corrupto(tmp_path):
    assert cache.leer_json(tmp_path / "no.json", "d") == "d"
    (tmp_path / "mal.json").write_text('{"truncado": ', encoding="utf-8")
    assert cache.leer_json(tmp_path / "mal.json", {}) == {}


def test_modo_del_archivo(tmp_path):
    ruta = tmp_path / "s"
    cache.escribir_atomico(ruta, b"x", modo=0o600)
    assert oct(os.stat(ruta).st_mode & 0o777) == "0o600"


# ── spool ─────────────────────────────────────────────────────────────────────

def test_spool_drena_en_orden_y_se_vacia(tmp_path):
    s = Spool(tmp_path)
    for i in range(3):
        s.agregar("ejecucion", {"i": i})
    vistos = []
    assert s.drenar("ejecucion", lambda r: vistos.append(r["i"]) or True) == (3, 0)
    assert vistos == [0, 1, 2] and s.pendientes("ejecucion") == []
    assert not (tmp_path / "ejecucion.jsonl").exists()


def test_spool_se_detiene_en_el_primer_fallo_y_conserva_el_resto(tmp_path):
    s = Spool(tmp_path)
    for i in range(4):
        s.agregar("q", {"i": i})
    assert s.drenar("q", lambda r: r["i"] < 2) == (2, 2)
    assert [r["i"] for r in s.pendientes("q")] == [2, 3]
    assert s.drenar("q", lambda r: True) == (2, 0)


def test_spool_excepcion_del_envio_no_pierde_nada(tmp_path):
    s = Spool(tmp_path)
    s.agregar("q", {"i": 0})

    def cae(_):
        raise ConnectionError("ADB caída")

    assert s.drenar("q", cae) == (0, 1)
    assert s.pendientes("q") == [{"i": 0}]


def test_spool_omite_lineas_danadas(tmp_path):
    s = Spool(tmp_path)
    s.agregar("q", {"i": 0})
    with open(tmp_path / "q.jsonl", "a", encoding="utf-8") as f:
        f.write("{truncado\n")
    s.agregar("q", {"i": 1})
    assert [r["i"] for r in s.pendientes("q")] == [0, 1]


def test_spool_rechaza_nombres_con_ruta(tmp_path):
    with pytest.raises(ValueError):
        Spool(tmp_path).agregar("../x", {})


# ── cifrado ───────────────────────────────────────────────────────────────────

def test_cifrado_roundtrip_bytes():
    claro = bytes(range(256)) * 40  # >7 KB con todos los valores de byte
    assert cifrado.descifrar("clave", cifrado.cifrar("clave", claro, "comprasal_sv"), "comprasal_sv") == claro


def test_cifrado_no_es_determinista_ni_deja_ver_el_claro():
    a, b = cifrado.cifrar("k", b"cookie=secreta"), cifrado.cifrar("k", b"cookie=secreta")
    assert a != b and "secreta" not in a


def test_cifrado_rechaza_clave_contexto_y_alteracion():
    g = cifrado.cifrar("k1", b"hola", "p1")
    assert cifrado.descifrar("k2", g, "p1") is None
    assert cifrado.descifrar("k1", g, "p2") is None
    assert cifrado.descifrar("k1", g[:-4] + "AAAA", "p1") is None
    assert cifrado.descifrar("k1", "no es base64 !!", "p1") is None
    assert cifrado.descifrar("k1", "", "p1") is None


def test_cifrado_exige_clave():
    with pytest.raises(ValueError):
        cifrado.cifrar("", b"x")


def test_cifrado_dominio_distinto_al_de_mfa():
    from src.identidad import mfa
    g = mfa.cifrar("k", "ABCDEF")
    assert cifrado.descifrar("k", g) is None
