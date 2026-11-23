"""CLI manual de `src.mercado` (mismo patrón que `classifier.trainer`).

Uso:
    python -m src.mercado mercado                       # registro completo (gobierno)
    python -m src.mercado mercado --id-institucion 25   # una institución
    python -m src.mercado participados --cliente multicomputos
    python -m src.mercado resolver --cliente multicomputos
    python -m src.mercado todo --cliente multicomputos  # los tres pasos, en orden

    # SICOP (Costa Rica), sin subcomando; dry-run por defecto (no escribe en la ADB):
    python -m src.mercado --portal sicop_cr                       # lo adjudicado a nuestras cédulas
    python -m src.mercado --portal sicop_cr --aplicar             # + adjudicacion_partida y GANADO
    python -m src.mercado --portal sicop_cr --mercado --aplicar   # + mercado completo en firme (pesado)

No es un timer diario (ver el docstring de `mercado.service`): se corre a
mano hasta que se decida una cadencia (candidato: semanal).
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

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
    parser.add_argument("--portal", default="comprasal_sv", choices=("comprasal_sv", "sicop_cr"),
                        help="Portal de compras (default comprasal_sv). sicop_cr no usa subcomando.")
    sicop = parser.add_argument_group("SICOP (solo con --portal sicop_cr)")
    sicop.add_argument("--cliente-sicop", default="multicomputos_cr", dest="cliente_sicop")
    sicop.add_argument("--dias", type=int, default=365,
                       help="Ventana de lo adjudicado a nuestras cédulas, en días hacia atrás (default 365).")
    sicop.add_argument("--mercado", action="store_true", dest="sicop_mercado",
                       help="Incluir el mercado completo en firme (pesado: ~7 s por página).")
    sicop.add_argument("--dias-mercado", type=int, default=31, dest="dias_mercado",
                       help="Ventana del mercado completo, en días hacia atrás (default 31).")
    sicop.add_argument("--max-paginas", type=int, default=None, dest="max_paginas",
                       help="Tope de páginas por consulta (default el de sicop_publica).")
    sicop.add_argument("--aplicar", action="store_true", dest="sicop_aplicar",
                       help="Escribir en la ADB. Sin esto es dry-run.")
    sub = parser.add_subparsers(dest="comando", required=False)

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
    if args.portal == "sicop_cr":
        if args.comando:
            parser.error("con --portal sicop_cr no se usa subcomando")
        _setup_logging()
        _correr_sicop(get_config(), args)
        return
    if not args.comando:
        parser.error("falta el subcomando (mercado|participados|resolver|todo|senales)")
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


async def _sicop_con_navegador(cfg, args) -> dict:
    """Abre el navegador anónimo (A3; sin User-Agent falsificado) y corre la sincronización de SICOP."""
    from src.mercado import service
    from src.mercado.sicop_publica import ORIGEN_SPA
    from src.scraper.navegador_anonimo import NavegadorAnonimo  # import tardío: es de A3

    hoy = date.today()
    async with NavegadorAnonimo(cfg, ORIGEN_SPA) as transporte:
        return await service.sincronizar_sicop(
            cfg, transporte,
            cliente_id=id_canonico(args.cliente_sicop),
            desde=hoy - timedelta(days=args.dias),
            hasta=hoy,
            incluir_mercado=args.sicop_mercado,
            mercado_desde=hoy - timedelta(days=args.dias_mercado),
            max_paginas=args.max_paginas,
            aplicar=args.sicop_aplicar,
        )


def _correr_sicop(cfg, args) -> None:
    resumen = asyncio.run(_sicop_con_navegador(cfg, args))
    logger.info("Listo: %s", resumen)
    if not args.sicop_aplicar:
        logger.info("DRY-RUN: no se escribió nada en la ADB. Repetir con --aplicar para cargar.")


if __name__ == "__main__":
    main()
