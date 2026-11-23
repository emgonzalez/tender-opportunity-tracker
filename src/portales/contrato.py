"""Contratos compartidos por los portales de compras: errores, extractor y localizador de pliegos.

Diseño en `docs/arquitectura/portales.md` §2. Este módulo no importa nada de `src.*` en tiempo de
ejecución (solo para anotaciones): lo importan `src.portales`, los extractores y el drill-down sin ciclos
y sin cargar Playwright.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.config import Config
    from src.portales import Portal
    from src.scraper.models import Proceso

# Tope del `detalle` de un error: es texto para ops, nunca un cuerpo de respuesta completo.
MAX_DETALLE = 500


class ErrorPortal(RuntimeError):
    """Fallo de un portal concreto. Ningún `ErrorPortal` sale del bucle por portal (§5).

    `detalle` es texto para ops: jamás secretos ni cuerpos completos; se recorta a `MAX_DETALLE`.
    """

    def __init__(self, portal_id: str, mensaje: str, *, detalle: str = "") -> None:
        super().__init__(f"{portal_id}: {mensaje}" if portal_id else mensaje)
        self.portal_id = portal_id
        self.mensaje = mensaje
        self.detalle = (detalle or "")[:MAX_DETALLE]


class CredencialesFaltantes(ErrorPortal):
    """`requiere_login` y faltan las credenciales del portal (p. ej. COMPRASAL_EMAIL/PASSWORD)."""


class SesionInvalida(ErrorPortal):
    """Login agotado (los 3 reintentos con TOTP)."""


class PortalNoDisponible(ErrorPortal):
    """Red, timeout o 5xx tras los reintentos: transitorio."""


class ContratoRoto(ErrorPortal):
    """Respuesta que no cumple el contrato (422, esquema, tipo). No se reintenta."""


class BloqueoAcceso(ContratoRoto):
    """WAF: HTML o texto con 200 donde se esperaba JSON. No se reintenta: reintentar un bloqueo es evadirlo."""


@dataclass(frozen=True)
class DocumentoPliego:
    """Un adjunto candidato a documento base del proceso."""
    nombre: str                     # nombre visible (SICOP: clientFileNm)
    url: str                        # URL de descarga
    extension: str = ""             # "pdf" | "docx" | "xlsx" | …, en minúsculas y sin punto
    tamano_bytes: int | None = None
    categoria: str = ""             # SICOP: fileTypeNm («Documentos del Pliego de condiciones», …)
    puntaje: int = 0                # lo pone el localizador; mayor = mejor candidato


@runtime_checkable
class LocalizadorPliego(Protocol):
    """Completa un proceso y localiza/descarga su documento base. Se usa como `async with`."""

    def __init__(self, config: Config, portal: Portal) -> None: ...

    async def __aenter__(self) -> LocalizadorPliego:
        """Abre UN navegador (o sesión) para todos los procesos."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Lo cierra siempre."""
        ...

    async def completar(self, proceso: Proceso) -> None:
        """Rellena lo que el listado no trajo (fecha_limite, monto/moneda, tipo_actividad, datos_portal).

        Muta `proceso`. NUNCA lanza: si falla, deja todo como estaba."""
        ...

    async def documentos(self, proceso: Proceso) -> list[DocumentoPliego]:
        """Ordenados del mejor candidato al peor. Lista vacía si no hay adjuntos."""
        ...

    async def texto_base(self, proceso: Proceso) -> str:
        """Texto del documento base que el portal publica sin adjunto. COMPRASAL: ""."""
        ...

    async def descargar(self, doc: DocumentoPliego, destino_dir: str, max_bytes: int) -> str | None:
        """Ruta del archivo guardado o None. Respeta `max_bytes` antes de bajar el cuerpo."""
        ...


@runtime_checkable
class TransporteJSON(Protocol):
    """Lo que ofrece el navegador anónimo. Se inyecta para probar sin red."""

    async def json(self, metodo: str, url: str, cuerpo: Any | None = None) -> Any: ...

    async def binario(self, url: str, max_bytes: int) -> bytes | None: ...
