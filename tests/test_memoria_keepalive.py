"""Tests del umbral de fallos del keepalive de la ADB.

Lo que se protege aquí es una decisión de producto, no una implementación: un
parpadeo de un día NO debe alertar (la ADB Always Free se reinicia sola para
parchear) y dos días seguidos SÍ, porque a partir de ahí la cuenta regresiva
hacia el auto-stop de 7 días es real. Si alguien afloja el umbral, estos tests
lo dicen.
"""
import json
import types

import pytest

from src.memoria import keepalive, service


@pytest.fixture
def cfg_adb(tmp_path):
    """Config con credenciales de ADB: es lo que habilita el keepalive.

    Deliberadamente con enable_memoria=False — el keepalive tiene que disparar
    aunque el feature flag esté apagado, que es el estado real de la validación.
    """
    return types.SimpleNamespace(
        enable_memoria=False,
        data_dir=str(tmp_path),
        adb_dsn="tcps://adb.example/servicio",
        adb_password="secreto",
        adb_user="AMALIA",
    )


# ── El umbral ────────────────────────────────────────────────────────────────

def test_primer_fallo_no_alerta(tmp_path):
    estado = keepalive.registrar(str(tmp_path), ok=False, detalle="timeout")
    assert estado.ok is False
    assert estado.fallos_consecutivos == 1
    assert estado.debe_alertar is False


def test_segundo_fallo_consecutivo_alerta(tmp_path):
    keepalive.registrar(str(tmp_path), ok=False)
    estado = keepalive.registrar(str(tmp_path), ok=False)
    assert estado.fallos_consecutivos == 2
    assert estado.debe_alertar is True


def test_exito_resetea_el_contador(tmp_path):
    keepalive.registrar(str(tmp_path), ok=False)
    ok = keepalive.registrar(str(tmp_path), ok=True)
    assert ok.fallos_consecutivos == 0
    assert ok.debe_alertar is False

    # Y tras el reset, hace falta otra racha completa para volver a alertar.
    assert keepalive.registrar(str(tmp_path), ok=False).debe_alertar is False


def test_fallos_alternados_nunca_alertan(tmp_path):
    """Fallar un día de cada dos no pone la ADB en riesgo: no debe alertar."""
    for _ in range(5):
        assert keepalive.registrar(str(tmp_path), ok=False).debe_alertar is False
        assert keepalive.registrar(str(tmp_path), ok=True).debe_alertar is False


def test_racha_larga_sigue_alertando(tmp_path):
    """Mientras la ADB siga caída, la alerta se repite: es una cuenta regresiva."""
    keepalive.registrar(str(tmp_path), ok=False)
    for esperado in (2, 3, 4):
        estado = keepalive.registrar(str(tmp_path), ok=False)
        assert estado.fallos_consecutivos == esperado
        assert estado.debe_alertar is True


# ── El estado en disco (es lo que da memoria entre corridas) ─────────────────

def test_el_estado_sobrevive_al_proceso(tmp_path):
    """El conteo vive en data/, el único directorio montado como volumen."""
    keepalive.registrar(str(tmp_path), ok=False, detalle="ORA-12170")
    ruta = keepalive.ruta_estado(str(tmp_path))
    datos = json.loads(open(ruta, encoding="utf-8").read())
    assert datos["fallos_consecutivos"] == 1
    assert datos["ultimo_error"] == "ORA-12170"

    # Leer sin registrar no altera el conteo.
    assert keepalive.leer_estado(str(tmp_path)).fallos_consecutivos == 1


def test_ultimo_exito_se_conserva_entre_fallos(tmp_path):
    """Sin esto la alerta no puede decir desde cuándo la ADB está muda."""
    keepalive.registrar(str(tmp_path), ok=True)
    marca = keepalive.leer_estado(str(tmp_path)).ultimo_exito
    assert marca

    estado = keepalive.registrar(str(tmp_path), ok=False)
    assert estado.ultimo_exito == marca


def test_estado_corrupto_no_rompe_ni_alerta_de_golpe(tmp_path):
    """Un JSON truncado reinicia el conteo; nunca hace fallar la corrida."""
    with open(keepalive.ruta_estado(str(tmp_path)), "w", encoding="utf-8") as f:
        f.write("{esto no es json")

    estado = keepalive.registrar(str(tmp_path), ok=False)
    assert estado.fallos_consecutivos == 1
    assert estado.debe_alertar is False


def test_directorio_inexistente_se_crea(tmp_path):
    destino = tmp_path / "aun" / "no" / "existe"
    estado = keepalive.registrar(str(destino), ok=False)
    assert estado.fallos_consecutivos == 1
    assert (destino / keepalive.ARCHIVO_ESTADO).exists()


# ── Integración con la fachada ───────────────────────────────────────────────

def test_ping_sin_adb_configurada_no_escribe_estado(tmp_path):
    """Track A puro (sin ADB) no puede acumular fallos ni alertar por nada."""
    cfg = types.SimpleNamespace(enable_memoria=False, data_dir=str(tmp_path),
                                adb_dsn="", adb_password="")
    estado = service.ping_con_estado(cfg)
    assert estado.debe_alertar is False
    assert estado.fallos_consecutivos == 0
    assert service.ping(cfg) is False
    import os
    assert not os.path.exists(keepalive.ruta_estado(str(tmp_path)))


def test_ping_con_adb_caida_acumula_hasta_el_umbral(cfg_adb, monkeypatch):
    monkeypatch.setattr("src.memoria.db.keepalive",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("ADB caída")))

    assert service.ping_con_estado(cfg_adb).debe_alertar is False
    segundo = service.ping_con_estado(cfg_adb)
    assert segundo.debe_alertar is True
    assert "ADB caída" in segundo.ultimo_error


def test_ping_exitoso_devuelve_true_y_no_alerta(cfg_adb, monkeypatch):
    monkeypatch.setattr("src.memoria.db.keepalive", lambda cfg: True)
    estado = service.ping_con_estado(cfg_adb)
    assert estado.ok is True
    assert estado.debe_alertar is False
    assert service.ping(cfg_adb) is True


def test_resumen_es_legible_para_una_alerta(tmp_path):
    """El texto va a un asunto de correo: tiene que decir qué pasó sin contexto."""
    keepalive.registrar(str(tmp_path), ok=True)
    keepalive.registrar(str(tmp_path), ok=False)
    estado = keepalive.registrar(str(tmp_path), ok=False)
    assert "2 fallo(s) consecutivo(s)" in estado.resumen
    assert "Última conexión exitosa" in estado.resumen
