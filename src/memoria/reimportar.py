"""src.memoria.reimportar — recuperación ante desastre: JSONL → ADB, sin LLM.

**Por qué existe** (plan del portal, §2.3 y §9): la ADB Always Free **no
ofrece ninguna restauración** (ni respaldo manual, ni PITR — verificado en la
documentación de Oracle). Si se corrompe o se borra algo, Oracle no tiene
vuelta atrás. La mitigación elegida no agrega infraestructura nueva: el
JSONL de cada cliente (`data/history/*.jsonl`, `*.analisis.jsonl`) YA es una
copia completa y ya respaldada (`amalia-backup.sh`, 09:00) de todo lo que el
LLM calculó — `es_relevante`, scores, tags, marcas, matriz de riesgos, monto
estimado. **Reimportarlo no vuelve a llamar al LLM**: solo repite, en la
ADB, el mismo INSERT/UPDATE que el pipeline ya hizo la primera vez. El costo
de una restauración completa es tiempo de cómputo, **nunca** el costo del
modelo.

**Qué NO toca:** `licitaciones_chunks` (los vectores) — esta herramienta no
tiene el texto completo del pliego, solo lo que el clasificador/analizador ya
resumió. Si además se perdieron los vectores, hay que re-vectorizar aparte
(sí requiere el PDF, no este JSONL); la búsqueda de precedentes queda
degradada mientras tanto, no la metadata ni el análisis.

**Cómo se arma la reimportación, por cliente:**

1. `data/history/{cliente}.jsonl` (una línea por CORRIDA, cada una con una
   lista de `ProcesoClasificado`): se aplana y se deduplica por
   `numero_referencia`, quedándose con la última aparición — mismo criterio
   que ya usa `history.analisis_estado` para `.analisis.jsonl`. De acá sale
   institución, título, categoría, monto, tags — todo lo que
   `repo.upsert_proceso` ya sabe mapear.
2. `data/history/{cliente}.analisis.jsonl` (una línea por análisis de
   drill-down): deduplicado por `proceso_uuid`, última aparición. De acá sale
   el `estado` real (`ANALIZADO`, `DESCARTADO_TRIAJE`, ...), la matriz de
   riesgos, `triaje`/`formato`.
3. Se hace `upsert_proceso` con TODO lo del paso 1, y luego `set_estado` +
   `save_analisis_db` con TODO lo del paso 2 — los mismos dos escritores que
   ya usa el pipeline en vivo (`memoria/repo.py`), no una copia paralela del
   SQL.

**Por qué no pasa por `memoria.service`:** las funciones de `service.py`
hacen DOBLE escritura (ADB + JSONL). Reimportar A PARTIR del JSONL y
escribirlo OTRA VEZ en el mismo JSONL duplicaría cada línea en el archivo
que se está leyendo. Este módulo llama a `memoria.repo` directo, que solo
toca la ADB.

**Huérfanos:** un `proceso_uuid` de `.analisis.jsonl` sin ninguna fila
correspondiente en `.jsonl` (posible tras un corte a mitad de una corrida) no
tiene institución/monto/etc. que reimportar — y `licitaciones_analisis`
tiene FK hacia `licitaciones_metadata`, así que insertarlo sin la fila padre
fallaría. Se loguea como advertencia y se salta; no tumba el resto de la
reimportación.

Uso:
    python -m src.memoria.reimportar --cliente multicomputos
    python -m src.memoria.reimportar --cliente multicomputos --dry-run
    python -m src.memoria.reimportar --todos
    python -m src.memoria.reimportar --vaciar-spool          # reenvía lo que la ADB no alcanzó a recibir
    python -m src.memoria.reimportar --vaciar-spool --dry-run

**Spool (ESTADO_FUENTE=adb, docs/planes/persistencia-adb.md Fase 3):** con la ADB como fuente de verdad el
historial JSONL deja de escribirse salvo lo que la ADB no recibió, que espera en `data/history/spool/`.
`--vaciar-spool` lo reenvía con los mismos escritores idempotentes (MERGE), así que repetirlo no duplica.
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.classifier.alias_clientes import id_canonico
from src.scraper.models import (
    MatrizRiesgos,
    Precedente,
    ProcesoClasificado,
    ResultadoAnalisis,
    RiesgoItem,
    UsoModelo,
)

logger = logging.getLogger(__name__)


# ── Lectura y deserialización del JSONL ──────────────────────────────────────

def _leer_jsonl(path: str) -> list[dict]:
    """Lee un archivo JSONL, tolerando líneas corruptas (se saltan con warning).

    Ausencia del archivo no es un error: un cliente nuevo, o uno que nunca
    tuvo drill-down, simplemente no tiene nada que reimportar.
    """
    import os

    if not os.path.exists(path):
        return []
    lineas = []
    with open(path, "r", encoding="utf-8") as f:
        for n, cruda in enumerate(f, start=1):
            cruda = cruda.strip()
            if not cruda:
                continue
            try:
                import json
                lineas.append(json.loads(cruda))
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s:%d línea corrupta, se salta (%s)", path, n, exc)
    return lineas


def _deserializar_uso(d: dict | None) -> UsoModelo | None:
    if not d:
        return None
    return UsoModelo(
        modelo=d.get("modelo", ""),
        input_tokens=int(d.get("input_tokens", 0) or 0),
        output_tokens=int(d.get("output_tokens", 0) or 0),
        llamadas=int(d.get("llamadas", 0) or 0),
    )


def _deserializar_matriz(d: dict | None) -> MatrizRiesgos | None:
    if not d:
        return None
    items = [
        RiesgoItem(
            categoria=it.get("categoria", "tecnico"),
            descripcion=it.get("descripcion", ""),
            severidad=it.get("severidad", "media"),
            clausula_ref=it.get("clausula_ref", ""),
        )
        for it in (d.get("items") or [])
        if isinstance(it, dict)
    ]
    return MatrizRiesgos(items=items, resumen=d.get("resumen", ""))


def _deserializar_precedentes(lista: list | None) -> list[Precedente]:
    return [
        Precedente(
            proceso_id=str(p.get("proceso_id", "")),
            institucion=str(p.get("institucion") or ""),
            titulo=str(p.get("titulo") or ""),
            fecha_pub=str(p.get("fecha_pub") or ""),
            url_comprasal=str(p.get("url_comprasal") or ""),
            extracto=str(p.get("extracto") or ""),
            distancia=float(p.get("distancia") or 0.0),
        )
        for p in (lista or [])
        if isinstance(p, dict)
    ]


def _deserializar_resultado_analisis(d: dict) -> ResultadoAnalisis:
    """El inverso de `dataclasses.asdict(ResultadoAnalisis)` (history.save_analisis).

    Tolerante a claves faltantes (defaults del propio dataclass) porque el
    esquema de este JSONL creció con el tiempo (`monto_estimado` es de la
    Fase 0 más reciente y no existe en líneas viejas).
    """
    return ResultadoAnalisis(
        proceso_uuid=d.get("proceso_uuid", ""),
        estado=d.get("estado", ""),
        formato=d.get("formato", ""),
        triaje=d.get("triaje", ""),
        triaje_confianza=float(d.get("triaje_confianza", 0.0) or 0.0),
        competible=d.get("competible") if isinstance(d.get("competible"), bool) else None,
        motivo_bloqueo=str(d.get("motivo_bloqueo") or ""),
        matriz=_deserializar_matriz(d.get("matriz")),
        anexos=d.get("anexos") or {},
        monto_estimado=d.get("monto_estimado"),
        modelo_analisis=d.get("modelo_analisis", ""),
        error=d.get("error", ""),
        generado_en=d.get("generado_en", ""),
        n_paginas=int(d.get("n_paginas", 0) or 0),
        tamano_bytes=int(d.get("tamano_bytes", 0) or 0),
        uso_triaje=_deserializar_uso(d.get("uso_triaje")),
        uso_analisis=_deserializar_uso(d.get("uso_analisis")),
        precedentes=_deserializar_precedentes(d.get("precedentes")),
    )


# ── Deduplicación: última aparición gana (mismo criterio que analisis_estado) ─

def leer_clasificados(client_id: str, data_dir: str) -> dict[str, ProcesoClasificado]:
    """`{numero_referencia: ProcesoClasificado}`, la última corrida que lo vio.

    `data/history/{cliente}.jsonl` es una línea por CORRIDA (no por
    proceso); cada línea trae una lista completa. Se aplana y se deduplica.
    """
    from src.history import _deserialize, _history_path

    path = _history_path(client_id, data_dir)
    resultado: dict[str, ProcesoClasificado] = {}
    for entrada in _leer_jsonl(path):
        for crudo in entrada.get("procesos") or []:
            try:
                p = _deserialize(crudo)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] proceso ilegible en %s, se salta (%s)", client_id, path, exc)
                continue
            if p.numero_referencia:
                resultado[p.numero_referencia] = p
    return resultado


def leer_analisis(client_id: str, data_dir: str) -> dict[str, ResultadoAnalisis]:
    """`{proceso_uuid: ResultadoAnalisis}`, la última línea que lo menciona."""
    from src.history import _analisis_path

    path = _analisis_path(client_id, data_dir)
    resultado: dict[str, ResultadoAnalisis] = {}
    for crudo in _leer_jsonl(path):
        try:
            r = _deserializar_resultado_analisis(crudo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] análisis ilegible en %s, se salta (%s)", client_id, path, exc)
            continue
        if r.proceso_uuid:
            resultado[r.proceso_uuid] = r
    return resultado


# ── Reimportación ─────────────────────────────────────────────────────────────

def reimportar_cliente(cfg, client_id: str, dry_run: bool = False) -> dict[str, int]:
    """Reimporta todo lo reimportable de un cliente. Devuelve conteos.

    Idempotente: usa los mismos MERGE/UPDATE de `memoria.repo`, así que
    correrlo dos veces (o sobre datos que ya están en la ADB) dejan el mismo
    resultado, no filas duplicadas.
    """
    from src.memoria import db, repo

    clasificados = leer_clasificados(client_id, cfg.data_dir)
    analisis = leer_analisis(client_id, cfg.data_dir)

    conteos = {"clasificados": 0, "analisis": 0, "huerfanos": 0}

    if dry_run:
        logger.info(
            "[%s] dry-run: %d clasificado(s), %d análisis(is) — nada se escribe",
            client_id, len(clasificados), len(analisis),
        )
        conteos["clasificados"] = len(clasificados)
        conteos["analisis"] = len(analisis)
        return conteos

    with db.connection(cfg) as conn:
        for numero_referencia, proceso in clasificados.items():
            resultado = analisis.get(numero_referencia)
            estado_inicial = resultado.estado if resultado else "DESCUBIERTO"
            repo.upsert_proceso(conn, proceso, client_id, estado=estado_inicial)
            conteos["clasificados"] += 1

        conocidos = set(clasificados)
        for proceso_uuid, resultado in analisis.items():
            if proceso_uuid not in conocidos:
                logger.warning(
                    "[%s] %s tiene análisis pero no aparece en el historial de "
                    "clasificación (posible corte a mitad de una corrida): se salta.",
                    client_id, proceso_uuid,
                )
                conteos["huerfanos"] += 1
                continue
            repo.set_estado(
                conn, proceso_uuid, resultado.estado,
                error=resultado.error or None,
                triaje=resultado.triaje or None,
                triaje_conf=resultado.triaje_confianza if resultado.triaje else None,
                formato=resultado.formato or None,
            )
            repo.save_analisis_db(conn, resultado)
            conteos["analisis"] += 1

    logger.info(
        "[%s] reimportado: %d proceso(s), %d análisis(is), %d huérfano(s) saltado(s)",
        client_id, conteos["clasificados"], conteos["analisis"], conteos["huerfanos"],
    )
    return conteos


def reimportar_todos(cfg, client_ids: list[str], dry_run: bool = False) -> dict[str, dict[str, int]]:
    """Reimporta varios clientes, uno por uno. Un cliente roto no frena a los demás."""
    resultado = {}
    for client_id in client_ids:
        try:
            resultado[client_id] = reimportar_cliente(cfg, client_id, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] la reimportación falló por completo: %s", client_id, exc)
            resultado[client_id] = {"error": str(exc)}
    return resultado


def vaciar_spool(cfg, dry_run: bool = False) -> dict[str, tuple[int, int]]:
    """Reenvía el spool de historial a la ADB. `{cola: (enviados, quedan)}`; con `dry_run` solo cuenta.

    No depende de `ESTADO_FUENTE`: es la herramienta manual (p. ej. antes de volver el flag a `archivo`).
    """
    from src import history

    if dry_run:
        sp = history._spool(cfg.data_dir)
        if not sp.directorio.is_dir():
            return {}
        return {p.stem: (0, len(sp.pendientes(p.stem))) for p in sorted(sp.directorio.glob("*.jsonl"))}
    return history.drenar_spool(cfg, forzar=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--vaciar-spool", action="store_true",
                       help="reenvía a la ADB lo que quedó en data/history/spool/ (idempotente)")
    grupo.add_argument("--cliente", help="client_id a reimportar (ej. multicomputos_sv)")
    grupo.add_argument("--todos", action="store_true", help="reimporta todos los clientes activos")
    parser.add_argument("--dry-run", action="store_true", help="solo cuenta, no escribe nada")
    args = parser.parse_args()
    if args.cliente:
        args.cliente = id_canonico(args.cliente)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    from src.config import get_config
    cfg = get_config()

    if args.vaciar_spool:
        logger.info("Spool: %s", vaciar_spool(cfg, dry_run=args.dry_run))
        return

    if args.todos:
        from src.classifier.client_profile import load_all_active_profiles
        clientes = [p.client_id for p in load_all_active_profiles(cfg.clients_dir)]
        resultado = reimportar_todos(cfg, clientes, dry_run=args.dry_run)
    else:
        resultado = {args.cliente: reimportar_cliente(cfg, args.cliente, dry_run=args.dry_run)}

    logger.info("Resumen: %s", resultado)


if __name__ == "__main__":
    main()


__all__ = [
    "leer_analisis",
    "leer_clasificados",
    "reimportar_cliente",
    "reimportar_todos",
    "vaciar_spool",
]
