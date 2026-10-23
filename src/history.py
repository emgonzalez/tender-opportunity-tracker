"""
Historial persistente de ejecuciones por cliente.

Cada ejecución del pipeline queda registrada en:
  data/history/{client_id}.jsonl   (una línea JSON por ejecución)

Esto permite:
  - Auditar qué procesos se detectaron en cada ejecución
  - Reenviar el último reporte sin volver a consultar COMPRASAL

Análisis profundo (Fase 2):
  data/history/{client_id}.analisis.jsonl   (una línea JSON por análisis)
  Persiste ResultadoAnalisis con matriz de riesgos y estado de drill-down.
"""
import dataclasses
import json
import logging
import os
from datetime import datetime

from src.scraper.models import (
    MarcaRecomendada,
    ProcesoClasificado,
    ResultadoAnalisis,
)

logger = logging.getLogger(__name__)


# ── Serialización ─────────────────────────────────────────────────────────────

def _serialize(p: ProcesoClasificado) -> dict:
    d = {
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
        "fecha_limite": p.fecha_limite.isoformat() if p.fecha_limite else None,
    }
    # La señal de triaje vive en ResultadoAnalisis, no en ProcesoClasificado;
    # se guarda solo si algún llamador la adjuntó al objeto.
    triaje = getattr(p, "triaje", "")
    if triaje:
        d["triaje"] = triaje
    return d


def _parse_fecha(valor) -> datetime | None:
    """ISO 8601 -> datetime; None si falta (líneas antiguas) o es ilegible."""
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        logger.warning("fecha_limite ilegible en historial: %r", valor)
        return None


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
        fecha_limite=_parse_fecha(d.get("fecha_limite")),
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


# ── Análisis profundo (Fase 2) ────────────────────────────────────────────────

def _analisis_path(client_id: str, data_dir: str) -> str:
    """Retorna la ruta del archivo de análisis del cliente."""
    return os.path.join(data_dir, "history", f"{client_id}.analisis.jsonl")


def save_analisis(client_id: str, resultado: ResultadoAnalisis, data_dir: str) -> None:
    """Persiste el resultado de análisis de un proceso en el historial del cliente.

    Serializa el ResultadoAnalisis a JSON con dataclasses.asdict (maneja nested
    MatrizRiesgos/RiesgoItem recursivamente) y lo agrega como una línea JSONL.
    """
    path = _analisis_path(client_id, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    entry = dataclasses.asdict(resultado)

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.debug(
        "[%s] Análisis guardado (proceso=%s, estado=%s): %s",
        client_id, resultado.proceso_uuid, resultado.estado, path
    )


def load_analisis(client_id: str, proceso_uuids, data_dir: str) -> dict[str, ResultadoAnalisis]:
    """Reconstruye los ResultadoAnalisis con matriz de riesgos para `proceso_uuids`.

    Por proceso toma la ÚLTIMA línea con matriz no vacía: la marca posterior
    NOTIFICADO se escribe sin matriz y no debe taparla. Tolera líneas corruptas.
    Retorna {} si no hay archivo o ningún proceso tiene análisis.
    """
    path = _analisis_path(client_id, data_dir)
    if not os.path.exists(path):
        return {}
    quiero = set(proceso_uuids)
    out: dict[str, ResultadoAnalisis] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                e = json.loads(stripped)
                uuid = e.get("proceso_uuid")
                m = e.get("matriz")
                if uuid not in quiero or not m or not m.get("items"):
                    continue
                out[uuid] = ResultadoAnalisis.desde_dict(e)
            except Exception as exc:
                logger.warning("[%s] Línea de análisis ilegible (saltando): %s", client_id, exc)
    return out


def analisis_estado(client_id: str, proceso_uuid: str, data_dir: str) -> str | None:
    """Retorna el estado más reciente del análisis de un proceso.

    Lee el archivo .analisis.jsonl del cliente, busca la ÚLTIMA línea cuyo
    `proceso_uuid` coincida y devuelve su campo `estado`.

    Tolera líneas corruptas (skip). Retorna None si no hay coincidencia.
    """
    path = _analisis_path(client_id, data_dir)
    if not os.path.exists(path):
        return None

    last_estado = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
                if entry.get("proceso_uuid") == proceso_uuid:
                    last_estado = entry.get("estado")
            except Exception as exc:
                logger.warning(
                    "[%s] Línea corrupta en análisis (saltando): %s", client_id, exc
                )
                continue

    return last_estado


def is_estado_terminal(estado: str | None) -> bool:
    """Retorna True si el estado es terminal en la máquina de estados.

    Estados terminales: NOTIFICADO, DESCARTADO_TAMANO, DESCARTADO_REGEX,
    DESCARTADO_TRIAJE, REQUIERE_REVISION_MANUAL.

    ANALIZADO no es terminal: aún falta notificar.
    """
    if estado is None:
        return False

    terminal_states = {
        "NOTIFICADO",
        "DESCARTADO_TAMANO",
        "DESCARTADO_REGEX",
        "DESCARTADO_TRIAJE",
        "REQUIERE_REVISION_MANUAL",
    }
    return estado in terminal_states
