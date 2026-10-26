"""src.feedback.service — fachada del feedback. Igual contrato que `memoria.service`.

  1. Con `enable_feedback` apagado (o sin URL base o sin secreto) → no-op: el
     correo sale sin botones y el reporte se comporta como antes.
  2. Si la ADB falla → se degrada: se loguea y se sigue. Un fallo acá NUNCA puede
     impedir que el reporte llegue.

La única excepción a "jamás propaga" es `guardar_voto`, que devuelve `False`: el
servicio web tiene que poder decirle a la persona que su voto NO se guardó.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from src.feedback import tokens

logger = logging.getLogger(__name__)


def activo(cfg) -> bool:
    """El feedback solo se ofrece si se pueden firmar y honrar los enlaces."""
    return bool(
        getattr(cfg, "enable_feedback", False)
        and getattr(cfg, "feedback_base_url", "")
        and getattr(cfg, "feedback_hmac_secret", "")
    )


def _portal(cfg, client_id: str) -> str:
    """Portal del cliente (su YAML); el `proceso_gid` v2 se arma con él."""
    from src.portales import portal_de_cliente
    return portal_de_cliente(getattr(cfg, "clients_dir", ""), client_id)


def _activo_v2(cfg) -> bool:
    """Doble escritura del esquema v2 (plan del portal §3.3 Fase 2).

    Mismo criterio que ``memoria.service._activo_v2``: depende del propio
    subsistema (``activo``, sin el cual no hay nada que espejar) Y de
    ``ENABLE_ESQUEMA_V2``.
    """
    return activo(cfg) and bool(getattr(cfg, "enable_esquema_v2", False))


def enlaces_para(cfg, client_id: str, proceso_id: str, email: str) -> dict[str, str] | None:
    """`{"relevante": url, "irrelevante": url}` de un destinatario, o `None` si está apagado.

    Nunca lanza: un proceso sin `numero_referencia` simplemente no lleva botones.
    """
    if not activo(cfg) or not proceso_id or not email:
        return None
    try:
        token = tokens.firmar(
            cfg.feedback_hmac_secret, proceso_id, client_id, tokens.destinatario_id(email)
        )
        return {v: tokens.url(cfg.feedback_base_url, token, v) for v in tokens.VOTOS}
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron firmar los enlaces de %s: %s", proceso_id, exc)
        return None


def registrar_reportadas(cfg, client_id: str, oportunidades) -> None:
    """Guarda la foto de cada proceso enviado. Se llama UNA vez por cliente y corrida."""
    if not activo(cfg) or not oportunidades:
        return
    try:
        from src.feedback import repo
        from src.memoria import db
        with db.connection(cfg) as conn:
            for opp in oportunidades:
                if not getattr(opp, "numero_referencia", ""):
                    continue
                repo.upsert_reportada(conn, client_id, opp)
                if _activo_v2(cfg):
                    try:
                        repo.upsert_reportada_v2(conn, client_id, opp,
                                                 portal_id=_portal(cfg, client_id))
                    except Exception as exc:  # noqa: BLE001 — v2 nunca revierte v1
                        logger.warning(
                            "[%s] Espejo v2 de registrar_reportadas falló (%s); v1 queda intacto: %s",
                            client_id, opp.numero_referencia, exc,
                        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] No se pudo registrar los procesos reportados en ADB: %s",
                       client_id, exc)


def leer_reportada(cfg, client_id: str, proceso_id: str) -> dict | None:
    """La foto del proceso, o `None` (no registrada, o la ADB no responde)."""
    try:
        from src.feedback import repo
        from src.memoria import db
        with db.connection(cfg) as conn:
            return repo.get_reportada(conn, proceso_id, client_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo leer el proceso reportado %s: %s", proceso_id, exc)
        return None


def leer_voto(cfg, client_id: str, proceso_id: str, dest_id: str) -> dict | None:
    """El voto vigente de un destinatario, o `None`."""
    try:
        from src.feedback import repo
        from src.memoria import db
        with db.connection(cfg) as conn:
            return repo.get_voto(conn, proceso_id, client_id, dest_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo leer el voto de %s: %s", proceso_id, exc)
        return None


def guardar_voto(
    cfg,
    *,
    client_id: str,
    proceso_id: str,
    dest_id: str,
    email: str | None,
    voto: str,
    motivos: list[str],
    comentario: str,
) -> bool:
    """Dual-write: JSONL (siempre, es el respaldo) + ADB (la fuente de verdad).

    Devuelve `True` si quedó en la ADB. El JSONL se escribe primero y aparte: si la
    ADB está caída, el criterio de la persona no se pierde y se puede reimportar.
    """
    _respaldo_jsonl(cfg, client_id, proceso_id, dest_id, email, voto, motivos, comentario)
    try:
        from src.feedback import repo
        from src.memoria import db
        with db.connection(cfg) as conn:
            repo.upsert_voto(
                conn, proceso_id=proceso_id, cliente_id=client_id, destinatario_id=dest_id,
                destinatario_email=email, voto=voto, motivos=motivos, comentario=comentario,
            )
            if _activo_v2(cfg):
                try:
                    repo.upsert_voto_v2(
                        conn, proceso_id=proceso_id, cliente_id=client_id,
                        destinatario_id=dest_id, voto=voto, motivos=motivos,
                        comentario=comentario, portal_id=_portal(cfg, client_id),
                    )
                except Exception as exc:  # noqa: BLE001 — v2 nunca revierte v1
                    logger.warning(
                        "[%s] Espejo v2 de guardar_voto falló (%s); v1 queda intacto: %s",
                        client_id, proceso_id, exc,
                    )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] No se pudo guardar el voto de %s en ADB: %s", client_id, proceso_id, exc)
        return False


def _respaldo_jsonl(cfg, client_id, proceso_id, dest_id, email, voto, motivos, comentario) -> None:
    try:
        ruta = os.path.join(cfg.data_dir, "history", f"{client_id}.feedback.jsonl")
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        linea = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "proceso_id": proceso_id, "cliente_id": client_id,
            "destinatario_id": dest_id, "destinatario_email": email,
            "voto": voto, "motivos": motivos, "comentario": comentario,
        }
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] No se pudo escribir el respaldo JSONL del voto: %s", client_id, exc)
