"""Reconciliación optativa por marca, sin cambiar relevancia ni roles del reporte."""
from __future__ import annotations

import logging
from copy import copy
from dataclasses import asdict

from src.aprendizaje import service as aprendizaje
from src.aprendizaje.semilla import terminos_semilla
from src.classifier.marcas import marcas_ajenas_en, normalizar
from src.drilldown.citas import validar_cita
from src.memoria import service as memoria
from src.notifier.veredicto_marca import EQUIVALENCIAS, a_json, veredictos_de_proceso

logger = logging.getLogger(__name__)


def _marca_id(nombre, catalogo):
    texto = normalizar(nombre)
    for marca_id, marca in catalogo.items():
        aliases = (marca_id, normalizar(marca.name), *EQUIVALENCIAS.get(marca_id, ()))
        if texto in aliases:
            return marca_id
    return None


def _analisis_con_cita_validada(resultado, paginas):
    """No acepta metadatos de validación producidos por el modelo."""
    if resultado is None:
        return None
    copia = copy(resultado)
    exigida = dict(getattr(resultado, "marca_exigida", None) or {})
    if not exigida:
        return copia
    exigida["evidencia_valida"] = False
    exigida.pop("segunda_fuente", None)
    pagina = exigida.get("pagina")
    fuente = next((p for p in paginas if type(pagina) is int and p.get("pagina") == pagina), None)
    if fuente is not None:
        texto = {"texto": fuente.get("texto", ""),
                 "fuente_texto": fuente.get("fuente", "desconocida")}
        validacion = validar_cita(exigida.get("cita", ""), texto)
        exigida["evidencia_valida"] = validacion.ok
        exigida["fuente_texto"] = validacion.fuente_texto
    copia.marca_exigida = exigida
    return copia


def reconciliar(clasificados, resultados_analisis, cfg, perfil):
    """En sombra solo añade campos ignorados por los lectores anteriores.

    Evalúa todo el catálogo, pero conserva entradas, nombres y roles: añadir una
    marca ausente cambiaría los destinatarios incluso con el filtro apagado.
    Los veredictos de marcas sin entrada quedan en la decisión de auditoría.
    """
    modo = getattr(cfg, "marca_veredicto", "off")
    if modo not in {"sombra", "activo"}:
        return
    try:
        catalogo = {normalizar(b.id): b for c in perfil.catalog.categories for b in c.brands}
        terminos = aprendizaje.terminos_activos(cfg, perfil.client_id)
        if not terminos:
            terminos = [dict(zip(("marca_id", "termino", "tipo"), t)) for t in terminos_semilla()]
        calibraciones = {m: aprendizaje.calibracion_activa(cfg, perfil.client_id, m) for m in catalogo}
    except Exception:  # noqa: BLE001 — ninguna dependencia opcional impide notificar
        logger.warning("Reconciliación: contexto no disponible")
        return
    for proceso in clasificados:
        if not proceso.es_relevante:
            continue
        try:
            referencia = proceso.numero_referencia
            resultado = (resultados_analisis or {}).get(referencia)
            gid = f"{memoria._portal(cfg, perfil.client_id)}:{referencia}"
            paginas = aprendizaje.paginas_pliego(cfg, gid)
            resultado = _analisis_con_cita_validada(resultado, paginas)
            antes = [asdict(m) for m in proceso.marcas_recomendadas]
            texto_proceso = "\n".join(filter(None, (proceso.descripcion, getattr(proceso, "titulo", ""))))
            ajenas = marcas_ajenas_en(texto_proceso, terminos)
            existentes = {normalizar(m.get("marca", "")) for m in proceso.marcas_ajenas}
            proceso.marcas_ajenas.extend(m for m in ajenas if normalizar(m["marca"]) not in existentes)
            verificaciones = ((getattr(resultado, "anexos", None) or {}).get("verificacion_c3", {})
                              if getattr(cfg, "intel_c3", "off") == "activo" else {})
            veredictos = {}
            for marca_id in catalogo:
                veredictos.update(veredictos_de_proceso(
                    proceso, resultado, [marca_id], terminos=terminos,
                    calibracion=calibraciones[marca_id],
                    verificacion=verificaciones.get(marca_id)))
            for entrada in proceso.marcas_recomendadas:
                marca_id = _marca_id(entrada.marca, catalogo)
                if marca_id is None:
                    continue
                veredicto = veredictos[marca_id]
                for campo, valor in a_json(veredicto).items():
                    setattr(entrada, campo, valor)
                # Un score probabilístico previo no se sustituye por certeza ficticia.
                if entrada.score is None and veredicto.nivel == "confirmada":
                    entrada.score = 1.0
                entrada.evidencia_valida = bool(veredicto.evidencia) if veredicto.fuente in {
                    "título", "diccionario"} else entrada.evidencia_valida
            # Persistencias separadas: una ADB caída nunca revierte lo reconciliado en memoria.
            try:
                memoria.actualizar_marcas(cfg, perfil.client_id, referencia, proceso.marcas_recomendadas)
            except Exception:  # noqa: BLE001
                logger.warning("Reconciliación: marcas no persistidas para %s", referencia)
            try:
                aprendizaje.guardar_decision(
                    cfg, perfil.client_id, gid, "veredicto",
                    variante="sombra" if modo == "sombra" else "activa",
                    salida={"antes": antes, "marcas": {m: a_json(v) for m, v in veredictos.items()},
                            "marcas_ajenas": proceso.marcas_ajenas})
            except Exception:  # noqa: BLE001
                logger.warning("Reconciliación: decisión no persistida para %s", referencia)
        except Exception:  # noqa: BLE001 — aislar también errores por proceso
            logger.warning("Reconciliación no disponible para %s", proceso.numero_referencia)
