"""Confirmaciones por destinatario, sin conexiones reales ni embeddings remotos."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.aprendizaje import repo as aprendizaje
from src.feedback import repo, service
from src.memoria import db


class Conexion:
    def __init__(self):
        self.ejemplos = []
        self.consultas = []
        self.resultados = []

    def cursor(self):
        return self

    def close(self):
        pass

    def execute(self, sql, binds):
        self.consultas.append((sql, binds))
        if sql.startswith("SELECT cliente_id"):
            self.resultados = [(binds["cliente_id"],)]
        elif sql.startswith("SELECT titulo"):
            self.resultados = [("Licencias de prueba",)]
        elif sql.startswith("SELECT ejemplo_id"):
            self.resultados = [
                (e["ejemplo_id"], e["etiqueta"]) for e in self.ejemplos
                if e["activo"] and all(e[k] == v for k, v in binds.items())
            ]
        elif sql.startswith("UPDATE ejemplo_marca SET proceso_gid"):
            self.ejemplos[binds["ejemplo_id"] - 1]["proceso_gid"] = binds["proceso_gid"]

    def fetchone(self):
        return self.resultados[0] if self.resultados else None

    def fetchall(self):
        return self.resultados


@pytest.fixture
def conexion(monkeypatch):
    conn = Conexion()

    def agregar(conn, cliente_id, marca_id, etiqueta, fuente, titulo_texto, **kw):
        assert fuente == "confirmacion"
        assert kw["peso"] == 1.0 and kw["split"] == "entrenamiento"
        n = len(conn.ejemplos) + 1
        conn.ejemplos.append(dict(ejemplo_id=n, cliente_id=cliente_id, marca_id=marca_id,
                                 etiqueta=etiqueta, activo=True, **kw))
        return {"ejemplo_id": n, "nuevo": True}

    def desactivar(conn, ejemplo_id):
        conn.ejemplos[ejemplo_id - 1]["activo"] = False

    monkeypatch.setattr(aprendizaje, "agregar_ejemplo", agregar)
    monkeypatch.setattr(aprendizaje, "desactivar_ejemplo", desactivar)
    return conn


def votar(conn, voto="si", destinatario="hash-a", marca="Oracle"):
    repo.guardar_marca(conn, cliente_id="demo", proceso_id="P-1", marca=marca,
                       destinatario_id=destinatario, voto=voto)


def test_repeticion_y_cambio_de_voto(conexion):
    votar(conexion)
    votar(conexion)
    assert len(conexion.ejemplos) == 1
    votar(conexion, "no")
    assert [e["activo"] for e in conexion.ejemplos] == [False, True]
    assert conexion.ejemplos[-1]["etiqueta"] == "negativo"
    assert conexion.ejemplos[-1]["proceso_gid"].endswith(":P-1")
    assert "FOR UPDATE" in conexion.consultas[0][0]
    assert conexion.ejemplos[-1]["marca_id"] == "oracle"


def test_otros_destinatarios_y_marcas_no_se_reemplazan(conexion):
    votar(conexion)
    votar(conexion, destinatario="hash-b")
    votar(conexion, marca="IBM")
    votar(conexion, "no")
    activos = [e for e in conexion.ejemplos if e["activo"]]
    assert len(activos) == 3
    assert activos[0]["creado_por"] == "hash-b"
    assert activos[1]["marca_id"] == "ibm"


def test_sql_no_interpola_marca(conexion):
    marca = "Oracle'; DROP TABLE ejemplo_marca; --"
    votar(conexion, marca=marca)
    assert all(marca not in sql for sql, _ in conexion.consultas)
    assert conexion.ejemplos[0]["marca_id"] == marca.lower()


def test_voto_invalido_no_escribe(conexion):
    with pytest.raises(ValueError):
        votar(conexion, "quizas")
    assert not conexion.consultas


def test_servicio_transaccion_y_degradacion(monkeypatch):
    cfg = SimpleNamespace(enable_feedback=True, feedback_base_url="https://example.com",
                          feedback_hmac_secret="prueba", adb_dsn="dsn-invalido")
    payload = SimpleNamespace(cliente_id="demo", proceso_id="P-1", marca="Oracle",
                              destinatario_id="hash-a", voto="si")
    monkeypatch.setattr(service, "_portal", lambda *_: "portal-demo")
    guardar = MagicMock()
    monkeypatch.setattr(repo, "guardar_marca", guardar)

    @contextmanager
    def connection(_):
        yield "conexion-falsa"

    monkeypatch.setattr(db, "connection", connection)
    assert service.guardar_marca(cfg, payload)
    assert guardar.call_args.kwargs["destinatario_id"] == "hash-a"
    assert guardar.call_args.kwargs["portal_id"] == "portal-demo"
    monkeypatch.setattr(db, "connection", MagicMock(side_effect=ValueError("DSN inválido")))
    assert not service.guardar_marca(cfg, payload)
    cfg.enable_feedback = False
    assert not service.guardar_marca(cfg, payload)
