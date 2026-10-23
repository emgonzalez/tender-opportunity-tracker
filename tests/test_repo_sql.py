"""Tests para src.memoria.repo — verificación de SQL (sin usar Oracle real).

Valida que las funciones emiten SQL seguro (sin inyección) y con palabras clave
esperadas (MERGE, UPDATE, SELECT, etc.).
"""
from unittest.mock import MagicMock

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
    from datetime import datetime

    from src.memoria import repo
    from src.scraper.models import Proceso

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

    # La otra mitad de lo que promete el docstring: además de emitir el SELECT,
    # debe devolver el valor de la columna `estado`, no la fila cruda.
    assert result == "ANALIZADO"

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

    # claim_siguiente NO usa fetchone: el proceso_id sale de una bind variable
    # OUT del bloque PL/SQL (`:pid := v_pid`), leída con var().getvalue(). Es lo
    # que hay que mockear — un mock de fetchone aquí no lo consulta nadie.
    mock_cursor.var.return_value.getvalue.return_value = "REF-002"

    result = repo.claim_siguiente(mock_conn, "worker-1")

    # Debe devolver el proceso_id reclamado, tomado de la bind variable.
    assert result == "REF-002"

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

    # Debe transformar cada fila en un dict con las cuatro claves del contrato,
    # no devolver las tuplas crudas de la BD.
    assert len(result) == 2
    assert result[0] == {
        "institucion": "Ministerio X",
        "categoria": "infraestructura",
        "conteo": 5,
        "pct_go": 80.0,
    }

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
    from datetime import datetime

    from src.memoria import repo
    from src.scraper.models import Proceso

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


class TestInsertarEjecucion:
    def test_emite_insert_con_los_campos_mapeados(self, mock_conn, mock_cursor):
        from datetime import datetime

        from src.memoria import repo

        ts = datetime(2026, 9, 27, 7, 0, 0)
        repo.insertar_ejecucion(
            mock_conn, ts, success=True,
            procesos_revisados=80, clientes_procesados=1, version="8fa7610",
        )

        assert mock_cursor.execute.called
        sql, binds = mock_cursor.execute.call_args[0]
        assert "INSERT INTO ejecucion" in sql
        assert binds["run_timestamp"] == ts
        assert binds["success"] == 1
        assert binds["procesos_revisados"] == 80
        assert binds["clientes_procesados"] == 1
        assert binds["version"] == "8fa7610"
        assert binds["error_msg"] is None

    def test_success_false_se_traduce_a_0(self, mock_conn, mock_cursor):
        from datetime import datetime

        from src.memoria import repo

        repo.insertar_ejecucion(mock_conn, datetime.now(), success=False, error="boom")
        _, binds = mock_cursor.execute.call_args[0]
        assert binds["success"] == 0
        assert binds["error_msg"] == "boom"

    def test_nunca_actualiza_una_fila_existente(self, mock_conn, mock_cursor):
        """Es historial append-only: cada corrida es un INSERT, nunca un MERGE/UPDATE."""
        from datetime import datetime

        from src.memoria import repo

        repo.insertar_ejecucion(mock_conn, datetime.now(), success=True)
        sql, _ = mock_cursor.execute.call_args[0]
        assert "UPDATE" not in sql.upper()
        assert "MERGE" not in sql.upper()


class TestContarFallosConsecutivos:
    def test_cuenta_hasta_el_primer_exito(self, mock_conn, mock_cursor):
        from src.memoria import repo

        # Más reciente primero (ORDER BY ... DESC): dos fallos y luego un éxito.
        mock_cursor.fetchall = MagicMock(return_value=[(0,), (0,), (1,)])
        assert repo.contar_fallos_consecutivos(mock_conn) == 2

    def test_sin_fallas_da_cero(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.fetchall = MagicMock(return_value=[(1,)])
        assert repo.contar_fallos_consecutivos(mock_conn) == 0

    def test_sin_historial_da_cero(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.fetchall = MagicMock(return_value=[])
        assert repo.contar_fallos_consecutivos(mock_conn) == 0

    def test_todo_fallas_cuenta_todo_lo_traido(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.fetchall = MagicMock(return_value=[(0,)] * 5)
        assert repo.contar_fallos_consecutivos(mock_conn) == 5


class TestEsquemaV2:
    """Espejo del esquema v2 (db/schema_v2.sql, plan del portal §3.3 Fase 2).

    Mismo nivel de verificación que el resto del archivo: SQL seguro (binds,
    no interpolación) y las palabras clave esperadas — no contra Oracle real
    (eso lo cubre ``tests/integration/test_oracle_local.py``).
    """

    def test_upsert_proceso_v2_emite_dos_merge(self, mock_conn, mock_cursor):
        """Dos escrituras separadas: el HECHO (`proceso`) y la vista del
        cliente (`cliente_proceso`) — la separación que cierra el bug de
        `licitaciones_metadata`."""
        from datetime import datetime

        from src.memoria import repo
        from src.scraper.models import Proceso

        proceso = Proceso(
            institucion="Ministerio X", descripcion="Test proceso",
            fecha_publicacion=datetime.now(), url_directa="https://comprasal/pac/1",
            numero_referencia="REF-001",
        )

        repo.upsert_proceso_v2(mock_conn, proceso, "client_1", estado="DESCUBIERTO")

        assert mock_cursor.execute.call_count == 2
        sql_proceso, binds_proceso = mock_cursor.execute.call_args_list[0][0]
        sql_cliente_proceso, binds_cp = mock_cursor.execute.call_args_list[1][0]

        assert "MERGE INTO proceso" in sql_proceso
        assert "MERGE INTO cliente_proceso" in sql_cliente_proceso
        assert binds_proceso["proceso_gid"] == "comprasal_sv:REF-001"
        assert binds_cp["cliente_id"] == "client_1"
        assert binds_cp["proceso_gid"] == "comprasal_sv:REF-001"
        assert binds_cp["estado"] == "DESCUBIERTO"

    def test_upsert_proceso_v2_no_pisa_cliente_id_ajeno(self, mock_conn, mock_cursor):
        """A diferencia de `upsert_proceso` (v1), `cliente_id` no viaja en la
        tabla `proceso` en absoluto: no hay columna que pisar."""
        from datetime import datetime

        from src.memoria import repo
        from src.scraper.models import Proceso

        proceso = Proceso(
            institucion="Ministerio X", descripcion="Test",
            fecha_publicacion=datetime.now(), url_directa="https://comprasal/pac/1",
            numero_referencia="REF-002",
        )
        repo.upsert_proceso_v2(mock_conn, proceso, "client_2")

        sql_proceso, binds_proceso = mock_cursor.execute.call_args_list[0][0]
        assert "cliente_id" not in binds_proceso
        assert "cliente_id" not in sql_proceso.lower()

    def test_upsert_proceso_v2_no_pisa_con_vacio(self, mock_conn, mock_cursor):
        """Sin clasificación (tags/marcas/categoría vacíos) el WHEN MATCHED
        conserva lo existente; con clasificación, escribe."""
        from datetime import datetime

        from src.memoria import repo
        from src.scraper.models import Proceso, ProcesoClasificado

        vacio = Proceso(
            institucion="M", descripcion="T", fecha_publicacion=datetime.now(),
            url_directa="https://u", numero_referencia="REF-003",
        )
        repo.upsert_proceso_v2(mock_conn, vacio, "client_1")
        sql, binds = mock_cursor.execute.call_args_list[1][0]
        assert "COALESCE(:categoria_detectada, tgt.categoria_detectada)" in sql
        assert "CASE WHEN :tags_vacio = 1" in sql
        assert "CASE WHEN :marcas_vacio = 1" in sql
        assert binds["tags_vacio"] == 1
        assert binds["marcas_vacio"] == 1
        assert binds["categoria_detectada"] is None

        mock_cursor.execute.reset_mock()
        clasificado = ProcesoClasificado(
            institucion="M", descripcion="T", fecha_publicacion=datetime.now(),
            url_directa="https://u", numero_referencia="REF-003",
            categoria_detectada="infraestructura", tags=["Servidores"],
        )
        repo.upsert_proceso_v2(mock_conn, clasificado, "client_1")
        _, binds = mock_cursor.execute.call_args_list[1][0]
        assert binds["tags_vacio"] == 0
        assert binds["tags"] == '["Servidores"]'
        assert binds["categoria_detectada"] == "infraestructura"

    def test_upsert_proceso_v2_anti_injection(self, mock_conn, mock_cursor):
        from datetime import datetime

        from src.memoria import repo
        from src.scraper.models import Proceso

        malicious_ref = "REF-001'; DROP TABLE proceso; --"
        proceso = Proceso(
            institucion="Ministerio X", descripcion="Test",
            fecha_publicacion=datetime.now(), url_directa="https://comprasal/pac/1",
            numero_referencia=malicious_ref,
        )
        repo.upsert_proceso_v2(mock_conn, proceso, "client_1")

        for call in mock_cursor.execute.call_args_list:
            sql, binds = call[0]
            assert malicious_ref not in sql, f"SQL injection detectada en: {sql}"
        assert malicious_ref in str(mock_cursor.execute.call_args_list[0][0][1])

    def test_get_estado_v2_emite_select_a_cliente_proceso(self, mock_conn, mock_cursor):
        """A diferencia de `get_estado` (v1), lee `cliente_proceso` con
        `cliente_id` en el WHERE — sin eso, dos clientes con distinto estado
        para el mismo proceso no podrían distinguirse."""
        from src.memoria import repo

        mock_cursor.fetchone = MagicMock(return_value=("ANALIZADO",))

        result = repo.get_estado_v2(mock_conn, "client_1", "REF-001")

        assert result == "ANALIZADO"
        sql, binds = mock_cursor.execute.call_args[0]
        assert "SELECT" in sql.upper()
        assert "cliente_proceso" in sql
        assert "licitaciones_metadata" not in sql
        assert binds["cliente_id"] == "client_1"
        assert binds["proceso_gid"] == "comprasal_sv:REF-001"

    def test_get_estado_v2_sin_fila_devuelve_none(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.fetchone = MagicMock(return_value=None)

        result = repo.get_estado_v2(mock_conn, "client_1", "REF-999")

        assert result is None

    def test_get_estado_v2_anti_injection(self, mock_conn, mock_cursor):
        from src.memoria import repo

        malicious_ref = "REF-001'; DROP TABLE cliente_proceso; --"
        repo.get_estado_v2(mock_conn, "client_1", malicious_ref)

        sql, binds = mock_cursor.execute.call_args[0]
        assert malicious_ref not in sql, f"SQL injection detectada en: {sql}"
        assert binds["proceso_gid"] == f"comprasal_sv:{malicious_ref}"

    def test_set_estado_v2_emite_update_a_cliente_proceso(self, mock_conn, mock_cursor):
        from src.memoria import repo

        repo.set_estado_v2(mock_conn, "REF-001", "client_1", "ANALIZADO", triaje="GO", triaje_conf=0.9)

        sql, binds = mock_cursor.execute.call_args[0]
        assert "UPDATE cliente_proceso" in sql
        assert "licitaciones_metadata" not in sql
        assert binds["cliente_id"] == "client_1"
        assert binds["proceso_gid"] == "comprasal_sv:REF-001"
        assert binds["triaje"] == "GO"
        assert binds["triaje_conf"] == 0.9

    def test_set_estado_v2_columnas_opcionales_ausentes_no_aparecen_en_el_set(self, mock_conn, mock_cursor):
        from src.memoria import repo

        repo.set_estado_v2(mock_conn, "REF-001", "client_1", "DESCUBIERTO")

        sql, binds = mock_cursor.execute.call_args[0]
        assert "triaje" not in binds
        assert "formato" not in binds

    def test_save_analisis_v2_emite_merge_con_clave_cliente_proceso(self, mock_conn, mock_cursor):
        from src.memoria import repo
        from src.scraper.models import MatrizRiesgos, ResultadoAnalisis, RiesgoItem

        resultado = ResultadoAnalisis(
            proceso_uuid="REF-001", estado="ANALIZADO", formato="NATIVO",
            matriz=MatrizRiesgos(
                items=[
                    RiesgoItem("financiero", "multa", "media", "5.1"),
                    RiesgoItem("legal", "penalidad", "alta", "6.2"),
                ],
                resumen="resumen de prueba",
            ),
            modelo_analisis="test-model", monto_estimado=45000.5,
        )

        repo.save_analisis_v2(mock_conn, resultado, "client_1")

        sql, binds = mock_cursor.execute.call_args[0]
        assert "MERGE INTO analisis" in sql
        assert binds["cliente_id"] == "client_1"
        assert binds["proceso_gid"] == "comprasal_sv:REF-001"
        assert binds["severidad_max"] == "alta", "debe tomar la severidad MÁS ALTA de la matriz"
        assert binds["resumen"] == "resumen de prueba"
        assert binds["monto_estimado"] == 45000.5

    def test_save_analisis_v2_sin_matriz_severidad_max_es_none(self, mock_conn, mock_cursor):
        from src.memoria import repo
        from src.scraper.models import ResultadoAnalisis

        resultado = ResultadoAnalisis(proceso_uuid="REF-001", estado="ANALIZADO")
        repo.save_analisis_v2(mock_conn, resultado, "client_1")

        _, binds = mock_cursor.execute.call_args[0]
        assert binds["severidad_max"] is None
        assert binds["matriz_riesgos"] is None

    def test_stats_por_institucion_v2_requiere_cliente_id_y_filtra(self, mock_conn, mock_cursor):
        """Mismo cambio de firma deliberado que rag.tendencias_v2 — ver su
        docstring para el razonamiento (triaje es una decisión por cliente,
        no un hecho global)."""
        from src.memoria import repo

        repo.stats_por_institucion_v2(mock_conn, "client_1", dias=30)

        sql, binds = mock_cursor.execute.call_args[0]
        assert "cliente_proceso" in sql
        assert "cp.cliente_id = :cliente_id" in sql
        assert "licitaciones_metadata" not in sql
        assert binds["cliente_id"] == "client_1"
        assert binds["dias"] == 30

    def test_stats_por_institucion_v2_mismas_claves_que_v1(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.fetchall = MagicMock(return_value=[("Ministerio X", "infra", 4, 50.0)])

        resultado = repo.stats_por_institucion_v2(mock_conn, "client_1")

        assert resultado == [{
            "institucion": "Ministerio X", "categoria": "infra", "conteo": 4, "pct_go": 50.0,
        }]

    def test_claim_siguiente_v2_emite_skip_locked_sobre_cliente_proceso(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.var.return_value.getvalue.return_value = "REF-002"

        result = repo.claim_siguiente_v2(mock_conn, "client_1", "worker-1")

        assert result == "REF-002"
        sql, binds = mock_cursor.execute.call_args[0]
        sql_upper = sql.upper()
        assert "SKIP LOCKED" in sql_upper
        assert "CLIENTE_PROCESO" in sql_upper
        assert "LICITACIONES_METADATA" not in sql_upper
        assert binds["cid"] == "client_1"

    def test_claim_siguiente_v2_no_persiste_worker_id(self, mock_conn, mock_cursor):
        """Documenta el hueco a propósito: cliente_proceso no tiene columna
        worker_id (mismo hueco que set_estado_v2) — worker_id no debe
        aparecer en el UPDATE ni en los binds."""
        from src.memoria import repo

        mock_cursor.var.return_value.getvalue.return_value = "REF-002"

        repo.claim_siguiente_v2(mock_conn, "client_1", "worker-1")

        sql, binds = mock_cursor.execute.call_args[0]
        update_stmt = sql.split("UPDATE cliente_proceso")[1].split("WHERE CURRENT OF c")[0]
        assert "worker_id" not in update_stmt.lower()
        assert "worker-1" not in str(binds)

    def test_claim_siguiente_v2_sin_pendientes_devuelve_none(self, mock_conn, mock_cursor):
        from src.memoria import repo

        mock_cursor.var.return_value.getvalue.return_value = None

        assert repo.claim_siguiente_v2(mock_conn, "client_1", "worker-1") is None
