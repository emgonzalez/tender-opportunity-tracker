"""Tests de src.mercado.service — orquestación, con ADB/red mockeadas.

Mismo patrón que tests/test_memoria_service.py: una conexión falsa como
context manager, y los submódulos (`api_publica`, `identidad`, `repo`)
parcheados para no tocar ni Oracle ni COMPRASAL.
"""
import types

import pytest

from src.mercado import service


class _ConexionFalsa:
    def __init__(self):
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1


@pytest.fixture
def cfg():
    return types.SimpleNamespace()


class TestSincronizarMercado:
    def test_pagina_upsertea_cada_fila_y_comitea_por_pagina(self, monkeypatch, cfg):
        conexion = _ConexionFalsa()
        monkeypatch.setattr("src.memoria.db.connection", lambda c: conexion)

        paginas = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        monkeypatch.setattr(
            "src.mercado.api_publica.paginar_todo",
            lambda **kw: iter(paginas),
        )
        monkeypatch.setattr("src.mercado.identidad.es_nuestro", lambda item: item["id"] == 2)

        upserts = []
        monkeypatch.setattr(
            "src.mercado.repo.upsert_adjudicacion_item",
            lambda conn, item, es_multicomputos: upserts.append((item["id"], es_multicomputos)),
        )

        total = service.sincronizar_mercado(cfg)

        assert total == 3
        assert upserts == [(1, False), (2, True), (3, False)]
        assert conexion.commits == 2  # una vez por página, no una vez al final

    def test_reenvia_filtros_a_paginar_todo(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        capturado = {}

        def fake_paginar_todo(**kw):
            capturado.update(kw)
            return iter([])

        monkeypatch.setattr("src.mercado.api_publica.paginar_todo", fake_paginar_todo)

        service.sincronizar_mercado(cfg, id_institucion=25, modalidad=12, nombre="medicamentos")

        assert capturado == {"id_institucion": 25, "modalidad": 12, "nombre": "medicamentos"}

    def test_sin_filas_devuelve_cero(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.api_publica.paginar_todo", lambda **kw: iter([]))
        assert service.sincronizar_mercado(cfg) == 0


class TestSincronizarParticipados:
    def test_delega_al_modulo_participacion(self, monkeypatch, cfg):
        async def fake_sync(cfg_, cliente_id, max_pages=0):
            assert cliente_id == "multicomputos"
            assert max_pages == 3
            return 7

        monkeypatch.setattr("src.mercado.participacion.sincronizar_participados", fake_sync)

        import asyncio
        resultado = asyncio.run(service.sincronizar_participados(cfg, "multicomputos", max_pages=3))
        assert resultado == 7


class TestResolverResultados:
    def test_devuelve_ganados_perdidos_y_pendientes(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr(
            "src.mercado.repo.resolver_resultados",
            lambda conn, cliente_id: {"ganados": 2, "perdidos": 5},
        )
        monkeypatch.setattr("src.mercado.repo.contar_pendientes", lambda conn, cliente_id: 10)

        resultado = service.resolver_resultados(cfg, "multicomputos")

        assert resultado == {"ganados": 2, "perdidos": 5, "pendientes": 10}
