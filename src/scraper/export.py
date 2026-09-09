"""
Exportador completo de procesos de COMPRASAL.

Extrae TODOS los procesos publicados actualmente (sin filtro de fecha)
y los guarda en data/export/procesos_YYYY-MM-DD_HHMM.csv|json.

Uso:
  # Desde Docker (recomendado):
  docker compose run --rm tender-opportunity-tracker python -m src.scraper.export
  docker compose run --rm tender-opportunity-tracker python -m src.scraper.export --format json
  docker compose run --rm tender-opportunity-tracker python -m src.scraper.export --format both

  # El archivo queda en data/export/ dentro del volumen del contenedor.
  # Para copiarlo al host:
  docker cp tender-opportunity-tracker:/app/data/export/. ./data/export/
"""
import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

from src.config import get_config
from src.scraper.extractor import _fetch_page, _get_token_and_page, _item_to_proceso

logger = logging.getLogger(__name__)

TZ_ELSALVADOR = timezone(timedelta(hours=-6))


async def _extract_all(config) -> list:
    """
    Extrae todos los procesos de COMPRASAL sin filtro de fecha.
    Itera todas las páginas hasta que la API no devuelva más datos.
    """
    all_procesos = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            storage_state=config.storage_state_path,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        try:
            page, token = await _get_token_and_page(context)

            page_num = 1
            total_rows = None
            per_page = 12

            while True:
                logger.info("Extrayendo página %d...", page_num)
                result = await _fetch_page(page, token, page_num)

                items = result["data"]
                if not items:
                    logger.info("Página %d vacía — fin de datos.", page_num)
                    break

                if total_rows is None:
                    total_rows = result["total_rows"]
                    per_page = result["per_page"] or per_page
                    total_pages = -(-total_rows // per_page)  # ceiling div
                    logger.info(
                        "COMPRASAL reporta %d procesos en total (%d páginas aprox.)",
                        total_rows,
                        total_pages,
                    )

                for item in items:
                    all_procesos.append(_item_to_proceso(item))

                logger.info(
                    "Página %d procesada — %d/%d procesos acumulados",
                    page_num,
                    len(all_procesos),
                    total_rows or "?",
                )

                if total_rows and page_num * per_page >= total_rows:
                    logger.info("Última página alcanzada (%d).", page_num)
                    break

                page_num += 1
                await asyncio.sleep(0.5)  # pausa cortés entre llamadas

        finally:
            await browser.close()

    return all_procesos


def _save_csv(procesos: list, path: str) -> None:
    fieldnames = [
        "numero_referencia",
        "institucion",
        "descripcion",
        "tipo_actividad",
        "forma_contratacion",
        "fecha_publicacion",
        "monto",
        "url_directa",
    ]
    # utf-8-sig añade BOM para que Excel lo abra correctamente con tildes
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in procesos:
            writer.writerow({
                "numero_referencia":  p.numero_referencia,
                "institucion":        p.institucion,
                "descripcion":        p.descripcion,
                "tipo_actividad":     p.tipo_actividad,
                "forma_contratacion": p.forma_contratacion,
                "fecha_publicacion":  p.fecha_publicacion.strftime("%Y-%m-%d %H:%M"),
                "monto":              p.monto if p.monto is not None else "",
                "url_directa":        p.url_directa,
            })
    logger.info("CSV guardado: %s (%d filas)", path, len(procesos))


def _save_json(procesos: list, path: str) -> None:
    data = [
        {
            "numero_referencia":  p.numero_referencia,
            "institucion":        p.institucion,
            "descripcion":        p.descripcion,
            "tipo_actividad":     p.tipo_actividad,
            "forma_contratacion": p.forma_contratacion,
            "fecha_publicacion":  p.fecha_publicacion.isoformat(),
            "monto":              p.monto,
            "url_directa":        p.url_directa,
        }
        for p in procesos
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("JSON guardado: %s (%d registros)", path, len(data))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exporta todos los procesos actuales de COMPRASAL a CSV/JSON."
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "both"],
        default="csv",
        help="Formato de salida (default: csv)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    config = get_config()
    now = datetime.now(TZ_ELSALVADOR)
    timestamp = now.strftime("%Y-%m-%d_%H%M")

    export_dir = os.path.join(config.data_dir, "export")
    os.makedirs(export_dir, exist_ok=True)

    logger.info(
        "Iniciando exportación completa de COMPRASAL — %s",
        now.strftime("%Y-%m-%d %H:%M %Z"),
    )

    procesos = asyncio.run(_extract_all(config))
    logger.info("Extracción completa: %d procesos en total.", len(procesos))

    saved_paths = []

    if args.format in ("csv", "both"):
        path = os.path.join(export_dir, f"procesos_{timestamp}.csv")
        _save_csv(procesos, path)
        saved_paths.append(path)

    if args.format in ("json", "both"):
        path = os.path.join(export_dir, f"procesos_{timestamp}.json")
        _save_json(procesos, path)
        saved_paths.append(path)

    print("\n" + "=" * 60)
    print(f"  Exportación completa: {len(procesos)} procesos")
    for p in saved_paths:
        print(f"  Archivo: {p}")
    print("=" * 60)


if __name__ == "__main__":
    main()
