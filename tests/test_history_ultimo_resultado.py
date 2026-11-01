"""Recuperación de resultados locales y competibilidad al leer la ADB, sin red."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import history
from src.memoria import db


def _escribir(tmp_path, entradas):
    carpeta = tmp_path / "history"
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / "cli.analisis.jsonl"
    ruta.write_text("\n".join(json.dumps(e) for e in entradas) + "\n{corrupto\n", encoding="utf-8")
    return ruta


def _entrada(error, estado="REQUIERE_REVISION_MANUAL", uuid="p1"):
    return {"proceso_uuid": uuid, "estado": estado, "error": error}


def test_ultima_linea_del_estado_sin_matriz(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_FUENTE", "archivo")
    ruta = _escribir(tmp_path, [_entrada("viejo"), [], _entrada("otro", uuid="p2"),
                                _entrada("último"), _entrada("notificado", "NOTIFICADO")])
    antes = ruta.read_bytes()
    res = history.ultimo_resultado("cli", "p1", "REQUIERE_REVISION_MANUAL", str(tmp_path))
    assert res.error == "último" and res.matriz is None
    assert ruta.read_bytes() == antes
    assert history.ultimo_resultado("cli", "p1", "ANALIZADO", str(tmp_path)) is None


def test_spool_prevalece_y_no_se_drena(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_FUENTE", "adb")
    _escribir(tmp_path, [_entrada("local")])
    spool = history._spool(str(tmp_path))
    cola = history._cola("analisis", "cli")
    for e in [_entrada("pendiente"), _entrada("último pendiente"), _entrada("otro", "NOTIFICADO")]:
        spool.agregar(cola, e)
    res = history.ultimo_resultado("cli", "p1", "REQUIERE_REVISION_MANUAL", str(tmp_path))
    assert res.error == "último pendiente"
    assert len(spool.pendientes(cola)) == 3


@pytest.mark.parametrize("modo", ["archivo", "sombra", "adb"])
def test_sin_historial(tmp_path, monkeypatch, modo):
    monkeypatch.setenv("ESTADO_FUENTE", modo)
    assert history.ultimo_resultado("cli", "p1", "NOTIFICADO", str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []


def test_fallos_de_lectura_y_deserializacion(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_FUENTE", "archivo")
    ruta = _escribir(tmp_path, [_entrada("x")])
    monkeypatch.setattr(history.ResultadoAnalisis, "desde_dict", MagicMock(side_effect=ValueError))
    assert history.ultimo_resultado("cli", "p1", "REQUIERE_REVISION_MANUAL", str(tmp_path)) is None
    ruta.unlink()
    ruta.mkdir()
    assert history.ultimo_resultado("cli", "p1", "NOTIFICADO", str(tmp_path)) is None


def test_fallo_spool(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTADO_FUENTE", "adb")
    monkeypatch.setattr(history, "_spool", MagicMock(side_effect=OSError))
    assert history.ultimo_resultado("cli", "p1", "NOTIFICADO", str(tmp_path)) is None


@pytest.mark.parametrize("falla", [False, True])
def test_notificado_adb_deriva_competibilidad_y_conserva_estado(tmp_path, monkeypatch, falla):
    monkeypatch.setenv("ESTADO_FUENTE", "adb")
    monkeypatch.setattr(history, "_cfg", lambda: SimpleNamespace(adb_dsn="invalid.example", adb_password=""))
    monkeypatch.setattr(history, "_adb_lista", lambda cfg: True)
    matriz = {"items": [{"descripcion": "Proveedor único", "clausula_ref": "1"}]}
    cursor = MagicMock()
    cursor.fetchall.return_value = [("sv:p1", "NOTIFICADO", "GO", 1, "texto", matriz, "modelo", None,
                                    "Contratación directa")]
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def conexion(cfg):
        yield conn

    monkeypatch.setattr(db, "connection", conexion)
    if falla:
        monkeypatch.setattr("src.drilldown.analysis.aplicar_competibilidad", MagicMock(side_effect=ValueError))
    res = history.load_analisis("cli", ["p1"], str(tmp_path), marcas_cliente=["Oracle"])["p1"]
    assert res.estado == "NOTIFICADO"
    assert res.competible is (None if falla else False)
    if not falla:
        assert res.motivo_bloqueo == "proveedor_unico"


def test_sombra_no_marca_diferencia_entre_analizado_y_notificado(monkeypatch, caplog):
    """El JSONL devuelve la línea ANALIZADO (con matriz) y la ADB el estado vigente NOTIFICADO: es el mismo análisis.
    Regresión del 2026-11-01: ensuciaba `analisis[sombra]` en el criterio de corrida limpia."""
    import logging

    from src import history
    from src.scraper.models import ResultadoAnalisis

    def res(estado):
        return ResultadoAnalisis(proceso_uuid="u-eq", estado=estado)

    monkeypatch.setattr(history, "_leer_analisis_adb", lambda cid, quiero: {"u-eq": res("NOTIFICADO")})
    with caplog.at_level(logging.INFO, logger="src.history"):
        history._sombra_comparar_analisis("multicomputos_sv", {"u-eq"}, {"u-eq": res("ANALIZADO")})
    assert "analisis[sombra]" not in caplog.text

    # una diferencia real sigue avisando
    monkeypatch.setattr(history, "_leer_analisis_adb", lambda cid, quiero: {"u-dif": res("ERROR_LLM")})
    with caplog.at_level(logging.INFO, logger="src.history"):
        history._sombra_comparar_analisis("multicomputos_sv", {"u-dif"}, {"u-dif": res("ANALIZADO")})
    assert "analisis[sombra]" in caplog.text
