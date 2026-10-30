#!/usr/bin/env python3
"""Sincroniza `clients/*.yaml` → `cliente_config` (ADB): versión nueva SOLO si el contenido cambió.

Compara el hash del YAML con el de la versión activa; si difiere, apaga la activa e inserta la siguiente
(append-only). Si el cliente no existe en `cliente` no lo crea: eso es de `scripts/semilla_esquema_v2.py`.

Uso (en la VM, donde la ADB es alcanzable):
    python -m scripts.sincronizar_perfiles              # dry-run: dice qué haría, no escribe
    python -m scripts.sincronizar_perfiles --aplicar    # escribe
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from src.classifier.alias_clientes import id_canonico  # noqa: E402
from src.classifier.client_profile import hash_contenido, registrar_version  # noqa: E402

logger = logging.getLogger(__name__)


def _yamls(clients_dir: str) -> dict[str, dict]:
    if not os.path.isdir(clients_dir):
        return {}
    salida: dict[str, dict] = {}
    for nombre in sorted(os.listdir(clients_dir)):
        if not nombre.endswith(".yaml"):
            continue
        with open(os.path.join(clients_dir, nombre), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning("%s no es un mapa YAML; se salta", nombre)
            continue
        salida[id_canonico(nombre[:-5])] = data
    return salida


def _existe_cliente(conn, cliente_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cliente WHERE cliente_id = :c", c=cliente_id)
    (n,) = cur.fetchone()
    cur.close()
    return n > 0


def _version_activa_hash(conn, cliente_id: str) -> str | None:
    from src.classifier.client_profile import _a_dict
    cur = conn.cursor()
    cur.execute(
        "SELECT JSON_SERIALIZE(contenido RETURNING CLOB) FROM cliente_config WHERE cliente_id = :c AND activa = 1 "
        "ORDER BY version DESC", c=cliente_id,
    )
    fila = cur.fetchone()
    cur.close()
    previo = _a_dict(fila[0]) if fila else None
    return hash_contenido(previo) if previo is not None else None


def sincronizar(conn, clients_dir: str, aplicar: bool = False) -> dict[str, str]:
    """{cliente_id: 'nuevo'|'actualizado'|'sin_cambio'|'sin_cliente'} (en dry-run, lo que ocurriría)."""
    resultado: dict[str, str] = {}
    for cliente_id, data in _yamls(clients_dir).items():
        if not _existe_cliente(conn, cliente_id):
            resultado[cliente_id] = "sin_cliente"
            logger.warning("%s no existe en `cliente`: correr scripts.semilla_esquema_v2 primero", cliente_id)
            continue
        previo = _version_activa_hash(conn, cliente_id)
        if previo == hash_contenido(data):
            resultado[cliente_id] = "sin_cambio"
        else:
            resultado[cliente_id] = "nuevo" if previo is None else "actualizado"
            if aplicar:
                registrar_version(conn, cliente_id, data, "sync:yaml")
        logger.info("%s%s: %s", "" if aplicar else "[dry-run] ", cliente_id, resultado[cliente_id])
    return resultado


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aplicar", action="store_true", help="escribe en la ADB (sin esto es dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="explícito; es el comportamiento por defecto")
    args = parser.parse_args()
    if args.aplicar and args.dry_run:
        parser.error("--aplicar y --dry-run son excluyentes")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    from src.config import get_config
    from src.memoria import db
    cfg = get_config()
    with db.connection(cfg) as conn:
        resultado = sincronizar(conn, cfg.clients_dir, aplicar=args.aplicar)
    logger.info("Resultado: %s%s", resultado, "" if args.aplicar else " (dry-run: nada escrito)")


if __name__ == "__main__":
    main()
