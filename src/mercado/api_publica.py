"""src.mercado.api_publica — cliente del registro público de adjudicaciones.

``GET /api/v1/publico/obtener/procesos/publicos`` es el "Registro de
Adjudicaciones" público de COMPRASAL: **no requiere sesión ni JWT**, a
diferencia de todo lo demás que consume este repo. Verificado en vivo
(2026-09-27), con `curl` sin credenciales:

- Pagina con ``pagination=true&page=N&per_page=N``; el **total real viaja en
  la cabecera** ``total_rows`` (no en el body) — mismo patrón que
  ``/api/v1/procesos/proveedores`` (ver ``scraper/extractor.py``).
- Probado hasta ``per_page=1000`` sin tope aparente.
- Filtra de verdad (no los ignora en silencio): ``id_institucion=<id>`` bajó
  ``total_rows`` de 131 775 a 256, con el 100 % de las filas de esa
  institución; ``modalidad=<id>`` (catálogo en
  ``/api/v1/publico/tipos/contrato``) bajó a 61 358; ``nombre=<texto>``
  (matchea ``codigo_proceso`` **o** ``nombre_proceso``) bajó a 5 087;
  combinados, a 4. Otros nombres de parámetro probados y descartados por no
  tener efecto: ``institucion``, ``institucion_id``, ``codigo_proceso``,
  ``codigo``, ``id_forma_contratacion``, ``forma_contratacion``, ``search``,
  ``buscar``, ``nombre_proceso``, ``fecha_inicio``/``fecha_fin``, ``q``,
  ``texto``, ``termino``, ``consulta``.

**Granularidad — importante:** una fila es un renglón/lote de la
adjudicación, NO un proceso completo. Un mismo ``codigo_proceso`` puede tener
docenas de filas, cada una con su propio ``monto`` y ``proveedor`` (varios
ganadores posibles por proceso, uno por lote). Verificado: un código con 29
filas, las 29 con pares (monto, proveedor) únicos.

Es HTTP simple con ``requests`` (ya es dependencia del proyecto): a
diferencia del resto del scraper, este endpoint no necesita Playwright.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

BASE = "https://www.comprasal.gob.sv"
ENDPOINT = f"{BASE}/api/v1/publico/obtener/procesos/publicos"
CATALOGO_MODALIDADES_ENDPOINT = f"{BASE}/api/v1/publico/tipos/contrato"

# Probado sin tope aparente hasta este tamaño (2026-09-27).
DEFAULT_PER_PAGE = 1000
TIMEOUT_S = 30

# No se observó ningún límite de tasa en las pruebas, pero tampoco se probó a
# volumen sostenido (~132 páginas para un barrido completo): backoff
# prudente igual, mejor pecar de lento que de tumbar el endpoint público de
# un tercero.
PAUSA_ENTRE_PAGINAS_S = 0.3


def _pagina(page: int, per_page: int = DEFAULT_PER_PAGE, **filtros: Any) -> tuple[list[dict], int]:
    """Pide una página del registro. Devuelve ``(filas, total_rows)``.

    ``total_rows`` sale de la CABECERA de la respuesta, no del body (el body
    solo trae ``{"data": [...], "message": "..."}`` — verificado).
    """
    params: dict[str, Any] = {"pagination": "true", "page": page, "per_page": per_page}
    params.update({k: v for k, v in filtros.items() if v is not None})

    r = requests.get(ENDPOINT, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    body = r.json()
    filas = body.get("data") or []
    total = int(r.headers.get("total_rows", "0") or "0")
    return filas, total


def paginar_todo(
    per_page: int = DEFAULT_PER_PAGE,
    id_institucion: int | None = None,
    modalidad: int | None = None,
    nombre: str | None = None,
) -> Iterator[list[dict]]:
    """Recorre TODAS las páginas del registro (o de un filtro) y entrega una
    página (lista de filas) por vez, para no acumular 131 775+ filas en RAM
    de una sola vez.

    Sin filtros, recorre el registro **completo** (decisión del usuario:
    gobierno completo, no acotado a instituciones ya monitoreadas — ver el
    plan del portal, §3.5).

    Nunca lanza a mitad de un barrido por un fallo transitorio de red en una
    sola página: eso queda a cargo del llamador (que ya comitea por página,
    ver ``mercado.service.sincronizar_mercado``) — esta función sí propaga
    errores de HTTP genuinos (4xx/5xx), no los esconde.
    """
    page = 1
    total: int | None = None
    vistas = 0
    while True:
        filas, total_actual = _pagina(
            page, per_page, id_institucion=id_institucion, modalidad=modalidad, nombre=nombre,
        )
        if total is None:
            total = total_actual
            logger.info(
                "Registro de adjudicaciones: %d fila(s) totales (id_institucion=%s modalidad=%s nombre=%s)",
                total, id_institucion, modalidad, nombre,
            )
        if not filas:
            break

        vistas += len(filas)
        yield filas

        if total and vistas >= total:
            break
        page += 1
        time.sleep(PAUSA_ENTRE_PAGINAS_S)

    logger.info("Barrido completo: %d fila(s) recorridas.", vistas)


def obtener_catalogo_modalidades() -> list[dict]:
    """Catálogo `id → nombre` de modalidades/métodos de contratación.

    Usado para resolver el parámetro `modalidad` a un id conocido. Público,
    sin sesión. Ejemplo de fila: ``{"id": 12, "nombre": "Comparación de
    precios", "codigo": "CP", "compra_tradicional": true}``.
    """
    r = requests.get(CATALOGO_MODALIDADES_ENDPOINT, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json() or []
