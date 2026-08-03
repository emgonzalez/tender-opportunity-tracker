"""src.drilldown.downloader — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md."""

import logging
import os
import requests
import tenacity
from uuid import uuid4

logger = logging.getLogger(__name__)


def _is_transient_download_error(exc: BaseException) -> bool:
    """Predicado de reintento: True solo ante errores transitorios.

    Transitorios (se reintentan): timeout de red, error de conexión, o
    HTTPError con status_code >= 500 (5xx del servidor).

    NO transitorios (se propagan de inmediato, sin reintento): HTTPError con
    status_code 4xx (ej. 404 — el pliego no existe, reintentar no ayuda), y
    cualquier otra excepción.
    """
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is not None and response.status_code >= 500
    return False


@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=8),
    retry=tenacity.retry_if_exception(_is_transient_download_error),
    reraise=True,
)
def _download_pliego_attempt(url: str, dest_dir: str, max_bytes: int) -> str | None:
    """Un intento de descarga (envuelto en reintentos por `download_pliego`)."""
    # 2. Hacer request con stream=True
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()

        # 3. Validar Content-Length si existe
        content_length = resp.headers.get("Content-Length")
        if content_length:
            content_length_bytes = int(content_length)
            if content_length_bytes > max_bytes:
                logger.warning(
                    f"DESCARTADO_TAMANO: {url} "
                    f"({content_length_bytes} bytes > {max_bytes} bytes)"
                )
                return None

        # 4. Generar nombre de archivo
        filename = f"{uuid4().hex}.pdf"
        filepath = os.path.join(dest_dir, filename)

        # 5. Escribir en chunks
        bytes_written = 0
        try:
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:  # filter out keep-alive new chunks
                        f.write(chunk)
                        bytes_written += len(chunk)

                        # 6. Validar durante escritura si no había Content-Length
                        if bytes_written > max_bytes:
                            logger.warning(
                                f"DESCARTADO_TAMANO: {url} "
                                f"({bytes_written} bytes > {max_bytes} bytes)"
                            )
                            f.close()
                            os.remove(filepath)
                            return None
        except Exception:
            # Si algo falla durante la escritura, limpiar
            if os.path.exists(filepath):
                os.remove(filepath)
            raise

        # 7. Retornar ruta absoluta
        return os.path.abspath(filepath)


def download_pliego(url: str, dest_dir: str, max_mb: int) -> str | None:
    """
    Descarga un PDF desde una URL en streaming, con límite de tamaño.

    Características:
    - Streaming sin cargar el PDF completo en RAM
    - Validación de Content-Length antes de descargar (si está disponible)
    - Aborto durante la descarga si se excede el límite de tamaño
    - Limpieza de archivos parciales en caso de error
    - Reintentos automáticos (hasta 3 intentos, backoff exponencial 2s/4s/8s)
      SOLO ante errores transitorios (timeout, error de conexión, HTTP 5xx).
      Los errores HTTP 4xx (ej. 404) no se reintentan, se propagan de inmediato.

    Args:
        url: URL del archivo a descargar
        dest_dir: Directorio destino para guardar el archivo
        max_mb: Tamaño máximo permitido en MB

    Returns:
        Ruta absoluta del archivo descargado, o None si se descartó por tamaño

    Raises:
        requests.RequestException: Si hay errores de conexión o HTTP tras
            agotar los reintentos (o de inmediato si el error no es transitorio).
    """
    # 1. Crear directorio si no existe
    os.makedirs(dest_dir, exist_ok=True)

    max_bytes = max_mb * 1024 * 1024

    return _download_pliego_attempt(url, dest_dir, max_bytes)
