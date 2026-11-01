"""Minado con procesos sintéticos, sin conexiones ni modelos externos."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

from src.aprendizaje import minado


def ejemplos(termino="exadata", fuentes=True):
    filas = [dict(ejemplo_id=i, proceso_gid=f"sv:{i}", marca_id="oracle", etiqueta="positivo",
                  fuente="participacion" if fuentes and i % 2 else "resultado_comercial",
                  titulo_texto=f"Licencias {termino}", split="entrenamiento", activo=True,
                  creado_en=datetime(2026, 1, 1) + timedelta(days=i)) for i in range(20)]
    filas.append(dict(ejemplo_id=100, proceso_gid="sv:critico", marca_id="oracle", etiqueta="ajena",
                      fuente="humana", titulo_texto="Cognos", split="critico", activo=True,
                      creado_en=datetime(2025, 1, 1)))
    return filas


def candidato(filas, termino="exadata"):
    return next(f for f in minado.proponer(filas) if f["termino"] == termino)


def test_autoactiva_con_holdout_temporal_y_dos_fuentes():
    p = candidato(ejemplos())
    assert p["activar"] and p["soporte"] == 16 and p["soporte_holdout"] == 4


def test_sin_holdout_no_hay_autoactivacion():
    filas = ejemplos()
    for f in filas:
        f["creado_en"] = datetime(2026, 1, 1)
    assert minado.proponer(filas) == []


def test_holdout_negativo_bloquea():
    filas = ejemplos()
    filas[19]["etiqueta"] = "negativo"
    assert not candidato(filas)["activar"]


def test_critico_bloquea_y_humano_inmutable():
    filas = ejemplos()
    filas[-1]["titulo_texto"] = "No corresponde Exadata"
    previo = deepcopy(filas)
    assert not candidato(filas)["activar"]
    assert filas == previo
    assert not candidato(filas[:-1])["activar"]


def test_crystal_solo_y_fuente_unica_no_activan():
    assert not candidato(ejemplos("crystal"), "crystal")["activar"]
    assert not candidato(ejemplos(fuentes=False))["activar"]


def test_prueba_nunca_entrena_ni_proceso_se_filtra():
    filas = ejemplos()
    filas[0]["split"] = "prueba"
    repetido = dict(filas[1], creado_en=datetime(2026, 1, 20))
    entrenamiento, holdout = minado._partir([*filas, repetido])
    assert all(f["split"] != "prueba" for f in entrenamiento)
    assert not ({f["proceso_gid"] for f in entrenamiento} & {f["proceso_gid"] for f in holdout})
    assert "sv:1" not in {f["proceso_gid"] for f in entrenamiento}


def test_soporte_no_se_infla_con_varios_votos_del_mismo_proceso():
    filas = ejemplos()
    assert candidato([*filas, *filas])["soporte"] == candidato(filas)["soporte"]


def test_ejecutar_dry_run_y_escritura_auditada(monkeypatch):
    monkeypatch.setattr(minado.repo, "listar_ejemplos", Mock(return_value=ejemplos()))
    proponer = Mock(return_value=42)
    activar = Mock(return_value=True)
    monkeypatch.setattr(minado.repo, "proponer_termino", proponer)
    monkeypatch.setattr(minado.repo, "activar_termino", activar)
    assert minado.ejecutar(None, "sintetico")["propuestos"] == 0
    proponer.assert_not_called()
    resultado = minado.ejecutar(None, "sintetico", aplicar=True)
    assert resultado["propuestos"] == 1 and resultado["activados"] == 1
    activar.assert_called_once_with(None, 42)


def test_rechazo_previo_no_se_reactiva(monkeypatch):
    monkeypatch.setattr(minado.repo, "listar_ejemplos", Mock(return_value=ejemplos()))
    monkeypatch.setattr(minado.repo, "proponer_termino", Mock(return_value=None))
    activar = Mock()
    monkeypatch.setattr(minado.repo, "activar_termino", activar)
    assert minado.ejecutar(None, "sintetico", aplicar=True)["activados"] == 0
    activar.assert_not_called()
