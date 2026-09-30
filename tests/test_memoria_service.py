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
def cfg_v2_enabled(tmp_path):
    """Config con memoria Y el espejo del esquema v2 activados (§3.3 Fase 2)."""
    return types.SimpleNamespace(
        enable_memoria=True,
        enable_esquema_v2=True,
        data_dir=str(tmp_path),
        precedentes_max=3,
    )


class _ConexionFalsa:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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


# ── Historial de corridas / circuit breaker real ─────────────────────────────

def test_registrar_ejecucion_desactivado_no_hace_nada(cfg_memoria_disabled):
    from datetime import datetime
    # No debe lanzar; con Track B apagado es no-op puro.
    service.registrar_ejecucion(cfg_memoria_disabled, datetime.now(), success=True)


def test_registrar_ejecucion_llama_al_repo_con_los_datos(cfg_memoria_enabled, monkeypatch):
    from datetime import datetime

    class _ConexionFalsa:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    llamadas = []
    monkeypatch.setattr(
        "src.memoria.repo.insertar_ejecucion",
        lambda conn, ts, success, **kw: llamadas.append((ts, success, kw)),
    )

    ts = datetime(2026, 9, 27, 7, 0, 0)
    service.registrar_ejecucion(
        cfg_memoria_enabled, ts, success=True, procesos_revisados=10, clientes_procesados=1,
    )

    assert len(llamadas) == 1
    ts_pasado, success, kw = llamadas[0]
    assert ts_pasado == ts
    assert success is True
    assert kw["procesos_revisados"] == 10
    assert kw["version"] == getattr(cfg_memoria_enabled, "app_version", "")


def test_registrar_ejecucion_adb_caida_no_lanza(cfg_memoria_enabled, monkeypatch):
    from datetime import datetime

    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))
    service.registrar_ejecucion(cfg_memoria_enabled, datetime.now(), success=False, error="x")


def test_fallos_consecutivos_desactivado_da_cero(cfg_memoria_disabled):
    assert service.fallos_consecutivos(cfg_memoria_disabled) == 0


def test_fallos_consecutivos_delega_al_repo(cfg_memoria_enabled, monkeypatch):
    class _ConexionFalsa:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.contar_fallos_consecutivos", lambda conn: 2)

    assert service.fallos_consecutivos(cfg_memoria_enabled) == 2


def test_fallos_consecutivos_adb_caida_da_cero_no_lanza(cfg_memoria_enabled, monkeypatch):
    """Degradación: con la ADB caída, el circuit breaker sigue funcionando
    igual que hoy (contador en memoria de main.py) — nunca peor."""
    def mock_connection(cfg):
        raise RuntimeError("ADB caída")

    monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=mock_connection))
    assert service.fallos_consecutivos(cfg_memoria_enabled) == 0


# ── Espejo del esquema v2 (plan del portal §3.3 Fase 2) ─────────────────────
#
# `cfg_memoria_enabled` no trae `enable_esquema_v2` (getattr por defecto en
# False): las funciones v2 no deben ni siquiera intentarse ahí. Solo
# `cfg_v2_enabled` los activa.

def test_persistir_memoria_v2_apagado_no_llama_a_las_funciones_v2(
    cfg_memoria_enabled, monkeypatch, proceso, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_db", MagicMock())
    llamado_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", llamado_v2)
    monkeypatch.setattr("src.memoria.repo.save_analisis_v2", MagicMock())

    service.persistir_memoria(cfg_memoria_enabled, proceso, resultado, "", "cli")

    llamado_v2.assert_not_called()


def test_persistir_memoria_v2_activado_llama_upsert_y_analisis_v2(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_db", MagicMock())
    upsert_v2 = MagicMock()
    analisis_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert_v2)
    monkeypatch.setattr("src.memoria.repo.save_analisis_v2", analisis_v2)

    service.persistir_memoria(cfg_v2_enabled, proceso, resultado, "", "cli")

    upsert_v2.assert_called_once()
    args, kwargs = upsert_v2.call_args.args, upsert_v2.call_args.kwargs
    assert args[1] is proceso
    assert args[2] == "cli"
    assert kwargs["estado"] == resultado.estado

    analisis_v2.assert_called_once()
    args = analisis_v2.call_args.args
    assert args[1] is resultado
    assert args[2] == "cli"


def test_persistir_memoria_v2_fallando_no_afecta_la_escritura_v1(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    """Un fallo del espejo v2 (p. ej. bug de SQL, FK inexistente) nunca debe
    impedir ni revertir la escritura del esquema actual — es exactamente lo
    que motiva envolver la llamada v2 en su PROPIO try/except."""
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    upsert_v1 = MagicMock()
    analisis_v1 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", upsert_v1)
    monkeypatch.setattr("src.memoria.repo.save_analisis_db", analisis_v1)
    monkeypatch.setattr(
        "src.memoria.repo.upsert_proceso_v2",
        MagicMock(side_effect=RuntimeError("ORA-02291: fila padre inexistente")),
    )
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso", lambda *a, **k: 0)

    # No debe lanzar.
    service.persistir_memoria(cfg_v2_enabled, proceso, resultado, "", "cli")

    upsert_v1.assert_called_once()
    analisis_v1.assert_called_once()


def test_persistir_memoria_v2_activado_con_texto_llama_vectorizar_v2(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_db", MagicMock())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_v2", MagicMock())
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso", lambda *a, **k: 3)
    vectorizar_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso_v2", vectorizar_v2)

    n = service.persistir_memoria(cfg_v2_enabled, proceso, resultado, "texto del pliego", "cli")

    assert n == 3, "el conteo devuelto sigue siendo el de v1, no el del espejo v2"
    vectorizar_v2.assert_called_once()
    args = vectorizar_v2.call_args.args
    assert args[1] == proceso.numero_referencia
    assert args[2] == "texto del pliego"


def test_persistir_memoria_v2_sin_texto_no_llama_a_ningun_vectorizador(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_db", MagicMock())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_v2", MagicMock())
    vectorizar_v1 = MagicMock()
    vectorizar_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso", vectorizar_v1)
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso_v2", vectorizar_v2)

    service.persistir_memoria(cfg_v2_enabled, proceso, resultado, "", "cli")

    vectorizar_v1.assert_not_called()
    vectorizar_v2.assert_not_called()


def test_persistir_memoria_v2_vectorizar_fallando_no_afecta_el_conteo_v1(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    """El espejo v2 de la vectorización falla (p. ej. FK a `proceso`
    inexistente); el conteo de chunks que devuelve la función sigue siendo
    el de v1, sin propagar la excepción."""
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_db", MagicMock())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", MagicMock())
    monkeypatch.setattr("src.memoria.repo.save_analisis_v2", MagicMock())
    monkeypatch.setattr("src.memoria.vectorizer.vectorizar_proceso", lambda *a, **k: 5)
    monkeypatch.setattr(
        "src.memoria.vectorizer.vectorizar_proceso_v2",
        MagicMock(side_effect=RuntimeError("ORA-02291: fila padre inexistente")),
    )

    n = service.persistir_memoria(cfg_v2_enabled, proceso, resultado, "texto del pliego", "cli")

    assert n == 5


def test_guardar_estado_v2_activado_llama_set_estado_v2_con_cliente_id(
    cfg_v2_enabled, monkeypatch, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.set_estado", MagicMock())
    set_estado_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.set_estado_v2", set_estado_v2)

    service.guardar_estado(cfg_v2_enabled, "cli", resultado)

    set_estado_v2.assert_called_once()
    args = set_estado_v2.call_args.args
    assert args[1] == resultado.proceso_uuid
    assert args[2] == "cli"
    assert args[3] == resultado.estado


def test_guardar_estado_v2_fallando_no_impide_el_dual_write_v1(
    cfg_v2_enabled, monkeypatch, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    set_estado_v1 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.set_estado", set_estado_v1)
    monkeypatch.setattr(
        "src.memoria.repo.set_estado_v2",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    # No debe lanzar; el JSONL (dual-write existente) tampoco se ve afectado.
    service.guardar_estado(cfg_v2_enabled, "cli", resultado)

    set_estado_v1.assert_called_once()
    jsonl_path = os.path.join(cfg_v2_enabled.data_dir, "history", "cli.analisis.jsonl")
    assert os.path.exists(jsonl_path)


def test_registrar_proceso_v2_activado_llama_upsert_proceso_v2(
    cfg_v2_enabled, monkeypatch, proceso
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    upsert_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert_v2)

    service.registrar_proceso(cfg_v2_enabled, proceso, "cli", "DESCARTADO_REGEX")

    upsert_v2.assert_called_once()
    args, kwargs = upsert_v2.call_args.args, upsert_v2.call_args.kwargs
    assert args[1] is proceso
    assert args[2] == "cli"
    assert kwargs["estado"] == "DESCARTADO_REGEX"


def test_registrar_proceso_v2_apagado_no_llama_upsert_proceso_v2(
    cfg_memoria_enabled, monkeypatch, proceso
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    monkeypatch.setattr("src.memoria.repo.upsert_proceso", MagicMock())
    upsert_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.upsert_proceso_v2", upsert_v2)

    service.registrar_proceso(cfg_memoria_enabled, proceso, "cli", "DESCARTADO_REGEX")

    upsert_v2.assert_not_called()


# ── Fase "switch" del esquema v2 (plan del portal §3.7 punto 4) ─────────────
#
# A diferencia de la sección de arriba (dual-write: v2 nunca revierte v1),
# acá la relación se invierte: con enable_esquema_v2 activo, v2 es la fuente
# de verdad y un fallo de v2 NO degrada a v1 — degradar a v1 reintroduciría
# la fuga entre clientes que esta fase cierra. Ver el docstring de
# `service.estado_de`.

def test_estado_de_v2_activado_lee_cliente_proceso_no_licitaciones_metadata(
    cfg_v2_enabled, monkeypatch, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    get_estado_v1 = MagicMock()
    get_estado_v2 = MagicMock(return_value="ANALIZADO")
    monkeypatch.setattr("src.memoria.repo.get_estado", get_estado_v1)
    monkeypatch.setattr("src.memoria.repo.get_estado_v2", get_estado_v2)

    estado = service.estado_de(cfg_v2_enabled, "cli", "REF-TEST-001")

    assert estado == "ANALIZADO"
    get_estado_v2.assert_called_once()
    args = get_estado_v2.call_args.args
    assert args[1] == "cli"
    assert args[2] == "REF-TEST-001"
    get_estado_v1.assert_not_called()


def test_estado_de_v2_error_no_cae_a_v1_cae_directo_a_jsonl(
    cfg_v2_enabled, monkeypatch, resultado
):
    """Un fallo de la lectura v2 salta directo al fallback de disponibilidad
    (JSONL) que ya existía — nunca intenta v1 en el camino."""
    history.save_analisis("cli", resultado, cfg_v2_enabled.data_dir)

    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    get_estado_v1 = MagicMock()
    monkeypatch.setattr("src.memoria.repo.get_estado", get_estado_v1)
    monkeypatch.setattr(
        "src.memoria.repo.get_estado_v2",
        MagicMock(side_effect=RuntimeError("ORA-00942: tabla o vista no existe")),
    )

    estado = service.estado_de(cfg_v2_enabled, "cli", "REF-TEST-001")

    assert estado == "ANALIZADO", "debe venir del JSONL, no de v1"
    get_estado_v1.assert_not_called()


def test_obtener_precedentes_v2_activado_llama_buscar_precedentes_v2(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    buscar_v1 = MagicMock()
    buscar_v2 = MagicMock(return_value=[])
    monkeypatch.setattr("src.memoria.rag.buscar_precedentes", buscar_v1)
    monkeypatch.setattr("src.memoria.rag.buscar_precedentes_v2", buscar_v2)

    service.obtener_precedentes(cfg_v2_enabled, proceso, resultado, "cli")

    buscar_v2.assert_called_once()
    kwargs = buscar_v2.call_args.kwargs
    assert kwargs["cliente_id"] == "cli"
    assert kwargs["excluir_proceso_id"] == proceso.numero_referencia
    buscar_v1.assert_not_called()


def test_obtener_precedentes_v2_error_no_cae_a_v1_devuelve_vacio(
    cfg_v2_enabled, monkeypatch, proceso, resultado
):
    """Un fallo de la búsqueda v2 (ADB viva, query v2 rota) NO debe caer a
    v1 — eso reintroduciría la fuga que esta fase cierra. El resultado
    degrada a [] como cualquier otro fallo de Track B."""
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    buscar_v1 = MagicMock()
    monkeypatch.setattr("src.memoria.rag.buscar_precedentes", buscar_v1)
    monkeypatch.setattr(
        "src.memoria.rag.buscar_precedentes_v2",
        MagicMock(side_effect=RuntimeError("ORA-00942: tabla o vista no existe")),
    )

    precedentes = service.obtener_precedentes(cfg_v2_enabled, proceso, resultado, "cli")

    assert precedentes == []
    buscar_v1.assert_not_called()


def test_obtener_precedentes_v2_apagado_sigue_llamando_v1(
    cfg_memoria_enabled, monkeypatch, proceso, resultado
):
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
    buscar_v1 = MagicMock(return_value=[])
    buscar_v2 = MagicMock()
    monkeypatch.setattr("src.memoria.rag.buscar_precedentes", buscar_v1)
    monkeypatch.setattr("src.memoria.rag.buscar_precedentes_v2", buscar_v2)

    service.obtener_precedentes(cfg_memoria_enabled, proceso, resultado, "cli")

    buscar_v1.assert_called_once()
    buscar_v2.assert_not_called()
