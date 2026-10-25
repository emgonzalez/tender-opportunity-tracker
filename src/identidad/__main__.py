"""CLI de identidad: onboarding hasta que exista la UI de invitaciones (Entrega 8).

    python -m src.identidad alta --email a@b.com --rol gerente_pais --cliente multicomputos
    python -m src.identidad alta --email a@b.com --rol ceo
    python -m src.identidad baja --email a@b.com
    python -m src.identidad purgar [--dry-run]   # sesiones vencidas hace más de SESION_PURGA_DIAS (P5)
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.identidad")
    sub = parser.add_subparsers(dest="comando", required=True)
    purgar = sub.add_parser("purgar", help="borra las sesiones vencidas hace más de SESION_PURGA_DIAS")
    purgar.add_argument("--dry-run", action="store_true", help="solo cuenta, no borra nada")
    alta = sub.add_parser("alta", help="da de alta a una persona con un rol (o le suma otro país)")
    alta.add_argument("--email", required=True)
    alta.add_argument("--rol", required=True)
    alta.add_argument("--cliente", help="país (cliente_id); solo para gerente_pais y equipo_ventas")
    alta.add_argument("--nombre")
    alta.add_argument("--org", default="multicomputos")
    baja = sub.add_parser("baja", help="desactiva a una persona y cierra sus sesiones")
    baja.add_argument("--email", required=True)
    baja.add_argument("--org", default="multicomputos")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    from src.config import get_config
    from src.identidad import repo
    from src.memoria import db

    cfg = get_config()
    if args.comando in ("alta", "baja"):
        from src.identidad import service
        try:
            with db.connection(cfg) as conn:
                if args.comando == "alta":
                    uid = service.alta_usuario(conn, args.org, args.email, args.rol, args.cliente, args.nombre)
                    logger.info("Alta de %s como %s%s (usuario_id %s).", args.email, args.rol,
                                f" en {args.cliente}" if args.cliente else "", uid)
                else:
                    ok = service.baja_usuario(conn, args.org, args.email)
                    logger.info("Baja de %s: %s.", args.email, "hecha" if ok else "no existía o ya estaba de baja")
        except service.IdentidadError as exc:
            logger.error("%s", exc)
            sys.exit(2)
        return
    with db.connection(cfg) as conn:
        n = repo.purgar_sesiones_vencidas(conn, cfg.sesion_purga_dias, dry_run=args.dry_run)
    logger.info("Sesiones vencidas hace más de %d días: %d %s.", cfg.sesion_purga_dias, n,
                "por borrar (dry-run)" if args.dry_run else "borradas")


if __name__ == "__main__":
    main()
