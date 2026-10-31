"""CLI de perfiles de cliente: `python -m src.clientes <subcomando>` (la ADB es la única fuente del perfil).

    listar | mostrar <id> | exportar <id> [-o f.json] | versiones <id>      (solo leen)
    importar <id> f.json | crear <id> --nombre N | activar <id> | desactivar <id> | revertir <id> <versión>

Los que escriben son dry-run por defecto: dicen qué harían; con `--aplicar` lo hacen, en una transacción.
Nunca imprime credenciales ni el DSN. Se corre donde la ADB es alcanzable (la VM).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import json
import logging
import sys
from typing import Callable, Optional

from src.classifier.alias_clientes import id_canonico
from src.classifier.client_profile import hash_contenido
from src.clientes import repo
from src.clientes.repo import ErrorClientes

logger = logging.getLogger(__name__)


def _quien() -> str:
    try:
        usuario = getpass.getuser()
    except Exception:  # noqa: BLE001 — sin usuario del SO igual se puede operar
        usuario = "desconocido"
    return f"cli:{usuario}"[:64]


def _conexion_por_defecto():
    from src.config import get_config
    from src.memoria import db
    cfg = get_config()
    if not (cfg.adb_dsn and cfg.adb_password):
        raise ErrorClientes("la ADB no está configurada (ADB_DSN / ADB_PASSWORD)")
    return db.connection(cfg)


def _decir(texto: str) -> None:
    print(texto)


def _cambio(previo: Optional[dict], nuevo: dict) -> bool:
    return previo is None or hash_contenido(previo) != hash_contenido(nuevo)


def _informar_version(cid: str, previo: Optional[tuple[int, dict]], nuevo: dict, aplicar: bool,
                      escribir: Callable[[], Optional[int]]) -> None:
    """Dry-run o aplicación de un cambio de contenido, con el mismo mensaje."""
    if not _cambio(previo[1] if previo else None, nuevo):
        _decir(f"{cid}: sin cambios (versión activa {previo[0]})")
        if aplicar:
            escribir()  # solo alinea cliente.activo con client.active, sin versión nueva
        return
    siguiente = (previo[0] + 1) if previo else 1
    if not aplicar:
        _decir(f"[dry-run] {cid}: crearía la versión {siguiente} (usar --aplicar)")
        return
    version = escribir()
    _decir(f"{cid}: versión {version} registrada")


def _cmd_listar(conn, args) -> None:
    filas = repo.listar(conn)
    if not filas:
        _decir("(sin clientes)")
    for f in filas:
        _decir(f"{f['cliente_id']:<28} {'activo  ' if f['activo'] else 'inactivo'} org={f['org_id']} "
               f"v{f['version'] if f['version'] is not None else '-'}  {f['nombre']}")


def _cmd_mostrar(conn, args) -> None:
    cid = id_canonico(args.cliente)
    version, datos = repo.requerir_activo(conn, cid)
    cliente = datos.get("client") or {}
    catalogo = datos.get("catalog") or {}
    categorias = catalogo.get("categories") or []
    marcas = sum(len(c.get("brands") or []) for c in categorias)
    notif = datos.get("notification") or {}
    inteligencia = datos.get("intelligence") or {}
    _decir(f"{cid} — {cliente.get('name', '?')}")
    _decir(f"  versión activa : {version} (hash {hash_contenido(datos)[:12]})")
    _decir(f"  client.active  : {cliente.get('active', True)}   portal: {cliente.get('portal') or repo.PORTAL_DEFAULT}")
    _decir(f"  catálogo       : {len(categorias)} categorías, {marcas} marcas, "
           f"{len(catalogo.get('tag_taxonomy') or [])} tags")
    _decir(f"  destinatarios  : {len(notif.get('email_to') or [])}   admins: {len(notif.get('system_admins_email') or [])}")
    _decir(f"  inteligencia   : {'entrenada ' + str(inteligencia.get('generated_at')) if inteligencia.get('profile') else 'sin entrenar'}")


def _cmd_exportar(conn, args) -> None:
    cid = id_canonico(args.cliente)
    _, datos = repo.requerir_activo(conn, cid)
    texto = json.dumps(datos, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if args.salida:
        with open(args.salida, "w", encoding="utf-8") as f:
            f.write(texto)
        _decir(f"{cid}: exportado a {args.salida}")
    else:
        sys.stdout.write(texto)


def _cmd_importar(conn, args) -> None:
    cid = id_canonico(args.cliente)
    try:
        with open(args.archivo, encoding="utf-8") as f:
            datos = json.load(f)
    except OSError as exc:
        raise ErrorClientes(f"no se pudo leer {args.archivo}: {exc.strerror}") from exc
    except ValueError as exc:
        raise ErrorClientes(f"{args.archivo} no es JSON válido: {exc}") from exc
    repo.validar(datos, cid)
    if not repo.existe(conn, cid):
        raise ErrorClientes(f"el cliente {cid!r} no existe: crearlo primero con `crear`")
    previo = repo.activo(conn, cid)
    _informar_version(cid, previo, datos, args.aplicar,
                      lambda: repo.importar(conn, cid, datos, _quien()))


def _cmd_crear(conn, args) -> None:
    cid = id_canonico(args.cliente)
    if repo.existe(conn, cid):
        raise ErrorClientes(f"el cliente {cid!r} ya existe")
    repo.validar(repo.perfil_nuevo(cid, args.nombre, args.portal), cid)
    if not args.aplicar:
        _decir(f"[dry-run] {cid}: crearía el cliente {args.nombre!r} (org={args.org}, portal={args.portal}) "
               "con la versión 1 (usar --aplicar)")
        return
    version = repo.crear(conn, cid, args.nombre, args.portal, args.org, _quien())
    _decir(f"{cid}: cliente creado, versión {version}")


def _cmd_activar(conn, args, valor: bool) -> None:
    cid = id_canonico(args.cliente)
    previo = repo.requerir_activo(conn, cid)
    nuevo = copy.deepcopy(previo[1])
    nuevo.setdefault("client", {})["active"] = valor
    _informar_version(cid, previo, nuevo, args.aplicar,
                      lambda: repo.fijar_activo(conn, cid, valor, _quien()))


def _cmd_versiones(conn, args) -> None:
    cid = id_canonico(args.cliente)
    filas = repo.versiones(conn, cid)
    if not filas:
        raise ErrorClientes(f"el cliente {cid!r} no tiene versiones")
    for v in filas:
        _decir(f"v{v['version']:<4} {'ACTIVA' if v['activa'] else '      '} {v['hash']}  "
               f"{v['creado_por'] or '-'}  {v['creado_en']}")


def _cmd_revertir(conn, args) -> None:
    cid = id_canonico(args.cliente)
    previo = repo.requerir_activo(conn, cid)
    antiguo = repo.version(conn, cid, args.version)
    if antiguo is None:
        raise ErrorClientes(f"{cid!r} no tiene la versión {args.version}")
    repo.validar(antiguo, cid)
    _informar_version(cid, previo, antiguo, args.aplicar,
                      lambda: repo.revertir(conn, cid, args.version, _quien()))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.clientes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def con_cliente(nombre: str, ayuda: str, escribe: bool = False):
        s = sub.add_parser(nombre, help=ayuda)
        s.add_argument("cliente", help="id del cliente (p. ej. multicomputos_sv)")
        if escribe:
            s.add_argument("--aplicar", action="store_true", help="escribe en la ADB (sin esto es dry-run)")
        return s

    sub.add_parser("listar", help="clientes y su versión activa")
    con_cliente("mostrar", "resumen de la versión activa")
    ex = con_cliente("exportar", "JSON de la versión activa")
    ex.add_argument("-o", "--salida", help="archivo de salida (por defecto, stdout)")
    im = con_cliente("importar", "valida el JSON y registra versión si cambió", escribe=True)
    im.add_argument("archivo", help="JSON del perfil")
    cr = con_cliente("crear", "cliente nuevo con perfil mínimo (versión 1)", escribe=True)
    cr.add_argument("--nombre", required=True)
    cr.add_argument("--portal", default=repo.PORTAL_DEFAULT)
    cr.add_argument("--org", default=repo.ORG_DEFAULT)
    con_cliente("activar", "client.active y cliente.activo = verdadero", escribe=True)
    con_cliente("desactivar", "client.active y cliente.activo = falso", escribe=True)
    con_cliente("versiones", "historial de versiones")
    rv = con_cliente("revertir", "versión nueva con el contenido de una antigua", escribe=True)
    rv.add_argument("version", type=int)
    return p


_COMANDOS = {
    "listar": _cmd_listar, "mostrar": _cmd_mostrar, "exportar": _cmd_exportar, "importar": _cmd_importar,
    "crear": _cmd_crear, "activar": lambda c, a: _cmd_activar(c, a, True),
    "desactivar": lambda c, a: _cmd_activar(c, a, False), "versiones": _cmd_versiones, "revertir": _cmd_revertir,
}


def main(argv: Optional[list[str]] = None, conectar: Optional[Callable[[], contextlib.AbstractContextManager]] = None) -> int:
    """Devuelve el código de salida. `conectar` (tests) reemplaza la conexión real a la ADB."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        with (conectar or _conexion_por_defecto)() as conn:
            _COMANDOS[args.cmd](conn, args)
    except ErrorClientes as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — sin traza: podría arrastrar el DSN; el tipo basta para diagnosticar
        print(f"ERROR: la operación falló ({type(exc).__name__}); nada quedó a medias", file=sys.stderr)
        logger.debug("detalle", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
