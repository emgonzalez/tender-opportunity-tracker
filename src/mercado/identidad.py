"""src.mercado.identidad — quién es MULTICOMPUTOS en COMPRASAL.

Verificado en vivo el 2026-09-27, contra ``GET /api/v1/proveedores`` con una
sesión real de proveedor (no adivinado ni pedido por texto al usuario):

    id_proveedor:              20371
    nombre (razón social):     "MULTICOMPUTOS DE EL SALVADOR, S.A.S DE C.V"
    nombre_comercial:          "MULTICOMPUTOS EL SALVADOR"

El match contra el registro de adjudicaciones (``adjudicacion_item.raw`` trae
un objeto ``proveedor`` con ``id``/``id_proveedor``) es por **ID exacto**, no
por comparación de texto: no hace falta normalizar razón social ni mantener
una lista de alias, que es justo la clase de fragilidad que esto evita.
"""
from __future__ import annotations

ID_PROVEEDOR_MULTICOMPUTOS = 20371

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
