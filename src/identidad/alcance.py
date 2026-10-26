"""src.identidad.alcance — qué puede ver y hacer una persona (plan de identidad §1.1, P3/P4).

Se resuelve UNA vez por petición y se pasa hacia abajo: las funciones de `src.analytics`
reciben ya los ``clientes`` permitidos, nunca los eligen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.identidad import politica, repo


@dataclass(frozen=True)
class Alcance:
    usuario_id: str
    org_id: str
    roles: frozenset[str]
    clientes: frozenset[str]          # países (cliente_id) cuyos KPIs de negocio puede ver
    cambia_estado: frozenset[str]     # subconjunto de `clientes` donde puede cambiar el pipeline
    ve_operativo: bool                # /portal/operativo (KPIs 11-13)

    @property
    def ve_negocio(self) -> bool:
        return bool(self.clientes)

    @property
    def es_regional(self) -> bool:
        """Tiene un rol de organización de negocio: ve la vista regional y el selector de país."""
        return bool(self.roles & politica.ROLES_ORG_NEGOCIO)

    def puede_ver(self, cliente_id: str) -> bool:
        return cliente_id in self.clientes

    def puede_cambiar_estado(self, cliente_id: str) -> bool:
        return cliente_id in self.cambia_estado


def resolver_alcance(conn: Any, usuario_id: str) -> Alcance | None:
    """None si el usuario no existe o está dado de baja.

    - Un rol de organización de negocio ve todos los países activos, incluido uno dado de alta
      después. Quien además es de un país queda como regional (P3: la unión, sin rol nuevo).
    - ``admin_plataforma`` y ``operador`` solo ven lo operativo: sin membresía de negocio no ven
      ningún país (P3).
    - Cambian el estado ``director_comercial`` (todos los países), ``gerente_pais`` y
      ``equipo_ventas`` (el suyo); los demás regionales solo leen (P4).
    """
    usuario = repo.obtener_usuario(conn, usuario_id)
    if not usuario or not usuario["activo"]:
        return None

    roles_org = set(repo.roles_org(conn, usuario_id))
    membresias = repo.membresias_pais(conn, usuario_id)
    roles = roles_org | {rol for _, rol in membresias}

    clientes: set[str] = set()
    cambia: set[str] = set()
    if roles_org & politica.ROLES_ORG_NEGOCIO:
        todos = set(repo.clientes_activos(conn, usuario["org_id"]))
        clientes |= todos
        if "director_comercial" in roles_org:
            cambia |= todos
    for cliente_id, rol in membresias:
        clientes.add(cliente_id)
        if rol in politica.ROLES_CAMBIAN_ESTADO:
            cambia.add(cliente_id)

    return Alcance(
        usuario_id=usuario_id,
        org_id=usuario["org_id"],
        roles=frozenset(roles),
        clientes=frozenset(clientes),
        cambia_estado=frozenset(cambia),
        ve_operativo=bool(roles & politica.ROLES_OPERATIVOS),
    )
