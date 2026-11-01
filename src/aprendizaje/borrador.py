"""Propuestas Oracle de baja ponderación para revisión humana; nunca etiquetas de prueba."""
from __future__ import annotations

import json
import re
from collections import Counter

from src.aprendizaje import repo
from src.aprendizaje.semilla import terminos_semilla
from src.classifier.marcas import contiene, normalizar


def terminos(conn=None, cliente_id="multicomputos_sv") -> list[dict]:
    """Sin conexión permite preparar propuestas con el diccionario público."""
    if conn is not None:
        return repo.listar_terminos_activos(conn, cliente_id)
    return [dict(marca_id=m, termino=t, tipo=k) for m, t, k in terminos_semilla()]


def proponer(titulo: str, razon: str = "", diccionario=None) -> dict:
    filas = terminos() if diccionario is None else diccionario
    filas = [t for t in filas if t.get("activo", True) and
             t.get("estado", "activo") in ("semilla", "activo")]
    fuertes = [t for t in filas if t["marca_id"] == "oracle" and t["tipo"] == "fuerte"]
    salida = dict(etiqueta="negativo", motivo="Oracle sugerida sin evidencia en el título",
                  cita="", avisos="")
    migracion = re.search(r"\b(?:migracion|migrar)\s+(?:de|desde)\s+oracle\b", normalizar(titulo))
    excluye = any(t["marca_id"] == "oracle" and t["tipo"] == "excluye" and
                  contiene(titulo, t["termino"]) for t in filas)
    if migracion or excluye:
        return dict(etiqueta="positivo", motivo="contrapatrón de salida de Oracle", cita=titulo,
                    avisos="revisar: migración fuera de Oracle")
    if any(contiene(titulo, t["termino"]) for t in fuertes):
        return dict(etiqueta="positivo", motivo="término fuerte Oracle en título", cita=titulo, avisos="")
    for t in filas:
        if t["tipo"] == "ajena" and t["marca_id"] != "oracle" and contiene(titulo, t["termino"]):
            return dict(etiqueta="ajena", motivo=f"marca ajena {t['marca_id']}: {t['termino']}",
                        cita=titulo, avisos="")
    if any(contiene(razon, t["termino"]) for t in fuertes):
        return dict(etiqueta="positivo", motivo="término Oracle en razón de la marca", cita=razon,
                    avisos="revisar: solo en la razón del LLM")
    return salida


def leer_procesos(conn, cliente_id: str, diccionario: list[dict]) -> list[dict]:
    """SELECT acotado: JSON_TABLE extrae solo el nombre; el resto del JSON viaja como CLOB."""
    binds = {"cliente_id": cliente_id}
    filtros = []
    for i, t in enumerate(diccionario):
        if t["marca_id"] == "oracle" and t["tipo"] == "fuerte":
            # Cada alias tiene su bind: no se supera el límite de bytes de un patrón Oracle.
            clave = f"termino_{i}"
            binds[clave] = "(^|[^[:alnum:]_])" + re.escape(normalizar(t["termino"])) + "([^[:alnum:]_]|$)"
            filtros.append("REGEXP_LIKE(TRANSLATE(LOWER(p.titulo), 'áéíóúüñ', 'aeiouun'), "
                           f":{clave})")
    titulo_sql = " OR ".join(filtros) or "1 = 0"
    sql = f"""
        SELECT p.proceso_gid, p.titulo, p.institucion,
               JSON_SERIALIZE(cp.marcas RETURNING CLOB), cp.match_tecnico,
               cp.match_categoria, cp.factibilidad, cp.match_overall, cp.estado
          FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid
         WHERE cp.cliente_id = :cliente_id AND (
               EXISTS (SELECT 1 FROM JSON_TABLE(cp.marcas, '$[*]' COLUMNS (
                   marca VARCHAR2(256) PATH '$.marca' NULL ON ERROR)) j
                       WHERE LOWER(j.marca) LIKE 'oracle%') OR ({titulo_sql}))
         ORDER BY p.proceso_gid FETCH FIRST 200 ROWS ONLY
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        salida = []
        for f in cursor.fetchall():
            raw = f[3].read() if hasattr(f[3], "read") else f[3]
            marcas = json.loads(raw) if raw else []
            oracle = [m for m in marcas if isinstance(m, dict) and
                      normalizar(m.get("marca", "")).startswith("oracle")]
            salida.append(dict(proceso_gid=f[0], titulo=f[1] or "", institucion=f[2],
                               razon="\n".join(str(m.get("razon") or "") for m in oracle),
                               marcas=oracle, scores=f[4:8], estado=f[8]))
        return salida
    finally:
        cursor.close()


def _publico(texto: str) -> str:
    # La consola no expone contactos que puedan venir dentro de títulos o razones.
    texto = re.sub(r"\S+@\S+", "[contacto omitido]", texto)
    texto = re.sub(r"\b\+?\d[\d ()-]{6,}\d\b", "[contacto omitido]", texto)
    return " ".join(texto.split())


def ejecutar(conn, args, decir=print) -> None:
    from src.classifier.alias_clientes import id_canonico
    cid = id_canonico(args.cliente)
    diccionario = terminos(conn, cid)
    procesos = leer_procesos(conn, cid, diccionario)
    conteos = Counter()
    nuevos = 0
    decir("BORRADOR — revisión humana pendiente")
    decir(f"{'Proceso':12} | {'Título':70} | Etiqueta | Motivo | Avisos")
    for p in procesos:
        propuesta = proponer(p["titulo"], p["razon"], diccionario)
        conteos[propuesta["etiqueta"]] += 1
        decir(f"{_publico(p['proceso_gid'])[:12]:12} | {_publico(p['titulo'])[:70]:70} | "
              f"{propuesta['etiqueta']} | {_publico(propuesta['motivo'])} | {propuesta['avisos']}")
        if args.aplicar:
            resultado = repo.agregar_ejemplo(
                conn, cid, "oracle", propuesta["etiqueta"], "semilla", p["titulo"],
                proceso_gid=p["proceso_gid"], peso=0.3, split="entrenamiento",
                creado_por="borrador:oracle", evidencia=propuesta["cita"] or "borrador: revisar",
            )
            nuevos += bool(resultado["nuevo"])
    decir("Resumen: " + ", ".join(f"{e}={conteos[e]}" for e in ("positivo", "negativo", "ajena")))
    decir(f"BORRADOR: {nuevos} insertados" if args.aplicar else "[dry-run] no se escribió nada (usar --aplicar)")
