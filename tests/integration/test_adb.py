"""Test de integración contra una Oracle ADB 23ai REAL (Fase 2, Track B · B8.2).

Se SALTA por defecto: solo corre si están las credenciales de ADB en el entorno.

    # Correrlo (requiere ADB provisionada + db/schema.sql + db/load_onnx.sql):
    export ENABLE_MEMORIA=true ADB_USER=... ADB_PASSWORD=... \
           ADB_DSN='(description= ...connection string TLS...)'
    python -m pytest tests/integration -m integration -v

Verifica el camino completo: esquema → vectorización in-DB → búsqueda semántica
→ precedentes inter-documento, y limpia todo lo que crea.
"""
import os
import uuid as _uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

_REQUERIDAS = ("ADB_USER", "ADB_PASSWORD", "ADB_DSN")
_HAY_CREDENCIALES = all(os.getenv(v) for v in _REQUERIDAS)

pytest_skip_reason = (
    "Sin credenciales de ADB (definir " + ", ".join(_REQUERIDAS) + ")"
)


@pytest.fixture(scope="module")
def cfg():
    if not _HAY_CREDENCIALES:
        pytest.skip(pytest_skip_reason)
    os.environ.setdefault("ENABLE_MEMORIA", "true")
    # Env dummy que exige Config pero no se usa en este test.
    for k in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "ANTHROPIC_API_KEY",
              "SMTP_USER", "SMTP_PASSWORD"):
        os.environ.setdefault(k, "x")
    from src.config import Config
    return Config()


@pytest.fixture(scope="module")
def conn(cfg):
    from src.memoria import db
    with db.connection(cfg) as c:
        yield c
    db.close_pool()


@pytest.fixture
def proceso_temporal(conn, cfg):
    """Crea un proceso de prueba y lo borra al final (CASCADE limpia los chunks)."""
    from src.memoria import repo
    from src.scraper.models import Proceso

    pid = f"TEST-{_uuid.uuid4().hex[:12]}"
    p = Proceso(
        institucion="INSTITUCION DE PRUEBA",
        descripcion="Adquisición de servidores rack y firewall perimetral",
        fecha_publicacion=datetime.now(timezone.utc),
        url_directa="https://example.test/proceso",
        numero_referencia=pid,
        forma_contratacion="Licitación Pública",
        tipo_actividad="Infraestructura",
    )
    repo.upsert_proceso(conn, p, "test_client", estado="APROBADO")
    yield p
    cur = conn.cursor()
    cur.execute("DELETE FROM licitaciones_metadata WHERE proceso_id = :pid", pid=pid)
    conn.commit()


def test_keepalive(cfg):
    """La ADB responde (y con esto se evita el auto-stop a 7 días)."""
    from src.memoria import db
    assert db.keepalive(cfg) is True


def test_embedding_in_db_devuelve_384_dims(conn, cfg):
    """El modelo ONNX está cargado y genera vectores de 384 dimensiones."""
    cur = conn.cursor()
    modelo = cfg.embedding_model_name
    cur.execute(
        f"SELECT VECTOR_DIMENSION_COUNT("
        f"VECTOR_EMBEDDING({modelo} USING :txt AS data)) FROM dual",
        txt="servidor rack firewall",
    )
    assert cur.fetchone()[0] == 384


def test_ciclo_vectorizar_y_buscar(conn, cfg, proceso_temporal):
    """Vectoriza un pliego y lo recupera por búsqueda semántica (RAG intra-doc)."""
    from src.memoria import rag, vectorizer

    texto = (
        "CLAUSULA QUINTA - PENALIDADES. El contratista pagará una multa del 0.5% "
        "por cada día de atraso en la entrega de los servidores. "
        "ESPECIFICACIONES TECNICAS. Se requieren servidores tipo rack con "
        "procesadores de alto rendimiento, almacenamiento en estado sólido y "
        "un firewall perimetral con licenciamiento por tres años. "
    ) * 6

    n = vectorizer.vectorizar_proceso(conn, proceso_temporal.numero_referencia, texto, cfg)
    assert n > 0
    assert vectorizer.contar_chunks(conn, proceso_temporal.numero_referencia) == n

    chunks = rag.buscar_chunks(
        conn, proceso_temporal.numero_referencia, "¿qué multas por atraso hay?", cfg)
    assert chunks, "la búsqueda semántica no devolvió chunks"
    assert len(chunks) <= cfg.rag_top_k
    assert any("multa" in c.lower() or "penalidad" in c.lower() for c in chunks)


def test_precedentes_excluyen_el_proceso_actual(conn, cfg, proceso_temporal):
    """buscar_precedentes NO debe devolver el propio pliego consultado."""
    from src.memoria import rag, vectorizer

    texto = "Servidores rack, firewall perimetral y almacenamiento para data center. " * 20
    vectorizer.vectorizar_proceso(conn, proceso_temporal.numero_referencia, texto, cfg)

    filas = rag.buscar_precedentes(
        conn, "servidores y firewall", cfg, "test_client",
        excluir_proceso_id=proceso_temporal.numero_referencia)
    ids = {f["proceso_id"] for f in filas}
    assert proceso_temporal.numero_referencia not in ids


def test_idempotencia_revectorizar(conn, cfg, proceso_temporal):
    """Re-vectorizar reemplaza los chunks previos, no los duplica."""
    from src.memoria import vectorizer

    texto = "Firewall perimetral con licenciamiento y soporte por tres años. " * 20
    n1 = vectorizer.vectorizar_proceso(conn, proceso_temporal.numero_referencia, texto, cfg)
    n2 = vectorizer.vectorizar_proceso(conn, proceso_temporal.numero_referencia, texto, cfg)
    assert n1 == n2
    assert vectorizer.contar_chunks(conn, proceso_temporal.numero_referencia) == n2


def test_estado_y_cola(conn, cfg, proceso_temporal):
    """set_estado/get_estado y la cola SKIP LOCKED operan sobre el proceso."""
    from src.memoria import repo

    repo.set_estado(conn, proceso_temporal.numero_referencia, "DESCARGADO")
    assert repo.get_estado(conn, proceso_temporal.numero_referencia) == "DESCARGADO"

    reclamado = repo.claim_siguiente(conn, worker_id="test-worker")
    assert reclamado is None or isinstance(reclamado, str)


# ── adjudicacion_partida (SICOP; db/schema.sql §9) ──────────────────────────
# Todo corre dentro de una transacción que termina en ROLLBACK: ninguna fila
# de prueba (`ZZTEST-SICOP-…`) llega a la ADB. Validado contra la ADB de DEV
# el 2026-11-23.

_ZZ_CLI = "ZZTEST-SICOP-CLI"
_ZZ_P1, _ZZ_P2, _ZZ_P3 = "ZZTEST-SICOP-001", "ZZTEST-SICOP-002", "ZZTEST-SICOP-003"


def _zz_fila(cod, partida, proveedor, propio, crc=None):
    return {"portal_id": "comprasal_sv", "codigo_proceso": cod, "id_externo": "ZZ",
            "partida": partida, "nombre_proceso": "Prueba ZZTEST", "institucion": "INST ZZTEST",
            "modalidad_codigo": "LY", "fecha_adjudicacion": None, "monto_crc": crc,
            "monto_usd": None, "proveedor_id": proveedor, "es_propio": propio,
            "datos_crudos": {"zz": 1}}


def _zz_uno(cur, sql, **binds):
    cur.execute(sql, binds)
    return cur.fetchone()[0]


@pytest.fixture
def transaccion_partida(conn):
    """Cursor dentro de una transacción que siempre termina en ROLLBACK."""
    cur = conn.cursor()
    if not _zz_uno(cur, "SELECT COUNT(*) FROM user_tables WHERE table_name = 'ADJUDICACION_PARTIDA'"):
        pytest.skip("adjudicacion_partida no existe (aplicar db/schema.sql §9)")
    conn.rollback()
    try:
        yield cur
    finally:
        conn.rollback()
        cur.close()


def test_upsert_adjudicacion_partida_idempotente_y_es_propio_solo_sube(conn, transaccion_partida):
    from src.mercado import repo
    cur = transaccion_partida

    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P1, "*", "3101ZZ", True, 1000))
    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P1, "*", "3101ZZ", False, 1500))
    assert _zz_uno(cur, "SELECT COUNT(*) FROM adjudicacion_partida WHERE codigo_proceso = :c", c=_ZZ_P1) == 1
    cur.execute("SELECT es_propio, monto_crc FROM adjudicacion_partida WHERE codigo_proceso = :c", c=_ZZ_P1)
    assert tuple(cur.fetchone()) == (1, 1500)

    # Centinela de mercado completo ('-') junto al de partida desconocida ('*').
    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P1, "1", "-", False, 50))
    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P1, "1", "-", True, 50))
    assert _zz_uno(cur, "SELECT COUNT(*) FROM adjudicacion_partida WHERE codigo_proceso = :c", c=_ZZ_P1) == 2
    assert _zz_uno(cur, "SELECT es_propio FROM adjudicacion_partida "
                        "WHERE codigo_proceso = :c AND partida = '1'", c=_ZZ_P1) == 1


def test_registrar_ganados_portal_y_prepoblar_oportunidad(conn, transaccion_partida):
    from src.mercado import repo
    cur = transaccion_partida
    if _zz_uno(cur, "SELECT COUNT(*) FROM adjudicacion_partida WHERE portal_id = 'comprasal_sv' "
                    "AND es_propio = 1 AND codigo_proceso NOT LIKE 'ZZTEST-SICOP-%'"):
        pytest.skip("hay filas propias reales en comprasal_sv: el conteo exacto no aplica")

    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P1, "*", "3101ZZ", True, 1500))
    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P1, "1", "-", True, 50))
    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P2, "*", "3101ZZ", True, 2000))
    repo.upsert_adjudicacion_partida(conn, _zz_fila(_ZZ_P3, "*", "3101ZZ", True, 3000))
    cur.execute("INSERT INTO proceso_participado (cliente_id, codigo_proceso, resultado) "
                "VALUES (:a, :b, 'PENDIENTE')", a=_ZZ_CLI, b=_ZZ_P2)
    cur.execute("INSERT INTO proceso_participado (cliente_id, codigo_proceso, resultado) "
                "VALUES (:a, :b, 'PERDIDO')", a=_ZZ_CLI, b=_ZZ_P3)

    assert repo.registrar_ganados_portal(conn, _ZZ_CLI, "comprasal_sv") == 2
    assert repo.registrar_ganados_portal(conn, _ZZ_CLI, "comprasal_sv") == 0
    cur.execute("SELECT codigo_proceso, resultado, monto_adjudicado_a_nosotros FROM proceso_participado "
                "WHERE cliente_id = :a", a=_ZZ_CLI)
    res = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert res[_ZZ_P1] == ("GANADO", 1550)        # nuevo; monto = suma de monto_crc
    assert res[_ZZ_P2][0] == "GANADO"             # PENDIENTE → GANADO
    assert res[_ZZ_P3][0] == "PERDIDO"            # lo resuelto no se pisa

    assert repo.prepoblar_oportunidad_estado_v2(conn, _ZZ_CLI, "comprasal_sv") == 3
    cur.execute("SELECT proceso_gid, estado, origen FROM oportunidad_estado WHERE cliente_id = :a", a=_ZZ_CLI)
    oe = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert oe["comprasal_sv:" + _ZZ_P1] == ("GANADO", "AUTOMATICO_MERCADO")
    assert oe["comprasal_sv:" + _ZZ_P3] == ("PERDIDO", "AUTOMATICO_MERCADO")
    repo.prepoblar_oportunidad_estado_v2(conn, _ZZ_CLI, "comprasal_sv")
    assert _zz_uno(cur, "SELECT COUNT(*) FROM oportunidad_evento WHERE cliente_id = :a", a=_ZZ_CLI) == 3
