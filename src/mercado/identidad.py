"""src.mercado.identidad — quién es MULTICOMPUTOS en cada portal de compras.

`IDS_PROPIOS` mapea `portal_id` → ids de proveedor de la organización en ese portal
(`docs/arquitectura/portales.md` §1 y §10.4): los ids identifican a la ORGANIZACIÓN, no al portal,
por eso viven acá y no en `src.portales.Portal`. Cuando haya más de una organización, el mapa se muda
a `cliente_config`.

- ``comprasal_sv``: ``20371`` (ver abajo).
- ``sicop_cr``: ``3102813498`` (MULTICOMPUTOS ADVANCED TECHNOLOGIES SRL, cédula jurídica) y
  ``9000018410`` (MULTICOMPUTOS S.R.L.). Ambas confirmadas por Emilio como de Multicómputos
  (`portales.md` §12, 2026-11-23).

COMPRASAL:

Verificado en vivo el 2026-09-27, contra ``GET /api/v1/proveedores`` con una
sesión real de proveedor (no adivinado ni pedido por texto al usuario):

    id_proveedor:              20371
    nombre (razón social):     "MULTICOMPUTOS DE EL SALVADOR, S.A.S DE C.V"
    nombre_comercial:          "MULTICOMPUTOS EL SALVADOR"

El match contra el registro de adjudicaciones (``adjudicacion_item.datos_crudos`` trae
un objeto ``proveedor`` con ``id``/``id_proveedor``) es por **ID exacto**, no
por comparación de texto: no hace falta normalizar razón social ni mantener
una lista de alias, que es justo la clase de fragilidad que esto evita.
"""
from __future__ import annotations

ID_PROVEEDOR_MULTICOMPUTOS = 20371

IDS_PROPIOS: dict[str, frozenset[str]] = {
    "comprasal_sv": frozenset({"20371"}),
    "sicop_cr": frozenset({"3102813498", "9000018410"}),
}

# Documentado por completitud (no se usan para el match, solo para mostrar
# en reportes/depuración): confirmados el mismo día que el id de arriba.
NOMBRE_MULTICOMPUTOS = "MULTICOMPUTOS DE EL SALVADOR, S.A.S DE C.V"
NOMBRE_COMERCIAL_MULTICOMPUTOS = "MULTICOMPUTOS EL SALVADOR"


def id_proveedor_de(item: dict) -> int | None:
    """Extrae el ``id_proveedor`` de una fila del registro de adjudicaciones.

    Tolera que la API use a veces ``id`` y a veces ``id_proveedor`` dentro
    del objeto ``proveedor`` (ambos se vieron con el mismo valor en las
    filas capturadas en vivo).
    """
    proveedor = item.get("proveedor")
    if not isinstance(proveedor, dict):
        return None
    return proveedor.get("id_proveedor", proveedor.get("id"))


def es_nuestro(item: dict) -> bool:
    """True si la fila de adjudicación (del registro público) es de MULTICOMPUTOS."""
    return id_proveedor_de(item) == ID_PROVEEDOR_MULTICOMPUTOS


def es_propio(portal_id: str, id_proveedor: object) -> bool:
    """True si `id_proveedor` es de MULTICOMPUTOS en `portal_id`.

    Comparación de texto **exacta** (sin recortar espacios ni ceros): un id numérico se compara por su
    `str` (20371 → "20371"). Un portal desconocido o un id ausente dan False.
    """
    if id_proveedor is None or isinstance(id_proveedor, bool):
        return False
    return str(id_proveedor) in IDS_PROPIOS.get(portal_id, frozenset())
