"""Tipo de cambio diario de Hacienda para la corrida, con respaldo local."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.config import Config
from src.memoria import db, fx_hacienda, repo
from src.memoria.fx_regla import TasaDia, convertir_a_usd, dia_local_cr, tasa_vigente
from src.notifier.ops_alert import send_ops_alert
from src.persistencia.cache import escribir_json, leer_json
from src.portales import Portal
from src.scraper.models import Proceso

logger = logging.getLogger(__name__)

_ARCHIVO_TASAS = "fx_tasas.json"
_ARCHIVO_ESTADO = "fx_estado.json"
_DIAS_CACHE = 14


@dataclass
class TasasDiarias:
    tasas: dict[str, list[TasaDia]] = field(default_factory=dict)
    ultima: TasaDia | None = None
    origen: str = "ninguno"
    aplica: bool = False


_corrida: tuple[Config, date, TasasDiarias] | None = None


def monedas_requeridas(portales: Iterable[Portal]) -> set[str]:
    """Devuelve las monedas soportadas de los portales seleccionados."""
    monedas = {m for p in portales for m in (p.monedas or (p.moneda,))} - {"USD"}
    for moneda in sorted(monedas - {"CRC"}):
        logger.warning("Hacienda no ofrece tipo de cambio para %s.", moneda)
    return monedas & {"CRC"}


def _fecha(valor: object) -> date | None:
    if isinstance(valor, str):
        try:
            return date.fromisoformat(valor)
        except ValueError:
            return None
    return None


def _leer_tasas(ruta: Path, hoy: date) -> dict[date, TasaDia]:
    filas = leer_json(ruta, [])
    tasas: dict[date, TasaDia] = {}
    if isinstance(filas, list):
        for fila in filas:
            try:
                dia = date.fromisoformat(fila["dia"])
                if fila["moneda"] == "CRC" and 0 <= (hoy - dia).days < _DIAS_CACHE:
                    tasas[dia] = TasaDia("CRC", dia, fila["venta"], fila["compra"], fila["fuente"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Se omitió una tasa de la caché de Hacienda con datos inválidos.")
    return tasas


def _leer_estado(ruta: Path) -> dict[str, Any]:
    estado = leer_json(ruta, {})
    return estado if isinstance(estado, dict) else {}


def _guardar(ruta: Path, datos: object) -> None:
    try:
        escribir_json(ruta, datos)
    except Exception:  # noqa: BLE001 — el resultado en memoria sigue disponible
        logger.warning("No se pudo guardar %s.", ruta, exc_info=True)


def _operar_adb(config: Config, operacion: Callable[..., object], *args: object) -> None:
    if not getattr(config, "enable_esquema_v2", False):
        return
    try:
        with db.connection(config) as conn:
            operacion(conn, *args)
    except Exception:  # noqa: BLE001 — la ADB es opcional para este paso
        logger.warning("FX: falló %s en la ADB.", operacion.__name__, exc_info=True)


def preparar(
    config: Config,
    portales: Iterable[Portal],
    *,
    sesion: fx_hacienda._Sesion | None = None,
    ahora: datetime | None = None,
) -> TasasDiarias:
    """Consulta Hacienda una vez y conserva la última tasa publicada."""
    global _corrida
    resultado = TasasDiarias()
    _corrida = None
    try:
        instante = ahora if ahora is not None else datetime.now(timezone.utc)
        hoy = dia_local_cr(instante)
        _corrida = (config, hoy, resultado)
        _preparar(config, portales, sesion, instante, hoy, resultado)
    except Exception:  # noqa: BLE001 — FX es opcional para la corrida
        logger.warning("No se pudo preparar el tipo de cambio diario.", exc_info=True)
    return resultado


def _preparar(
    config: Config, portales: Iterable[Portal], sesion: fx_hacienda._Sesion | None,
    instante: datetime, hoy: date, resultado: TasasDiarias,
) -> None:
    if not getattr(config, "fx_auto", False):
        return

    habilitados = getattr(config, "portales_deshabilitados", ()) or ()
    resultado.aplica = bool(monedas_requeridas(p for p in portales if p.portal_id not in habilitados))
    if not resultado.aplica:
        return

    ruta_tasas = Path(config.data_dir) / _ARCHIVO_TASAS
    tasas = _leer_tasas(ruta_tasas, hoy)
    try:
        nueva = fx_hacienda.obtener_ultimo(url=config.fx_api_url, sesion=sesion, intentos=1)
    except Exception:  # noqa: BLE001 — se degrada a la caché local
        nueva = None
        logger.warning("No se pudo obtener el tipo de cambio de Hacienda.", exc_info=True)

    if nueva is not None:
        if 0 <= (hoy - nueva.dia).days < _DIAS_CACHE:
            tasas[nueva.dia] = nueva
        resultado.origen = "red"
        _operar_adb(config, repo.guardar_tasa_dia, nueva)
    elif tasas:
        resultado.origen = "cache"
    if nueva is not None or ruta_tasas.is_file():
        _guardar(ruta_tasas, [asdict(tasa) for tasa in tasas.values()])

    if tasas:
        resultado.tasas = {"CRC": list(tasas.values())}
    resultado.ultima = max(tasas.values(), key=lambda tasa: tasa.dia, default=nueva)

    ruta_estado = Path(config.data_dir) / _ARCHIVO_ESTADO
    estado = _leer_estado(ruta_estado)
    fallos = estado.get("fallos_consecutivos")
    ultimo_dia = _fecha(estado.get("ultimo_dia_tasa"))
    if resultado.ultima is not None and (ultimo_dia is None or resultado.ultima.dia > ultimo_dia):
        estado["ultimo_dia_tasa"] = resultado.ultima.dia.isoformat()
    estado["fallos_consecutivos"] = 0 if nueva is not None else (
        fallos + 1 if isinstance(fallos, int) and fallos >= 0 else 1
    )
    if nueva is not None:
        estado["ultimo_exito"] = instante.isoformat()
    _guardar(ruta_estado, estado)
    logger.info("FX CRC: origen %s, última tasa %s.", resultado.origen, estado.get("ultimo_dia_tasa"))


def enriquecer(config: Config, procesos: Iterable[Proceso]) -> int:
    """Completa equivalentes en memoria, también en procesos que solo tienen caché."""
    if not getattr(config, "fx_auto", False):
        return 0
    tasas = None
    enriquecidos = 0
    for proceso in procesos:
        try:
            if (proceso.moneda != "CRC" or proceso.monto is None
                    or proceso.fecha_publicacion is None or proceso.monto_usd is not None):
                continue
            if tasas is None:
                if _corrida is not None and _corrida[0] is config:
                    tasas = _corrida[2].tasas.get("CRC", [])
                else:
                    hoy = dia_local_cr(datetime.now(timezone.utc))
                    tasas = _leer_tasas(Path(config.data_dir) / _ARCHIVO_TASAS, hoy)
            tasa = tasa_vigente(tasas, dia_local_cr(proceso.fecha_publicacion))
            if tasa is not None:
                proceso.monto_usd = convertir_a_usd(proceso.monto, tasa.venta)
                enriquecidos += 1
        except Exception:  # noqa: BLE001 — una fila inválida deja intacto su monto original
            logger.warning("No se pudo calcular un equivalente en USD.", exc_info=True)
    return enriquecidos


def debe_alertar(
    estado: dict[str, Any], hoy: date, ultimo_dia_tasa: date | None,
    alerta_dias: int, repetir_dias: int,
) -> bool:
    """Decide por antigüedad de la tasa y fecha del último aviso entregado."""
    ultima_alerta = _fecha(estado.get("ultima_alerta"))
    return (
        (ultimo_dia_tasa is None or (hoy - ultimo_dia_tasa).days > alerta_dias)
        and (ultima_alerta is None or (hoy - ultima_alerta).days >= repetir_dias)
    )


def cerrar(config: Config) -> None:
    """Recalcula las filas persistidas y avisa a operación si falta una tasa vigente."""
    if _corrida is None or _corrida[0] is not config or not _corrida[2].aplica:
        return
    _, hoy, resultado = _corrida

    _operar_adb(config, repo.recalcular_monto_usd)
    try:
        ruta_estado = Path(config.data_dir) / _ARCHIVO_ESTADO
        estado = _leer_estado(ruta_estado)
        ultimo_dia = _fecha(estado.get("ultimo_dia_tasa"))
        if resultado.ultima is not None and (ultimo_dia is None or resultado.ultima.dia > ultimo_dia):
            ultimo_dia = resultado.ultima.dia
        if not debe_alertar(estado, hoy, ultimo_dia, config.fx_alerta_dias, config.fx_repetir_alerta_dias):
            return

        detalle = ("No hay ninguna tasa de venta registrada." if ultimo_dia is None else
                   f"La última tasa de venta es del {ultimo_dia} y tiene {(hoy - ultimo_dia).days} días de atraso.")
        if send_ops_alert(
            config, "Tipo de cambio CRC/USD de Hacienda atrasado",
            f"{detalle}\nPara actualizar: python -m src.memoria.fx descargar --aplicar",
        ):
            estado["ultima_alerta"] = hoy.isoformat()
            _guardar(ruta_estado, estado)
    except Exception:  # noqa: BLE001 — el canal de operación es opcional
        logger.warning("No se pudo procesar la alerta de tipo de cambio.", exc_info=True)
