"""src.memoria.backfill_v2 — Fase "Backfill" de la migración al esquema v2
(plan del portal AMALIA §3.3 Fase 3, §3.7 punto 3).

Reprocesa lo que YA existe en la ADB (``licitaciones_metadata``,
``licitaciones_analisis``, ``licitaciones_chunks``, ``licitaciones_reportadas``,
``licitaciones_feedback``, y de paso ``proceso_participado`` de
``src.mercado``) hacia las tablas nuevas de ``db/schema_v2.sql`` (``proceso``,
``cliente_proceso``, ``analisis``, ``chunk``, ``reportada``, ``feedback``,
``oportunidad_estado``), reutilizando los MISMOS escritores que ya usa la
doble escritura en vivo (``src.memoria.repo``, ``src.memoria.vectorizer``,
``src.feedback.repo``, ``src.mercado.repo``) — no una copia paralela del SQL,
mismo principio que ``src.memoria.reimportar``.

**Límite honesto de este backfill** (documentado, no oculto): el bug que
motiva toda la migración es que ``licitaciones_metadata`` tiene un solo
``cliente_id`` por fila, reasignado por el último cliente que la tocó, y
``licitaciones_analisis`` es una fila global sin ``cliente_id`` en absoluto.
Este script solo puede reconstruir el estado ACTUAL de v1 — no recupera
vistas de cliente que ya fueron sobrescritas antes de correr esto. A partir
de la Fase 2 (doble escritura, PR #39-#42, ya en producción) los datos
NUEVOS son correctos por cliente desde el momento en que se activa
``ENABLE_ESQUEMA_V2``; este backfill solo cierra la brecha del historial
previo a esa fecha, con la mejor información disponible.

**Requisito previo (descubierto validando este script contra Oracle real):**
`cliente_proceso`/`analisis` v2 tienen FK real hacia `cliente` (a
diferencia de v1, que no tiene esa restricción). Un `cliente_id` histórico
que ya no exista en `cliente` (p. ej. un cliente decomisionado, fuera de
`clients/*.yaml`) hace que ESA fila falle con `ORA-02291` — se loguea como
error y se salta, no tumba el resto del backfill (ver el try/except por
fila de cada paso), pero **el resultado queda incompleto para ese
cliente** hasta que se le agregue una fila a `cliente` (aunque sea con
`activo=0`). Correr `scripts/semilla_esquema_v2` primero cubre el caso
normal (clientes activos); un cliente ya decomisionado se sembra a mano si
se quiere backfillear su historial también.

**Volumen esperado** (plan §3.3 Fase 3): ~1-2 mil filas — trivial, una sola
conexión, pensado para correr UNA vez, fuera de la ventana de las 07:00 (sin
concurrencia con el pipeline).

**Idempotencia:** cada paso usa los mismos MERGE/DELETE+INSERT de
producción — correr el backfill dos veces no duplica nada.

Uso:
    python -m src.memoria.backfill_v2
    python -m src.memoria.backfill_v2 --dry-run
    python -m src.memoria.backfill_v2 --verificar   # solo compara conteos v1 vs v2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import types
from typing import Any

logger = logging.getLogger(__name__)


# ── Helpers de deserialización (JSON de la ADB → objetos que ya sabe leer
#    src.memoria.repo/vectorizer/src.feedback.repo) ──────────────────────────

def _a_dict(valor: Any) -> dict | None:
    """Una columna JSON puede volver de oracledb ya parseada (dict) o como
    texto — normaliza a `dict`, nunca lanza."""
    if valor is None:
        return None
    if isinstance(valor, dict):
        return valor
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        logger.warning("JSON con formato inesperado, se ignora: %r", valor)
        return None


def _a_lista(valor: Any) -> list:
    if valor is None:
        return []
    if isinstance(valor, list):
        return valor
    try:
        cargado = json.loads(valor)
        return cargado if isinstance(cargado, list) else []
    except (TypeError, ValueError):
        return []


def _matriz_desde_json(raw: Any):
    """Reconstruye una `MatrizRiesgos` desde la columna JSON de
    `licitaciones_analisis`. Reutiliza el mismo deserializador que
    `src.memoria.reimportar` ya usa para el JSONL — misma forma de dict
    (`dataclasses.asdict(MatrizRiesgos(...))`), distinto origen (ADB en vez
    de archivo)."""
    from src.memoria.reimportar import _deserializar_matriz

    return _deserializar_matriz(_a_dict(raw))


# ── Paso 1: proceso + cliente_proceso, desde licitaciones_metadata ──────────

def backfill_procesos(conn: Any) -> int:
    """Un `proceso`+`cliente_proceso` por fila de `licitaciones_metadata`.

    Reutiliza `repo.upsert_proceso_v2` (crea el hecho + la vista inicial del
    cliente con el `estado` de alta) y luego `repo.set_estado_v2` (rellena
    `triaje`/`triaje_conf`/`formato`/`ultimo_error`, que `upsert_proceso_v2`
    no toca — mismo patrón que ya sigue el pipeline en vivo: primero
    `registrar_proceso`/`persistir_memoria`, después `guardar_estado`).
    """
    from src.memoria import repo

    cursor = conn.cursor()
    cursor.execute(
        "SELECT proceso_id, cliente_id, institucion, titulo, categoria, modalidad, "
        "monto_estimado, fecha_pub, fecha_cierre, url_comprasal, estado, "
        "triaje, triaje_conf, formato, ultimo_error "
        "FROM licitaciones_metadata"
    )
    filas = cursor.fetchall()
    cursor.close()

    total = 0
    for row in filas:
        (proceso_id, cliente_id, institucion, titulo, categoria, modalidad,
         monto_estimado, fecha_pub, fecha_cierre, url_comprasal, estado,
         triaje, triaje_conf, formato, ultimo_error) = row
        try:
            ns = types.SimpleNamespace(
                numero_referencia=proceso_id,
                institucion=institucion,
                descripcion=titulo,
                tipo_actividad=categoria,
                forma_contratacion=modalidad,
                monto=float(monto_estimado) if monto_estimado is not None else None,
                fecha_publicacion=fecha_pub,
                fecha_limite=fecha_cierre,
                url_directa=url_comprasal,
            )
            repo.upsert_proceso_v2(conn, ns, cliente_id, estado=estado)
            repo.set_estado_v2(
                conn, proceso_id, cliente_id, estado,
                error=ultimo_error, triaje=triaje,
                triaje_conf=float(triaje_conf) if triaje_conf is not None else None,
                formato=formato,
            )
            total += 1
        except Exception as exc:  # noqa: BLE001 — una fila rota no frena al resto
            logger.error("backfill_procesos: %s/%s falló, se salta: %s", cliente_id, proceso_id, exc)

    logger.info("backfill_procesos: %d fila(s) procesada(s).", total)
    return total


def backfill_clasificacion(conn: Any, data_dir: str) -> int:
    """Reaplica clasificación (tags, marcas, categoría, scores) desde el JSONL.

    `licitaciones_metadata` no guarda la clasificación, así que
    `backfill_procesos` deja `cliente_proceso` sin ella. El historial
    `data/history/{cliente}.jsonl` sí la tiene (ver `reimportar.leer_clasificados`).
    Solo toca procesos que YA existen en `cliente_proceso`: nunca crea filas.
    """
    from src.memoria import repo
    from src.memoria.reimportar import leer_clasificados

    cursor = conn.cursor()
    cursor.execute(
        "SELECT cp.cliente_id, p.numero_referencia "
        "FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid"
    )
    filas = cursor.fetchall()
    cursor.close()

    existentes: dict[str, set[str]] = {}
    for cliente_id, numero_referencia in filas:
        existentes.setdefault(cliente_id, set()).add(numero_referencia)

    total = 0
    for cliente_id, refs in existentes.items():
        clasificados = leer_clasificados(cliente_id, data_dir)
        for ref in refs:
            p = clasificados.get(ref)
            if p is None:
                continue
            try:
                repo.upsert_proceso_v2(conn, p, cliente_id)
                total += 1
            except Exception as exc:  # noqa: BLE001 — una fila rota no frena al resto
                logger.error("backfill_clasificacion: %s/%s falló, se salta: %s", cliente_id, ref, exc)

    logger.info("backfill_clasificacion: %d fila(s) reaplicada(s).", total)
    return total


# ── Paso 2: analisis, desde licitaciones_analisis + licitaciones_metadata ───

def backfill_analisis(conn: Any) -> int:
    """Un `analisis` v2 por fila de `licitaciones_analisis`.

    `licitaciones_analisis` no tiene `cliente_id` propio (es exactamente el
    bug que motiva la migración): se toma el de `licitaciones_metadata` para
    ese `proceso_id` — el mismo límite honesto del módulo: si esa fila ya
    fue "pisada" por otro cliente antes de este backfill, hereda ese mismo
    cliente_id (no hay forma de recuperar el original, se perdió en v1).
    """
    from src.memoria import repo

    cursor = conn.cursor()
    cursor.execute(
        "SELECT a.proceso_id, a.matriz_riesgos, a.modelo_llm, m.cliente_id, m.monto_estimado "
        "FROM licitaciones_analisis a JOIN licitaciones_metadata m "
        "ON a.proceso_id = m.proceso_id"
    )
    filas = cursor.fetchall()
    cursor.close()

    total = 0
    for proceso_id, matriz_riesgos, modelo_llm, cliente_id, monto_estimado in filas:
        try:
            resultado = types.SimpleNamespace(
                proceso_uuid=proceso_id,
                matriz=_matriz_desde_json(matriz_riesgos),
                modelo_analisis=modelo_llm,
                monto_estimado=float(monto_estimado) if monto_estimado is not None else None,
            )
            repo.save_analisis_v2(conn, resultado, cliente_id)
            total += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("backfill_analisis: %s/%s falló, se salta: %s", cliente_id, proceso_id, exc)

    logger.info("backfill_analisis: %d fila(s) procesada(s).", total)
    return total


# ── Paso 3: chunk, desde licitaciones_chunks (copia el vector, no lo recalcula) ─

def backfill_chunks(conn: Any) -> int:
    """Copia los chunks ya vectorizados, agrupados por `proceso_id`, hacia
    `chunk` v2 — usa `vectorizer.copiar_chunks_v2` (sin volver a llamar
    `VECTOR_EMBEDDING`: el vector ya existe, recalcularlo sería gasto de
    cómputo innecesario para una simple copia).
    """
    from src.memoria import vectorizer

    cursor = conn.cursor()
    cursor.execute(
        "SELECT proceso_id, seq, pagina, chunk_text, embedding "
        "FROM licitaciones_chunks ORDER BY proceso_id, seq"
    )
    filas = cursor.fetchall()
    cursor.close()

    por_proceso: dict[str, list[tuple[int, int | None, str, Any]]] = {}
    for proceso_id, seq, pagina, chunk_text, embedding in filas:
        por_proceso.setdefault(proceso_id, []).append((seq, pagina, chunk_text, embedding))

    total = 0
    for proceso_id, chunks in por_proceso.items():
        try:
            total += vectorizer.copiar_chunks_v2(conn, proceso_id, chunks)
        except Exception as exc:  # noqa: BLE001
            logger.error("backfill_chunks: %s falló, se salta (%d chunk(s)): %s",
                         proceso_id, len(chunks), exc)

    logger.info("backfill_chunks: %d chunk(s) copiado(s) en %d proceso(s).", total, len(por_proceso))
    return total


# ── Paso 4: reportada + feedback, desde licitaciones_reportadas/_feedback ───

def backfill_reportadas(conn: Any) -> int:
    from src.feedback import repo as feedback_repo

    cursor = conn.cursor()
    cursor.execute("SELECT proceso_id, cliente_id FROM licitaciones_reportadas")
    filas = cursor.fetchall()
    cursor.close()

    total = 0
    for proceso_id, cliente_id in filas:
        try:
            ns = types.SimpleNamespace(numero_referencia=proceso_id)
            feedback_repo.upsert_reportada_v2(conn, cliente_id, ns)
            total += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("backfill_reportadas: %s/%s falló, se salta: %s", cliente_id, proceso_id, exc)

    logger.info("backfill_reportadas: %d fila(s) procesada(s).", total)
    return total


def backfill_feedback(conn: Any) -> int:
    from src.feedback import repo as feedback_repo

    cursor = conn.cursor()
    cursor.execute(
        "SELECT proceso_id, cliente_id, destinatario_id, voto, motivos, comentario "
        "FROM licitaciones_feedback"
    )
    filas = cursor.fetchall()
    cursor.close()

    total = 0
    for proceso_id, cliente_id, destinatario_id, voto, motivos, comentario in filas:
        try:
            feedback_repo.upsert_voto_v2(
                conn, proceso_id=proceso_id, cliente_id=cliente_id,
                destinatario_id=destinatario_id, voto=voto,
                motivos=_a_lista(motivos), comentario=comentario or "",
            )
            total += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "backfill_feedback: %s/%s/%s falló, se salta: %s",
                cliente_id, proceso_id, destinatario_id, exc,
            )

    logger.info("backfill_feedback: %d fila(s) procesada(s).", total)
    return total


# ── Paso 5 (bonus, §3.5): oportunidad_estado, desde proceso_participado ─────

def backfill_oportunidades(conn: Any) -> int:
    """Prepuebla `oportunidad_estado` para TODOS los clientes con algo en
    `proceso_participado` — reutiliza `mercado.repo.prepoblar_oportunidad_estado_v2`,
    ya idempotente y con la guarda anti-pisado de entradas `MANUAL` (PR #42).
    No es estrictamente parte de licitaciones_*→v2, pero corre gratis acá:
    mismo alcance de "backfillear todo lo que la doble escritura ya cubre".
    """
    from src.mercado import repo as mercado_repo

    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT cliente_id FROM proceso_participado")
    clientes = [c for (c,) in cursor.fetchall()]
    cursor.close()

    total = 0
    for cliente_id in clientes:
        try:
            total += mercado_repo.prepoblar_oportunidad_estado_v2(conn, cliente_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("backfill_oportunidades: %s falló, se salta: %s", cliente_id, exc)

    logger.info("backfill_oportunidades: %d fila(s) en %d cliente(s).", total, len(clientes))
    return total


# ── Verificación: conteos v1 vs v2 ───────────────────────────────────────────

_PARES = (
    ("licitaciones_metadata", "proceso"),
    ("licitaciones_metadata", "cliente_proceso"),
    ("licitaciones_analisis", "analisis"),
    ("licitaciones_chunks", "chunk"),
    ("licitaciones_reportadas", "reportada"),
    ("licitaciones_feedback", "feedback"),
)


def verificar_conteos(conn: Any) -> dict[str, tuple[int, int]]:
    """`{tabla_v2: (conteo_v1, conteo_v2)}` — el criterio de aceptación del
    plan (§3.3 Fase 3): deben coincidir tras un backfill completo y limpio
    (una corrida sobre una v2 ya parcialmente poblada por la doble escritura
    en vivo puede NO coincidir exactamente, y está bien: v2 solo puede tener
    más filas que v1 desde ahí en adelante, nunca menos)."""
    cursor = conn.cursor()
    resultado = {}
    try:
        for tabla_v1, tabla_v2 in _PARES:
            cursor.execute(f"SELECT COUNT(*) FROM {tabla_v1}")  # noqa: S608 — literal fijo, no entrada
            n1 = cursor.fetchone()[0]
            cursor.execute(f"SELECT COUNT(*) FROM {tabla_v2}")  # noqa: S608
            n2 = cursor.fetchone()[0]
            resultado[tabla_v2] = (n1, n2)
    finally:
        cursor.close()
    return resultado


# ── Orquestación ─────────────────────────────────────────────────────────────

def backfill_v2(cfg, dry_run: bool = False) -> dict[str, int]:
    """Corre los pasos en orden (proceso antes que analisis/chunk: las
    FK de v2 lo exigen) dentro de una sola transacción por paso — cada paso
    comitea al terminar, así una falla en el paso 3 no deshace el 1 y el 2.
    """
    from src.memoria import db

    if dry_run:
        with db.connection(cfg) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM licitaciones_metadata")
            n_meta = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM licitaciones_analisis")
            n_analisis = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM licitaciones_chunks")
            n_chunks = cursor.fetchone()[0]
            cursor.close()
        logger.info(
            "[dry-run] backfillearía ~%d proceso(s), ~%d análisis(is), ~%d chunk(s) (no escribe nada)",
            n_meta, n_analisis, n_chunks,
        )
        return {"procesos": n_meta, "analisis": n_analisis, "chunks": n_chunks}

    conteos = {}
    with db.connection(cfg) as conn:
        conteos["procesos"] = backfill_procesos(conn)
        conn.commit()
        conteos["clasificacion"] = backfill_clasificacion(conn, cfg.data_dir)
        conn.commit()
        conteos["analisis"] = backfill_analisis(conn)
        conn.commit()
        conteos["chunks"] = backfill_chunks(conn)
        conn.commit()
        conteos["reportadas"] = backfill_reportadas(conn)
        conn.commit()
        conteos["feedback"] = backfill_feedback(conn)
        conn.commit()
        conteos["oportunidades"] = backfill_oportunidades(conn)
        conn.commit()

    logger.info("Backfill v2 completo: %s", conteos)
    return conteos


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="solo cuenta, no escribe nada")
    parser.add_argument("--verificar", action="store_true", help="solo compara conteos v1 vs v2, no escribe nada")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    from src.config import get_config
    cfg = get_config()

    if args.verificar:
        from src.memoria import db
        with db.connection(cfg) as conn:
            resultado = verificar_conteos(conn)
        for tabla, (n1, n2) in resultado.items():
            estado = "OK" if n1 == n2 else "DIFIERE"
            logger.info("%-24s v1=%-6d v2=%-6d [%s]", tabla, n1, n2, estado)
        return

    resultado = backfill_v2(cfg, dry_run=args.dry_run)
    logger.info("Resumen: %s", resultado)


if __name__ == "__main__":
    main()


__all__ = [
    "backfill_analisis",
    "backfill_chunks",
    "backfill_feedback",
    "backfill_oportunidades",
    "backfill_procesos",
    "backfill_reportadas",
    "backfill_v2",
    "verificar_conteos",
]
