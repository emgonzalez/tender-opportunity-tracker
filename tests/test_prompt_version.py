"""Procedencia de decisiones y claves sin proveedores externos."""
import hashlib
import json
from types import SimpleNamespace

from src.aprendizaje import cache
from src.llm.contrato import BloqueTexto, Pedido, metadatos_prompt


def test_prompt_estable_y_sistema():
    cfg = SimpleNamespace()
    pedido = Pedido("triaje", [BloqueTexto("Entrada ficticia")], sistema="Sistema A")
    bloque = metadatos_prompt(pedido, cfg, SimpleNamespace(version=7))
    assert bloque == metadatos_prompt(pedido, cfg, SimpleNamespace(version=7))
    assert bloque["app"] == "dev" and bloque["cliente_config"] == 7
    assert len(bloque["hash"]) == 12
    assert all(c in "0123456789abcdef" for c in bloque["hash"])
    pedido.sistema = "Sistema B"
    assert metadatos_prompt(pedido, cfg)["hash"] != bloque["hash"]


def test_cache_excluye_filas_antiguas_y_cambio_prompt(monkeypatch):
    entrada = ["C0", "v1", "modelo", {"cliente": "demo"}]
    vieja = hashlib.sha256(json.dumps(entrada, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    pedido = Pedido("triaje", [BloqueTexto("entrada")], sistema="A")
    clave = cache.clave_cache(*entrada, pedido=pedido)
    assert clave != vieja
    assert clave == cache.clave_cache(*entrada, pedido=pedido)
    pedido.sistema = "B"
    assert clave != cache.clave_cache(*entrada, pedido=pedido)
    monkeypatch.setattr(cache.repo, "buscar_decision_por_hash",
                        lambda conn, clave, capa, **kw: {"salida": {}} if clave == vieja else None)
    assert cache.obtener(None, clave, "C0", cliente_id="demo") is None
