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


def listar(conn) -> list[dict]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT c.cliente_id, c.org_id, c.nombre, c.activo, "
            "(SELECT MAX(cc.version) FROM cliente_config cc WHERE cc.cliente_id = c.cliente_id AND cc.activa = 1) "
            "FROM cliente c ORDER BY c.cliente_id"
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


def fijar_activo(conn, cliente_id: str, valor: bool, creado_por: str) -> Optional[int]:
    """Único punto que escribe `cliente.activo` y `client.active` del JSON, juntos."""
    _, actual = requerir_activo(conn, cliente_id)
    datos = copy.deepcopy(actual)
    datos.setdefault("client", {})["active"] = valor
    nueva = registrar_version(conn, cliente_id, datos, creado_por)
    _fijar_columna_activo(conn, cliente_id, valor)
    return nueva


def revertir(conn, cliente_id: str, numero: int, creado_por: str) -> Optional[int]:
    """Versión nueva con el contenido de la `numero`; None si ya es el activo. Nunca borra."""
    requerir_activo(conn, cliente_id)
    antiguo = version(conn, cliente_id, numero)
    if antiguo is None:
        raise ErrorClientes(f"{cliente_id!r} no tiene la versión {numero}")
    return importar(conn, cliente_id, antiguo, creado_por)
