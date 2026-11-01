#!/usr/bin/env python3
"""Evaluación solo lectura. Entrenamiento se informa, pero nunca abre la puerta."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.aprendizaje import evaluacion as ev


def configuracion():
    # Config importa load_dotenv al arrancar: estas herramientas solo usan el entorno.
    with patch("dotenv.load_dotenv", return_value=False):
        from src.config import get_config
    return get_config()


def leer(conn, sql, binds=None):
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds or {})
        return cursor.fetchall()
    finally:
        cursor.close()


def objeto(valor):
    if hasattr(valor, "read"):
        valor = valor.read()
    return json.loads(valor) if isinstance(valor, (str, bytes)) else valor


def marca_salida(salida, marca):
    """Contrato: marcas/lista de marcas_recomendadas o salida individual con marca_id."""
    salida = objeto(salida)
    if isinstance(salida, dict):
        salida = salida.get("marcas", salida.get("marcas_recomendadas", [salida]))
    if not isinstance(salida, list):
        return {}
    for m in salida:
        if isinstance(m, dict) and str(m.get("marca_id", m.get("marca", ""))).lower() == marca.lower():
            return m
    return {}


def informe(conn, cliente, capa):
    ejemplos = leer(conn, "SELECT ejemplo_id, proceso_gid, marca_id, etiqueta, split, fuente, creado_por FROM ejemplo_marca "
                    "WHERE cliente_id = :cliente AND activo = 1 ORDER BY ejemplo_id", {"cliente": cliente})
    decisiones = leer(conn, "SELECT proceso_gid, variante, JSON_SERIALIZE(salida RETURNING CLOB), score, propension "
                      "FROM decision_capa WHERE cliente_id = :cliente AND capa = :capa "
                      "ORDER BY creado_en, id", {"cliente": cliente, "capa": capa})
    portal = dict(leer(conn, "SELECT proceso_gid, JSON_SERIALIZE(marcas RETURNING CLOB) FROM cliente_proceso "
                       "WHERE cliente_id = :cliente", {"cliente": cliente}))
    ultimas = {(gid, variante): (salida, score, prop) for gid, variante, salida, score, prop in decisiones}
    ejemplos_originales = ejemplos
    unicos, conflictos = {}, set()
    for ejemplo in ejemplos:
        eid, gid, marca, etiqueta, split, fuente, actor = ejemplo
        clave = (gid or f"ejemplo:{eid}", marca.lower(), split)
        if clave in unicos and unicos[clave][3] != etiqueta:
            conflictos.add(clave)
        unicos[clave] = ejemplo
    ejemplos = list(unicos.values())
    filas = {"activa": [], "sombra": []}
    criticos, pares_a, pares_s, props, reales_ips, preds_ips = [], [], [], [], [], []
    for _, gid, marca, etiqueta, split, fuente, actor in ejemplos:
        par = []
        for variante in filas:
            salida, score, prop = ultimas.get((gid, variante), (None, None, None))
            m = marca_salida(salida, marca)
            # El portal solo es referencia activa si no existe decisión de esa capa.
            if (gid, variante) not in ultimas and variante == "activa":
                m = marca_salida(portal.get(gid), marca)
            veredicto = m.get("veredicto")
            prob = m.get("score", score)
            prob = float(prob) if prob is not None else None
            real = etiqueta == "positivo"
            f = dict(marca=marca, estrato=split, real=real, veredicto=veredicto, probabilidad=prob)
            filas[variante].append(f)
            correcto = veredicto == ("confirmada" if real else "fuera")
            par.append(correcto)
            if variante == "sombra" and split == "critico":
                criticos.append(correcto)
            if variante == "sombra" and split == "prueba" and prop is not None:
                props.append(float(prop))
                reales_ips.append(real)
                preds_ips.append(veredicto in {"confirmada", "por_confirmar"})
        if split == "prueba":
            pares_a.append(par[0])
            pares_s.append(par[1])
    metricas = {v: ev.evaluar(f) for v, f in filas.items()}
    # Solo compara dos actores humanos distintos; nunca confunde variantes con anotadores.
    humanas = {}
    for _, gid, marca, etiqueta, split, fuente, actor in ejemplos_originales:
        if fuente == "humana" and actor and gid:
            humanas.setdefault((gid, marca, split), {})[actor] = etiqueta
    actores = sorted({actor for etiquetas in humanas.values() for actor in etiquetas})
    pares_humanos = {}
    for i, actor in enumerate(actores):
        for otro in actores[i + 1:]:
            pares = [(d[actor], d[otro]) for d in humanas.values() if actor in d and otro in d]
            if pares:
                # No publica identificadores de personas en el reporte.
                pares_humanos[f"etiquetador_{i + 1}/etiquetador_{actores.index(otro) + 1}"] = {
                    "n": len(pares), "kappa": ev.kappa([a for a, _ in pares], [b for _, b in pares])}
    estratos_fuente = {}
    for variante in filas:
        por_fuente = [dict(f, estrato=f"{f['estrato']}:{ejemplo[5]}")
                      for f, ejemplo in zip(filas[variante], ejemplos)]
        estratos_fuente[variante] = ev.evaluar(por_fuente)
    promocion = ev.puede_promover(metricas["sombra"], metricas["activa"], criticos)
    if conflictos:
        promocion["puede_promover"] = False
        promocion["motivos"].append("Etiquetas contradictorias para el mismo caso/marca/split")
    return {"metricas": metricas, "promocion": promocion,
            "casos_con_etiquetas_contradictorias": len(conflictos),
            "mcnemar": ev.mcnemar(pares_a, pares_s),
            "kappa_variantes": ev.kappa([f["veredicto"] for f in filas["activa"]],
                                       [f["veredicto"] for f in filas["sombra"]]),
            "kappa_etiquetadores": pares_humanos or None,
            "metricas_por_fuente": estratos_fuente,
            "nota_kappa": "Solo pares humanos identificados por creado_por y con casos compartidos",
            "ips_sombra": ev.ips(reales_ips, preds_ips, props) if props else None,
            "cobertura_ips": len(props)}


def imprimir(reporte):
    for seccion in ("metricas", "metricas_por_fuente"):
        for variante, grupos in reporte[seccion].items():
            print(f"\n{seccion}/{variante}: marca/estrato n precisión recall F1 Wilson(confirmada) ECE matriz")
            for clave, m in grupos.items():
                print(clave, m["n"], *(round(m[k], 4) for k in ("precision", "recall", "f1")),
                      m["wilson_confirmada"], m["ece"], m["matriz"])
    for clave, valor in reporte.items():
        if clave not in {"metricas", "metricas_por_fuente"}:
            print(f"{clave}: {valor}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cliente", required=True)
    p.add_argument("--capa", default="C8")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.dry_run:
        print("Solo lectura: leería ejemplos, decisiones activa/sombra y veredictos del portal; sin conexión.")
        return 0
    try:
        from src.memoria.db import connection
        cfg = configuracion()
        with connection(cfg) as conn:
            reporte = informe(conn, args.cliente, args.capa)
        if args.json:
            print(json.dumps(reporte, ensure_ascii=False, allow_nan=False))
        else:
            imprimir(reporte)
        return 0
    except Exception:
        print("Evaluación no disponible: revise configuración ADB, tablas y formato de decisiones.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
