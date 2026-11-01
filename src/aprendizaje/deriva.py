"""Reporte semanal de deriva, de solo lectura y con alerta optativa."""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from src.aprendizaje import repo
from src.notifier import alerts

logger = logging.getLogger(__name__)
MIN_MUESTRA = 20


def distribucion(semana, base):
    """Variación total de frecuencias de términos; cuenta presencia por título."""
    def contar(titulos):
        return Counter(t for titulo in titulos for t in set(re.findall(r"[^\W\d_]{3,}", (titulo or "").lower())))
    a, b = contar(semana), contar(base)
    na, nb = sum(a.values()), sum(b.values())
    distancia = sum(abs(a[t] / na - b[t] / nb) for t in a.keys() | b.keys()) / 2 if na and nb else None
    return {"semana": dict(a), "base_ocho_semanas": dict(b), "distancia": distancia,
            "titulos_semana": len(semana), "titulos_base": len(base)}


def resumir(votos, marcas, decisiones, ejemplos, titulos):
    """Sin observaciones devuelve None: ausencia de medición no equivale a cero."""
    nv = sum(votos.values())
    precision = votos.get("RELEVANTE", 0) / nv if nv else None
    marcas = [m for lote in marcas for m in (lote if isinstance(lote, list) else []) if isinstance(m, dict)]
    confirmar = 100 * sum(m.get("veredicto") == "por_confirmar" for m in marcas) / len(marcas) if marcas else None
    vecinos = [d["vecinos"] for d in decisiones if isinstance(d, dict) and isinstance(d.get("vecinos"), list)]
    cobertura = sum(bool(v) for v in vecinos) / len(vecinos) if vecinos else None
    semana = [t for t, reciente in titulos if reciente]
    base = [t for t, reciente in titulos if not reciente]
    cambio = distribucion(semana, base)
    desvios = []
    if nv >= MIN_MUESTRA and precision < 0.8:
        desvios.append("Menos del 80 % de votos relevantes")
    if len(marcas) >= MIN_MUESTRA and confirmar > 50:
        desvios.append("Más del 50 % de marcas por confirmar")
    if len(vecinos) >= MIN_MUESTRA and cobertura < 0.5:
        desvios.append("Cobertura de vecinos menor al 50 %")
    if (len(semana) >= MIN_MUESTRA and len(base) >= MIN_MUESTRA
            and cambio["distancia"] is not None and cambio["distancia"] > 0.35):
        desvios.append("Cambió la distribución de términos de los títulos")
    return {"precision_estimada_votos": precision, "votos": nv,
            "precision_alcance": "Relevancia general de votos; no precisión por marca",
            "por_confirmar_pct": confirmar, "cobertura_vecinos": cobertura,
            "decisiones_con_cobertura": len(vecinos),
            "marcas_pocos_ejemplos": {m: n for m, n in ejemplos.items() if n < 10},
            "distribucion_titulos": cambio, "desviaciones": desvios}


def ejecutar(conn, cliente_id, aplicar=False, cfg=None):
    """ADB caída se informa sin fallar; dry-run jamás escribe ni envía alertas."""
    try:
        ahora = datetime.now(timezone.utc).replace(tzinfo=None)
        binds = {"cliente_id": cliente_id, "semana": ahora - timedelta(days=7),
                 "base": ahora - timedelta(weeks=9), "ahora": ahora}
        semanales = {k: binds[k] for k in ("cliente_id", "semana", "ahora")}
        votos = dict(repo._todos(conn, "SELECT voto, COUNT(*) FROM feedback WHERE cliente_id = :cliente_id "
                               "AND votado_en >= :semana AND votado_en < :ahora GROUP BY voto", semanales))
        marcas = [repo._a_json(f[0]) for f in repo._todos(
            conn, "SELECT JSON_SERIALIZE(marcas RETURNING CLOB) FROM cliente_proceso "
            "WHERE cliente_id = :cliente_id AND clasificado_en >= :semana AND clasificado_en < :ahora", semanales)]
        decisiones = [repo._a_json(f[0]) for f in repo._todos(
            conn, "SELECT JSON_SERIALIZE(salida RETURNING CLOB) FROM decision_capa "
            "WHERE cliente_id = :cliente_id AND variante = 'activa' "
            "AND creado_en >= :semana AND creado_en < :ahora", semanales)]
        ejemplos = dict(repo._todos(conn, "SELECT m.marca_id, COUNT(e.ejemplo_id) FROM "
                                   "(SELECT DISTINCT marca_id FROM marca_termino WHERE cliente_id = :cliente_id "
                                   "UNION SELECT marca_id FROM ejemplo_marca WHERE cliente_id = :cliente_id) m "
                                   "LEFT JOIN ejemplo_marca e ON e.marca_id = m.marca_id "
                                   "AND e.cliente_id = :cliente_id AND e.activo = 1 GROUP BY m.marca_id",
                                   {"cliente_id": cliente_id}))
        titulos = repo._todos(conn, "SELECT p.titulo, CASE WHEN p.fecha_pub_utc >= :semana THEN 1 ELSE 0 END "
                             "FROM proceso p JOIN cliente_proceso c ON c.proceso_gid = p.proceso_gid "
                             "WHERE c.cliente_id = :cliente_id AND p.fecha_pub_utc >= :base "
                             "AND p.fecha_pub_utc < :ahora", binds)
        resultado = resumir(votos, marcas, decisiones, ejemplos, titulos)
    except Exception as exc:  # noqa: BLE001 — reporte auxiliar, nunca detiene el ciclo
        logger.warning("Deriva: no corrió; ADB no disponible (%s)", type(exc).__name__)
        return {"ejecutado": False, "motivo": "Deriva no corrió: ADB no disponible"}
    resultado.update(ejecutado=True, alertas=[])
    if aplicar and cfg is not None and resultado["desviaciones"]:
        resultado["alertas"] = alerts.notify(cfg, "AMALIA: desviación semanal de aprendizaje",
                                            "\n".join(resultado["desviaciones"]), client_id=cliente_id)
    logger.info("Deriva semanal: %s desviaciones", len(resultado["desviaciones"]))
    return resultado
