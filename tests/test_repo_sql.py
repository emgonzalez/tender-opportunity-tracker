"""Tests para src.memoria.repo — verificación de SQL (sin usar Oracle real).

Valida que las funciones emiten SQL seguro (sin inyección) y con palabras clave
esperadas (MERGE, UPDATE, SELECT, etc.).
"""
from unittest.mock import MagicMock, ANY

import pytest


@pytest.fixture
def mock_cursor():
    """Cursor mock que captura execute/executemany calls."""
    cursor = MagicMock()
    cursor.fetchone = MagicMock(return_value=None)
    cursor.fetchall = MagicMock(return_value=[])
    cursor.rowcount = 1
    return cursor


@pytest.fixture
def mock_conn(mock_cursor):
    """Conexión mock que devuelve el cursor mock."""
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=mock_cursor)
    return conn


def test_upsert_proceso_emite_merge(mock_conn, mock_cursor):
    """upsert_proceso debe emitir un MERGE (o INSERT/UPDATE) hacia la tabla de procesos."""
    from src.memoria import repo
    from src.scraper.models import Proceso
    from datetime import datetime

    proceso = Proceso(
        institucion="Ministerio X",
        descripcion="Test proceso",
        fecha_publicacion=datetime.now(),
        url_directa="https://comprasal/pac/1",
        numero_referencia="REF-001",
    )

    repo.upsert_proceso(mock_conn, proceso, "client_1", estado="DESCUBIERTO")

    # Verificar que se llamó a execute
    assert mock_cursor.execute.called or mock_cursor.executemany.called

    # Obtener la llamada SQL
    if mock_cursor.execute.called:
        sql_call = mock_cursor.execute.call_args[0][0]
    else:
        sql_call = mock_cursor.executemany.call_args[0][0]

    # Debe contener palabra clave de operación (MERGE, INSERT, UPDATE)
    sql_upper = sql_call.upper()
    assert any(keyword in sql_upper for keyword in ["MERGE", "INSERT", "UPDATE"]), \
        f"SQL no contiene operación esperada: {sql_call}"


def test_set_estado_emite_update(mock_conn, mock_cursor):
    """set_estado debe emitir UPDATE hacia la tabla de procesos."""
    from src.memoria import repo

    repo.set_estado(mock_conn, "REF-001", "ANALIZADO", error=None)

    # Verificar que se llamó a execute
    assert mock_cursor.execute.called

    sql_call = mock_cursor.execute.call_args[0][0]
    sql_upper = sql_call.upper()

    assert "UPDATE" in sql_upper, \
        f"set_estado debe emitir UPDATE, pero emitió: {sql_call}"


def test_get_estado_emite_select(mock_conn, mock_cursor):
    """get_estado debe emitir SELECT y retornar resultado."""
    from src.memoria import repo

    # Mock para que fetchone devuelva un resultado
    mock_cursor.fetchone = MagicMock(return_value=("ANALIZADO",))

    result = repo.get_estado(mock_conn, "REF-001")

    # Verificar que se llamó a execute
    assert mock_cursor.execute.called

    sql_call = mock_cursor.execute.call_args[0][0]
    sql_upper = sql_call.upper()

    assert "SELECT" in sql_upper, \
        f"get_estado debe emitir SELECT, pero emitió: {sql_call}"

    # Verificar que fetchone se llamó
    assert mock_cursor.fetchone.called


def test_claim_siguiente_emite_skip_locked(mock_conn, mock_cursor):
    """claim_siguiente debe emitir SELECT con SKIP LOCKED para evitar deadlocks."""
    from src.memoria import repo
    import types

    cfg = types.SimpleNamespace(data_dir="/tmp")

    # Mock fetchone para devolver un proceso
    mock_cursor.fetchone = MagicMock(return_value=("REF-002",))

    result = repo.claim_siguiente(mock_conn, cfg)

    # Verificar que se llamó a execute
    assert mock_cursor.execute.called

    sql_call = mock_cursor.execute.call_args[0][0]
    sql_upper = sql_call.upper()

    assert "SKIP LOCKED" in sql_upper, \
        f"claim_siguiente debe usar SKIP LOCKED, pero emitió: {sql_call}"


def test_save_analisis_db_inserta_resultado(mock_conn, mock_cursor):
    """save_analisis_db debe insertar un registro de análisis."""
    from src.memoria import repo
    from src.scraper.models import ResultadoAnalisis

    resultado = ResultadoAnalisis(
        proceso_uuid="REF-001",
        estado="ANALIZADO",
        formato="NATIVO",
    )

    repo.save_analisis_db(mock_conn, resultado)

    # Verificar que se llamó a execute o executemany
    assert mock_cursor.execute.called or mock_cursor.executemany.called

    if mock_cursor.execute.called:
        sql_call = mock_cursor.execute.call_args[0][0]
    else:
        sql_call = mock_cursor.executemany.call_args[0][0]

    sql_upper = sql_call.upper()

    # Debe ser un INSERT (o similar)
    assert any(keyword in sql_upper for keyword in ["INSERT", "MERGE"]), \
        f"save_analisis_db debe insertar, pero emitió: {sql_call}"


def test_stats_por_institucion_emite_select(mock_conn, mock_cursor):
    """stats_por_institucion debe emitir SELECT aggregado."""
    from src.memoria import repo

    # Mock fetchall para devolver stats (institucion, categoria, conteo, pct_go)
    mock_cursor.fetchall = MagicMock(return_value=[
        ("Ministerio X", "infraestructura", 5, 80.0),
        ("Ministerio Y", "telecomunicaciones", 3, 66.67),
    ])

    result = repo.stats_por_institucion(mock_conn)

    # Verificar que se llamó a execute
    assert mock_cursor.execute.called

    sql_call = mock_cursor.execute.call_args[0][0]
    sql_upper = sql_call.upper()

    assert "SELECT" in sql_upper, \
        f"stats_por_institucion debe emitir SELECT, pero emitió: {sql_call}"

    # Verificar que fetchall se llamó
    assert mock_cursor.fetchall.called


def test_anti_injection_set_estado(mock_conn, mock_cursor):
    """set_estado con entrada maliciosa debe usar bindings, no concatenación."""
    from src.memoria import repo

    # Entrada malintencionada
    malicious_id = "REF-001' OR '1'='1"

    repo.set_estado(mock_conn, malicious_id, "ANALIZADO")

    # Verificar que se llamó a execute
    assert mock_cursor.execute.called

    sql_call = mock_cursor.execute.call_args[0][0]
    binds = mock_cursor.execute.call_args[0][1] if len(mock_cursor.execute.call_args[0]) > 1 else None

    # La cadena maliciosa NO debe aparecer dentro de la cadena SQL
    assert malicious_id not in sql_call, \
        f"SQL injection detectada: '{malicious_id}' aparece en el SQL: {sql_call}"

    # Pero SÍ debe aparecer en los bindings
    if binds:
        binds_str = str(binds)
        assert malicious_id in binds_str, \
            f"El parámetro malintencionado debe estar en los bindings: {binds}"


def test_anti_injection_upsert_proceso(mock_conn, mock_cursor):
    """upsert_proceso debe usar bindings para los parámetros."""
    from src.memoria import repo
    from src.scraper.models import Proceso
    from datetime import datetime

    # Entrada malintencionada
    malicious_ref = "REF-001'; DROP TABLE procesos; --"

    proceso = Proceso(
        institucion="Ministerio X",
        descripcion="Test",
        fecha_publicacion=datetime.now(),
        url_directa="https://comprasal/pac/1",
        numero_referencia=malicious_ref,
    )

    repo.upsert_proceso(mock_conn, proceso, "client_1", estado="DESCUBIERTO")

    # Obtener la llamada SQL
    if mock_cursor.execute.called:
        sql_call = mock_cursor.execute.call_args[0][0]
        binds = mock_cursor.execute.call_args[0][1] if len(mock_cursor.execute.call_args[0]) > 1 else None
    else:
        sql_call = mock_cursor.executemany.call_args[0][0]
        binds = mock_cursor.executemany.call_args[0][1] if len(mock_cursor.executemany.call_args[0]) > 1 else None

    # La cadena malintencionada NO debe aparecer en la cadena SQL
    assert malicious_ref not in sql_call, \
        f"SQL injection detectada: '{malicious_ref}' aparece en el SQL: {sql_call}"

    # Pero SÍ debe aparecer en los bindings
    if binds:
        binds_str = str(binds)
        assert malicious_ref in binds_str, \
            f"El parámetro malintencionado debe estar en los bindings: {binds}"
