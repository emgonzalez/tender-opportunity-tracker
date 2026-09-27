"""src.mercado.participacion — qué procesos participó el cliente.

Reutiliza `classifier.trainer.extract_all_participated` (mismo login vía
Playwright, mismo endpoint autenticado `/api/v1/procesos/proveedores?
participacion=true`) en vez de duplicar la extracción: la única diferencia
es que ACÁ cada fila se **persiste** en `proceso_participado`, no solo se
resume para el perfil de texto del clasificador (`trainer.py` sigue haciendo
eso, sin cambios).

Requiere sesión autenticada (Playwright + `storage_state`) — a diferencia de
`api_publica.py`, esto NO es gratis en tiempo de navegador; es el mismo
costo que ya paga `trainer.py` hoy.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def sincronizar_participados(cfg, cliente_id: str, max_pages: int = 0) -> int:
    """Extrae todos los procesos participados y los deja en `proceso_participado`.

    No resuelve el resultado (`GANADO`/`PERDIDO`/...) — eso es trabajo
    aparte de `mercado.repo.resolver_resultados`, para poder recorrer el
    registro de adjudicaciones (potencialmente pesado) en su propia corrida,
    sin volver a abrir Playwright.

    Devuelve la cantidad de filas escritas (alta o refresco). `max_pages=0`
    significa sin límite (recorre todo lo participado).
    """
    from src.classifier.trainer import extract_all_participated
    from src.memoria import db
    from src.mercado import repo

    items = await extract_all_participated(cfg, max_pages=max_pages)
    if not items:
        logger.info("Sin procesos participados para %s.", cliente_id)
        return 0

    with db.connection(cfg) as conn:
        for item in items:
            repo.upsert_proceso_participado(conn, cliente_id, item)

    logger.info("Procesos participados sincronizados: %d (cliente=%s)", len(items), cliente_id)
    return len(items)
