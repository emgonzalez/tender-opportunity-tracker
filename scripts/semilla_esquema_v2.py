#!/usr/bin/env python3
"""Semilla mínima del esquema v2 (db/schema_v2.sql) — Fase "Expandir".

Puebla lo mínimo para que el esquema v2 no arranque vacío, sin escribir
todavía ningún dato del pipeline (eso es la Fase "Doble escritura", aparte):

    * `portal`       — una fila fija: comprasal_sv / SV / America/El_Salvador / USD / es.
    * `organizacion` — una fila fija: multicomputos.
    * `cliente`      — una fila por cada `clients/*.yaml` con `client.active: true`,
                        con el MISMO `cliente_id` que ya usan hoy las tablas actuales
                        (`licitaciones_metadata.cliente_id`, etc.) — el backfill de la
                        Fase 3 no tiene que traducir nada.
    * `cliente_config` — versión 1 del YAML completo, parseado a JSON tal cual
                        (sin pasar por `ClientProfile`: la config versionada es una
                        copia fiel del archivo, no una proyección de sus campos).

Idempotente: correrlo de nuevo no duplica filas (MERGE para portal/organizacion/
cliente; para cliente_config, INSERT solo si ese cliente_id todavía no tiene
ninguna versión — nunca reescribe una versión ya creada).

Costa Rica (preparación, docs/planes/multicomputos-costa-rica.md): con `--con-costa-rica`
siembra además `portal=sicop_cr` y el cliente `multicomputos_cr` con `activo=0`, de modo que
ningún KPI ni corrida lo toma hasta que se active a propósito.

Uso:
    python -m scripts.semilla_esquema_v2
    python -m scripts.semilla_esquema_v2 --dry-run    # solo imprime qué haría
    python -m scripts.semilla_esquema_v2 --con-costa-rica
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Ejecutable como script suelto además de como módulo: sin esto, `src` no
# está en el path (mismo patrón que scripts/calibrar_cuota.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from src.memoria import db  # noqa: E402

logger = logging.getLogger(__name__)

ORG_ID = "multicomputos"
PORTAL_ID = "comprasal_sv"

_PORTAL = {
    "portal_id": PORTAL_ID,
    "nombre": "COMPRASAL",
    "pais": "SV",
    "zona_horaria": "America/El_Salvador",
    "moneda_default": "USD",
    "idioma": "es",
    "base_url": "https://www.comprasal.gob.sv",
}

_ORGANIZACION = {
    "org_id": ORG_ID,
    "nombre": "MULTICOMPUTOS",
    "pais_sede": "SV",
    "zona_horaria": "America/El_Salvador",
    "locale_default": "es-SV",
    "moneda_reporte": "USD",
}


def _merge_portal(conn, portal: dict | None = None) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        MERGE INTO portal tgt
        USING (SELECT :portal_id AS portal_id FROM dual) src
        ON (tgt.portal_id = src.portal_id)
        WHEN NOT MATCHED THEN INSERT (
            portal_id, nombre, pais, zona_horaria, moneda_default, idioma, base_url
        ) VALUES (
            :portal_id, :nombre, :pais, :zona_horaria, :moneda_default, :idioma, :base_url
        )
        """,
        portal or _PORTAL,
    )
    cursor.close()


def _merge_organizacion(conn) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        MERGE INTO organizacion tgt
        USING (SELECT :org_id AS org_id FROM dual) src
        ON (tgt.org_id = src.org_id)
        WHEN NOT MATCHED THEN INSERT (
            org_id, nombre, pais_sede, zona_horaria, locale_default, moneda_reporte
        ) VALUES (
            :org_id, :nombre, :pais_sede, :zona_horaria, :locale_default, :moneda_reporte
        )
        """,
        _ORGANIZACION,
    )
    cursor.close()


def _merge_cliente(conn, cliente_id: str, nombre: str, activo: bool) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        MERGE INTO cliente tgt
        USING (SELECT :cliente_id AS cliente_id FROM dual) src
        ON (tgt.cliente_id = src.cliente_id)
        WHEN NOT MATCHED THEN INSERT (cliente_id, org_id, nombre, activo)
            VALUES (:cliente_id, :org_id, :nombre, :activo)
        """,
        cliente_id=cliente_id, org_id=ORG_ID, nombre=nombre, activo=1 if activo else 0,
    )
    cursor.close()


def _tiene_config(conn, cliente_id: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM cliente_config WHERE cliente_id = :cliente_id",
        cliente_id=cliente_id,
    )
    (n,) = cursor.fetchone()
    cursor.close()
    return n > 0


def _insertar_config_v1(conn, cliente_id: str, contenido: dict) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO cliente_config (cliente_id, version, contenido, activa, creado_por)
        VALUES (:cliente_id, 1, JSON(:contenido), 1, 'seed:yaml')
        """,
        cliente_id=cliente_id, contenido=json.dumps(contenido),
    )
    cursor.close()


def _portal_dict(portal) -> dict:
    return {
        "portal_id": portal.portal_id, "nombre": portal.nombre, "pais": portal.pais,
        "zona_horaria": portal.zona_horaria, "moneda_default": portal.moneda,
        "idioma": portal.idioma, "base_url": portal.base_url,
    }


CLIENTE_COSTA_RICA = "multicomputos_cr"


def semilla(cfg, dry_run: bool = False, con_costa_rica: bool = False) -> dict[str, int]:
    """Aplica la semilla completa. Devuelve un conteo de lo tocado."""
    conteos = {"portal": 0, "organizacion": 0, "clientes": 0, "configs_nuevas": 0}
    if con_costa_rica:
        conteos["costa_rica"] = 0

    yamls = sorted(
        f for f in os.listdir(cfg.clients_dir) if f.endswith(".yaml")
    ) if os.path.isdir(cfg.clients_dir) else []

    if dry_run:
        logger.info("[dry-run] portal=%s organizacion=%s", PORTAL_ID, ORG_ID)
        if con_costa_rica:
            logger.info("[dry-run] portal=sicop_cr cliente=%s (inactivo)", CLIENTE_COSTA_RICA)
        for filename in yamls:
            with open(os.path.join(cfg.clients_dir, filename), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            client = data.get("client", {})
            logger.info(
                "[dry-run] cliente=%s activo=%s (config v1 si no existe)",
                client.get("id"), client.get("active", True),
            )
        return conteos

    with db.connection(cfg) as conn:
        _merge_portal(conn)
        conteos["portal"] = 1
        _merge_organizacion(conn)
        conteos["organizacion"] = 1
        if con_costa_rica:
            from src.portales import obtener_portal
            _merge_portal(conn, _portal_dict(obtener_portal("sicop_cr")))
            _merge_cliente(conn, CLIENTE_COSTA_RICA, "MULTICOMPUTOS Costa Rica", activo=False)
            conteos["costa_rica"] = 1

        for filename in yamls:
            with open(os.path.join(cfg.clients_dir, filename), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            client = data.get("client", {})
            cliente_id = client.get("id")
            if not cliente_id:
                logger.warning("%s no tiene client.id — se salta", filename)
                continue

            _merge_cliente(
                conn, cliente_id=cliente_id,
                nombre=client.get("name", cliente_id),
                activo=bool(client.get("active", True)),
            )
            conteos["clientes"] += 1

            if not _tiene_config(conn, cliente_id):
                _insertar_config_v1(conn, cliente_id, data)
                conteos["configs_nuevas"] += 1

    logger.info("Semilla aplicada: %s", conteos)
    return conteos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="solo imprime qué haría, no escribe nada")
    parser.add_argument("--con-costa-rica", action="store_true",
                        help="siembra también el portal sicop_cr y el cliente multicomputos_cr (inactivo)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    from src.config import get_config
    cfg = get_config()
    semilla(cfg, dry_run=args.dry_run, con_costa_rica=args.con_costa_rica)


if __name__ == "__main__":
    main()
