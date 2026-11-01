"""Minado conservador: propuestas auditadas y validación temporal antes de activar."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

from src.aprendizaje import repo
from src.classifier.marcas import contiene, normalizar

_STOP = frozenset("de del la el los las y o para por con en un una suministro servicio servicios compra contratacion adquisicion renovacion soporte licencias".split())


def terminos(texto):
    """Unigramas y bigramas completos; cada título aporta un único voto por término."""
    palabras = re.findall(r"[a-z][a-z0-9]+", normalizar(texto))
    salida = {p for p in palabras if len(p) >= 3 and p not in _STOP}
    salida.update(f"{a} {b}" for a, b in zip(palabras, palabras[1:])
                  if a not in _STOP and b not in _STOP)
    return {t for t in salida if len(t.encode("utf-8")) <= 120}


def _fecha(fila):
    fecha = fila.get("creado_en")
    if not isinstance(fecha, datetime):
        return None
    return fecha.replace(tzinfo=timezone.utc) if fecha.tzinfo is None else fecha.astimezone(timezone.utc)


def _partir(filas):
    """El proceso entero queda a un lado del corte; fechas ausentes no habilitan activación."""
    grupos = defaultdict(list)
    for fila in filas:
        if fila.get("split") != "critico" and fila.get("proceso_gid") and _fecha(fila):
            grupos[fila["proceso_gid"]].append(fila)
    fechas = sorted({max(_fecha(f) for f in grupo) for grupo in grupos.values()})
    if len(fechas) < 2:
        return [], []
    corte = fechas[min(len(fechas) - 1, max(1, int(len(fechas) * .8)))]
    entrenamiento, holdout = [], []
    for grupo in grupos.values():
        # Un proceso con observaciones a ambos lados no entra en entrenamiento.
        if min(_fecha(f) for f in grupo) >= corte:
            holdout.extend(grupo)
        elif max(_fecha(f) for f in grupo) < corte and all(f.get("split") != "prueba" for f in grupo):
            entrenamiento.extend(grupo)
    return entrenamiento, holdout


def _estadistica(filas, termino):
    grupos = defaultdict(list)
    for fila in filas:
        if contiene(fila.get("titulo_texto", ""), termino):
            grupos[fila.get("proceso_gid") or ("ejemplo", fila.get("ejemplo_id"))].append(fila)
    positivos = sum(all(f["etiqueta"] == "positivo" for f in grupo) for grupo in grupos.values())
    fuentes = {f["fuente"] for grupo in grupos.values() for f in grupo}
    return len(grupos), positivos / len(grupos) if grupos else 0.0, fuentes


def proponer(filas, *, soporte_minimo=5, soporte_holdout=3, precision_minima=.95):
    """Calcula propuestas sin escribir. Los ejemplos humanos se leen, nunca se modifican."""
    por_marca = defaultdict(list)
    for fila in filas:
        if fila.get("activo", True):
            por_marca[fila["marca_id"]].append(fila)
    propuestas = []
    for marca, ejemplos in sorted(por_marca.items()):
        entrenamiento, holdout = _partir(ejemplos)
        criticos = [f for f in ejemplos if f.get("split") == "critico"]
        candidatos = set().union(*(terminos(f.get("titulo_texto", "")) for f in entrenamiento
                                   if f["etiqueta"] == "positivo")) if entrenamiento else set()
        for termino in sorted(candidatos):
            soporte, precision, fuentes = _estadistica(entrenamiento, termino)
            if soporte < soporte_minimo or precision < precision_minima:
                continue
            n_h, p_h, fuentes_h = _estadistica(holdout, termino)
            # Un término fuerte sólo puede confirmar positivos: jamás un crítico negativo/ajeno.
            seguros = bool(criticos) and all(f["etiqueta"] == "positivo" or
                                           not contiene(f.get("titulo_texto", ""), termino) for f in criticos)
            activar = (termino != "crystal" and seguros and len(fuentes) >= 2 and len(fuentes_h) >= 2
                       and n_h >= soporte_holdout and p_h >= precision_minima)
            propuestas.append(dict(marca_id=marca, termino=termino, tipo="fuerte", soporte=soporte,
                                   precision_est=precision, soporte_holdout=n_h, precision_holdout=p_h,
                                   activar=activar))
    return propuestas


def ejecutar(conn, cliente_id, aplicar=False, *, soporte_minimo=5, soporte_holdout=3, precision_minima=.95):
    """Usa la transacción del llamador: ante fallo se debe hacer rollback del paso completo."""
    filas = repo.listar_ejemplos(conn, cliente_id, limite=100000)
    if len(filas) >= 100000:
        return {"candidatos": [], "propuestos": 0, "activados": 0, "motivo": "muestra truncada"}
    propuestas = proponer(filas, soporte_minimo=soporte_minimo, soporte_holdout=soporte_holdout,
                          precision_minima=precision_minima)
    resultado = {"candidatos": propuestas, "propuestos": 0, "activados": 0}
    if not aplicar:
        return resultado
    for propuesta in propuestas:
        termino_id = repo.proponer_termino(conn, cliente_id, propuesta["marca_id"], propuesta["termino"],
                                          propuesta["tipo"], soporte=propuesta["soporte"],
                                          precision_est=propuesta["precision_est"], origen="minado")
        if termino_id is not None:
            resultado["propuestos"] += 1
            if propuesta["activar"] and repo.activar_termino(conn, termino_id):
                resultado["activados"] += 1
    return resultado
