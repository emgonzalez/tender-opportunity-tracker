"""Test de integración contra una Oracle ADB 23ai REAL (Fase 2, Track B · B8.2).

Se SALTA por defecto: solo corre si están las credenciales de ADB en el entorno.

    # Correrlo (requiere ADB provisionada + db/schema.sql + db/load_onnx.sql):
    export ENABLE_MEMORIA=true ADB_USER=... ADB_PASSWORD=... ADB_DSN=... \
           ADB_WALLET_DIR=/ruta/al/wallet ADB_WALLET_PASSWORD=...
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
    from src.memoria import vectorizer, rag

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
    from src.memoria import vectorizer, rag

    texto = "Servidores rack, firewall perimetral y almacenamiento para data center. " * 20
    vectorizer.vectorizar_proceso(conn, proceso_temporal.numero_referencia, texto, cfg)

    filas = rag.buscar_precedentes(
        conn, "servidores y firewall", cfg,
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
