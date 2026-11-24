"""Pruebas del cliente de Hacienda con respuestas capturadas y HTTP simulado."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from src.memoria import fx_hacienda
from src.memoria.fx_regla import TasaDia

_FIXTURES = Path(__file__).parent / "fixtures" / "hacienda"
_DIA = date(2026, 11, 24)
_DESDE = date(2026, 11, 10)


def _leer(nombre: str) -> dict:
    return json.loads((_FIXTURES / nombre).read_text(encoding="utf-8"))


def _respuesta(datos: object, codigo: int = 200) -> Mock:
    respuesta = Mock(status_code=codigo)
    respuesta.json.return_value = datos
    return respuesta


@pytest.mark.parametrize("fixture", ["tc_dolar.json", "tc.json"])
def test_ultimo_lee_venta_y_compra_de_las_fixtures(fixture: str) -> None:
    sesion = Mock()
    sesion.get.return_value = _respuesta(_leer(fixture))
    assert fx_hacienda.obtener_ultimo(sesion=sesion, url="https://fixture.invalid") == TasaDia(
        "CRC", _DIA, 445.2, 440.9, "Hacienda",
    )
    sesion.get.assert_called_once_with("https://fixture.invalid", timeout=10, allow_redirects=False)


def test_descarta_compra_de_otro_dia(caplog: pytest.LogCaptureFixture) -> None:
    datos = _leer("tc_dolar.json")
    datos["compra"]["fecha"] = "2026-11-23"
    sesion = Mock()
    sesion.get.return_value = _respuesta(datos)
    with caplog.at_level(logging.WARNING, logger=fx_hacienda.__name__):
        tasa = fx_hacienda.obtener_ultimo(sesion=sesion)
    assert tasa == TasaDia("CRC", _DIA, 445.2, None, "Hacienda")
    assert "se descarta compra del 2026-11-23" in caplog.text


@pytest.mark.parametrize(("clave", "valor", "mensaje"), [
    ("valor", 99.99, "fuera del rango"),
    ("valor", 2000.01, "fuera del rango"),
    ("valor", float("nan"), "fuera del rango"),
    ("valor", "445.2", "no numérico"),
    ("valor", True, "no numérico"),
    ("fecha", "2026-02-30", "fecha ISO inválida"),
    ("fecha", "20261124", "fecha ISO inválida"),
])
def test_rechaza_cotizacion_invalida(clave: str, valor: object, mensaje: str) -> None:
    datos = _leer("tc_dolar.json")
    datos["venta"][clave] = valor
    sesion = Mock()
    sesion.get.return_value = _respuesta(datos)
    espera = Mock()
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match=mensaje):
        fx_hacienda.obtener_ultimo(sesion=sesion, espera=espera)
    assert sesion.get.call_count == 1
    espera.assert_not_called()


def test_rechaza_clave_ausente() -> None:
    sesion = Mock()
    datos = _leer("tc_dolar.json")
    del datos["compra"]
    sesion.get.return_value = _respuesta(datos)
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match="falta la clave «compra»"):
        fx_hacienda.obtener_ultimo(sesion=sesion)


def test_rechaza_json_invalido_sin_reintentar() -> None:
    sesion = Mock()
    respuesta = _respuesta(None)
    respuesta.json.side_effect = ValueError("contenido inválido")
    sesion.get.return_value = respuesta
    espera = Mock()
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match="JSON inválido"):
        fx_hacienda.obtener_ultimo(sesion=sesion, espera=espera)
    assert sesion.get.call_count == 1
    espera.assert_not_called()


@pytest.mark.parametrize("codigo", [201, 302, 404, 429])
def test_no_reintenta_http_fuera_de_5xx(codigo: int) -> None:
    sesion = Mock()
    sesion.get.return_value = _respuesta({}, codigo)
    espera = Mock()
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match=f"HTTP {codigo}"):
        fx_hacienda.obtener_ultimo(sesion=sesion, espera=espera)
    assert sesion.get.call_count == 1
    espera.assert_not_called()


def test_reintenta_503_con_espera_creciente_y_agota_intentos() -> None:
    sesion = Mock()
    respuesta = _respuesta(_leer("error_503.json"), 503)
    sesion.get.return_value = respuesta
    esperas: list[float] = []
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match="HTTP 503"):
        fx_hacienda.obtener_rango(_DESDE, _DIA, sesion=sesion, espera=esperas.append)
    assert sesion.get.call_count == 3
    assert esperas == [1, 2]
    respuesta.json.assert_not_called()


def test_reintenta_errores_de_red_y_devuelve_el_primer_exito() -> None:
    sesion = Mock()
    sesion.get.side_effect = [
        requests.Timeout(), requests.ConnectionError(), _respuesta(_leer("tc_dolar.json")),
    ]
    esperas: list[float] = []
    assert fx_hacienda.obtener_ultimo(sesion=sesion, espera=esperas.append).dia == _DIA
    assert sesion.get.call_count == 3
    assert esperas == [1, 2]


def test_permite_limitar_a_una_peticion() -> None:
    sesion = Mock()
    sesion.get.return_value = _respuesta({}, 503)
    espera = Mock()
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match="HTTP 503"):
        fx_hacienda.obtener_ultimo(sesion=sesion, espera=espera, intentos=1)
    assert sesion.get.call_count == 1
    espera.assert_not_called()


def test_historico_acepta_lista_de_pares_y_envia_parametros() -> None:
    sesion = Mock()
    sesion.get.return_value = _respuesta([_leer("tc_dolar.json")])
    assert fx_hacienda.obtener_rango(_DESDE, _DIA, sesion=sesion) == [
        TasaDia("CRC", _DIA, 445.2, 440.9, "Hacienda"),
    ]
    assert sesion.get.call_args.args[0].endswith("/dolar/historico")
    assert sesion.get.call_args.kwargs["params"] == {"d": "2026-11-10", "h": "2026-11-24"}


def test_historico_empata_series_por_fecha_y_ordena_ventas() -> None:
    datos = _leer("tc_dolar.json")
    sesion = Mock()
    sesion.get.return_value = _respuesta({
        "venta": [datos["venta"], {"fecha": "2026-11-23", "valor": 444.0}],
        "compra": [datos["compra"]],
    })
    assert fx_hacienda.obtener_rango(_DESDE, _DIA, sesion=sesion) == [
        TasaDia("CRC", date(2026, 11, 23), 444.0, None, "Hacienda"),
        TasaDia("CRC", _DIA, 445.2, 440.9, "Hacienda"),
    ]


@pytest.mark.parametrize(("datos", "mensaje"), [
    ({"data": []}, "formato histórico"),
    ([{}, _leer("tc_dolar.json")], "falta la clave"),
    ([_leer("tc_dolar.json")] * 2, "duplicada"),
])
def test_historico_rechaza_formatos_desconocidos_y_datos_invalidos(
    datos: object, mensaje: str,
) -> None:
    sesion = Mock()
    sesion.get.return_value = _respuesta(datos)
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match=mensaje):
        fx_hacienda.obtener_rango(_DESDE, _DIA, sesion=sesion)


def test_historico_rechaza_una_venta_fuera_del_tramo() -> None:
    sesion = Mock()
    sesion.get.return_value = _respuesta([_leer("tc_dolar.json")])
    with pytest.raises(fx_hacienda.ErrorFxHacienda, match="fuera del tramo"):
        fx_hacienda.obtener_rango(_DESDE, _DESDE, sesion=sesion)
