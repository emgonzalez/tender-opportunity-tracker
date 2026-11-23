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

SICOP (Costa Rica) tiene su propia corrida, `sincronizar_sicop` (`python -m src.mercado --portal
sicop_cr`, dry-run por defecto): solo GANADO, ver la limitación en `mercado.sicop_publica`.
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
                conflictos = repo.listar_conflictos_oportunidad_v2(conn, cliente_id)
                if conflictos:
                    logger.warning(
                        "%d oportunidad(es) de %s con estado manual distinto del resultado de COMPRASAL "
                        "(no se pisan): %s",
                        len(conflictos), cliente_id, ", ".join(c["proceso_gid"] for c in conflictos[:10]),
                    )
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


# ── SICOP (Costa Rica): docs/arquitectura/portales.md §10.4 (A8) ─────────────
#
# Una sola corrida, por `portal_id`, dry-run por defecto:
#   1. por cada cédula propia (`identidad.IDS_PROPIOS[portal_id]`), lo adjudicado a ella → filas
#      propias de `adjudicacion_partida`;
#   2. con `aplicar`, esas filas pasan a `proceso_participado` como GANADO (nunca PERDIDO: ver la
#      limitación en `mercado.sicop_publica`) y, con `enable_esquema_v2`, a `oportunidad_estado`;
#   3. opcional (`incluir_mercado`): el mercado completo adjudicado en firme, por partida, CRC y USD.
# El paso 3 va al final a propósito: es el pesado (~7 s por página) y un bloqueo del WAF a mitad de
# camino no debe impedir que el resultado propio quede registrado.

CLIENTE_SICOP_DEFAULT = "multicomputos_cr"
_NOTA_EVENTO_SICOP = "Resultado publicado en SICOP"


async def sincronizar_sicop(
    cfg,
    transporte,
    *,
    cliente_id: str = CLIENTE_SICOP_DEFAULT,
    desde,
    hasta,
    incluir_mercado: bool = False,
    mercado_desde=None,
    max_paginas: int | None = None,
    aplicar: bool = False,
    portal_id: str = "sicop_cr",
    cliente_publico=None,
) -> dict:
    """Sincroniza adjudicaciones públicas de SICOP y el resultado propio (GANADO).

    `transporte` es un `TransporteJSON` ya abierto (navegador anónimo en producción, falso en pruebas).
    Sin `aplicar` no abre la ADB: solo cuenta lo que cargaría. Los errores del portal
    (`BloqueoAcceso`, `ContratoRoto`, `PortalNoDisponible`) se propagan sin reintentar el bloqueo; lo ya
    comiteado (por página) queda.
    """
    from contextlib import nullcontext

    from src.mercado import identidad, repo, sicop_publica

    if portal_id != sicop_publica.PORTAL_ID:
        raise ValueError(f"sincronizar_sicop solo conoce {sicop_publica.PORTAL_ID!r}, no {portal_id!r}")

    cliente = cliente_publico or sicop_publica.ClienteSicopPublico(
        transporte, max_paginas=max_paginas or sicop_publica.MAX_PAGINAS,
    )
    resumen = {
        "aplicado": aplicar, "propias": 0, "ganados": 0, "mercado": 0,
        "monto_propio_crc": 0.0, "monto_propio_usd": 0.0, "propias_sin_moneda": 0,
        "mercado_crc": 0.0, "mercado_usd": 0.0, "truncado": False,
    }

    if aplicar:
        from src.memoria import db
        contexto = db.connection(cfg)
    else:
        contexto = nullcontext(None)

    with contexto as conn:
        # 1) Lo adjudicado a cada cédula propia.
        for cedula in sorted(identidad.IDS_PROPIOS.get(portal_id, frozenset())):
            async for pagina in cliente.adjudicado_a_proveedor(cedula, desde, hasta):
                for crudo in pagina:
                    fila = sicop_publica.fila_adjudicada_a(
                        crudo, cedula, es_propio=identidad.es_propio(portal_id, cedula),
                    )
                    resumen["propias"] += 1
                    resumen["monto_propio_crc"] += fila["monto_crc"] or 0.0
                    resumen["monto_propio_usd"] += fila["monto_usd"] or 0.0
                    if fila["monto_crc"] is None and fila["monto_usd"] is None:
                        resumen["propias_sin_moneda"] += 1
                    if conn is not None:
                        repo.upsert_adjudicacion_partida(conn, fila)
                if conn is not None:
                    conn.commit()

        # 2) GANADO en proceso_participado (y su espejo v2).
        if conn is not None:
            resumen["ganados"] = repo.registrar_ganados_portal(conn, cliente_id, portal_id)
            conn.commit()
            if getattr(cfg, "enable_esquema_v2", False):
                try:
                    repo.prepoblar_oportunidad_estado_v2(conn, cliente_id, portal_id, nota=_NOTA_EVENTO_SICOP)
                    conn.commit()
                except Exception as exc:  # noqa: BLE001 — v2 nunca revierte lo ya registrado en v1
                    logger.warning("Espejo v2 (oportunidad_estado) falló para %s en %s: %s",
                                   cliente_id, portal_id, exc)

        # 3) Mercado completo en firme (pesado, con tope de páginas).
        if incluir_mercado:
            async for pagina in cliente.adjudicado_en_firme(mercado_desde or desde, hasta):
                for crudo in pagina:
                    fila = sicop_publica.fila_firme(crudo)
                    resumen["mercado"] += 1
                    resumen["mercado_crc"] += fila["monto_crc"] or 0.0
                    resumen["mercado_usd"] += fila["monto_usd"] or 0.0
                    if conn is not None:
                        repo.upsert_adjudicacion_partida(conn, fila)
                if conn is not None:
                    conn.commit()
                logger.info("SICOP: %d partida(s) de mercado hasta ahora.", resumen["mercado"])

    resumen["truncado"] = bool(cliente.truncado)
    logger.info(
        "SICOP %s (%s): %d adjudicación(es) propia(s) [CRC %.2f · USD %.2f · %d sin moneda], "
        "%d GANADO nuevo(s), %d partida(s) de mercado%s.",
        "aplicado" if aplicar else "DRY-RUN", cliente_id, resumen["propias"], resumen["monto_propio_crc"],
        resumen["monto_propio_usd"], resumen["propias_sin_moneda"], resumen["ganados"], resumen["mercado"],
        " (TRUNCADO por tope de páginas)" if resumen["truncado"] else "",
    )
    return resumen
