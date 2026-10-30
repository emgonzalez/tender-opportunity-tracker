"""src.identidad.alcance — qué puede ver y hacer una persona (plan de identidad §1.1, P3/P4).

Se resuelve UNA vez por petición y se pasa hacia abajo: las funciones de `src.analytics`
reciben ya los ``clientes`` permitidos, nunca los eligen.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from src.identidad import politica, repo


@dataclass(frozen=True)
class Alcance:
    usuario_id: str
    org_id: str
    roles: frozenset[str]
    clientes: frozenset[str]          # países (cliente_id) cuyos KPIs de negocio puede ver
    cambia_estado: frozenset[str]     # subconjunto de `clientes` donde puede cambiar el pipeline
    ve_operativo: bool                # /portal/operations (KPIs 11-13)
    simulado: str | None = None       # E10: rol que el admin está viendo como; None = alcance real
    solo_marcas: frozenset[str] = frozenset()  # E14: vacío = sin restricción; si no, solo oportunidades de esas marcas

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


def _alcance_desde(usuario_id: str, org_id: str, roles_org: Iterable[str],
                   membresias: Iterable[tuple[str, str]], activos: Iterable[str]) -> Alcance:
    """El único constructor de alcance, puro: lo usan el real y el simulado, así no divergen.

    - Un rol de organización de negocio ve todos los países ``activos``, incluido uno dado de alta
      después. Quien además es de un país queda como regional (P3: la unión, sin rol nuevo).
    - ``admin_plataforma`` y ``operador`` solo ven lo operativo: sin membresía de negocio no ven
      ningún país (P3).
    - Cambian el estado ``director_comercial`` (todos los países, confirmado en P4), ``gerente_pais`` y
      ``equipo_ventas`` (el suyo); los demás regionales solo leen (P4).
    """
    roles_org = set(roles_org)
    membresias = list(membresias)
    roles = roles_org | {rol for _, rol in membresias}

    clientes: set[str] = set()
    cambia: set[str] = set()
    if roles_org & politica.ROLES_ORG_NEGOCIO:
        todos = set(activos)
        clientes |= todos
        if "director_comercial" in roles_org:
            cambia |= todos
    for cliente_id, rol in membresias:
        clientes.add(cliente_id)
        if rol in politica.ROLES_CAMBIAN_ESTADO:
            cambia.add(cliente_id)

    # E14: solo se restringe quien NO tiene ningún otro rol que dé datos de negocio (gana el permisivo, como la
    # unión de P3). Los roles técnicos no dan datos de negocio, así que no levantan la restricción.
    restringidos = {politica.ROLES_SOLO_MARCA[r] for r in roles if r in politica.ROLES_SOLO_MARCA}
    # Una membresía de país NO la levanta: `solo_marcas` es único para todo el alcance y levantarla en un país la
    # levantaría en todos (fuga de los demás países). Solo la levanta un rol de organización sin restricción.
    sin_restriccion = roles_org - set(politica.ROLES_SOLO_MARCA) - politica.ROLES_TECNICOS
    solo_marcas = frozenset(restringidos) if restringidos and not sin_restriccion else frozenset()

    return Alcance(
        usuario_id=usuario_id,
        org_id=org_id,
        roles=frozenset(roles),
        clientes=frozenset(clientes),
        cambia_estado=frozenset(cambia),
        ve_operativo=bool(roles & politica.ROLES_OPERATIVOS),
        solo_marcas=solo_marcas,
    )


def resolver_alcance(conn: Any, usuario_id: str) -> Alcance | None:
    """None si el usuario no existe o está dado de baja. Las reglas viven en `_alcance_desde`."""
    usuario = repo.obtener_usuario(conn, usuario_id)
    if not usuario or not usuario["activo"]:
        return None

    roles_org = set(repo.roles_org(conn, usuario_id))
    membresias = repo.membresias_pais(conn, usuario_id)  # ya solo países activos
    activos = repo.clientes_activos(conn, usuario["org_id"]) if roles_org & politica.ROLES_ORG_NEGOCIO else []
    return _alcance_desde(usuario_id, usuario["org_id"], roles_org, membresias, activos)


def puede_ver_como(real: Alcance | None) -> bool:
    """Solo un alcance REAL (no ya simulado) con un rol de `ROLES_VER_COMO`."""
    return real is not None and real.simulado is None and bool(real.roles & politica.ROLES_VER_COMO)


def alcance_simulado(conn: Any, real: Alcance, rol: str, cliente: str | None = None) -> Alcance | None:
    """Lo que vería alguien con ``rol`` (y ``cliente`` si es de país) en la organización del admin.

    None si ``real`` no puede simular, si ``rol`` no es simulable, si un rol de país llega sin un
    cliente activo de la org, o si un rol de organización llega CON cliente (se rechaza, no se
    ignora). Conserva ``usuario_id`` y ``org_id`` reales: toda traza apunta al admin.
    """
    if not puede_ver_como(real) or rol not in politica.ROLES_SIMULABLES:
        return None
    activos = set(repo.clientes_activos(conn, real.org_id))
    if rol in politica.ROLES_PAIS:
        if not cliente or cliente not in activos:
            return None
        base = _alcance_desde(real.usuario_id, real.org_id, (), [(cliente, rol)], activos)
    else:
        if cliente is not None or (rol in politica.ROLES_ORG_NEGOCIO and not activos):
            return None  # un rol de negocio sin países no ve nada: se ignora la simulación, no se encierra al admin
        base = _alcance_desde(real.usuario_id, real.org_id, {rol}, (), activos)
    return replace(base, simulado=rol)
