"""C7: señales léxicas de mercado, sin LLM ni vectores.

No se ejecuta automáticamente: solo mediante ``python -m src.mercado senales``.
Los agregados son contexto histórico, nunca evidencia para descartar una marca.
La afinidad cuenta renglones adjudicados, no participación ni cuota de mercado.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from src.classifier.marcas import contiene, normalizar

SHARE_TOP_MINIMO = 0.9
DIRECTAS_MINIMAS = 3
TIPOS = ("institucion", "institucion_marca")


def proveedor_unico_de_hecho(senal: dict) -> bool:
    return senal["share_top"] >= SHARE_TOP_MINIMO and senal["n_directas"] >= DIRECTAS_MINIMAS


def patron_lexico(terminos) -> str:
    """Prefiltro POSIX conservador; Oracle limita patrones a 512 bytes.

    Títulos no ASCII siempre pasan a validación Python (tildes). Diccionarios
    largos o no ASCII desactivan solo el prefiltro, nunca el cruce léxico.
    """
    aliases = {normalizar(t["termino"]) for t in terminos
               if t.get("tipo") == "fuerte" and t.get("termino")}
    if any(not re.fullmatch(r"[a-z0-9_ ]+", a) for a in aliases):
        return "."
    tokens = sorted({a.split()[0] for a in aliases})
    patron = "(^|[^[:alnum:]_])(" + "|".join(tokens) + ")([^[:alnum:]_]|$)"
    return patron if aliases and len(patron.encode()) <= 500 else "."


def agregar(filas, terminos) -> list[dict]:
    """Agrupa renglones por institución y por marca fuerte vigente.

    Sin normalización de tildes portable en Oracle, el léxico se normaliza en
    Python. No se agrupan proveedores desconocidos como si fueran uno solo.
    ``directa`` solo procede de una modalidad conocida para ese cliente.
    """
    grupos = defaultdict(list)
    fuertes = defaultdict(set)
    for t in terminos:
        if (t.get("tipo") == "fuerte" and t.get("activo", 1)
                and t.get("estado", "activo") in ("semilla", "activo") and t.get("termino")):
            fuertes[t["marca_id"]].add(t["termino"])
    for fila in filas:
        institucion = normalizar(fila.get("institucion") or "")
        if not institucion:
            continue
        grupos[(institucion, "")].append(fila)
        for marca, aliases in fuertes.items():
            if fila.get("candidato_lexico", True) and any(
                    contiene(fila.get("nombre_proceso") or "", alias) for alias in aliases):
                grupos[(institucion, marca)].append(fila)
    salida = []
    for (institucion, marca), items in sorted(grupos.items()):
        proveedores = Counter()
        nombres = {}
        for f in items:
            nombre = normalizar(f.get("proveedor_nombre") or "")
            identidad = ("id", str(f["id_proveedor"])) if f.get("id_proveedor") is not None else (
                ("nombre", nombre) if nombre else ("desconocido", str(f["id"])))
            proveedores[identidad] += 1
            nombres[identidad] = nombre if identidad[0] != "desconocido" else ""
        top = sorted(proveedores, key=lambda p: (-proveedores[p], p))[0]
        fechas = [f["fecha_adjudicacion"] for f in items if f.get("fecha_adjudicacion")]
        senal = dict(tipo="institucion_marca" if marca else "institucion", institucion_norm=institucion,
                     marca_id=marca or None, n_adj=len(items), n_proveedores=len(proveedores),
                     proveedor_top=nombres[top], share_top=proveedores[top] / len(items),
                     n_directas=len({f["codigo_proceso"] for f in items if f.get("directa")}),
                     ultima_fecha=max(fechas) if fechas else None)
        senal["proveedor_unico_de_hecho"] = bool(nombres[top]) and proveedor_unico_de_hecho(senal)
        salida.append(senal)
    return salida


def ejecutar(cfg, cliente_id: str, aplicar: bool = False) -> None:
    """CLI manual tolerante; dry-run solo lee. No se ejecuta automáticamente."""
    from src.memoria import db
    from src.mercado import repo
    try:
        with db.connection(cfg) as conn:
            senales = repo.calcular_senales(conn, cliente_id, estricto=True)
            print(f"{'Aplicar' if aplicar else 'Dry-run'}: {len(senales)} señales")
            unicos = sorted((s for s in senales if s['tipo'] == 'institucion'
                             and s['proveedor_unico_de_hecho']), key=lambda s: -s['n_adj'])
            print("Proveedor único de hecho (top 10):")
            for s in unicos[:10]:
                print(f"  {s['institucion_norm']}: {s['proveedor_top']} ({s['share_top']:.1%})")
            print("Afinidad por marca (top 10):")
            afinidades = sorted((s for s in senales if s['tipo'] == 'institucion_marca'),
                                key=lambda s: (-s['n_adj'], s['marca_id'], s['institucion_norm']))
            for s in afinidades[:10]:
                print(f"  {s['marca_id']} / {s['institucion_norm']}: {s['n_adj']} renglones")
            if aplicar:
                print(f"Guardadas: {repo.guardar_senales(conn, cliente_id, senales, version=1)}")
    except Exception:
        repo.logger.warning("C7 no disponible; no se modificaron señales.")
