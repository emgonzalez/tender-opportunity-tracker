"""
Reenvía el último reporte registrado en el historial a uno o todos los clientes activos.

El reporte se reconstruye desde data/history/{client_id}.jsonl sin volver a
consultar COMPRASAL ni llamar a la API de clasificación.

Uso:
    python -m src.notifier.resend                         # todos los clientes activos
    python -m src.notifier.resend --client multicomputos  # cliente específico
"""
import argparse
import logging
import sys

from src.config import get_config
from src.history import load_last_run
from src.classifier.client_profile import load_all_active_profiles, load_client_profile
from src.notifier.email_sender import send_report

logger = logging.getLogger(__name__)


def _resend_client(config, client_profile) -> bool:
    last_run = load_last_run(client_profile.client_id, config.data_dir)
    if not last_run:
        logger.warning(
            "[%s] Sin historial disponible. Esperar a que el agente ejecute al menos una vez.",
            client_profile.client_id,
        )
        return False

    logger.info(
        "[%s] Reenviando reporte del %s — %d procesos, %d relevantes.",
        client_profile.client_id,
        last_run["run_timestamp"].strftime("%d/%m/%Y %H:%M"),
        last_run["total_revisados"],
        last_run["total_relevantes"],
    )

    send_report(
        config=config,
        client_profile=client_profile,
        procesos_clasificados=last_run["procesos"],
        total_revisados=last_run["total_revisados"],
        run_timestamp=last_run["run_timestamp"],
    )
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Reenvía el último reporte del historial sin consultar COMPRASAL"
    )
    parser.add_argument(
        "--client",
        help="ID del cliente (ej: multicomputos). Sin este flag, reenvía a todos los activos.",
    )
    args = parser.parse_args()

    config = get_config()

    if args.client:
        try:
            profile = load_client_profile(args.client, config.clients_dir)
        except FileNotFoundError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        profiles = [profile]
    else:
        profiles = load_all_active_profiles(config.clients_dir)
        if not profiles:
            print("No hay clientes activos. Verificar clients/*.yaml")
            sys.exit(1)

    success_count = 0
    for profile in profiles:
        if _resend_client(config, profile):
            success_count += 1

    print(f"\nReenvío completado: {success_count}/{len(profiles)} cliente(s).")
    sys.exit(0 if success_count == len(profiles) else 1)


if __name__ == "__main__":
    main()
