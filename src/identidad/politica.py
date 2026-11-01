"""Qué se audita (P6 del plan de identidad, `docs/planes/portal-fase3-identidad.md` §9).

Se audita el acceso y los cambios, NO cada vista consultada: es mucho volumen para una tabla de 24 meses
y el portal es de solo lectura sobre datos públicos. Si hiciera falta la traza de vistas, se enciende por
rol sin cambiar el esquema (`auditoria.accion` no tiene CHECK).
"""
from __future__ import annotations

ACCIONES_AUDITADAS = frozenset({
    "TERMINOS_SEMBRADOS", "TERMINO_PROPUESTO", "TERMINO_ACTIVADO", "TERMINO_RECHAZADO",
    "CALIBRACION_REGISTRADA", "CALIBRACION_PROMOVIDA", "CALIBRACION_ARCHIVADA",
    "EJEMPLO_ETIQUETADO", "EJEMPLO_DESACTIVADO",
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
    "VER_COMO_INICIO",        # E10: admin_plataforma eligió una vista simulada (recurso `rol=X;cliente=Y`)
    "VER_COMO_FIN",           # E10: volvió a su vista real
    "RESPONSABLE_ASIGNADO",   # E11: asignó o quitó el responsable de una oportunidad (recurso `cliente=;proceso=;responsable=`)
    "DESCARTADO_RECUPERADO",  # E11: rescató un proceso que AMALIA descartó (crea la oportunidad NUEVO)
    # Fase G (administración en el portal): cada CRUD deja su fila; `recurso` dice sobre qué.
    "USUARIO_REACTIVADO",
    "USUARIO_EDITADO",
    "INVITACION_CREADA",
    "INVITACION_REVOCADA",
    "INVITACION_USADA",
    "MFA_REINICIADO",         # un admin_plataforma borró el TOTP de otro admin
    "PERFIL_VERSION_CREADA",  # nueva versión de `cliente_config`
    "PERFIL_VERSION_REVERTIDA",
    "CLIENTE_CREADO",
    "CLIENTE_ACTIVADO",
    "CLIENTE_DESACTIVADO",
    "PORTAL_EDITADO",         # portal de compras o `cliente_portal`
    "CUOTA_EDITADA",
    "ORGANIZACION_EDITADA",
    "SESION_REVOCADA_POR_ADMIN",  # distinta de SESION_REVOCADA: quien cierra no es el dueño de la sesión
})


# Roles (P2, palabras completas). Los mismos 11 del CHECK `ck_membresia_rol`.
ROLES_ORG_NEGOCIO = frozenset({
    "ceo", "director_estrategia", "director_comercial", "gerente_marca_oracle",
    "gerente_regional_comercial_oracle", "director_servicios_oracle", "admin_organizacion",
})
ROLES_TECNICOS = frozenset({"admin_plataforma", "operador"})
# E14: roles cuyas vistas muestran solo oportunidades de una marca (clave de `relevancia.MARCAS_RESTRINGIBLES`).
ROLES_SOLO_MARCA = {
    "gerente_marca_oracle": "oracle",
    "gerente_regional_comercial_oracle": "oracle",
    "director_servicios_oracle": "oracle",
}
ROLES_ORG = ROLES_ORG_NEGOCIO | ROLES_TECNICOS      # viven en `membresia_org`
ROLES_PAIS = frozenset({"gerente_pais", "equipo_ventas"})  # viven en `membresia`, con cliente
ROLES = ROLES_ORG | ROLES_PAIS
ROLES_OPERATIVOS = ROLES_TECNICOS                    # ven /portal/operations (KPIs 11-13)
ROLES_CAMBIAN_ESTADO = frozenset({"director_comercial", "gerente_pais", "equipo_ventas"})  # P4

# Administración (Fase G): quién puede escribir configuración desde el portal. Ambos exigen MFA (`ROLES_MFA`).
# `admin_organizacion` administra lo de su organización; lo global (organizaciones, portales, cuotas) es solo de
# `admin_plataforma` (`Alcance.es_plataforma`). El modo «ver como» nunca administra.
ROLES_ADMIN = frozenset({"admin_organizacion", "admin_plataforma"})

# Roles con segundo factor TOTP obligatorio (P4). El resto entra con el código del correo.
ROLES_MFA = frozenset({"admin_plataforma", "admin_organizacion"})
TTL_SESION_MFA_PENDIENTE_MIN = 10   # tiempo para completar el TOTP tras el código del correo

# «Ver como» (E10): alcance simulado, de solo lectura, dentro de la sesión del admin real.
ROLES_VER_COMO = frozenset({"admin_plataforma"})     # quién puede simular (exige MFA)
ROLES_SIMULABLES = (ROLES_ORG - {"admin_plataforma"}) | ROLES_PAIS  # admin_plataforma es «Mi vista»
TTL_VER_COMO_MIN = 60               # fijo desde que se elige, no deslizante

# Control de fuerza bruta (plan de identidad, T1).
MAX_INTENTOS_CODIGO = 5
MAX_CODIGOS_POR_HORA = 3


class AccionNoAuditable(ValueError):
    """Una acción que no está en `ACCIONES_AUDITADAS`: un error de quien llama, no un dato."""
