"""Recálculo manual determinista del histórico; dry-run salvo petición explícita.

La lectura usa la conexión recibida: no abre conexiones ocultas ni consulta el
spool local. La ADB v2 conserva la matriz, pero no las señales estructuradas
nuevas del análisis; si el llamador las aporta, también se evalúan.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from dataclasses import asdict, is_dataclass

from src.aprendizaje import repo
from src.classifier.marcas import detectar_por_diccionario, marcas_ajenas_en, normalizar
from src.drilldown.analysis import evaluar_competibilidad
from src.memoria import repo as memoria_repo
from src.notifier.veredicto_marca import EQUIVALENCIAS, veredictos_de_proceso
from src.scraper.models import ResultadoAnalisis

NIVELES = ("confirmada", "por_confirmar", "no_competible", "fuera")


def publico(texto):
    """Consola sin correos, teléfonos ni saltos de línea provenientes del título."""
    texto = re.sub(r"\S+@\S+", "[contacto omitido]", str(texto or ""))
    texto = re.sub(r"\b\+?\d[\d ()-]{6,}\d\b", "[contacto omitido]", texto)
    return " ".join(texto.split())


def _json(raw, defecto):
    if hasattr(raw, "read"):
        raw = raw.read()
    return json.loads(raw) if isinstance(raw, str) and raw else raw if raw is not None else defecto


def leer_procesos(conn, cliente_id):
    """Todo el histórico, sin límite ni filtro de relevancia, con binds por cliente."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT cp.proceso_gid, p.titulo, JSON_SERIALIZE(cp.marcas RETURNING CLOB),
                   cp.razon, p.modalidad_raw, cp.es_relevante, cp.estado,
                   JSON_SERIALIZE(a.matriz_riesgos RETURNING CLOB)
              FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid
              LEFT JOIN analisis a ON a.cliente_id = cp.cliente_id
                                  AND a.proceso_gid = cp.proceso_gid
             WHERE cp.cliente_id = :cliente_id ORDER BY cp.proceso_gid
        """, {"cliente_id": cliente_id})
        salida = []
        for gid, titulo, raw, razon, modalidad, relevante, estado, matriz in cur.fetchall():
            ra = ResultadoAnalisis.desde_dict(dict(
                proceso_uuid=gid.split(":", 1)[-1], estado=estado or "",
                matriz=_json(matriz, None)))
            salida.append(dict(proceso_gid=gid, titulo=titulo or "", marcas=_json(raw, []),
                               razon=razon or "", modalidad=modalidad or "",
                               es_relevante=bool(relevante), analisis=ra))
        return salida
    finally:
        cur.close()


def marca_id(nombre):
    nombre = normalizar(nombre)
    for marca, aliases in EQUIVALENCIAS.items():
        if nombre in aliases or nombre.startswith(marca + " "):
            return marca
    return nombre


def legado(fila, marca="oracle"):
    """La regla histórica Oracle usa prefijo de marca o substring en título."""
    return (marca in str(fila.get("titulo", "")).lower() or
            any(str(m.get("marca", "")).lower().startswith(marca)
                for m in fila.get("marcas", []) if isinstance(m, dict)))


def calcular(fila, terminos, marcas=None):
    """Ignora scores, evidencia y veredictos guardados: las sugerencias son Alternativa.

    Sun se confirma aun sin término activo: EQUIVALENCIAS del veredicto puro
    ya incluye ese alias. Una sugerencia sin evidencia queda por_confirmar,
    nunca se convierte en negativa por mera ausencia de señales.
    """
    guardadas = [m for m in fila.get("marcas", []) if isinstance(m, dict)]
    ids = list(dict.fromkeys(marca_id(m.get("marca", "")) for m in guardadas))
    ids = list(marcas) if marcas is not None else ids
    proc = dict(titulo=fila.get("titulo", ""), razon=fila.get("razon", ""),
                marcas_recomendadas=[dict(marca=m.get("marca", ""), role="Alternativa")
                                     for m in guardadas],
                marcas_ajenas=marcas_ajenas_en(fila.get("titulo", ""), terminos))
    original = fila.get("analisis")
    ra = asdict(original) if is_dataclass(original) else copy.deepcopy(original or {})
    salida = {}
    for marca in ids:
        # La función pura sigue siendo la autoridad; el detector ayuda a conservar
        # la misma lectura del diccionario que la clasificación normal.
        deteccion = detectar_por_diccionario(proc["titulo"], terminos, marca)
        if deteccion == "CONFIRMADA" and not any(
                marca_id(m["marca"]) == marca for m in proc["marcas_recomendadas"]):
            proc["marcas_recomendadas"].append(dict(marca=marca, role="Alternativa"))
        matriz = ResultadoAnalisis.desde_dict(ra).matriz
        competible, motivo, evidencia = evaluar_competibilidad(
            matriz, fila.get("modalidad", ""), [marca], nuevo=True,
            **{k: ra.get(k) for k in ("marca_exigida", "proveedor_unico", "carta_fabricante",
                                      "equivalente_admitido")})
        contexto = dict(ra)
        if competible is not None:
            contexto.update(competible=competible, motivo_bloqueo=motivo,
                            anexos={"competibilidad": {"evidencia": evidencia}})
        salida.update(veredictos_de_proceso(proc, contexto, [marca], terminos=terminos))
    return salida


def ejecutar(conn, args, decir=print, *, lote=100):
    """Solo completa entradas sin veredicto; conserva incluso las marcas fuera."""
    if lote < 1:
        raise ValueError("lote debe ser positivo")
    terminos = repo.listar_terminos_activos(conn, args.cliente)
    conteos = Counter()
    pendientes = 0
    cambios = []
    for fila in leer_procesos(conn, args.cliente):
        marcas = fila.get("marcas", [])
        faltantes = [m for m in marcas if isinstance(m, dict) and not m.get("veredicto")]
        if not faltantes:
            continue
        resultados = calcular(fila, terminos)
        nuevas = copy.deepcopy(marcas)
        for m in nuevas:
            if not isinstance(m, dict) or m.get("veredicto"):
                continue
            mid = marca_id(m.get("marca", ""))
            v = resultados[mid]
            m.update(veredicto=v.nivel, por_que=v.por_que, evidencia=v.evidencia, origen="diccionario")
            conteos[mid, v.nivel] += 1
        oracle = calcular(fila, terminos, ["oracle"])["oracle"]
        antes = legado(fila)
        despues = oracle.nivel in {"confirmada", "por_confirmar"}
        if antes != despues:
            cambios.append(f"{publico(fila['titulo'])[:70]} | Oracle {antes} → {oracle.nivel}")
        if args.aplicar:
            memoria_repo.actualizar_marcas_v2(conn, args.cliente, fila["proceso_gid"], nuevas)
            pendientes += 1
            if pendientes % lote == 0:
                conn.commit()
    if args.aplicar and pendientes % lote:
        conn.commit()
    decir("Marca | " + " | ".join(NIVELES))
    for marca in sorted({m for m, _ in conteos}):
        decir(marca + " | " + " | ".join(str(conteos[marca, n]) for n in NIVELES))
    for cambio in cambios:
        decir(cambio)
    decir(f"{'Aplicado' if args.aplicar else '[dry-run] sin escrituras'}: "
          f"{sum(conteos.values())} marcas; {len(cambios)} cambios de alcance Oracle")
    return dict(conteos=conteos, cambios=cambios, filas_actualizadas=pendientes)
