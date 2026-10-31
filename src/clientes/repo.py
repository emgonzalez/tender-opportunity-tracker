"""SQL y reglas de los perfiles de cliente (`cliente` + `cliente_config`), sobre una `conn` ya abierta.

Nada acá hace commit: quien abrió la conexión (`db.connection`) decide. Toda versión nueva pasa por
`registrar_version`, que solo inserta si el hash cambió y nunca borra: `revertir` es una versión más.
"""
from __future__ import annotations

import copy
import logging
from typing import Optional

from src.classifier.alias_clientes import id_canonico
from src.classifier.client_profile import _a_dict, _parse_profile, hash_contenido, registrar_version

logger = logging.getLogger(__name__)

ORG_DEFAULT = "multicomputos"      # el de la semilla (scripts/semilla_esquema_v2.py)
PORTAL_DEFAULT = "comprasal_sv"


class ErrorClientes(Exception):
    """Fallo esperable de la CLI (cliente inexistente, JSON inválido…): se informa sin traza."""


class ConflictoVersion(ErrorClientes):
    """La versión activa cambió desde que quien edita la leyó (control de concurrencia optimista del portal)."""


def validar(datos: object, cliente_id: str) -> dict:
    """Valida un perfil con el mismo parser que usa la corrida; devuelve el dict si es bueno."""
    if not isinstance(datos, dict):
        raise ErrorClientes("el perfil debe ser un objeto JSON")
    try:
        perfil = _parse_profile(datos, f"cli:{cliente_id}")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ErrorClientes(f"perfil inválido ({type(exc).__name__}: {exc})") from exc
    if id_canonico(perfil.client_id) != id_canonico(cliente_id):
        raise ErrorClientes(f"client.id {perfil.client_id!r} no coincide con {cliente_id!r}")
    return datos


def existe(conn, cliente_id: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM cliente WHERE cliente_id = :c", c=cliente_id)
        (n,) = cur.fetchone()
    finally:
        cur.close()
    return n > 0


def _existe_org(conn, org_id: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM organizacion WHERE org_id = :o", o=org_id)
        (n,) = cur.fetchone()
    finally:
        cur.close()
    return n > 0


def listar(conn, org_id: Optional[str] = None) -> list[dict]:
    """Los clientes con su versión activa; con `org_id`, solo los de esa organización."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT c.cliente_id, c.org_id, c.nombre, c.activo, "
            "(SELECT MAX(cc.version) FROM cliente_config cc WHERE cc.cliente_id = c.cliente_id AND cc.activa = 1) "
            "FROM cliente c " + ("WHERE c.org_id = :o " if org_id is not None else "") + "ORDER BY c.cliente_id",
            **({"o": org_id} if org_id is not None else {}),
        )
        filas = cur.fetchall()
    finally:
        cur.close()
    return [{"cliente_id": f[0], "org_id": f[1], "nombre": f[2], "activo": bool(f[3]), "version": f[4]}
            for f in filas]


def activo(conn, cliente_id: str) -> Optional[tuple[int, dict]]:
    """(versión, contenido) de la versión activa; None si el cliente no tiene configuración."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT version, JSON_SERIALIZE(contenido RETURNING CLOB) FROM cliente_config "
            "WHERE cliente_id = :c AND activa = 1 ORDER BY version DESC", c=cliente_id,
        )
        fila = cur.fetchone()
    finally:
        cur.close()
    if not fila:
        return None
    contenido = _a_dict(fila[1])
    return (int(fila[0]), contenido) if contenido is not None else None


def requerir_activo(conn, cliente_id: str) -> tuple[int, dict]:
    if not existe(conn, cliente_id):
        raise ErrorClientes(f"el cliente {cliente_id!r} no existe")
    previo = activo(conn, cliente_id)
    if previo is None:
        raise ErrorClientes(f"el cliente {cliente_id!r} no tiene versión activa de configuración")
    return previo


def versiones(conn, cliente_id: str) -> list[dict]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT version, activa, creado_por, creado_en, JSON_SERIALIZE(contenido RETURNING CLOB) "
            "FROM cliente_config WHERE cliente_id = :c ORDER BY version DESC", c=cliente_id,
        )
        filas = cur.fetchall()
    finally:
        cur.close()
    salida = []
    for numero, activa, por, cuando, crudo in filas:
        contenido = _a_dict(crudo)
        salida.append({"version": int(numero), "activa": bool(activa), "creado_por": por, "creado_en": cuando,
                       "hash": hash_contenido(contenido)[:12] if contenido is not None else "?"})
    return salida


def version(conn, cliente_id: str, numero: int) -> Optional[dict]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT JSON_SERIALIZE(contenido RETURNING CLOB) FROM cliente_config "
            "WHERE cliente_id = :c AND version = :v", c=cliente_id, v=numero,
        )
        fila = cur.fetchone()
    finally:
        cur.close()
    return _a_dict(fila[0]) if fila else None


def perfil_nuevo(cliente_id: str, nombre: str, portal: str = PORTAL_DEFAULT) -> dict:
    """Perfil mínimo válido para un cliente recién creado (sin catálogo ni destinatarios)."""
    return {
        "client": {"id": cliente_id, "name": nombre, "active": True, "portal": portal},
        "catalog": {"categories": [], "inclusion_signals": [], "exclusion_signals": [], "tag_taxonomy": []},
        "rules": {"context_note": ""},
        "notification": {"email_to": [], "system_admins_email": []},
    }


def crear(conn, cliente_id: str, nombre: str, portal: str = PORTAL_DEFAULT, org_id: str = ORG_DEFAULT,
          creado_por: str = "cli") -> int:
    """Inserta `cliente` y la versión 1 de `cliente_config`. Devuelve la versión."""
    if existe(conn, cliente_id):
        raise ErrorClientes(f"el cliente {cliente_id!r} ya existe")
    if not _existe_org(conn, org_id):
        raise ErrorClientes(f"la organización {org_id!r} no existe")
    datos = validar(perfil_nuevo(cliente_id, nombre, portal), cliente_id)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO cliente (cliente_id, org_id, nombre, activo) VALUES (:c, :o, :n, 1)",
            c=cliente_id, o=org_id, n=nombre,
        )
    finally:
        cur.close()
    return int(registrar_version(conn, cliente_id, datos, creado_por) or 1)


def _fijar_columna_activo(conn, cliente_id: str, valor: bool) -> None:
    cur = conn.cursor()
    try:
        cur.execute("UPDATE cliente SET activo = :a WHERE cliente_id = :c", a=1 if valor else 0, c=cliente_id)
    finally:
        cur.close()


def importar(conn, cliente_id: str, datos: object, creado_por: str) -> Optional[int]:
    """Valida y registra el perfil. None si no hay cambios. `cliente.activo` sigue a `client.active`."""
    datos = validar(datos, cliente_id)
    if not existe(conn, cliente_id):
        raise ErrorClientes(f"el cliente {cliente_id!r} no existe: crearlo primero con `crear`")
    nueva = registrar_version(conn, cliente_id, datos, creado_por)
    _fijar_columna_activo(conn, cliente_id, bool((datos.get("client") or {}).get("active", True)))
    return nueva


def fijar_activo(conn, cliente_id: str, valor: bool, creado_por: str,
                 version_base: Optional[int] = None) -> Optional[int]:
    """Único punto que escribe `cliente.activo` y `client.active` del JSON, juntos."""
    _, actual = requerir_base(conn, cliente_id, version_base) if version_base is not None \
        else requerir_activo(conn, cliente_id)
    datos = copy.deepcopy(actual)
    datos.setdefault("client", {})["active"] = valor
    nueva = registrar_version(conn, cliente_id, datos, creado_por)
    _fijar_columna_activo(conn, cliente_id, valor)
    return nueva


def revertir(conn, cliente_id: str, numero: int, creado_por: str, version_base: Optional[int] = None) -> Optional[int]:
    """Versión nueva con el contenido de la `numero`; None si ya es el activo. Nunca borra."""
    if version_base is not None:
        requerir_base(conn, cliente_id, version_base)
    else:
        requerir_activo(conn, cliente_id)
    antiguo = version(conn, cliente_id, numero)
    if antiguo is None:
        raise ErrorClientes(f"{cliente_id!r} no tiene la versión {numero}")
    return importar(conn, cliente_id, antiguo, creado_por)


# ── Lo que usa el portal (src/clientes/servicio.py): lecturas por cliente y escrituras con versión base ────────

def obtener(conn, cliente_id: str) -> Optional[dict]:
    """Las columnas de `cliente`; None si no existe."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT cliente_id, org_id, nombre, activo, locale, zona_horaria FROM cliente WHERE cliente_id = :c",
            c=cliente_id,
        )
        f = cur.fetchone()
    finally:
        cur.close()
    if not f:
        return None
    return {"cliente_id": f[0], "org_id": f[1], "nombre": f[2], "activo": bool(f[3]), "locale": f[4],
            "zona_horaria": f[5]}


def organizaciones(conn) -> list[dict]:
    cur = conn.cursor()
    try:
        cur.execute("SELECT org_id, nombre FROM organizacion ORDER BY org_id")
        filas = cur.fetchall()
    finally:
        cur.close()
    return [{"org_id": f[0], "nombre": f[1]} for f in filas]


def portales_de(conn, cliente_id: str) -> list[dict]:
    """Filas de `cliente_portal`. Hoy NADIE las lee en el pipeline (el portal sale de `client.portal` del
    perfil), por eso el portal de administración las muestra de solo lectura."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT portal_id, activo, credencial_ref, lookback_dias, horario_cron FROM cliente_portal "
            "WHERE cliente_id = :c ORDER BY portal_id", c=cliente_id,
        )
        filas = cur.fetchall()
    finally:
        cur.close()
    return [{"portal_id": f[0], "activo": bool(f[1]), "credencial_ref": f[2], "lookback_dias": f[3],
             "horario_cron": f[4]} for f in filas]


def bloquear(conn, cliente_id: str) -> None:
    """Bloquea la fila de `cliente` hasta el commit: serializa a quienes editan el mismo perfil a la vez."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT cliente_id FROM cliente WHERE cliente_id = :c FOR UPDATE", c=cliente_id)
        cur.fetchone()
    finally:
        cur.close()


def requerir_base(conn, cliente_id: str, version_base: Optional[int]) -> tuple[int, dict]:
    """Bloquea y devuelve (versión, contenido) activos; `ConflictoVersion` si ya no es la `version_base`."""
    bloquear(conn, cliente_id)
    version_activa, contenido = requerir_activo(conn, cliente_id)
    if version_base is not None and version_activa != version_base:
        raise ConflictoVersion(f"{cliente_id!r}: la versión activa es la {version_activa}, no la {version_base}")
    return version_activa, contenido


def guardar_perfil(conn, cliente_id: str, datos: object, creado_por: str, version_base: int) -> Optional[int]:
    """Valida y registra `datos` solo si la versión activa sigue siendo `version_base`. None = sin cambios."""
    requerir_base(conn, cliente_id, version_base)
    return importar(conn, cliente_id, datos, creado_por)


def editar_general(conn, cliente_id: str, nombre: str, locale: str, zona_horaria: str, creado_por: str,
                   version_base: int) -> Optional[int]:
    """Nombre, locale y zona horaria. `cliente.nombre` y `client.name` del JSON se escriben juntos."""
    _, actual = requerir_base(conn, cliente_id, version_base)
    datos = copy.deepcopy(actual)
    datos.setdefault("client", {})["name"] = nombre
    datos = validar(datos, cliente_id)
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE cliente SET nombre = :n, locale = :l, zona_horaria = :z WHERE cliente_id = :c",
            n=nombre, l=locale, z=zona_horaria, c=cliente_id,
        )
    finally:
        cur.close()
    return registrar_version(conn, cliente_id, datos, creado_por)
