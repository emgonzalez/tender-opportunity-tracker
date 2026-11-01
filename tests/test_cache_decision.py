"""Caché aislada por cliente y TTL, con repositorio falso."""
from datetime import datetime, timedelta, timezone

import pytest

from src.aprendizaje import cache


def test_clave_estable_y_dependencias():
    clave = cache.clave_cache("C2", "v1", "modelo", {"b": 2, "a": "á"})
    assert len(clave) == 64
    assert clave == cache.clave_cache("C2", "v1", "modelo", {"a": "á", "b": 2})
    for args in (("C3", "v1", "modelo", {"a": "á", "b": 2}),
                 ("C2", "v2", "modelo", {"a": "á", "b": 2}),
                 ("C2", "v1", "otro", {"a": "á", "b": 2})):
        assert cache.clave_cache(*args) != clave
    with pytest.raises(ValueError):
        cache.clave_cache("C2", "v1", "m", float("nan"))


def test_ttl_y_scope(monkeypatch):
    ahora = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fila = {"salida": {"veredicto": "confirmada"}, "creado_en": ahora - timedelta(seconds=10)}
    def buscar(conn, clave, capa, **kw):
        assert kw == dict(cliente_id="sintetico", version="v1", variante="sombra")
        return fila
    monkeypatch.setattr(cache.repo, "buscar_decision_por_hash", buscar)
    kw = dict(cliente_id="sintetico", version="v1", variante="sombra", ahora=ahora)
    assert cache.obtener(None, "hash", "C2", ttl=11, **kw) == fila["salida"]
    assert cache.obtener(None, "hash", "C2", ttl=10, **kw) is None
    fila["creado_en"] = ahora + timedelta(seconds=1)
    assert cache.obtener(None, "hash", "C2", **kw) is None
    fila.update(creado_en=ahora, salida={"truncado": True})
    assert cache.obtener(None, "hash", "C2", **kw) is None


def test_fallo_repo_y_guardado(monkeypatch):
    def caida(*a, **kw):
        raise RuntimeError("DSN inválido sintético")
    monkeypatch.setattr(cache.repo, "buscar_decision_por_hash", caida)
    monkeypatch.setattr(cache.repo, "guardar_decision", caida)
    assert cache.obtener(None, "hash", "C2", cliente_id="sintetico") is None
    assert cache.guardar(None, "hash", "C2", {}, cliente_id="sintetico", proceso_gid="sintetico:1") is None
    monkeypatch.setattr(cache.repo, "guardar_decision", lambda *a, **kw: 42)
    assert cache.guardar(None, "hash", "C2", {}, cliente_id="sintetico", proceso_gid="sintetico:1") == 42
