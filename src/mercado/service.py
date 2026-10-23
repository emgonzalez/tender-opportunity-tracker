"""src.mercado.service — orquesta la sincronización de mercado y la
resolución de resultado propio (plan del portal, §3.5).

Dos corridas independientes, con costos y cadencias distintas:

- `sincronizar_mercado`: HTTP simple, sin sesión, pero pesado en volumen
  (131 775+ filas al 2026-09-27, gobierno completo — decisión del usuario).
- `sincronizar_participados`: liviano en volumen pero necesita Playwright
  autenticado (mismo costo que ya paga `classifier.trainer` hoy).

Ninguna de las dos tiene sentido correrla a diario: es información que se
resuelve semanas o meses después de publicado un proceso. Candidato a timer
semanal (no diario) una vez que se use en producción — por ahora, CLI manual
(`python -m src.mercado {mercado|participados|resolver|todo}`, ver
`src/mercado/__main__.py`), mismo patrón que `trainer.py`.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def sincronizar_mercado(
    cfg,
    id_institucion: int | None = None,
    modalidad: int | None = None,
    nombre: str | None = None,
) -> int:
    """Pagina el registro público de adjudicaciones (completo o filtrado) y
    hace upsert de cada fila en `adjudicacion_item`.

    Comitea **por página**, no al final: con ~132 páginas para un barrido
    completo del registro, una falla de red a mitad de camino no debe
    perder lo ya cargado (`db.connection` comitea una sola vez al salir del
    context manager; acá se hace explícito por página a propósito).

    Devuelve cuántas filas se procesaron (alta o refresco).
    """
    from src.memoria import db
    from src.mercado import api_publica, identidad, repo

    total = 0
    with db.connection(cfg) as conn:
        for pagina in api_publica.paginar_todo(
            id_institucion=id_institucion, modalidad=modalidad, nombre=nombre,
        ):
            for item in pagina:
                es_mc = identidad.es_nuestro(item)
                repo.upsert_adjudicacion_item(conn, item, es_multicomputos=es_mc)
                total += 1
            conn.commit()
            logger.info("Adjudicaciones cargadas hasta ahora: %d", total)

    logger.info("Sincronización de mercado completa: %d fila(s).", total)
    return total


async def sincronizar_participados(cfg, cliente_id: str, max_pages: int = 0) -> int:
    """Ver `mercado.participacion.sincronizar_participados` (reexportado acá
    para que todo el flujo de orquestación viva bajo `mercado.service`)."""
    from src.mercado.participacion import sincronizar_participados as _sync

    return await _sync(cfg, cliente_id, max_pages=max_pages)


def resolver_resultados(cfg, cliente_id: str) -> dict[str, int]:
    """Cruza lo participado contra el registro de mercado ya cargado y
    resuelve `GANADO`/`PERDIDO`. Ver `mercado.repo.resolver_resultados` para
    el criterio exacto."""
    from src.memoria import db
    from src.mercado import repo

    with db.connection(cfg) as conn:
        resultado = repo.resolver_resultados(conn, cliente_id)
        pendientes = repo.contar_pendientes(conn, cliente_id)
        if getattr(cfg, "enable_esquema_v2", False):
            try:
                repo.prepoblar_oportunidad_estado_v2(conn, cliente_id)
            except Exception as exc:  # noqa: BLE001 — v2 nunca revierte la resolución v1
                logger.warning(
                    "Espejo v2 (oportunidad_estado) falló para %s; la resolución v1 queda intacta: %s",
                    cliente_id, exc,
                )

    logger.info(
        "Resultados resueltos para %s: %d ganado(s), %d perdido(s), %d aún pendiente(s).",
        cliente_id, resultado["ganados"], resultado["perdidos"], pendientes,
    )
    resultado["pendientes"] = pendientes
    return resultado
