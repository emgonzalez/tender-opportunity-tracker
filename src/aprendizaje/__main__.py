"""CLI de la memoria de aprendizaje: `python -m src.aprendizaje <subcomando>`.

    semilla [--cliente multicomputos_sv] [--aplicar]   siembra el diccionario público (dry-run por defecto)
    estado  [--cliente multicomputos_sv]               conteos por tabla y del diccionario (solo lee)

`semilla` es idempotente: inserta solo los términos que faltan, en estado `semilla`, y nunca toca una fila
existente (un término rechazado sigue rechazado). Con `--aplicar` escribe en una transacción, con su fila
en `auditoria`. Nunca imprime credenciales ni el DSN. Se corre donde la ADB es alcanzable (la VM).
`ciclo [--cliente ...] [--segundos 300] [--aplicar]` ejecuta el ciclo semanal optativo.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import sys
from typing import Callable, Optional

from src.aprendizaje import repo, semilla
from src.classifier.alias_clientes import id_canonico
from src.identidad.politica import AccionNoAuditable

logger = logging.getLogger(__name__)

CLIENTE_DEFAULT = "multicomputos_sv"


class ErrorAprendizaje(Exception):
    """Fallo esperable de la CLI (cliente inexistente, ADB sin configurar…): se informa sin traza."""


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
        raise ErrorAprendizaje("la ADB no está configurada (ADB_DSN / ADB_PASSWORD)")
    return db.connection(cfg)


def _decir(texto: str) -> None:
    print(texto)


def portafolio(conn, cliente_id: str) -> Optional[set[str]]:
    """ids de marca del perfil activo (`cliente_config`); None si no hay perfil legible."""
    from src.clientes import repo as clientes_repo
    activo = clientes_repo.activo(conn, cliente_id)
    if activo is None:
        return None
    categorias = ((activo[1].get("catalog") or {}).get("categories")) or []
    return {str(b["id"]).lower() for c in categorias for b in (c.get("brands") or []) if b.get("id")}


def _cmd_semilla(conn, args) -> None:
    cid = id_canonico(args.cliente)
    if not repo.existe_cliente(conn, cid):
        raise ErrorAprendizaje(f"el cliente {cid!r} no existe en la ADB")
    candidatos, avisos = semilla.filtrar_por_portafolio(semilla.terminos_semilla(), portafolio(conn, cid))
    for aviso in avisos:
        _decir(f"aviso: {aviso}")
    existentes = repo.terminos_existentes(conn, cid)
    faltan = [t for t in candidatos if t not in existentes]
    _decir(f"{cid}: {len(candidatos)} términos de semilla, {len(candidatos) - len(faltan)} ya existen, "
           f"{len(faltan)} por insertar")
    for marca, termino, tipo in faltan:
        _decir(f"  + {marca:<12} {tipo:<8} {termino}")
    if not faltan:
        return
    if not args.aplicar:
        _decir("[dry-run] no se escribió nada (usar --aplicar)")
        return
    insertados = repo.sembrar_terminos(conn, cid, faltan, creado_por=_quien())
    _decir(f"{cid}: {insertados} términos insertados en estado 'semilla'")


def _cmd_estado(conn, args) -> None:
    cid = id_canonico(args.cliente) if args.cliente else None
    _decir(f"memoria de aprendizaje{' de ' + cid if cid else ''}:")
    for tabla, n in repo.contar_por_tabla(conn, cid).items():
        _decir(f"  {tabla:<18} {n}")
    if cid:
        for (tipo, estado), n in sorted(repo.contar_terminos_por_estado(conn, cid).items()):
            _decir(f"  marca_termino {tipo:<8} {estado:<10} {n}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.aprendizaje", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    se = sub.add_parser("semilla", help="siembra el diccionario público de marcas (dry-run por defecto)")
    se.add_argument("--cliente", default=CLIENTE_DEFAULT)
    se.add_argument("--aplicar", action="store_true", help="escribe de verdad (si no, solo informa)")
    es = sub.add_parser("estado", help="conteos por tabla (solo lectura)")
    es.add_argument("--cliente", default=None)
    ci = sub.add_parser("ciclo", help="ciclo semanal (dry-run por defecto)")
    ci.add_argument("--cliente", default=CLIENTE_DEFAULT)
    ci.add_argument("--aplicar", action="store_true")
    ci.add_argument("--segundos", type=float, default=300)
    return p


_COMANDOS = {"semilla": _cmd_semilla, "estado": _cmd_estado}


def main(argv: Optional[list[str]] = None,
         conectar: Optional[Callable[[], contextlib.AbstractContextManager]] = None) -> int:
    """Devuelve el código de salida. `conectar` (tests) reemplaza la conexión real a la ADB."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.cmd == "ciclo":
        from src.aprendizaje import ciclo
        try:
            cfg = None
            if conectar is None:
                from src.config import get_config
                cfg = get_config()
            resultado = ciclo.ejecutar(conectar or _conexion_por_defecto, id_canonico(args.cliente),
                                      aplicar=args.aplicar, segundos=args.segundos, cfg=cfg)
            _decir(ciclo.texto(resultado))
        except (ValueError, AccionNoAuditable) as exc:
            print(f"ERROR de configuración: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        with (conectar or _conexion_por_defecto)() as conn:
            _COMANDOS[args.cmd](conn, args)
    except ErrorAprendizaje as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except AccionNoAuditable as exc:
        print(f"ERROR: la acción {exc} no está en ACCIONES_AUDITADAS (src/identidad/politica.py); "
              "no se escribió nada", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — sin traza: podría arrastrar el DSN; el tipo basta para diagnosticar
        print(f"ERROR: la operación falló ({type(exc).__name__}); nada quedó a medias", file=sys.stderr)
        logger.debug("detalle", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
