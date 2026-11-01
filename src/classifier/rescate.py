"""C5 optativa: similitud in-DB propone revisión, nunca afirma una marca.

La cola de exploración y la reserva diaria viven en decision_capa; la transacción
pertenece al llamador. No se modifica ningún ProcesoClasificado recibido.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from src.memoria.vectorizer import _validar_nombre_modelo, con_prefijo

logger = logging.getLogger(__name__)
# Punto inicial conservador, calibrable con evaluación etiquetada (no probabilidad).
UMBRAL_SIMILITUD = 0.85
CUPO_DIARIO = 1


def rescatar(conn, cfg, procesos_no_relevantes, cliente_id, k=5):
    """Devuelve hasta un candidato diario con destino «Por confirmar» en activo.

    Sombra calcula y registra en exploración, pero devuelve []. El resto, incluso
    bajo umbral, queda en decision_capa como exploración. k limita vecinos por
    título (1..100). Un descarte determinista explícito nunca se rescata.
    El bloqueo de cliente serializa el cupo entre llamadas/conexiones: el llamador
    debe confirmar o revertir la transacción antes de publicar los resultados.
    ADB/modelo ausente ⇒ []; no abre conexiones ni llama al LLM.
    """
    modo = getattr(cfg, "intel_c5", "off")
    if modo not in {"activo", "sombra"}:
        return []
    cursor = None
    try:
        modelo = _validar_nombre_modelo(getattr(cfg, "embedding_model_name", "MULTILINGUAL_E5_SMALL"))
        limite = min(100, max(1, int(k)))
        cursor = conn.cursor()
        cursor.execute("SAVEPOINT rescate_c5")
        cursor.execute("SELECT cliente_id FROM cliente WHERE cliente_id = :c FOR UPDATE", {"c": cliente_id})
        if not cursor.fetchone():
            return []
        dia = datetime.now(ZoneInfo("America/El_Salvador")).date().isoformat()
        cursor.execute("""SELECT COUNT(*) FROM decision_capa
            WHERE cliente_id = :c AND capa = 'C5' AND variante = 'activa'
              AND JSON_VALUE(salida, '$.dia') = :dia
              AND JSON_VALUE(salida, '$.destino') = 'Por confirmar'""", {"c": cliente_id, "dia": dia})
        usados = int(cursor.fetchone()[0])
        candidatos = []
        for proceso in procesos_no_relevantes:
            if getattr(proceso, "es_relevante", False) or getattr(proceso, "descartado_determinista", False):
                continue
            cursor.execute(f"""
                WITH catalogo AS (
                    SELECT j.texto FROM cliente_config c,
                        JSON_TABLE(c.contenido, '$.catalog.categories[*].items[*]'
                            COLUMNS (texto VARCHAR2(1000) PATH '$')) j
                    WHERE c.cliente_id = :c AND c.activa = 1
                    UNION
                    SELECT j.texto FROM cliente_config c,
                        JSON_TABLE(c.contenido, '$.catalog.categories[*].brands[*]'
                            COLUMNS (texto VARCHAR2(1000) PATH '$.name')) j
                    WHERE c.cliente_id = :c AND c.activa = 1
                    UNION
                    SELECT j.texto FROM cliente_config c,
                        JSON_TABLE(c.contenido, '$.catalog.categories[*].brands[*].products[*]'
                            COLUMNS (texto VARCHAR2(1000) PATH '$')) j
                    WHERE c.cliente_id = :c AND c.activa = 1
                )
                SELECT texto, 1 - VECTOR_DISTANCE(
                    VECTOR_EMBEDDING({modelo} USING :titulo AS data),
                    VECTOR_EMBEDDING({modelo} USING :prefijo || texto AS data), COSINE) similitud
                FROM catalogo WHERE texto IS NOT NULL
                ORDER BY similitud DESC, texto FETCH FIRST :k ROWS ONLY
            """, {"c": cliente_id, "titulo": con_prefijo(cfg, "query", proceso.descripcion),
                  "prefijo": con_prefijo(cfg, "passage", ""), "k": limite})
            filas = cursor.fetchall()
            validas = [(texto, float(score)) for texto, score in filas
                       if score is not None and math.isfinite(float(score)) and -1 <= float(score) <= 1]
            if validas:
                texto, score = max(validas, key=lambda f: f[1])
                candidatos.append({"proceso": proceso, "item": texto, "score": score})
        resultados, vistos = [], set()
        for candidato in sorted(candidatos, key=lambda c: -c["score"]):
            proceso = candidato["proceso"]
            gid = getattr(proceso, "proceso_gid", None) or (
                f"{getattr(proceso, 'portal_id', 'comprasal_sv')}:{proceso.numero_referencia}")
            if gid in vistos:
                continue
            vistos.add(gid)
            elegido = modo == "activo" and usados < CUPO_DIARIO and candidato["score"] >= UMBRAL_SIMILITUD
            destino = "Por confirmar" if elegido else "exploracion"
            salida = {"dia": dia, "destino": destino, "item": candidato["item"],
                      "motivo": "Afinidad semántica sin evidencia de marca"}
            cursor.execute("""INSERT INTO decision_capa
                (cliente_id, proceso_gid, capa, version, variante, salida, score)
                VALUES (:c, :p, 'C5', 'rescate-v1', :v, JSON(:s), :score)""",
                           {"c": cliente_id, "p": gid, "v": "activa" if modo == "activo" else "sombra",
                            "s": json.dumps(salida, ensure_ascii=False), "score": candidato["score"]})
            if elegido:
                usados += 1
                resultados.append({**candidato, **salida})
        return resultados
    except Exception as exc:  # noqa: BLE001 — memoria opcional
        if cursor is not None:
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT rescate_c5")
            except Exception:  # noqa: BLE001
                pass
        logger.warning("Rescate C5 no disponible (%s)", type(exc).__name__)
        return []
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass
