"""CLI manual de `src.mercado` (mismo patrón que `classifier.trainer`).

Uso:
    python -m src.mercado mercado                       # registro completo (gobierno)
    python -m src.mercado mercado --id-institucion 25   # una institución
    python -m src.mercado participados --cliente multicomputos
    python -m src.mercado resolver --cliente multicomputos
    python -m src.mercado todo --cliente multicomputos  # los tres pasos, en orden

No es un timer diario (ver el docstring de `mercado.service`): se corre a
mano hasta que se decida una cadencia (candidato: semanal).
"""
import argparse
import asyncio
import logging
import sys

from src.classifier.alias_clientes import id_canonico
from src.config import get_config

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="comando", required=True)

    p_mercado = sub.add_parser("mercado", help="Sincroniza el registro público de adjudicaciones.")
    p_mercado.add_argument("--id-institucion", type=int, default=None)
    p_mercado.add_argument("--modalidad", type=int, default=None)
    p_mercado.add_argument("--nombre", type=str, default=None)

    p_participados = sub.add_parser("participados", help="Sincroniza los procesos participados.")
    p_participados.add_argument("--cliente", required=True, help="client_id (ej. multicomputos_sv)")
    p_participados.add_argument("--max-pages", type=int, default=0)

    p_resolver = sub.add_parser("resolver", help="Resuelve GANADO/PERDIDO para lo participado.")
    p_resolver.add_argument("--cliente", required=True)

    p_todo = sub.add_parser("todo", help="mercado + participados + resolver, en orden.")
    p_todo.add_argument("--cliente", required=True)

    p_senales = sub.add_parser("senales", help="C7 manual, no se ejecuta automáticamente; dry-run por defecto.")
    p_senales.add_argument("--cliente", default="multicomputos_sv")
    p_senales.add_argument("--aplicar", action="store_true")

    args = parser.parse_args()
    if getattr(args, "cliente", None):
        args.cliente = id_canonico(args.cliente)
    _setup_logging()
    cfg = get_config()

    from src.mercado import service

    if args.comando == "senales":
        from src.mercado.senales import ejecutar
        ejecutar(cfg, args.cliente, args.aplicar)

    elif args.comando == "mercado":
        n = service.sincronizar_mercado(
            cfg, id_institucion=args.id_institucion, modalidad=args.modalidad, nombre=args.nombre,
        )
        logger.info("Listo: %d fila(s) de mercado sincronizadas.", n)

    elif args.comando == "participados":
        n = asyncio.run(service.sincronizar_participados(cfg, args.cliente, max_pages=args.max_pages))
        logger.info("Listo: %d proceso(s) participado(s) sincronizados.", n)

    elif args.comando == "resolver":
        resultado = service.resolver_resultados(cfg, args.cliente)
        logger.info("Listo: %s", resultado)

    elif args.comando == "todo":
        service.sincronizar_mercado(cfg)
        asyncio.run(service.sincronizar_participados(cfg, args.cliente))
        resultado = service.resolver_resultados(cfg, args.cliente)
        logger.info("Listo: %s", resultado)


if __name__ == "__main__":
    main()
