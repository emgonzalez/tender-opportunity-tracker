"""Correo por marca (E14): qué parte del reporte ve un destinatario con rol restringido a un fabricante.

El portal filtra en SQL con `memoria.relevancia.sql_marca`; el correo filtra en memoria los
`ProcesoClasificado` del día. En off/sombra rige marca recomendada o título; en activo
ambos leen el veredicto persistido y los equivalentes de producto del diccionario.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from src.memoria.relevancia import MARCAS_RESTRINGIBLES, es_de_marca
from src.notifier.veredicto_marca import EQUIVALENCIAS

logger = logging.getLogger(__name__)


def _restringibles(solo_marcas: Iterable[str]) -> frozenset[str]:
    """Solo las claves conocidas: una marca fuera de la lista cerrada no restringe ni lanza KeyError."""
    return frozenset(m for m in (solo_marcas or ()) if m in MARCAS_RESTRINGIBLES)


def _en_titulo(texto: str, solo_marcas: Iterable[str]) -> bool:
    """La palabra de la marca como palabra completa en el texto (mismo patrón que `sql_marca`)."""
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(MARCAS_RESTRINGIBLES[m])}(?![A-Za-z0-9_])", texto or "", re.I)
        for m in solo_marcas
    )


def es_de_marca_proceso(proc, solo_marcas: Iterable[str], modo: str = "off") -> bool:
    """True si ``proc`` involucra alguna de ``solo_marcas``; sin restricción (vacío) todo pasa."""
    if not solo_marcas:
        return True
    marcas = _restringibles(solo_marcas)
    if not marcas:
        return False  # restricción que no sabemos evaluar: nunca se abre de más
    if modo == "activo":
        return veredicto_de_proceso(proc, marcas) != "fuera"
    if any(es_de_marca(m.marca or "", marcas, modo="off") for m in (getattr(proc, "marcas_recomendadas", None) or [])):
        return True
    return _en_titulo(getattr(proc, "descripcion", ""), marcas)


def marcas_visibles(marcas: Iterable[str], solo_marcas: Iterable[str]) -> list[str]:
    """Nombres de marca que puede ver un perfil restringido (sin co-marcas); sin restricción, todos."""
    marcas = list(marcas)
    if not solo_marcas:
        return marcas
    restringibles = _restringibles(solo_marcas)
    return [m for m in marcas if restringibles and es_de_marca(m, restringibles, modo="off")]


def _valor(obj, clave, default=""):
    return obj.get(clave, default) if isinstance(obj, dict) else getattr(obj, clave, default)


def entradas_de_marca(proc, solo_marcas: Iterable[str]) -> list:
    """Entradas persistidas de las marcas restringidas, incluyendo productos equivalentes."""
    aliases = [a for m in _restringibles(solo_marcas) for a in EQUIVALENCIAS.get(m, (m,))]
    entradas = _valor(proc, "marcas_recomendadas", None)
    if entradas is None:
        entradas = _valor(proc, "marcas", [])
    return [entrada for entrada in entradas or [] if any(
        re.match(rf"{re.escape(alias)}(?![A-Za-z0-9_])", _valor(entrada, "marca"), re.I)
        for alias in aliases)]


def veredicto_de_proceso(proc, solo_marcas: Iterable[str]) -> str:
    """Nivel visible por perfil; las entradas antiguas requieren confirmación."""
    niveles = {_valor(m, "veredicto") or "por_confirmar" for m in entradas_de_marca(proc, solo_marcas)}
    for nivel in ("confirmada", "por_confirmar", "no_competible", "fuera"):
        if nivel in niveles:
            return nivel
    return "por_confirmar" if niveles else "fuera"


def registrar_sombra(config, cliente_id, procesos, perfiles, timestamp, resultados_analisis=None):
    """Registra diferencias por perfil sin destinatarios ni contenido del proceso. Nunca lanza."""
    if getattr(config, "marca_veredicto", "off") != "sombra":
        return
    try:
        from src.notifier.agrupamiento import grupo_de, grupo_de_valores, match_de

        analisis = resultados_analisis or {}
        filas = []
        for perfil in set(perfiles) | {frozenset()}:
            for proc in procesos:
                referencia = proc.numero_referencia
                legado = grupo_de(match_de(proc), analisis.get(referencia))
                if not es_de_marca_proceso(proc, perfil):
                    legado = "fuera"
                nuevo = veredicto_de_proceso(proc, perfil) if perfil else "confirmada"
                if nuevo == "confirmada":
                    ra = analisis.get(referencia)
                    nuevo = (grupo_de_valores(match_de(proc), None, bool(ra and ra.descartado_por_triaje))
                             if perfil else grupo_de(match_de(proc), ra))
                # El completo también cambia cuando aparecen explicaciones por marca.
                ra = analisis.get(referencia)
                explicaciones = not perfil and (any(
                    _valor(m, "veredicto") in ("confirmada", "no_competible")
                    for m in (proc.marcas_recomendadas or []))
                    or bool(_valor(ra, "marca_exigida", None)))
                if legado != nuevo or explicaciones:
                    filas.append({"ts": timestamp.isoformat(), "perfil": sorted(perfil) or ["completo"],
                                  "proceso": referencia, "legado": legado, "nuevo": nuevo,
                                  "explicaciones_nuevas": bool(explicaciones)})
        if filas:
            # La clave del cliente es parte de la ruta: rechazar escapes, nunca sanear hacia otro tenant.
            if not re.fullmatch(r"[A-Za-z0-9_-]+", cliente_id):
                return
            ruta = Path(config.data_dir) / "history" / f"{cliente_id}.marca_sombra.jsonl"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with ruta.open("a", encoding="utf-8") as archivo:
                for fila in filas:
                    archivo.write(json.dumps(fila, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — la observación no interrumpe la corrida
        logger.warning("No se pudo registrar la sombra de marcas (%s).", type(exc).__name__)
