"""Tests para src.memoria.service — contrato de aislamiento (regla de oro).

Valida que:
  1. Con enable_memoria=False → todo es no-op (Track A intacto)
  2. Con ADB caída → se degrada elegantemente, nunca propaga la excepción

Esto asegura que un fallo de Track B NUNCA rompe Track A en producción.
"""
import json
import os
import types
from unittest.mock import MagicMock

import pytest

from src import history
from src.memoria import service
from src.scraper.models import MatrizRiesgos, Proceso, ResultadoAnalisis, RiesgoItem


@pytest.fixture
def cfg_memoria_disabled(tmp_path):
    """Config con memoria desactivada (Track A puro)."""
    return types.SimpleNamespace(
        enable_memoria=False,
        data_dir=str(tmp_path),
        precedentes_max=3,
    )


@pytest.fixture
def cfg_memoria_enabled(tmp_path):
    """Config con memoria activada (Track A + Track B)."""
    return types.SimpleNamespace(
        enable_memoria=True,
        data_dir=str(tmp_path),
        precedentes_max=3,
    )


@pytest.fixture
def proceso():
    """Proceso de prueba."""
    from datetime import datetime
    return Proceso(
        institucion="Ministerio Test",
        descripcion="Prueba de adquisición de servidores",
        fecha_publicacion=datetime.now(),
        url_directa="https://comprasal/pac/1",
        numero_referencia="REF-TEST-001",
    )


@pytest.fixture
def resultado():
    """Resultado de análisis de prueba."""
    return ResultadoAnalisis(
        proceso_uuid="REF-TEST-001",
        estado="ANALIZADO",
        formato="NATIVO",
        matriz=MatrizRiesgos(
            items=[RiesgoItem("financiero", "multa", "alta", "5.1")],
            resumen="Riesgos identificados"
        ),
    )


# ── Desactivación de memoria (enable_memoria=False) ──────────────────────────

def test_ping_desactivado_retorna_false(cfg_memoria_disabled):
    """Con enable_memoria=False, ping(cfg) debe retornar False."""
    result = service.ping(cfg_memoria_disabled)
    assert result is False


def test_cerrar_desactivado_noop(cfg_memoria_disabled):
    """Con enable_memoria=False, cerrar(cfg) debe ser no-op."""
    # No debe lanzar
    service.cerrar(cfg_memoria_disabled)


def test_persistir_memoria_desactivado_retorna_cero(cfg_memoria_disabled, proceso, resultado):
    """Con enable_memoria=False, persistir_memoria deve retornar 0."""
    result = service.persistir_memoria(
        cfg_memoria_disabled, proceso, resultado, "texto completo", "cli"
    )
    assert result == 0


def test_obtener_precedentes_desactivado_retorna_vacio(cfg_memoria_disabled, proceso, resultado):
    """Con enable_memoria=False, obtener_precedentes debe retornar []."""
    result = service.obtener_precedentes(cfg_memoria_disabled, proceso, resultado, "cli")
    assert result == []
    assert isinstance(result, list)


def test_registrar_proceso_desactivado_no_lanza(cfg_memoria_disabled, proceso):
    """Con enable_memoria=False, registrar_proceso es no-op y no lanza."""
    service.registrar_proceso(cfg_memoria_disabled, proceso, "cli", "DESCARTADO_REGEX")


def test_obtener_tendencias_desactivado_retorna_vacio(cfg_memoria_disabled):
    """Con enable_memoria=False, obtener_tendencias debe retornar []."""
    result = service.obtener_tendencias(cfg_memoria_disabled)
    assert result == []
    assert isinstance(result, list)


# ── Degradación elegante (ADB caída) ─────────────────────────────────────────

def test_ping_adb_caida_retorna_false(cfg_memoria_enabled, monkeypatch):
    """Si ADB falla, ping retorna False sin propagar excepción."""
    def mock_db_keepalive(cfg):
        raise RuntimeError("ADB caída: wallet ausente")

    monkeypatch.setattr("src.memoria.db.keepalive", mock_db_keepalive)

    # No debe lanzar, debe retornar False
    result = service.ping(cfg_memoria_enabled)
    assert result is False


def test_persistir_memoria_adb_caida_retorna_cero(cfg_memoria_enabled, monkeypatch, proceso, resultado):
    """Si ADB falla, persistir_memoria retorna 0 sin propagar excepción."""
    def mock_connection(cfg):
        raise RuntimeError("ADB caída: no puedo conectar")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # No debe lanzar, debe retornar 0
    result = service.persistir_memoria(
        cfg_memoria_enabled, proceso, resultado, "texto", "cli"
    )
    assert result == 0


def test_obtener_precedentes_adb_caida_retorna_vacio(cfg_memoria_enabled, monkeypatch, proceso, resultado):
    """Si ADB falla, obtener_precedentes retorna [] sin propagar excepción."""
    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # No debe lanzar, debe retornar []
    result = service.obtener_precedentes(cfg_memoria_enabled, proceso, resultado, "cli")
    assert result == []


def test_registrar_proceso_adb_caida_no_lanza(cfg_memoria_enabled, monkeypatch, proceso):
    """Si ADB falla, registrar_proceso no propaga excepción."""
    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # No debe lanzar
    service.registrar_proceso(cfg_memoria_enabled, proceso, "cli", "DESCARTADO_REGEX")


def test_registrar_proceso_llama_upsert_con_el_estado_indicado(
    cfg_memoria_enabled, monkeypatch, proceso
):
    """Con ADB activa, registrar_proceso delega en repo.upsert_proceso."""
    llamadas = []

    class _ConexionFalsa:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr(
        "src.memoria.repo.upsert_proceso",
        lambda conn, p, client_id, estado="DESCUBIERTO":
            llamadas.append((p.numero_referencia, client_id, estado)),
    )

    service.registrar_proceso(cfg_memoria_enabled, proceso, "cli", "REQUIERE_REVISION_MANUAL")

    assert llamadas == [(proceso.numero_referencia, "cli", "REQUIERE_REVISION_MANUAL")]


def test_obtener_tendencias_adb_caida_retorna_vacio(cfg_memoria_enabled, monkeypatch):
    """Si ADB falla, obtener_tendencias retorna [] sin propagar excepción."""
    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # No debe lanzar, debe retornar []
    result = service.obtener_tendencias(cfg_memoria_enabled)
    assert result == []


# ── Dual-write (JSONL + ADB) con ADB caída ──────────────────────────────────

def test_guardar_estado_fallback_jsonl_con_adb_caida(cfg_memoria_enabled, monkeypatch, resultado):
    """guardar_estado debe escribir JSONL aunque ADB falle."""
    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # No debe lanzar
    service.guardar_estado(cfg_memoria_enabled, "cli", resultado)

    # Verificar que se escribió en JSONL
    jsonl_path = os.path.join(cfg_memoria_enabled.data_dir, "history", "cli.analisis.jsonl")
    assert os.path.exists(jsonl_path), "JSONL debe existir tras fallo de ADB"

    # Leer y verificar contenido
    with open(jsonl_path, "r") as f:
        lines = f.readlines()
    assert len(lines) > 0, "JSONL debe contener al menos una línea"

    # Parsear la línea y verificar que es el resultado guardado
    data = json.loads(lines[-1])
    assert data["proceso_uuid"] == "REF-TEST-001"
    assert data["estado"] == "ANALIZADO"


def test_estado_de_fallback_jsonl_con_adb_caida(cfg_memoria_enabled, monkeypatch, resultado):
    """estado_de debe caer a JSONL cuando ADB falla."""
    # Primero, guardar el estado en JSONL
    history.save_analisis("cli", resultado, cfg_memoria_enabled.data_dir)

    # Luego, simular que ADB falla
    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # estado_de debe recuperar del JSONL
    estado = service.estado_de(cfg_memoria_enabled, "cli", "REF-TEST-001")
    assert estado == "ANALIZADO", "Debe leer del JSONL cuando ADB falla"


def test_estado_de_con_memoria_desactivada_siempre_usa_jsonl(cfg_memoria_disabled, resultado):
    """Con enable_memoria=False, estado_de siempre usa JSONL."""
    # Guardar estado
    history.save_analisis("cli", resultado, cfg_memoria_disabled.data_dir)

    # Consultar sin memoria
    estado = service.estado_de(cfg_memoria_disabled, "cli", "REF-TEST-001")
    assert estado == "ANALIZADO"


def test_guardar_estado_desactivado_solo_jsonl(cfg_memoria_disabled, resultado):
    """Con enable_memoria=False, guardar_estado solo escribe JSONL."""
    service.guardar_estado(cfg_memoria_disabled, "cli", resultado)

    # Verificar que se escribió en JSONL
    jsonl_path = os.path.join(cfg_memoria_disabled.data_dir, "history", "cli.analisis.jsonl")
    assert os.path.exists(jsonl_path)

    with open(jsonl_path, "r") as f:
        data = json.loads(f.readline())
    assert data["proceso_uuid"] == "REF-TEST-001"


# ── Continuidad de Track A (regla de oro) ──────────────────────────────────

def test_ruta_critica_track_a_no_bloqueada_por_track_b_caida(cfg_memoria_enabled, monkeypatch, proceso, resultado):
    """Un fallo catastrófico de Track B NO debe afectar Track A.

    Simula una secuencia típica:
      1. ADB cae completamente
      2. Pero Track A (scraper → drilldown → notifier) sigue adelante
      3. Solo Track B (memoria + RAG) degrada silenciosamente
    """
    # Simular que ADB está completamente fuera
    def mock_connection(cfg):
        raise RuntimeError("ADB: wallet no encontrado, credenciales inválidas")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # Track A sigue:
    # 1. Persistir memoria (Track B) falla silenciosamente
    chunks = service.persistir_memoria(cfg_memoria_enabled, proceso, resultado, "texto", "cli")
    assert chunks == 0  # No lanza, retorna 0

    # 2. Obtener precedentes (Track B) falla silenciosamente
    precedentes = service.obtener_precedentes(cfg_memoria_enabled, proceso, resultado, "cli")
    assert precedentes == []  # No lanza, retorna []

    # 3. Guardar estado (dual-write) escribe al menos en JSONL
    service.guardar_estado(cfg_memoria_enabled, "cli", resultado)
    jsonl_path = os.path.join(cfg_memoria_enabled.data_dir, "history", "cli.analisis.jsonl")
    assert os.path.exists(jsonl_path)  # JSONL existe

    # 4. Leer estado cae a JSONL correctamente
    estado = service.estado_de(cfg_memoria_enabled, "cli", "REF-TEST-001")
    assert estado == "ANALIZADO"


def test_contexto_falla_de_red_adb_no_afecta_analisis(cfg_memoria_enabled, monkeypatch, proceso, resultado):
    """Simula contexto real: ADB con falta de conectividad de red."""
    from socket import error as SocketError

    def mock_connection(cfg):
        raise SocketError("Network unreachable: 193.122.188.14:1522")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))

    # El pipeline debe continuar
    service.persistir_memoria(cfg_memoria_enabled, proceso, resultado, "texto", "cli")
    precedentes = service.obtener_precedentes(cfg_memoria_enabled, proceso, resultado, "cli")

    assert precedentes == []
