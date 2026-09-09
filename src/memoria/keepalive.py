"""src.memoria.keepalive — memoria del keepalive de la ADB entre corridas.

**Por qué existe.** `db.keepalive()` sabe si la ADB respondió *ahora*, pero el
proceso muere al terminar la corrida diaria: sin estado en disco, cada día
empieza amnésico y solo puede elegir entre callarse siempre o gritar ante
cualquier parpadeo. Ninguna de las dos sirve.

Un fallo aislado casi nunca significa algo: la ADB Always Free se reinicia
sola para aplicar parches, la red de la VM se cae un minuto, el pool tarda de
más. Alertar por eso entrena al operador a ignorar la alerta, que es peor que
no tenerla. Dos fallos consecutivos, en cambio, son 48 horas sin conexión: ya
no es ruido, y quedan cinco días de margen antes del auto-stop a los 7 días de
inactividad. Ese es el punto donde avisar tiene valor y todavía hay tiempo de
actuar.

**Por qué el archivo vive en `data/`.** Es el único directorio montado como
volumen: el contenedor se ejecuta con `docker run --rm` y todo lo demás se
evapora al terminar. Ver `docker-compose.yml`.

Este módulo no notifica a nadie a propósito — solo decide *si hay que
notificar*. El canal es responsabilidad de `src/notifier/`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Nombre del archivo de estado dentro de `cfg.data_dir`.
ARCHIVO_ESTADO = "adb_keepalive.json"

# Cuántos fallos consecutivos hacen falta para alertar. Con una corrida diaria,
# 2 equivale a ~48 h sin conexión y deja 5 días de margen antes del auto-stop.
UMBRAL_FALLOS = 2


@dataclass(frozen=True)
class EstadoKeepalive:
    """Resultado de registrar una corrida del keepalive.

    `debe_alertar` es la única salida que le interesa a quien notifica: ya trae
    aplicado el umbral, de modo que el llamador no tiene que conocer la regla.
    """

    ok: bool
    fallos_consecutivos: int
    debe_alertar: bool
    ultimo_exito: str | None = None
    ultimo_error: str = ""

    @property
    def resumen(self) -> str:
        """Texto listo para un asunto de correo o una línea de log."""
        if self.ok:
            return "Keepalive de ADB: OK"
        visto = self.ultimo_exito or "nunca en este registro"
        return (
            f"Keepalive de ADB: {self.fallos_consecutivos} fallo(s) consecutivo(s). "
            f"Última conexión exitosa: {visto}."
        )


def ruta_estado(data_dir: str) -> str:
    return os.path.join(data_dir, ARCHIVO_ESTADO)


def _ahora() -> str:
    # UTC explícito: la VM corre en America/El_Salvador y los runners en UTC;
    # un timestamp sin zona sería imposible de comparar entre ambos.
    return datetime.now(timezone.utc).isoformat()


def _leer(ruta: str) -> dict:
    """Lee el estado previo. Un archivo ausente o corrupto equivale a 'sin historia'.

    No propaga: el keepalive nunca puede tumbar el pipeline, y menos por su
    propia contabilidad.
    """
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
        if not isinstance(datos, dict):
            raise ValueError("el estado no es un objeto JSON")
        return datos
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Estado del keepalive ilegible (%s); se reinicia el conteo: %s",
                       ruta, exc)
        return {}


def _escribir(ruta: str, datos: dict) -> None:
    """Escritura atómica: tmp + rename.

    Si el proceso muere a mitad de escritura, el archivo viejo sigue entero. Un
    JSON truncado costaría el conteo acumulado, que es justo lo que este módulo
    existe para conservar.
    """
    try:
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        tmp = f"{ruta}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ruta)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo guardar el estado del keepalive en %s: %s", ruta, exc)


def leer_estado(data_dir: str) -> EstadoKeepalive:
    """Consulta el estado persistido sin registrar una corrida nueva."""
    datos = _leer(ruta_estado(data_dir))
    fallos = int(datos.get("fallos_consecutivos", 0) or 0)
    return EstadoKeepalive(
        ok=fallos == 0,
        fallos_consecutivos=fallos,
        debe_alertar=fallos >= UMBRAL_FALLOS,
        ultimo_exito=datos.get("ultimo_exito"),
        ultimo_error=str(datos.get("ultimo_error") or ""),
    )


def registrar(data_dir: str, ok: bool, detalle: str = "") -> EstadoKeepalive:
    """Registra el resultado de un ping y devuelve la decisión de alerta.

    Un éxito **resetea el contador**: lo que importa es la racha en curso, no el
    total histórico de fallos. Una ADB que falla un día de cada tres está viva y
    no corre riesgo de auto-stop.

    Mientras la racha siga en el umbral o por encima, `debe_alertar` sigue en
    True todos los días. Es deliberado: no es ruido repetido sino una cuenta
    regresiva hacia el auto-stop, y el operador debe verla hasta que la arregle.
    """
    ruta = ruta_estado(data_dir)
    previo = _leer(ruta)
    ahora = _ahora()

    if ok:
        estado = EstadoKeepalive(
            ok=True,
            fallos_consecutivos=0,
            debe_alertar=False,
            ultimo_exito=ahora,
        )
        datos = {
            "fallos_consecutivos": 0,
            "ultimo_exito": ahora,
            "ultimo_intento": ahora,
            "ultimo_error": "",
        }
    else:
        fallos = int(previo.get("fallos_consecutivos", 0) or 0) + 1
        estado = EstadoKeepalive(
            ok=False,
            fallos_consecutivos=fallos,
            debe_alertar=fallos >= UMBRAL_FALLOS,
            ultimo_exito=previo.get("ultimo_exito"),
            ultimo_error=detalle,
        )
        datos = {
            "fallos_consecutivos": fallos,
            "ultimo_exito": previo.get("ultimo_exito"),
            "ultimo_intento": ahora,
            "ultimo_error": detalle,
        }

    _escribir(ruta, datos)

    if estado.debe_alertar:
        # ERROR y no WARNING: esta línea es la señal que hoy ve el operador en
        # `journalctl -u amalia.service`, hasta que se cablee el canal de alertas.
        logger.error("%s Requiere intervención: la ADB se auto-detiene a los 7 días "
                     "sin actividad.", estado.resumen)
    elif not ok:
        logger.warning("%s Aún por debajo del umbral (%d); no se alerta.",
                       estado.resumen, UMBRAL_FALLOS)

    return estado
