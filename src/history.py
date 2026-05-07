"""
Historial persistente de ejecuciones por cliente.

Cada ejecución del pipeline queda registrada en:
  data/history/{client_id}.jsonl   (una línea JSON por ejecución)

Esto permite:
  - Auditar qué procesos se detectaron en cada ejecución
  - Reenviar el último reporte sin volver a consultar COMPRASAL
"""
import json
import logging
import os
from datetime import datetime

from src.scraper.models import ProcesoClasificado, MarcaRecomendada

logger = logging.getLogger(__name__)


# ── Serialización ─────────────────────────────────────────────────────────────

def _serialize(p: ProcesoClasificado) -> dict:
    return {
        "numero_referencia": p.numero_referencia,
        "institucion": p.institucion,
        "descripcion": p.descripcion,
        "forma_contratacion": p.forma_contratacion,
        "tipo_actividad": p.tipo_actividad,
        "fecha_publicacion": p.fecha_publicacion.isoformat(),
        "url_directa": p.url_directa,
        "monto": p.monto,
        "es_relevante": p.es_relevante,
        "categoria_detectada": p.categoria_detectada,
        "confianza": p.confianza,
        "razon": p.razon,
        "match_tecnico": p.match_tecnico,
        "match_categoria": p.match_categoria,
        "factibilidad": p.factibilidad,
        "tags": p.tags,
        "marcas_recomendadas": [
            {"marca": m.marca, "razon": m.razon}
            for m in p.marcas_recomendadas
        ],
    }


def _deserialize(d: dict) -> ProcesoClasificado:
    return ProcesoClasificado(
        numero_referencia=d.get("numero_referencia", ""),
        institucion=d["institucion"],
        descripcion=d["descripcion"],
        forma_contratacion=d.get("forma_contratacion", ""),
        tipo_actividad=d.get("tipo_actividad", ""),
        fecha_publicacion=datetime.fromisoformat(d["fecha_publicacion"]),
        url_directa=d.get("url_directa", ""),
        monto=d.get("monto"),
        es_relevante=d.get("es_relevante", False),
        categoria_detectada=d.get("categoria_detectada", ""),
        confianza=float(d.get("confianza", 0.0)),
        razon=d.get("razon", ""),
        match_tecnico=float(d.get("match_tecnico", 0.0)),
        match_categoria=float(d.get("match_categoria", 0.0)),
        factibilidad=float(d.get("factibilidad", d.get("confianza", 0.0))),
        tags=d.get("tags", []),
        marcas_recomendadas=[
            MarcaRecomendada(marca=m.get("marca", ""), razon=m.get("razon", ""))
            for m in d.get("marcas_recomendadas", [])
        ],
    )


def _history_path(client_id: str, data_dir: str) -> str:
    return os.path.join(data_dir, "history", f"{client_id}.jsonl")


# ── API pública ───────────────────────────────────────────────────────────────

def save_run(
    client_id: str,
    run_timestamp: datetime,
    procesos_clasificados: list[ProcesoClasificado],
    total_revisados: int,
    data_dir: str,
) -> None:
    """Persiste los resultados de una ejecución en el historial del cliente."""
    path = _history_path(client_id, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    entry = {
        "run_timestamp": run_timestamp.isoformat(),
        "total_revisados": total_revisados,
        "total_relevantes": sum(1 for p in procesos_clasificados if p.es_relevante),
        "procesos": [_serialize(p) for p in procesos_clasificados],
    }

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.debug("[%s] Historial guardado: %s", client_id, path)


def load_last_run(client_id: str, data_dir: str) -> dict | None:
    """
    Retorna la última ejecución del cliente con los procesos deserializados.
    Retorna None si no hay historial.
    """
    path = _history_path(client_id, data_dir)
    if not os.path.exists(path):
        return None

    last_line = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped

    if not last_line:
        return None

    try:
        entry = json.loads(last_line)
        entry["run_timestamp"] = datetime.fromisoformat(entry["run_timestamp"])
        entry["procesos"] = [_deserialize(p) for p in entry.get("procesos", [])]
        return entry
    except Exception as exc:
        logger.error("[%s] Error leyendo historial: %s", client_id, exc)
        return None
