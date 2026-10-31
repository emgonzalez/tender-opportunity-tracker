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

Fuente del estado (docs/planes/persistencia-adb.md, Fase 3) — `ESTADO_FUENTE`:
  archivo (default)  los JSONL de siempre; no se toca la ADB.
  sombra             manda el JSONL; además se intenta la ADB y se LOGUEAN las diferencias (una vez por par).
  adb                la ADB manda; el JSONL pasa a ser SPOOL: solo se escribe (en `data/history/spool/`) lo que la
                     ADB no alcanzó a recibir, y se vacía —sin duplicar, los escritores son MERGE— al inicio de la
                     corrida siguiente. Un fallo de la ADB jamás propaga: la corrida y el reporte siguen.
"""
import dataclasses
import json
import logging
import os
from datetime import datetime

from src.classifier.alias_clientes import id_canonico, ruta_con_legado
from src.persistencia import cache
from src.persistencia.fuente import fuente
from src.persistencia.spool import Spool
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
    client_id = id_canonico(client_id)
    return ruta_con_legado(os.path.join(data_dir, "history", f"{client_id}.jsonl"), client_id)


# ── API pública ───────────────────────────────────────────────────────────────

def save_run(
    client_id: str,
    run_timestamp: datetime,
    procesos_clasificados: list[ProcesoClasificado],
    total_revisados: int,
    data_dir: str,
) -> None:
    """Persiste los resultados de una ejecución en el historial del cliente."""
    entry = {
        "run_timestamp": run_timestamp.isoformat(),
        "total_revisados": total_revisados,
        "total_relevantes": sum(1 for p in procesos_clasificados if p.es_relevante),
        "procesos": [_serialize(p) for p in procesos_clasificados],
    }

    modo = _modo()
    if modo == "adb":
        _guardar_en_adb_o_spool(client_id, data_dir, "run", entry, _enviar_run)
        return

    path = _history_path(client_id, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.debug("[%s] Historial guardado: %s", client_id, path)

    if modo == "sombra":
        _sombra_escribir(client_id, "run", entry, _enviar_run)


def _entrada_a_run(entry: dict) -> dict:
    entry = dict(entry)
    entry["run_timestamp"] = datetime.fromisoformat(entry["run_timestamp"])
    entry["procesos"] = [_deserialize(p) for p in entry.get("procesos", [])]
    return entry


def _load_last_run_archivo(client_id: str, path: str) -> dict | None:
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
        return _entrada_a_run(json.loads(last_line))
    except Exception as exc:
        logger.error("[%s] Error leyendo historial: %s", client_id, exc)
        return None


def load_last_run(client_id: str, data_dir: str) -> dict | None:
    """
    Retorna la última ejecución del cliente con los procesos deserializados.
    Retorna None si no hay historial.

    Con `ESTADO_FUENTE=adb`: lo pendiente en el spool (lo más nuevo) → la ADB (reconstruida de
    `cliente_proceso`+`proceso`) → el JSONL viejo. Con `sombra` manda el JSONL y se compara con la ADB.
    """
    path = _history_path(client_id, data_dir)
    modo = _modo()
    if modo == "archivo":
        return _load_last_run_archivo(client_id, path)
    if modo == "sombra":
        local = _load_last_run_archivo(client_id, path)
        _sombra_comparar_run(client_id, local)
        return local

    try:
        pend = _spool(data_dir).pendientes(_cola("run", client_id))
        if pend:
            return _entrada_a_run(pend[-1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Spool de historial ilegible: %s", client_id, exc)
    desde_adb = _leer_run_adb(client_id)
    if desde_adb is not None:
        return desde_adb
    return _load_last_run_archivo(client_id, path)


# ── Análisis profundo (Fase 2) ────────────────────────────────────────────────

def _analisis_path(client_id: str, data_dir: str) -> str:
    """Retorna la ruta del archivo de análisis del cliente."""
    client_id = id_canonico(client_id)
    return ruta_con_legado(os.path.join(data_dir, "history", f"{client_id}.analisis.jsonl"), client_id)


def save_analisis(client_id: str, resultado: ResultadoAnalisis, data_dir: str) -> None:
    """Persiste el resultado de análisis de un proceso en el historial del cliente.

    Serializa el ResultadoAnalisis a JSON con dataclasses.asdict (maneja nested
    MatrizRiesgos/RiesgoItem recursivamente) y lo agrega como una línea JSONL.
    """
    entry = dataclasses.asdict(resultado)

    modo = _modo()
    if modo == "adb":
        _guardar_en_adb_o_spool(client_id, data_dir, "analisis", entry, _enviar_analisis)
        return

    path = _analisis_path(client_id, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if modo == "sombra":
        _sombra_escribir(client_id, "analisis", entry, _enviar_analisis)

    logger.debug(
        "[%s] Análisis guardado (proceso=%s, estado=%s): %s",
        client_id, resultado.proceso_uuid, resultado.estado, path
    )


def _load_analisis_archivo(client_id: str, proceso_uuids, data_dir: str) -> dict[str, ResultadoAnalisis]:
    """Reconstruye los ResultadoAnalisis con matriz de riesgos para `proceso_uuids`.

    Por proceso toma la ÚLTIMA línea con matriz no vacía: la marca posterior
    NOTIFICADO se escribe sin matriz y no debe taparla. Si el proceso solo tiene
    un descarte por triaje (DESCARTADO_TRIAJE, que no lleva matriz), devuelve ese.
    Tolera líneas corruptas.
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
                if uuid not in quiero:
                    continue
                m = e.get("matriz")
                if m and m.get("items"):
                    out[uuid] = ResultadoAnalisis.desde_dict(e)
                elif e.get("estado") == "DESCARTADO_TRIAJE" and uuid not in out:
                    # El NO-GO del triaje se guarda sin matriz; el correo lo necesita
                    # para mandarlo a «Baja prioridad». Un análisis con matriz, anterior
                    # o posterior, siempre prevalece sobre este descarte.
                    out[uuid] = ResultadoAnalisis.desde_dict(e)
            except Exception as exc:
                logger.warning("[%s] Línea de análisis ilegible (saltando): %s", client_id, exc)
    return out


def _analisis_estado_archivo(client_id: str, proceso_uuid: str, data_dir: str) -> str | None:
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


def load_analisis(client_id: str, proceso_uuids, data_dir: str,
                  marcas_cliente: tuple[str, ...] | list[str] | None = None) -> dict[str, ResultadoAnalisis]:
    """Análisis con matriz para `proceso_uuids` (ver `_load_analisis_archivo` para la regla de «última gana»).

    Con `ESTADO_FUENTE=adb`: spool (lo más nuevo) > JSONL viejo (si trae `competible`, no se pisa) > ADB
    (`analisis` + `cliente_proceso` + `proceso`). La ADB no guarda `competible`: se DERIVA al leer
    (`aplicar_competibilidad` con `proceso.modalidad_raw` y `marcas_cliente`, el portafolio del perfil).
    Sin `marcas_cliente` no se deriva y `competible` queda en None. Con `sombra` manda el JSONL y se
    compara con la ADB.
    """
    modo = _modo()
    local = _load_analisis_archivo(client_id, proceso_uuids, data_dir)
    if modo == "archivo":
        return local
    quiero = set(proceso_uuids)
    if modo == "sombra":
        _sombra_comparar_analisis(client_id, quiero, local)
        return local
    out = _leer_analisis_adb(client_id, quiero, marcas_cliente) or {}
    out.update(local)
    try:
        for e in _spool(data_dir).pendientes(_cola("analisis", client_id)):
            _aplicar_entrada_analisis(e, quiero, out)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Spool de análisis ilegible: %s", client_id, exc)
    return out


def analisis_estado(client_id: str, proceso_uuid: str, data_dir: str) -> str | None:
    """Estado más reciente del análisis de un proceso (última línea gana; tolera líneas corruptas).

    Con `ESTADO_FUENTE=adb`: spool (más nuevo) → `cliente_proceso.estado` → JSONL viejo. Con `sombra` manda
    el JSONL y se compara con la ADB.
    """
    modo = _modo()
    if modo == "archivo":
        return _analisis_estado_archivo(client_id, proceso_uuid, data_dir)
    if modo == "sombra":
        local = _analisis_estado_archivo(client_id, proceso_uuid, data_dir)
        _sombra_comparar_estado(client_id, proceso_uuid, local)
        return local
    try:
        ultimo = None
        for e in _spool(data_dir).pendientes(_cola("analisis", client_id)):
            if e.get("proceso_uuid") == proceso_uuid:
                ultimo = e.get("estado")
        if ultimo is not None:
            return ultimo
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Spool de análisis ilegible: %s", client_id, exc)
    desde_adb = _estado_adb(client_id, proceso_uuid)
    if desde_adb is not None:
        return desde_adb
    return _analisis_estado_archivo(client_id, proceso_uuid, data_dir)


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


# ── Fuente del estado (archivo | sombra | adb) — docs/planes/persistencia-adb.md, Fase 3 ───────────────────

DATO = "ESTADO"
_diferencias_avisadas: set[tuple] = set()


def reiniciar_estado() -> None:
    """Vacía los avisos de diferencias ya emitidos (tests)."""
    _diferencias_avisadas.clear()


def _modo() -> str:
    return fuente(DATO)


def _cfg():
    try:
        from src.config import get_config
        return get_config()
    except Exception:  # noqa: BLE001 — sin config no hay ADB, pero la corrida sigue
        logger.debug("sin config para el estado en la ADB", exc_info=True)
        return None


def _adb_lista(cfg) -> bool:
    return bool(cfg is not None and getattr(cfg, "adb_dsn", "") and getattr(cfg, "adb_password", ""))


def _servicio():
    from src.memoria import service
    return service


def _avisar_una_vez(clave: tuple, mensaje: str, *args) -> None:
    if clave in _diferencias_avisadas:
        return
    _diferencias_avisadas.add(clave)
    logger.info(mensaje, *args)


def _spool(data_dir: str) -> Spool:
    return Spool(os.path.join(data_dir, "history", "spool"))


def _cola(tipo: str, client_id: str) -> str:
    return f"{tipo}.{id_canonico(client_id)}"


def _texto(valor):
    # JSON_SERIALIZE sin RETURNING CLOB corta en 4000 caracteres: se pide CLOB y llega como LOB.
    if hasattr(valor, "read"):
        valor = valor.read()
    if isinstance(valor, (bytes, bytearray)):
        valor = valor.decode("utf-8")
    return valor


def _json(valor, defecto):
    valor = _texto(valor)
    if valor is None or valor == "":
        return defecto
    return json.loads(valor) if isinstance(valor, str) else valor


# ── Escritura en la ADB (los mismos MERGE/UPDATE de memoria.repo: reenviar no duplica) ──────────────────────

def _enviar_run(cfg, client_id: str, entry: dict) -> bool:
    svc = _servicio()
    if not svc._activo_v2(cfg):
        return False  # sin ENABLE_MEMORIA + ENABLE_ESQUEMA_V2 no hay dónde escribir: queda en el spool
    procesos = []
    for crudo in entry.get("procesos") or []:
        try:
            procesos.append(_deserialize(crudo))
        except Exception as exc:  # noqa: BLE001 — un registro ilegible no puede trabar el spool para siempre
            logger.warning("[%s] proceso ilegible en el historial, se omite: %s", client_id, exc)
    r = svc.registrar_clasificados(cfg, client_id, procesos)
    return not r.get("error") and not r.get("fallidos")


def _enviar_analisis(cfg, client_id: str, entry: dict) -> bool:
    svc = _servicio()
    if not svc._activo_v2(cfg):
        return False
    r = ResultadoAnalisis.desde_dict(entry)
    from src.memoria import db, repo
    portal = svc._portal(cfg, client_id)
    triaje = r.triaje or None
    with db.connection(cfg) as conn:
        if r.matriz is not None:
            repo.save_analisis_v2(conn, r, client_id, portal_id=portal)
        repo.set_estado_v2(
            conn, r.proceso_uuid, client_id, r.estado, error=r.error or None, triaje=triaje,
            triaje_conf=r.triaje_confianza if triaje else None, formato=r.formato or None, portal_id=portal,
        )
    return True


def _enviar_ejecucion(cfg, _client_id, reg: dict) -> bool:
    """Una fila de `ejecucion`; si ya está (corte entre el INSERT y el retiro del spool) no la repite."""
    if not _adb_lista(cfg):
        return False
    from src.memoria import db, repo
    ts = datetime.fromisoformat(reg["timestamp"])
    with db.connection(cfg) as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM ejecucion WHERE run_timestamp = :ts AND success = :s",
                        {"ts": ts, "s": 1 if reg["success"] else 0})
            ya = cur.fetchone()[0]
        finally:
            cur.close()
        if not ya:
            repo.insertar_ejecucion(
                conn, ts, bool(reg["success"]),
                procesos_revisados=int(reg.get("procesos_revisados") or 0),
                clientes_procesados=int(reg.get("clientes_procesados") or 0),
                version=str(reg.get("version") or ""), error=reg.get("error"),
            )
    return True


def _intentar(enviar, cfg, cid, registro) -> bool:
    try:
        return bool(enviar(cfg, cid, registro))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] la ADB no respondió (%s)", cid, type(exc).__name__)
        logger.debug("detalle", exc_info=True)
        return False


def _guardar_en_adb_o_spool(client_id: str, data_dir: str, tipo: str, registro: dict, enviar) -> bool:
    """Modo `adb`: escribe en la ADB; si no se puede, encola en el spool. Jamás propaga.

    El orden importa: si ya hay pendientes se intenta vaciarlos primero y, si quedan, el registro nuevo se
    encola DETRÁS (si no, una repetición posterior del spool pisaría el estado más nuevo: ANALIZADO sobre
    NOTIFICADO haría reanalizar).
    """
    cid = id_canonico(client_id)
    cola = _cola(tipo, cid)
    try:
        cfg = _cfg()
        sp = _spool(data_dir)
        if sp.pendientes(cola):
            sp.drenar(cola, lambda r: _intentar(enviar, cfg, cid, r))
        if not sp.pendientes(cola) and _intentar(enviar, cfg, cid, registro):
            return True
        sp.agregar(cola, registro)
        logger.warning("[%s] ADB no recibió %s: queda en el spool (%s)", cid, tipo, cola)
    except Exception as exc:  # noqa: BLE001 — el estado nunca tumba la corrida
        logger.error("[%s] No se pudo guardar %s ni en la ADB ni en el spool: %s", cid, tipo, exc)
    return False


def _sombra_escribir(client_id: str, tipo: str, registro: dict, enviar) -> None:
    """Modo `sombra`: el JSONL ya se escribió; además se intenta la ADB, sin spool ni propagar."""
    cid = id_canonico(client_id)
    if not _intentar(enviar, _cfg(), cid, registro):
        _avisar_una_vez(("sombra-escritura", tipo, cid), "estado[sombra]: la ADB no recibió %s de %s", tipo, cid)


# ── Lectura de la ADB ────────────────────────────────────────────────────────────────────────────────────────

_SQL_RUN = """
    SELECT p.numero_referencia, p.institucion, p.titulo, p.modalidad_raw, p.categoria_raw, p.fecha_pub_utc,
           p.url_portal, p.monto_original, p.fecha_limite_utc, cp.es_relevante, cp.categoria_detectada,
           cp.razon, cp.match_tecnico, cp.match_categoria, cp.factibilidad,
           JSON_SERIALIZE(cp.tags RETURNING CLOB), JSON_SERIALIZE(cp.marcas RETURNING CLOB), cp.clasificado_en
      FROM cliente_proceso cp
      JOIN proceso p ON p.proceso_gid = cp.proceso_gid
     WHERE cp.cliente_id = :cid
       AND cp.clasificado_en >= (SELECT MAX(c2.clasificado_en) FROM cliente_proceso c2
                                  WHERE c2.cliente_id = :cid) - NUMTODSINTERVAL(2, 'HOUR')
"""

_SQL_REVISADOS = ("SELECT procesos_revisados FROM ejecucion WHERE success = 1 "
                  "ORDER BY run_timestamp DESC FETCH FIRST 1 ROWS ONLY")


def _leer_run_adb(client_id: str) -> dict | None:
    """La última corrida del cliente reconstruida de la ADB; None si no hay ADB, no responde o no hay filas."""
    cfg = _cfg()
    if not _adb_lista(cfg):
        return None
    cid = id_canonico(client_id)
    try:
        from src.memoria import db
        with db.connection(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(_SQL_RUN, {"cid": cid})
                filas = cur.fetchall()
                revisados = None
                if filas:
                    cur.execute(_SQL_REVISADOS)
                    fila = cur.fetchone()
                    revisados = int(fila[0]) if fila else None
            finally:
                cur.close()
        if not filas:
            return None
        procesos, ultimo = [], None
        for f in filas:
            marcas = [MarcaRecomendada(marca=m.get("marca", ""), razon=m.get("razon", ""))
                      for m in _json(f[16], []) if isinstance(m, dict)]
            procesos.append(ProcesoClasificado(
                numero_referencia=f[0], institucion=f[1] or "", descripcion=f[2] or "",
                forma_contratacion=f[3] or "", tipo_actividad=f[4] or "", fecha_publicacion=f[5],
                url_directa=f[6] or "", monto=float(f[7]) if f[7] is not None else None, fecha_limite=f[8],
                es_relevante=bool(f[9]), categoria_detectada=f[10] or "", razon=f[11] or "",
                match_tecnico=float(f[12] or 0), match_categoria=float(f[13] or 0),
                factibilidad=float(f[14] or 0), confianza=float(f[14] or 0),
                tags=list(_json(f[15], [])), marcas_recomendadas=marcas,
            ))
            if f[17] is not None and (ultimo is None or f[17] > ultimo):
                ultimo = f[17]
        return {
            "run_timestamp": ultimo,
            "total_revisados": revisados if revisados is not None else len(procesos),
            "total_relevantes": sum(1 for p in procesos if p.es_relevante),
            "procesos": procesos,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] historial: ADB no disponible (%s)", cid, type(exc).__name__)
        logger.debug("detalle", exc_info=True)
        return None


def _estado_adb(client_id: str, proceso_uuid: str) -> str | None:
    cfg = _cfg()
    if not _adb_lista(cfg):
        return None
    try:
        from src.memoria import db, repo
        cid = id_canonico(client_id)
        with db.connection(cfg) as conn:
            return repo.get_estado_v2(conn, cid, proceso_uuid, portal_id=_servicio()._portal(cfg, cid))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] estado de %s: ADB no disponible (%s)", client_id, proceso_uuid, type(exc).__name__)
        return None


_SQL_ANALISIS = """
    SELECT cp.proceso_gid, cp.estado, cp.triaje, cp.triaje_conf, cp.formato,
           JSON_SERIALIZE(a.matriz_riesgos RETURNING CLOB), a.modelo_llm, a.monto_estimado, p.modalidad_raw
      FROM cliente_proceso cp
      LEFT JOIN proceso p ON p.proceso_gid = cp.proceso_gid
      LEFT JOIN analisis a ON a.cliente_id = cp.cliente_id AND a.proceso_gid = cp.proceso_gid
     WHERE cp.cliente_id = :cid
       AND cp.estado IN ('ANALIZADO', 'NOTIFICADO', 'DESCARTADO_TRIAJE')
"""


def _leer_analisis_adb(client_id: str, quiero: set,
                       marcas_cliente: tuple[str, ...] | list[str] | None = None,
                       ) -> dict[str, ResultadoAnalisis] | None:
    cfg = _cfg()
    if not _adb_lista(cfg):
        return None
    cid = id_canonico(client_id)
    try:
        from src.drilldown.analysis import aplicar_competibilidad
        from src.memoria import db
        with db.connection(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(_SQL_ANALISIS, {"cid": cid})
                filas = cur.fetchall()
            finally:
                cur.close()
        out: dict[str, ResultadoAnalisis] = {}
        for gid, estado, triaje, conf, formato, matriz, modelo, monto, modalidad in filas:
            uuid = gid.split(":", 1)[1] if ":" in gid else gid
            if uuid not in quiero:
                continue
            m = _json(matriz, None)
            if not (m and m.get("items")) and estado != "DESCARTADO_TRIAJE":
                continue
            res = ResultadoAnalisis.desde_dict({
                "proceso_uuid": uuid, "estado": estado, "formato": formato or "", "triaje": triaje or "",
                "triaje_confianza": float(conf or 0), "matriz": m if (m and m.get("items")) else None,
                "modelo_analisis": modelo or "", "monto_estimado": float(monto) if monto is not None else None,
            })
            if marcas_cliente is not None:
                # `modalidad_raw` es `forma_contratacion` tal como la guardó `upsert_proceso_v2`.
                try:
                    aplicar_competibilidad(res, forma_contratacion=modalidad or "", marcas_cliente=marcas_cliente)
                except Exception:  # noqa: BLE001 — una fila rara no tumba la lectura
                    logger.warning("[%s] competibilidad no evaluable para %s", cid, uuid)
            out[uuid] = res
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] análisis: ADB no disponible (%s)", cid, type(exc).__name__)
        logger.debug("detalle", exc_info=True)
        return None


def _aplicar_entrada_analisis(e: dict, quiero: set, out: dict) -> None:
    """Misma regla que `_load_analisis_archivo`, sobre una entrada ya parseada (spool)."""
    try:
        uuid = e.get("proceso_uuid")
        if uuid not in quiero:
            return
        m = e.get("matriz")
        if m and m.get("items"):
            out[uuid] = ResultadoAnalisis.desde_dict(e)
        elif e.get("estado") == "DESCARTADO_TRIAJE" and uuid not in out:
            out[uuid] = ResultadoAnalisis.desde_dict(e)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Entrada de análisis ilegible en el spool (saltando): %s", exc)


# ── Modo sombra: comparar y loguear (una vez por par) ───────────────────────────────────────────────────────

def _sombra_comparar_run(client_id: str, local: dict | None) -> None:
    cid = id_canonico(client_id)
    try:
        adb = _leer_run_adb(cid)
        if adb is None:
            _avisar_una_vez(("run-sin-adb", cid), "historial[sombra]: %s sin lectura de la ADB", cid)
            return
        a = {(p.numero_referencia, p.es_relevante) for p in adb["procesos"]}
        b = {(p.numero_referencia, p.es_relevante) for p in (local or {}).get("procesos", [])}
        if a != b:
            _avisar_una_vez(("run", cid, len(a ^ b)), "historial[sombra]: %s difiere entre JSONL (%d) y ADB (%d); "
                            "%d procesos distintos", cid, len(b), len(a), len(a ^ b))
    except Exception:  # noqa: BLE001
        logger.debug("comparación de sombra falló", exc_info=True)


def _sombra_comparar_estado(client_id: str, uuid: str, local: str | None) -> None:
    cid = id_canonico(client_id)
    try:
        adb = _estado_adb(cid, uuid)
        if adb != local:
            _avisar_una_vez(("estado", cid, uuid, local, adb),
                            "analisis[sombra]: %s/%s difiere entre JSONL (%s) y ADB (%s)", cid, uuid, local, adb)
    except Exception:  # noqa: BLE001
        logger.debug("comparación de sombra falló", exc_info=True)


def _sombra_comparar_analisis(client_id: str, quiero: set, local: dict) -> None:
    cid = id_canonico(client_id)
    try:
        adb = _leer_analisis_adb(cid, quiero)
        if adb is None:
            return
        for u in sorted(set(adb) | set(local)):
            a, b = adb.get(u), local.get(u)
            ea, eb = (a.estado if a else None), (b.estado if b else None)
            if ea != eb:
                _avisar_una_vez(("analisis", cid, u, eb, ea),
                                "analisis[sombra]: %s/%s difiere entre JSONL (%s) y ADB (%s)", cid, u, eb, ea)
    except Exception:  # noqa: BLE001
        logger.debug("comparación de sombra falló", exc_info=True)


# ── last_run ─────────────────────────────────────────────────────────────────────────────────────────────────

def guardar_last_run(config, data: dict, error: str | None = None) -> None:
    """Escribe `last_run.json` de forma atómica (tmp + os.replace) y, con `ESTADO_FUENTE=adb`, la fila de `ejecucion`.

    El archivo es la caché local; la ADB jamás hace fallar esto.
    """
    cache.escribir_atomico(config.last_run_path, json.dumps(data, indent=2))
    if _modo() != "adb":
        return
    try:
        if not _adb_lista(config):
            logger.warning("ESTADO_FUENTE=adb pero sin credenciales de la ADB: last_run solo queda en el archivo")
            return
        reg = {**data, "error": error}
        cola = "ejecucion"
        sp = _spool(config.data_dir)
        if sp.pendientes(cola):
            sp.drenar(cola, lambda r: _intentar(_enviar_ejecucion, config, "", r))
        if sp.pendientes(cola) or not _intentar(_enviar_ejecucion, config, "", reg):
            sp.agregar(cola, reg)
            logger.warning("ADB no recibió la ejecución: queda en el spool")
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo registrar la ejecución en la ADB ni en el spool: %s", exc)


def _fila_a_last_run(fila) -> dict:
    ts, success, revisados, clientes, version = fila
    return {
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "success": bool(success), "procesos_revisados": int(revisados or 0),
        "clientes_procesados": int(clientes or 0), "version": version or "",
    }


_SQL_ULTIMA_EJECUCION = ("SELECT run_timestamp, success, procesos_revisados, clientes_procesados, version "
                         "FROM ejecucion ORDER BY run_timestamp DESC, id DESC FETCH FIRST 1 ROWS ONLY")


def leer_last_run(config) -> dict | None:
    """Última corrida: `adb` = ADB primero (y refresca la caché) y, si no responde, `last_run.json`; `sombra` y
    `archivo` = el archivo (con `sombra` se compara con la ADB y se loguean las diferencias)."""
    modo = _modo()
    local = cache.leer_json(config.last_run_path)
    if modo == "archivo":
        return local
    desde_adb = None
    try:
        if _adb_lista(config):
            from src.memoria import db
            with db.connection(config) as conn:
                cur = conn.cursor()
                try:
                    cur.execute(_SQL_ULTIMA_EJECUCION)
                    fila = cur.fetchone()
                finally:
                    cur.close()
            desde_adb = _fila_a_last_run(fila) if fila else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("last_run: ADB no disponible (%s); se usa la caché local", type(exc).__name__)
        logger.debug("detalle", exc_info=True)
    if modo == "sombra":
        if desde_adb is not None and local is not None:
            campos = [k for k in ("success", "procesos_revisados", "clientes_procesados", "version")
                      if desde_adb.get(k) != local.get(k)]
            if campos:
                _avisar_una_vez(("last_run", tuple(campos), local.get("timestamp")),
                                "last_run[sombra]: difiere entre archivo y ADB; campos: %s", ", ".join(campos))
        return local
    if desde_adb is not None:
        if local is None or any(desde_adb[k] != local.get(k) for k in desde_adb):
            try:
                cache.escribir_atomico(config.last_run_path, json.dumps({**(local or {}), **desde_adb}, indent=2))
            except Exception:  # noqa: BLE001
                logger.debug("no se pudo refrescar la caché de last_run", exc_info=True)
        return desde_adb
    return local


# ── Spool: vaciado ───────────────────────────────────────────────────────────────────────────────────────────

def drenar_spool(cfg, forzar: bool = False) -> dict[str, tuple[int, int]]:
    """Reenvía a la ADB lo que quedó en el spool: `{cola: (enviados, quedan)}`. Nunca propaga.

    Solo actúa con `ESTADO_FUENTE=adb` (o `forzar=True`, que usa `reimportar --vaciar-spool`). Orden: corridas →
    análisis → ejecuciones, porque `analisis` exige la fila de `cliente_proceso` que crea la corrida.
    """
    resultado: dict[str, tuple[int, int]] = {}
    try:
        if not forzar and _modo() != "adb":
            return resultado
        if not _adb_lista(cfg):
            return resultado
        sp = _spool(cfg.data_dir)
        if not sp.directorio.is_dir():
            return resultado
        colas = sorted(p.stem for p in sp.directorio.glob("*.jsonl"))
        orden = {"run": 0, "analisis": 1, "ejecucion": 2}
        colas.sort(key=lambda c: (orden.get(c.split(".", 1)[0], 3), c))
        enviadores = {"run": _enviar_run, "analisis": _enviar_analisis, "ejecucion": _enviar_ejecucion}
        for cola in colas:
            tipo, _, cid = cola.partition(".")
            enviar = enviadores.get(tipo)
            if enviar is None:
                continue
            resultado[cola] = sp.drenar(cola, lambda r, e=enviar, c=cid: _intentar(e, cfg, c, r))
            if resultado[cola][0]:
                logger.info("spool %s: %d enviado(s), quedan %d", cola, *resultado[cola])
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo vaciar el spool: %s", exc)
    return resultado
