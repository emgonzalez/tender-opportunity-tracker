"""Cliente de la API pública de tipo de cambio de Hacienda de Costa Rica."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Protocol

import requests

from src.memoria.fx_regla import TasaDia

logger = logging.getLogger(__name__)

_URL_DOLAR = "https://api.hacienda.go.cr/indicadores/tc/dolar"
_MAX_INTENTOS = 3
_MAX_RETRY_AFTER = 60


class ErrorFxHacienda(Exception):
    """Hacienda no devolvió un tipo de cambio utilizable."""


class _Sesion(Protocol):
    def get(self, url: str, **kwargs) -> requests.Response: ...


def _pedir_json(
    url: str,
    *,
    timeout: float,
    sesion: _Sesion | None,
    espera: Callable[[float], None] | None,
    intentos: int,
    params: dict[str, str] | None = None,
    reintentar_429: bool = False,
) -> object:
    if not 1 <= intentos <= _MAX_INTENTOS:
        raise ValueError(f"intentos debe estar entre 1 y {_MAX_INTENTOS}")
    get = requests.get if sesion is None else sesion.get
    esperar = time.sleep if espera is None else espera
    opciones = {"params": params} if params is not None else {}
    for intento in range(1, intentos + 1):
        demora = 2 ** (intento - 1)
        try:
            respuesta = get(url, timeout=timeout, allow_redirects=False, **opciones)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            error = "error de red al consultar Hacienda"
        except requests.RequestException:
            raise ErrorFxHacienda("falló la solicitud a Hacienda") from None
        else:
            codigo = respuesta.status_code
            if codigo == 200:
                try:
                    return respuesta.json()
                except ValueError:
                    raise ErrorFxHacienda("Hacienda devolvió JSON inválido") from None
            error = f"Hacienda devolvió HTTP {codigo}"
            if codigo == 429 and reintentar_429:
                retry_after = respuesta.headers.get("Retry-After", "").strip()
                if retry_after.isascii() and retry_after.isdecimal():
                    # Quita ceros iniciales y limita los dígitos antes de convertir.
                    segundos = retry_after.lstrip("0")[:3] or "0"
                    demora = min(int(segundos), _MAX_RETRY_AFTER)
            elif not 500 <= codigo < 600:
                break
        if intento < intentos:
            esperar(demora)
    raise ErrorFxHacienda(error)


def _campo(datos: object, clave: str) -> object:
    if not isinstance(datos, dict) or clave not in datos:
        raise ErrorFxHacienda(f"falta la clave «{clave}» en la respuesta de Hacienda")
    return datos[clave]


def _cotizacion(datos: object, tipo: str) -> tuple[date, float]:
    fecha = _campo(datos, "fecha")
    valor = _campo(datos, "valor")
    try:
        if not isinstance(fecha, str):
            raise ValueError
        dia = date.fromisoformat(fecha)
        if dia.isoformat() != fecha:
            raise ValueError
    except ValueError:
        raise ErrorFxHacienda(f"fecha ISO inválida en {tipo} de Hacienda") from None

    if type(valor) not in (int, float):
        raise ErrorFxHacienda(f"valor no numérico en {tipo} de Hacienda")
    if not 100 <= valor <= 2000:
        raise ErrorFxHacienda(f"valor fuera del rango [100, 2000] en {tipo} de Hacienda")
    return dia, float(valor)


def _par(datos: object) -> TasaDia:
    dia, venta = _cotizacion(_campo(datos, "venta"), "venta")
    dia_compra, compra = _cotizacion(_campo(datos, "compra"), "compra")
    if dia_compra != dia:
        logger.warning("Hacienda: se descarta compra del %s para la venta del %s", dia_compra, dia)
        return TasaDia("CRC", dia, venta, None, "Hacienda")
    return TasaDia("CRC", dia, venta, compra, "Hacienda")


def obtener_ultimo(
    *,
    timeout: float = 10,
    sesion: _Sesion | None = None,
    url: str | None = None,
    espera: Callable[[float], None] | None = None,
    intentos: int = _MAX_INTENTOS,
) -> TasaDia:
    """Devuelve la última venta publicada y la compra del mismo día."""
    datos = _pedir_json(
        url or _URL_DOLAR, timeout=timeout, sesion=sesion, espera=espera, intentos=intentos,
    )
    if isinstance(datos, dict) and "dolar" in datos:
        datos = datos["dolar"]
    return _par(datos)


def _serie(filas: list, tipo: str) -> dict[date, float]:
    serie: dict[date, float] = {}
    for fila in filas:
        dia, valor = _cotizacion(fila, tipo)
        if dia in serie:
            raise ErrorFxHacienda(f"fecha duplicada en serie de {tipo} de Hacienda")
        serie[dia] = valor
    return serie


def obtener_rango(
    desde: date,
    hasta: date,
    *,
    timeout: float = 10,
    sesion: _Sesion | None = None,
    url: str | None = None,
    espera: Callable[[float], None] | None = None,
    intentos: int = _MAX_INTENTOS,
) -> list[TasaDia]:
    """Consulta un tramo inclusivo. El formato histórico está sin verificar.

    Solo se aceptan una lista de pares con el formato diario o un objeto con
    series separadas de «venta» y «compra». En las series, la compra se une
    por fecha y una venta sin compra emparejada queda con compra nula.
    """
    if desde > hasta:
        raise ErrorFxHacienda("la fecha desde supera la fecha hasta")
    datos = _pedir_json(
        url or f"{_URL_DOLAR}/historico",
        timeout=timeout,
        sesion=sesion,
        espera=espera,
        intentos=intentos,
        params={"d": desde.isoformat(), "h": hasta.isoformat()},
        reintentar_429=True,
    )
    if isinstance(datos, list):
        tasas = [_par(fila) for fila in datos]
    elif isinstance(datos, dict) and isinstance(datos.get("venta"), list) and isinstance(datos.get("compra"), list):
        ventas = _serie(datos["venta"], "venta")
        compras = _serie(datos["compra"], "compra")
        tasas = [TasaDia("CRC", dia, venta, compras.get(dia), "Hacienda") for dia, venta in ventas.items()]
    else:
        raise ErrorFxHacienda("formato histórico de Hacienda no reconocido")
    if len({t.dia for t in tasas}) != len(tasas):
        raise ErrorFxHacienda("fecha de venta duplicada en el histórico de Hacienda")
    if any(not desde <= t.dia <= hasta for t in tasas):
        raise ErrorFxHacienda("fecha de venta fuera del tramo solicitado a Hacienda")
    return sorted(tasas, key=lambda t: t.dia)
