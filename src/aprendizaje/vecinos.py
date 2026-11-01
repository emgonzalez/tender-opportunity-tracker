"""Vecinos de marca en Oracle y calibración conservadora; nunca deciden el veredicto."""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from src.memoria.vectorizer import _validar_nombre_modelo

logger = logging.getLogger(__name__)

PESOS_FUENTE = {
    "humana": 1.0, "confirmacion": 1.0, "resultado_comercial": 1.0,
    "pliego": 0.9, "voto_usuario": 0.8, "participacion": 0.7,
    "verificacion_llm": 0.4, "semilla": 0.3,
}


def consultar_vecinos(conn: Any, cliente_id: str, marca_id: str, titulo: str,
                       k: int = 8) -> list[dict[str, Any]]:
    """Consulta exacta in-DB, aislada por cliente y marca, sin abrir conexiones.

    Solo usa ejemplos de entrenamiento activos; no modifica embeddings existentes.
    ADB/modelo no disponible o corpus vacío devuelve una lista vacía (score neutro).
    """
    cursor = None
    try:
        modelo = _validar_nombre_modelo("MULTILINGUAL_E5_SMALL")
        limite = min(100, max(1, int(k)))
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT ejemplo_id, titulo_texto, etiqueta, fuente, peso, creado_en, evidencia,
                   VECTOR_DISTANCE(embedding,
                       VECTOR_EMBEDDING({modelo} USING :t AS data), COSINE) AS distancia
            FROM ejemplo_marca
            WHERE cliente_id = :cliente_id AND marca_id = :marca_id
              AND activo = 1 AND split = 'entrenamiento' AND embedding IS NOT NULL
            ORDER BY distancia, ejemplo_id
            FETCH FIRST :k ROWS ONLY
        """, {"t": "query: " + titulo, "cliente_id": cliente_id,
              "marca_id": marca_id, "k": limite})
        claves = ("ejemplo_id", "titulo_texto", "etiqueta", "fuente", "peso",
                  "creado_en", "evidencia", "distancia")
        vecinos = [dict(zip(claves, fila)) for fila in cursor.fetchall()]
        if not vecinos:
            logger.info("Vecinos de marca: sin ejemplos; calibración neutra")
        return vecinos
    except Exception as exc:  # noqa: BLE001 — la corrida no depende de la ADB
        logger.warning("Vecinos de marca no disponibles (%s); calibración neutra", type(exc).__name__)
        return []
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 — tampoco propagar fallos al cerrar
                logger.debug("No se pudo cerrar el cursor de vecinos")


def _numero(valor: Any, defecto: float) -> float:
    try:
        numero = float(valor)
        return numero if math.isfinite(numero) else defecto
    except (TypeError, ValueError):
        return defecto


def _antiguedad(fecha: Any) -> float:
    if isinstance(fecha, str):
        try:
            fecha = datetime.fromisoformat(fecha)
        except ValueError:
            return 0.0
    if isinstance(fecha, date) and not isinstance(fecha, datetime):
        fecha = datetime.combine(fecha, datetime.min.time())
    if not isinstance(fecha, datetime):
        return 0.0
    fecha = fecha.replace(tzinfo=timezone.utc) if fecha.tzinfo is None else fecha
    return max(0.0, (datetime.now(timezone.utc) - fecha).total_seconds() / 86400)


def score_knn(vecinos: list[dict[str, Any]]) -> float:
    """Media ponderada con prior de cuatro ejemplos neutros y decaimiento anual.

    Distancia coseno limita la similitud a [0,1]; fuente y peso expresan la
    fiabilidad de cada voto. Un vecino aislado nunca se convierte en certeza.
    """
    positivos, total = 2.0, 4.0
    for vecino in vecinos:
        etiqueta = vecino.get("etiqueta")
        if etiqueta not in {"positivo", "negativo", "ajena"}:
            continue
        similitud = max(0.0, min(1.0, 1.0 - _numero(vecino.get("distancia"), 1.0)))
        peso = (similitud * max(0.0, _numero(vecino.get("peso"), 1.0))
                * PESOS_FUENTE.get(vecino.get("fuente"), 0.5)
                * 0.5 ** (_antiguedad(vecino.get("creado_en")) / 365.0))
        total += peso
        positivos += peso if etiqueta == "positivo" else 0.0
    return positivos / total


def casos_few_shot(vecinos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los tres resueltos positivo/negativo más cercanos, sin vectores ni metadatos internos."""
    resueltos = [v for v in vecinos if v.get("etiqueta") in {"positivo", "negativo"}]
    resueltos.sort(key=lambda v: _numero(v.get("distancia"), 2.0))
    return [{k: v.get(k) for k in ("titulo_texto", "etiqueta", "evidencia")}
            for v in resueltos[:3]]
