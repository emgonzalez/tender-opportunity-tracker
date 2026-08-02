"""src.drilldown.quota — Fase 2 (MVP Track A). Ver amalia-oci-serverless-arch/PLAN_FASE2_TRACK_A.md.

Guardia de cuota de tokens (pre-flight): antes de lanzar una llamada al modelo de
analisis, estima su costo en tokens y verifica que quepa en el presupuesto diario
configurado (cfg.daily_token_budget). El estado (tokens consumidos hoy) se persiste
en disco (cfg.token_budget_path) con reset automatico al cambiar de fecha.

Todas las funciones son tolerantes a errores de IO/red: loguean el problema y caen
a un comportamiento seguro (no deben tumbar el batch nocturno).
"""
import json
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)


def _load_budget(path: str) -> dict:
    """Carga el presupuesto diario desde `path`.

    Si el archivo no existe, esta corrupto, o su fecha no es la de hoy, devuelve
    un presupuesto reseteado para el dia actual (reset diario).
    """
    hoy = date.today().isoformat()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("fecha") != hoy:
            return {"fecha": hoy, "consumido": 0}
        if "consumido" not in data:
            return {"fecha": hoy, "consumido": 0}
        return data
    except FileNotFoundError:
        return {"fecha": hoy, "consumido": 0}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning("token_budget: no se pudo leer %s (%s); reseteando presupuesto del dia", path, e)
        return {"fecha": hoy, "consumido": 0}


def _save_budget(path: str, data: dict) -> None:
    """Escribe `data` en `path` de forma atomica (write-to-tmp + os.replace)."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("token_budget: no se pudo guardar %s (%s)", path, e)


def estimate_tokens(client, model: str, messages: list) -> int:
    """Estima los tokens de entrada de `messages` para `model`.

    Usa la API oficial de conteo de tokens; si falla por cualquier motivo, cae a
    una heuristica conservadora (len(str(content)) // 4 por mensaje) y lo loguea.
    """
    try:
        result = client.messages.count_tokens(model=model, messages=messages)
        return result.input_tokens
    except Exception as e:
        estimado = sum(len(str(m.get("content", ""))) // 4 for m in messages)
        logger.warning(
            "token_budget: count_tokens fallo (%s); usando estimacion heuristica de %d tokens",
            e,
            estimado,
        )
        return estimado


def check_and_reserve(client, model: str, messages: list, cfg) -> bool:
    """Verifica si hay presupuesto diario disponible y, de haberlo, lo reserva.

    Devuelve True si la llamada cabe en el presupuesto diario (y ya quedo
    reservada), False si excederia el presupuesto (no se reserva nada).
    """
    estimado = estimate_tokens(client, model, messages)

    path = cfg.token_budget_path
    data = _load_budget(path)

    consumido = data.get("consumido", 0)
    if consumido + estimado > cfg.daily_token_budget:
        logger.warning(
            "token_budget: presupuesto diario excedido (consumido=%d, estimado=%d, limite=%d); "
            "llamada rechazada",
            consumido,
            estimado,
            cfg.daily_token_budget,
        )
        return False

    data["consumido"] = consumido + estimado
    _save_budget(path, data)
    return True
