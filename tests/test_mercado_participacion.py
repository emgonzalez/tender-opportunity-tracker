"""Tests de src.mercado.participacion — sin red ni ADB.

`sincronizar_participados` es async; se prueba con `asyncio.run()` dentro de
un test sync (no `async def test_...`), mismo criterio ya usado en
tests/test_locator.py: el repo no tiene `pytest-asyncio` (pyproject.toml,
"agregarlo el día que lo haya, no antes") y esto no lo necesita.
"""
import asyncio
import types

from src.mercado import participacion


class _ConexionFalsa:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sin_participados_no_toca_la_adb(monkeypatch):
    async def fake_extract(cfg, max_pages=0):
        return []

    monkeypatch.setattr("src.classifier.trainer.extract_all_participated", fake_extract)
    llamado = []
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: llamado.append(1) or _ConexionFalsa())

    total = asyncio.run(participacion.sincronizar_participados(types.SimpleNamespace(), "multicomputos"))

    assert total == 0
    assert llamado == []  # ni siquiera abre conexión si no hay nada que guardar


def test_persiste_cada_item_extraido(monkeypatch):
    items = [
        {"codigo": "300-2026-P0168", "nombre": "X", "institucion": "TSE",
         "metodo": "Contratación directa", "fecha": "2026-09-01"},
        {"codigo": "8556-2026-P0131", "nombre": "Y", "institucion": "Municipio",
         "metodo": "Comparación de precios", "fecha": "2026-08-15"},
    ]

    async def fake_extract(cfg, max_pages=0):
        assert max_pages == 5
        return items

    monkeypatch.setattr("src.classifier.trainer.extract_all_participated", fake_extract)
    monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())

    guardados = []
    monkeypatch.setattr(
        "src.mercado.repo.upsert_proceso_participado",
        lambda conn, cliente_id, item: guardados.append((cliente_id, item["codigo"])),
    )

    total = asyncio.run(
        participacion.sincronizar_participados(types.SimpleNamespace(), "multicomputos", max_pages=5)
    )

    assert total == 2
    assert guardados == [
        ("multicomputos", "300-2026-P0168"),
        ("multicomputos", "8556-2026-P0131"),
    ]
