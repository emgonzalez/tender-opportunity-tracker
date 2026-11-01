"""Caché optativa sobre decision_capa; ningún fallo afecta a la corrida."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.aprendizaje import repo

TTL_SEGUNDOS = 86400  # Un día; cada consumidor puede reducirlo.


def clave_cache(capa, version_prompt, modelo, entrada_normalizada):
    """SHA256 de JSON canónico UTF-8. El consumidor normaliza la entrada.

    Debe incluir en la entrada toda dependencia (cliente, diccionario, perfil…).
    No convierte objetos desconocidos a str: evitar huellas no reproducibles.
    """
    texto = json.dumps([capa, version_prompt, modelo, entrada_normalizada],
                       sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def obtener(conn, clave, capa, *, cliente_id, version=None, variante="activa",
            ttl=TTL_SEGUNDOS, ahora=None):
    """Devuelve la salida o None (miss); TTL desde creado_en, nunca deslizante.

    Oracle TIMESTAMP sin zona se compara con hora local de la conexión/runtime.
    """
    try:
        fila = repo.buscar_decision_por_hash(conn, clave, capa, cliente_id=cliente_id,
                                            version=version, variante=variante)
        if not fila or fila.get("salida") is None or ttl <= 0:
            return None
        creado = fila["creado_en"]
        ahora = ahora or (datetime.now(creado.tzinfo) if creado.tzinfo else datetime.now())
        if creado.tzinfo and not ahora.tzinfo:
            return None
        edad = (ahora - creado).total_seconds()
        salida = fila["salida"]
        if not 0 <= edad < ttl or (isinstance(salida, dict) and salida.get("truncado")):
            return None
        return salida
    except Exception:
        return None


def guardar(conn, clave, capa, salida, *, cliente_id, proceso_gid, version=None, variante="activa"):
    """Guarda sin commit: la transacción pertenece al llamador. Fallo ⇒ None."""
    try:
        return repo.guardar_decision(conn, cliente_id, proceso_gid, capa, entrada_hash=clave,
                                     salida=salida, version=version, variante=variante)
    except Exception:
        return None
