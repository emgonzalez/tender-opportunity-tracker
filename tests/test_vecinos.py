"""Consulta vectorial falsa y calibración sin conexión externa."""
from datetime import datetime, timedelta, timezone

import pytest

from src.aprendizaje.vecinos import casos_few_shot, consultar_vecinos, score_knn


class Cursor:
    def __init__(self, filas=(), falla=False):
        self.filas, self.falla, self.cerrado = filas, falla, False

    def execute(self, sql, binds):
        self.sql, self.binds = sql, binds
        if self.falla:
            raise RuntimeError("DSN inválido")

    def fetchall(self):
        return self.filas

    def close(self):
        self.cerrado = True


class Conexion:
    def __init__(self, cursor):
        self.c = cursor

    def cursor(self):
        return self.c


def test_consulta_in_db_aislada_y_prefijo():
    c = Cursor([(1, "Licencias Oracle", "positivo", "humana", 1, None, "Oracle", 0.1)])
    vecinos = consultar_vecinos(Conexion(c), "cliente_demo", "oracle", "Oracle", 5)
    assert len(vecinos) == 1
    assert vecinos[0]["distancia"] == 0.1
    assert "VECTOR_DISTANCE" in c.sql
    assert "VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :t AS data)" in c.sql
    assert "split = 'entrenamiento'" in c.sql
    assert c.binds == {"cliente_id": "cliente_demo", "marca_id": "oracle", "t": "query: Oracle", "k": 5}
    assert c.cerrado


@pytest.mark.parametrize("falla", [False, True])
def test_adb_caida_o_sin_ejemplos_es_neutro(falla):
    c = Cursor(falla=falla)
    assert score_knn(consultar_vecinos(Conexion(c), "demo", "oracle", "licencias", 3)) == 0.5
    assert c.cerrado


def test_arranque_frio_encoge_hacia_neutro():
    positivo = {"etiqueta": "positivo", "fuente": "humana", "peso": 1, "distancia": 0}
    assert score_knn([positivo]) == pytest.approx(0.6)
    assert score_knn([positivo] * 20) > score_knn([positivo])
    assert score_knn([{**positivo, "etiqueta": "negativo"}]) == pytest.approx(0.4)
    assert score_knn([{**positivo, "fuente": "verificacion_llm"}]) < score_knn([positivo])
    viejo = datetime.now(timezone.utc) - timedelta(days=730)
    assert score_knn([{**positivo, "creado_en": viejo}]) < score_knn([positivo])
    assert score_knn([{**positivo, "distancia": float("nan")}]) == 0.5


def test_tres_casos_resueltos_mas_cercanos():
    vecinos = [{"titulo_texto": str(i), "etiqueta": "negativo", "distancia": i / 10} for i in (4, 1, 3, 2)]
    vecinos.append({"titulo_texto": "ajena", "etiqueta": "ajena", "distancia": 0})
    assert [v["titulo_texto"] for v in casos_few_shot(vecinos)] == ["1", "2", "3"]
