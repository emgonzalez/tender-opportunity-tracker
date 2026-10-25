"""`python -m src.identidad purgar [--dry-run]` — borra las sesiones vencidas hace más de SESION_PURGA_DIAS (P5)."""
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    from src.config import get_config
    from src.identidad import repo
    from src.memoria import db

    cfg = get_config()
    with db.connection(cfg) as conn:
        n = repo.purgar_sesiones_vencidas(conn, cfg.sesion_purga_dias, dry_run=args.dry_run)
    logger.info("Sesiones vencidas hace más de %d días: %d %s.", cfg.sesion_purga_dias, n,
                "por borrar (dry-run)" if args.dry_run else "borradas")


if __name__ == "__main__":
    main()
