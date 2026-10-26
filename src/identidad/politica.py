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
    "MFA_ENROLADO",           # el TOTP quedó confirmado y se emitieron los códigos de recuperación
    "MFA_RECUPERACION_USADA",  # entró con un código de recuperación (perdió el teléfono)
})


# Roles (P2, palabras completas). Los mismos 11 del CHECK `ck_membresia_rol`.
ROLES_ORG_NEGOCIO = frozenset({
    "ceo", "director_estrategia", "director_comercial", "gerente_marca_oracle",
    "gerente_regional_comercial_oracle", "director_servicios_oracle", "admin_organizacion",
})
ROLES_TECNICOS = frozenset({"admin_plataforma", "operador"})
ROLES_ORG = ROLES_ORG_NEGOCIO | ROLES_TECNICOS      # viven en `membresia_org`
ROLES_PAIS = frozenset({"gerente_pais", "equipo_ventas"})  # viven en `membresia`, con cliente
ROLES = ROLES_ORG | ROLES_PAIS
ROLES_OPERATIVOS = ROLES_TECNICOS                    # ven /portal/operativo (KPIs 11-13)
ROLES_CAMBIAN_ESTADO = frozenset({"director_comercial", "gerente_pais", "equipo_ventas"})  # P4

# Roles con segundo factor TOTP obligatorio (P4). El resto entra con el código del correo.
ROLES_MFA = frozenset({"admin_plataforma", "admin_organizacion"})
TTL_SESION_MFA_PENDIENTE_MIN = 10   # tiempo para completar el TOTP tras el código del correo

# Control de fuerza bruta (plan de identidad, T1).
MAX_INTENTOS_CODIGO = 5
MAX_CODIGOS_POR_HORA = 3


class AccionNoAuditable(ValueError):
    """Una acción que no está en `ACCIONES_AUDITADAS`: un error de quien llama, no un dato."""
