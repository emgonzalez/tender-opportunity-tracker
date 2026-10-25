"""Qué se audita (P6 del plan de identidad, `docs/planes/portal-fase3-identidad.md` §9).

Se audita el acceso y los cambios, NO cada vista consultada: es mucho volumen para una tabla de 24 meses
y el portal es de solo lectura sobre datos públicos. Si hiciera falta la traza de vistas, se enciende por
rol sin cambiar el esquema (`auditoria.accion` no tiene CHECK).
"""
from __future__ import annotations

ACCIONES_AUDITADAS = frozenset({
    "LOGIN_OK",
    "LOGIN_FALLIDO",          # código equivocado
    "CODIGO_VENCIDO",
    "SESION_REVOCADA",        # cierre de sesión
    "ESTADO_CAMBIADO",        # pipeline comercial (el detalle vive en `oportunidad_evento`)
    "USUARIO_ALTA",
    "USUARIO_BAJA",
    "MEMBRESIA_ALTA",
    "MEMBRESIA_BAJA",
    "ROL_CAMBIADO",
    "ACCESO_FUERA_DE_ALCANCE",  # 404 por `cliente_id` ajeno: la señal de abuso real
})


class AccionNoAuditable(ValueError):
    """Una acción que no está en `ACCIONES_AUDITADAS`: un error de quien llama, no un dato."""
