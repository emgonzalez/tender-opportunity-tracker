#!/usr/bin/env python3
"""Precisión y recobro de la clasificación de Costa Rica contra etiquetas humanas (sin ADB, sin red).

Une `muestra_ciega.csv` ya etiquetado con `clave_veredictos.csv` (ambos de `scripts/exportar_muestra_cr.py`) y,
si está, `modalidades.csv` respondido por el equipo de CR. Calcula:

- «relevante»: matriz de confusión, precisión y recobro **en la muestra** (con intervalo de Wilson) y
  **ponderados por estrato** (estimación para la población: la muestra sobrerrepresenta a propósito el borde y
  los no relevantes con señal de TI); κ de Cohen y acuerdo bruto; desglose por estrato.
- por marca: `src.aprendizaje.evaluacion.evaluar` y sus umbrales (recobro, precisión confirmada, Wilson Oracle).
- tabla de errores por familia UNSPSC, institución y palabras.
- modalidades cuya competibilidad en `src/portales/modalidades.py` contradice al equipo de CR.

Las filas sin etiquetar se cuentan y se excluyen; nunca hacen fallar. Un CSV mal formado sí falla, con un mensaje
que dice qué columna o qué fila corregir. Salida: informe Markdown y JSON (solo agregados e ids públicos).
Procedimiento completo en `docs/planes/evaluacion-cr.md`.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.exportar_muestra_cr import (  # noqa: E402
    COL_COMENTARIO,
    COL_MARCAS,
    COL_MOD_OFERTABLE,
    COL_OFERTABLE,
    COL_RELEVANTE,
    COLUMNAS_CIEGO,
    COLUMNAS_CLAVE,
    ESTRATOS,
    PORTAL_DEFAULT,
    normalizar,
)
from src.aprendizaje import evaluacion as ev  # noqa: E402
from src.notifier.veredicto_marca import EQUIVALENCIAS  # noqa: E402
from src.portales.modalidades import (  # noqa: E402
    COMPETIBLE,
    CONDICIONAL,
    NO_COMPETIBLE,
    competibilidad_de,
)

# Umbrales propuestos para promover CR a PROD (docs/planes/evaluacion-cr.md §5), sobre la estimación ponderada.
PRECISION_RELEVANTE_MIN = 0.70
RECOBRO_RELEVANTE_MIN = 0.85
ETIQUETADAS_MIN = 100
ENVIADOS = ("confirmada", "por_confirmar")
_SI = {"si", "s", "yes", "y", "x", "1", "true", "verdadero"}
_NO = {"no", "n", "0", "false", "falso"}
_DEPENDE = {"depende", "condicional", "a veces", "?"}
_SIN_MARCA = {"", "-", "ninguna", "ninguno", "n/a", "na", "no"}
# Palabras vacías para la tabla de errores por palabras: las de relleno de cualquier cartel.
_PALABRAS_VACIAS = frozenset(
    "para con por los las del que una uno unos unas sus este esta esto como mas entre sobre segun "
    "adquisicion compra contratacion servicio servicios suministro equipo equipos institucion "
    "proceso procedimiento cartel partida partidas lineas linea".split())


class CsvInvalido(ValueError):
    """Un CSV que no se puede evaluar; el mensaje dice qué corregir."""


# ── Lectura y validación ──────────────────────────────────────────────────────────────────────────────────

def leer_csv(ruta: Path, requeridas: tuple[str, ...]) -> list[dict]:
    """Lee un CSV en UTF-8 (con o sin BOM) separado por coma, punto y coma o tabulador (Excel en español)."""
    if not ruta.is_file():
        raise CsvInvalido(f"{ruta}: no existe")
    texto = ruta.read_text(encoding="utf-8-sig")
    if not texto.strip():
        raise CsvInvalido(f"{ruta}: está vacío")
    try:
        dialecto = csv.Sniffer().sniff(texto.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialecto = csv.excel
    lector = csv.DictReader(texto.splitlines(), dialect=dialecto)
    columnas = [c.strip() for c in lector.fieldnames or []]
    faltan = [c for c in requeridas if c not in columnas]
    if faltan:
        raise CsvInvalido(f"{ruta}: faltan columnas {faltan}; ¿se cambió el encabezado al etiquetar?")
    filas = []
    for fila in lector:
        if None in fila:
            raise CsvInvalido(f"{ruta}: la fila {lector.line_num} tiene más celdas que columnas "
                              "(¿una coma sin comillas en el comentario?)")
        filas.append({k.strip(): (v or "").strip() for k, v in fila.items()})
    return filas


def si_no(valor: str, *, ruta: Any, fila: int, columna: str, depende: bool = False) -> bool | str | None:
    """`sí`/`no` (y variantes) → bool; vacío → None; `depende` solo si se permite. Otro valor = error claro."""
    v = normalizar(valor)
    if v == "":
        return None
    if v in _SI:
        return True
    if v in _NO:
        return False
    if depende and v in _DEPENDE:
        return "depende"
    permitidos = "sí, no o depende" if depende else "sí o no"
    raise CsvInvalido(f"{ruta}: fila {fila}, columna «{columna}»: valor {valor!r} no válido (use {permitidos})")


def marca_canonica(nombre: str) -> str:
    """«Oracle NetSuite» y «oracle» → `oracle` (alias de `veredicto_marca.EQUIVALENCIAS`); otras, normalizadas."""
    n = normalizar(nombre)
    for clave, alias in EQUIVALENCIAS.items():
        if any(re.match(rf"{re.escape(a)}(?![a-z0-9])", n) for a in alias):
            return clave
    return n


def marcas_humanas(texto: str) -> set[str]:
    partes = re.split(r"[,;/\n|]+", texto or "")
    return {marca_canonica(p) for p in partes if normalizar(p) not in _SIN_MARCA}


def unir(ciego: list[dict], clave: list[dict], ruta_ciego: Any = "muestra_ciega.csv") -> dict:
    """Valida y une por `id`. Devuelve filas etiquetadas, sin etiquetar y avisos."""
    por_id: dict[str, dict] = {}
    for n, c in enumerate(clave, 2):
        if not c.get("id"):
            raise CsvInvalido(f"clave_veredictos.csv: fila {n} sin id")
        if c["id"] in por_id:
            raise CsvInvalido(f"clave_veredictos.csv: id repetido {c['id']!r}")
        if c.get("estrato") not in ESTRATOS:
            raise CsvInvalido(f"clave_veredictos.csv: fila {n}, estrato desconocido {c.get('estrato')!r}")
        try:
            c["_peso"] = float(c.get("peso") or "nan")
        except ValueError:
            c["_peso"] = math.nan
        if not math.isfinite(c["_peso"]) or c["_peso"] < 1:
            raise CsvInvalido(f"clave_veredictos.csv: fila {n}, peso inválido {c.get('peso')!r}")
        try:
            c["_marcas"] = json.loads(c.get("marcas_amalia") or "[]")
        except ValueError as exc:
            raise CsvInvalido(f"clave_veredictos.csv: fila {n}, marcas_amalia no es JSON") from exc
        por_id[c["id"]] = c
    etiquetadas, sin_etiquetar, avisos, vistos = [], [], [], set()
    for n, fila in enumerate(ciego, 2):
        ident = fila.get("id", "")
        if not ident:
            raise CsvInvalido(f"{ruta_ciego}: fila {n} sin id (no borre esa columna)")
        if ident in vistos:
            raise CsvInvalido(f"{ruta_ciego}: id repetido {ident!r} en la fila {n}")
        vistos.add(ident)
        if ident not in por_id:
            raise CsvInvalido(f"{ruta_ciego}: fila {n}, el id {ident!r} no está en clave_veredictos.csv "
                              "(¿se mezclaron muestras de dos exportaciones?)")
        relevante = si_no(fila[COL_RELEVANTE], ruta=ruta_ciego, fila=n, columna=COL_RELEVANTE)
        ofertable = si_no(fila[COL_OFERTABLE], ruta=ruta_ciego, fila=n, columna=COL_OFERTABLE)
        unida = {**fila, "clave": por_id[ident], "real": relevante, "ofertable": ofertable,
                 "marcas_humanas": marcas_humanas(fila[COL_MARCAS])}
        if relevante is None:
            if fila[COL_MARCAS] or fila[COL_OFERTABLE]:
                avisos.append(f"fila {n} ({ident}): tiene marcas u ofertable pero no «relevante»; se excluye")
            sin_etiquetar.append(unida)
        else:
            etiquetadas.append(unida)
    faltan_en_ciego = sorted(set(por_id) - vistos)
    if faltan_en_ciego:
        avisos.append(f"{len(faltan_en_ciego)} ids de la clave no aparecen en la muestra ciega (filas borradas)")
    return {"etiquetadas": etiquetadas, "sin_etiquetar": sin_etiquetar, "avisos": avisos,
            "faltan_en_ciego": faltan_en_ciego}


# ── Métricas ──────────────────────────────────────────────────────────────────────────────────────────────

def _pred(f: dict) -> bool:
    return f["clave"].get("es_relevante_amalia") == "sí"


def metricas_relevante(filas: list[dict]) -> dict:
    """En la muestra (Wilson) y ponderado por estrato (estimación poblacional, `ev.ips` con 1/peso)."""
    reales = [bool(f["real"]) for f in filas]
    preds = [_pred(f) for f in filas]
    m = ev.precision_recall_f1(ev.matriz_confusion(reales, preds))
    tp, fn = m["matriz"]["tp"], m["matriz"]["fn"]
    muestra = {"n": len(filas), "matriz": m["matriz"], "precision": m["precision"], "recobro": m["recall"],
               "f1": m["f1"], "wilson_precision": list(m["wilson"]), "wilson_recobro": list(ev.wilson(tp, tp + fn)),
               "kappa": ev.kappa(reales, preds),
               "acuerdo_bruto": sum(a == b for a, b in zip(reales, preds)) / len(filas) if filas else None}
    ponderado = None
    if filas:
        p = ev.ips(reales, preds, [1 / f["clave"]["_peso"] for f in filas])
        ponderado = {"precision": p["precision"], "recobro": p["recall"], "f1": p["f1"],
                     "matriz": {k: round(v, 3) for k, v in p["matriz"].items()}}
    por_estrato = {}
    for estrato in ESTRATOS:
        grupo = [f for f in filas if f["clave"]["estrato"] == estrato]
        humanos = sum(bool(f["real"]) for f in grupo)
        acuerdo = sum(bool(f["real"]) == _pred(f) for f in grupo)
        por_estrato[estrato] = {"n": len(grupo), "relevantes_humanos": humanos, "acuerdo": acuerdo,
                                "wilson_relevante_humano": list(ev.wilson(humanos, len(grupo)))}
    return {"muestra": muestra, "ponderado": ponderado, "por_estrato": por_estrato}


def decision_relevante(rel: dict, etiquetadas: int, minimo: int = ETIQUETADAS_MIN) -> dict:
    """Puerta de la Ola C sobre la estimación ponderada; con pocas etiquetas no hay decisión."""
    if etiquetadas < minimo or rel["ponderado"] is None:
        return {"estado": "insuficiente", "motivos": [f"solo {etiquetadas} filas etiquetadas (mínimo {minimo})"]}
    motivos = []
    p, r = rel["ponderado"]["precision"], rel["ponderado"]["recobro"]
    if p < PRECISION_RELEVANTE_MIN:
        motivos.append(f"precisión ponderada {p:.3f} < {PRECISION_RELEVANTE_MIN}")
    if r < RECOBRO_RELEVANTE_MIN:
        motivos.append(f"recobro ponderado {r:.3f} < {RECOBRO_RELEVANTE_MIN}")
    return {"estado": "no_cumple" if motivos else "cumple", "motivos": motivos}


def filas_por_marca(filas: list[dict]) -> list[dict]:
    """Una fila de `ev.evaluar` por (concurso etiquetado, marca). Marca que AMALIA no puso ⇒ `fuera` (no faltante).

    Una marca sin veredicto en la clave ya llega como `por_confirmar` (así la muestra el portal).
    """
    universo = set()
    for f in filas:
        universo |= f["marcas_humanas"]
        universo |= {marca_canonica(m.get("marca", "")) for m in f["clave"]["_marcas"]}
    universo.discard("")
    salida = []
    for f in filas:
        amalia = {}
        for m in f["clave"]["_marcas"]:
            amalia.setdefault(marca_canonica(m.get("marca", "")), m)
        for marca in sorted(universo):
            m = amalia.get(marca)
            score = m.get("score") if m else None
            salida.append({"marca": marca, "estrato": "prueba", "real": marca in f["marcas_humanas"],
                           "veredicto": (m.get("veredicto") or "por_confirmar") if m else "fuera",
                           "probabilidad": score if isinstance(score, (int, float)) and 0 <= score <= 1
                           else None})
    return salida


def metricas_marca(filas: list[dict]) -> dict:
    """`ev.evaluar` + los umbrales absolutos de `src/aprendizaje/evaluacion.py` por marca.

    La parte comparativa de `ev.puede_promover` (candidata contra activa, ECE) exige un par activa/sombra sobre
    los mismos casos: eso lo cubre `scripts/evaluar_marca.py`; acá no hay variante activa que comparar.
    """
    salida = {}
    for clave, m in ev.evaluar(filas_por_marca(filas)).items():
        marca = clave.split("/")[0]
        positivos = m["matriz"]["tp"] + m["matriz"]["fn"]
        motivos = []
        if not positivos:
            motivos.append("sin positivos humanos: no se puede medir recobro")
        if m["recall"] < ev.RECALL_MINIMO:
            motivos.append(f"recobro {m['recall']:.3f} < {ev.RECALL_MINIMO}")
        if m["precision_confirmada"] < ev.PRECISION_MINIMA:
            motivos.append(f"precisión confirmada {m['precision_confirmada']:.3f} < {ev.PRECISION_MINIMA}")
        if marca == "oracle" and m["wilson_confirmada"][0] < ev.WILSON_ORACLE_MINIMO:
            motivos.append(f"Wilson Oracle {m['wilson_confirmada'][0]:.3f} < {ev.WILSON_ORACLE_MINIMO}")
        salida[marca] = {"n": m["n"], "positivos_humanos": positivos, "matriz": m["matriz"],
                         "precision": m["precision"], "recobro": m["recall"], "f1": m["f1"],
                         "wilson_precision": list(m["wilson"]),
                         "wilson_recobro": list(ev.wilson(m["matriz"]["tp"], positivos)),
                         "precision_confirmada": m["precision_confirmada"],
                         "wilson_confirmada": list(m["wilson_confirmada"]), "ece": m["ece"],
                         "n_calibracion": m["n_calibracion"],
                         "puerta": {"cumple": not motivos, "motivos": motivos}}
    return salida


def _palabras(texto: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9]+", normalizar(texto)) if len(p) >= 5 and p not in _PALABRAS_VACIAS}


def tabla_errores(filas: list[dict], top: int = 10) -> dict:
    """Errores (falsos positivos y negativos de «relevante») agrupados por familia UNSPSC, institución y palabra.

    Cada grupo lleva n etiquetados, FP, FN y la tasa de error del grupo: así se ve qué familias se confunden.
    """
    grupos: dict[str, dict[str, Counter]] = {e: defaultdict(Counter) for e in ("unspsc_familia", "institucion",
                                                                              "palabra")}
    ids_fp, ids_fn = [], []
    for f in filas:
        tipo = "fp" if _pred(f) and not f["real"] else "fn" if f["real"] and not _pred(f) else ""
        if tipo == "fp":
            ids_fp.append(f["id"])
        elif tipo == "fn":
            ids_fn.append(f["id"])
        familias = {c[:4] for c in (f.get("unspsc") or f["clave"].get("unspsc") or "").split() if len(c) >= 4}
        claves = {"unspsc_familia": familias or {"(sin UNSPSC)"},
                  "institucion": {f.get("institucion") or "(sin institución)"},
                  "palabra": _palabras(f"{f.get('descripcion', '')} {f.get('detalle', '')}")}
        for eje, valores in claves.items():
            for v in valores:
                grupos[eje][v]["n"] += 1
                if tipo:
                    grupos[eje][v][tipo] += 1
    salida: dict[str, Any] = {"falsos_positivos": sorted(ids_fp), "falsos_negativos": sorted(ids_fn)}
    for eje, conteos in grupos.items():
        filas_eje = [{"grupo": g, "n": c["n"], "fp": c["fp"], "fn": c["fn"],
                      "tasa_error": (c["fp"] + c["fn"]) / c["n"]} for g, c in conteos.items() if c["fp"] + c["fn"]]
        filas_eje.sort(key=lambda x: (-(x["fp"] + x["fn"]), -x["tasa_error"], x["grupo"]))
        salida[eje] = filas_eje[:top]
    return salida


def _contraste(respuesta: Any, competibilidad: str) -> str:
    if respuesta is True:
        return "contradice" if competibilidad == NO_COMPETIBLE else "difiere" if competibilidad == CONDICIONAL \
            else "coincide"
    if respuesta is False:
        return "contradice" if competibilidad == COMPETIBLE else "difiere" if competibilidad == CONDICIONAL \
            else "coincide"
    return "coincide" if competibilidad == CONDICIONAL else "difiere"


def contrastar_modalidades(respuestas: list[dict] | None, filas: list[dict],
                           ruta: Any = "modalidades.csv", portal: str = PORTAL_DEFAULT) -> dict:
    """Competibilidad del código (`src/portales/modalidades.py`) frente al equipo (hoja) y frente a la muestra.

    `contradice`: el equipo dice «sí» y el código la da por no competible, o «no» y el código por competible.
    `difiere`: una de las dos partes dice «depende». Sin respuesta, la modalidad no se contrasta.
    """
    equipo = []
    for n, r in enumerate(respuestas or [], 2):
        codigo = r.get("codigo", "").strip().upper()
        if not codigo:
            raise CsvInvalido(f"{ruta}: fila {n} sin código")
        resp = si_no(r.get(COL_MOD_OFERTABLE, ""), ruta=ruta, fila=n, columna=COL_MOD_OFERTABLE, depende=True)
        if resp is None:
            continue
        comp = competibilidad_de(portal, codigo)
        equipo.append({"codigo": codigo, "nombre": r.get("nombre", ""), "equipo": resp, "codigo_dice": comp,
                       "resultado": _contraste(resp, comp)})
    por_modalidad: dict[str, Counter] = defaultdict(Counter)
    for f in filas:
        if f["ofertable"] is None:
            continue
        codigo = f["clave"].get("modalidad_codigo") or "(sin código)"
        por_modalidad[codigo]["si" if f["ofertable"] else "no"] += 1
    muestra = []
    for codigo, c in sorted(por_modalidad.items()):
        comp = competibilidad_de(portal, "" if codigo.startswith("(") else codigo)
        contradicen = c["no"] if comp == COMPETIBLE else c["si"] if comp == NO_COMPETIBLE else 0
        muestra.append({"codigo": codigo, "ofertable_si": c["si"], "ofertable_no": c["no"], "codigo_dice": comp,
                        "contradicen": contradicen})
    return {"equipo": equipo, "muestra": muestra,
            "contradicciones": sorted({e["codigo"] for e in equipo if e["resultado"] == "contradice"}
                                      | {m["codigo"] for m in muestra if m["contradicen"]})}


def comparar_sombra(filas: list[dict], sombra: list[dict], ruta: Any = "sombra.csv") -> dict:
    """La variante `sombra` de una capa (`id`, `es_relevante` sí/no) contra la activa, sobre las mismas etiquetas.

    Devuelve las métricas de la sombra en la muestra y McNemar (`ev.mcnemar`) sobre los pares discordantes.
    Solo cuentan los concursos etiquetados que tienen predicción de sombra.
    """
    preds = {}
    for n, s in enumerate(sombra, 2):
        valor = si_no(s.get("es_relevante", ""), ruta=ruta, fila=n, columna="es_relevante")
        if valor is not None:
            preds[s.get("id", "")] = valor
    comunes = [f for f in filas if f["id"] in preds]
    reales = [bool(f["real"]) for f in comunes]
    m = ev.precision_recall_f1(ev.matriz_confusion(reales, [preds[f["id"]] for f in comunes]))
    tp, fn = m["matriz"]["tp"], m["matriz"]["fn"]
    return {"n": len(comunes), "matriz": m["matriz"], "precision": m["precision"], "recobro": m["recall"],
            "wilson_precision": list(m["wilson"]), "wilson_recobro": list(ev.wilson(tp, tp + fn)),
            "mcnemar": ev.mcnemar([_pred(f) == bool(f["real"]) for f in comunes],
                                  [preds[f["id"]] == bool(f["real"]) for f in comunes])}


def evaluar(ciego: list[dict], clave: list[dict], modalidades: list[dict] | None = None, *,
            minimo: int = ETIQUETADAS_MIN, ruta_ciego: Any = "muestra_ciega.csv",
            sombra: list[dict] | None = None) -> dict:
    union = unir(ciego, clave, ruta_ciego)
    filas = union["etiquetadas"]
    rel = metricas_relevante(filas)
    extra = {"sombra": comparar_sombra(filas, sombra)} if sombra is not None else {}
    return {
        **extra,
        "conteos": {"muestra": len(ciego), "etiquetadas": len(filas), "sin_etiquetar": len(union["sin_etiquetar"]),
                    "faltan_en_ciego": len(union["faltan_en_ciego"]),
                    "ofertable_etiquetadas": sum(f["ofertable"] is not None for f in filas)},
        "avisos": union["avisos"],
        "relevante": rel,
        "decision_relevante": decision_relevante(rel, len(filas), minimo),
        "umbrales": {"precision_relevante": PRECISION_RELEVANTE_MIN, "recobro_relevante": RECOBRO_RELEVANTE_MIN,
                     "etiquetadas_min": minimo, "marca_recobro": ev.RECALL_MINIMO,
                     "marca_precision_confirmada": ev.PRECISION_MINIMA,
                     "marca_wilson_oracle": ev.WILSON_ORACLE_MINIMO},
        "por_marca": metricas_marca(filas),
        "errores": tabla_errores(filas),
        "modalidades": contrastar_modalidades(modalidades, filas),
    }


# ── Informe ───────────────────────────────────────────────────────────────────────────────────────────────

def _f(x: Any) -> str:
    return "—" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def _ic(par: Any) -> str:
    return f"[{par[0]:.3f}, {par[1]:.3f}]"


def informe_markdown(r: dict) -> str:
    c, rel, d = r["conteos"], r["relevante"], r["decision_relevante"]
    m, p = rel["muestra"], rel["ponderado"] or {}
    lineas = [
        "# Evaluación de precisión — Costa Rica", "",
        f"Muestra {c['muestra']} · etiquetadas {c['etiquetadas']} · sin etiquetar {c['sin_etiquetar']}"
        f" · faltan en el ciego {c['faltan_en_ciego']}", "",
        f"**Decisión «relevante»: {d['estado']}**" + (" — " + "; ".join(d["motivos"]) if d["motivos"] else ""), "",
        "## «Relevante»", "",
        "| Medida | Muestra | IC Wilson 95 % | Ponderado (población) | Umbral |", "|---|---|---|---|---|",
        f"| Precisión | {_f(m['precision'])} | {_ic(m['wilson_precision'])} | {_f(p.get('precision'))}"
        f" | ≥ {PRECISION_RELEVANTE_MIN} |",
        f"| Recobro | {_f(m['recobro'])} | {_ic(m['wilson_recobro'])} | {_f(p.get('recobro'))}"
        f" | ≥ {RECOBRO_RELEVANTE_MIN} |",
        f"| F1 | {_f(m['f1'])} | | {_f(p.get('f1'))} | |",
        f"| κ de Cohen | {_f(m['kappa'])} | | | |",
        f"| Acuerdo bruto | {_f(m['acuerdo_bruto'])} | | | |", "",
        "Matriz (muestra): " + " · ".join(f"{k.upper()} {v}" for k, v in m["matriz"].items()), "",
        "| Estrato | n | Relevantes humanos | IC Wilson | Acuerdo con AMALIA |", "|---|---|---|---|---|",
        *[f"| {e} | {v['n']} | {v['relevantes_humanos']} | {_ic(v['wilson_relevante_humano'])} | {v['acuerdo']} |"
          for e, v in rel["por_estrato"].items()], "",
        "## Por marca", "",
        "| Marca | n | Positivos humanos | Precisión | Recobro (IC) | Precisión confirmada | Puerta |",
        "|---|---|---|---|---|---|---|",
        *[f"| {k} | {v['n']} | {v['positivos_humanos']} | {_f(v['precision'])} | {_f(v['recobro'])}"
          f" {_ic(v['wilson_recobro'])} | {_f(v['precision_confirmada'])} | "
          + ("cumple" if v["puerta"]["cumple"] else "no: " + "; ".join(v["puerta"]["motivos"])) + " |"
          for k, v in r["por_marca"].items()], "",
        "## Errores por familia", "",
    ]
    e = r["errores"]
    for eje, titulo in (("unspsc_familia", "Familia UNSPSC (4 dígitos)"), ("institucion", "Institución"),
                        ("palabra", "Palabra")):
        lineas += [f"### {titulo}", "", "| Grupo | n | FP | FN | Tasa de error |", "|---|---|---|---|---|",
                   *[f"| {g['grupo']} | {g['n']} | {g['fp']} | {g['fn']} | {g['tasa_error']:.2f} |" for g in e[eje]],
                   ""]
    lineas += [f"Falsos positivos ({len(e['falsos_positivos'])}): " + ", ".join(e["falsos_positivos"]), "",
               f"Falsos negativos ({len(e['falsos_negativos'])}): " + ", ".join(e["falsos_negativos"]), "",
               "## Modalidades", "", "| Código | Equipo CR | Código dice | Resultado |", "|---|---|---|---|"]
    mod = r["modalidades"]
    lineas += [f"| {x['codigo']} | {'sí' if x['equipo'] is True else 'no' if x['equipo'] is False else x['equipo']}"
               f" | {x['codigo_dice']} | {x['resultado']} |" for x in mod["equipo"]]
    lineas += ["", "| Código | Ofertable sí | Ofertable no | Código dice | Etiquetas que lo contradicen |",
               "|---|---|---|---|---|",
               *[f"| {x['codigo']} | {x['ofertable_si']} | {x['ofertable_no']} | {x['codigo_dice']}"
                 f" | {x['contradicen']} |" for x in mod["muestra"]], "",
               "Modalidades en contradicción: " + (", ".join(mod["contradicciones"]) or "ninguna"), ""]
    if "sombra" in r:
        s = r["sombra"]
        lineas += ["## Sombra contra activa", "",
                   f"n {s['n']} · precisión {_f(s['precision'])} {_ic(s['wilson_precision'])}"
                   f" · recobro {_f(s['recobro'])} {_ic(s['wilson_recobro'])}",
                   f"McNemar: activa mejor {s['mcnemar']['activa_mejor']} · sombra mejor"
                   f" {s['mcnemar']['sombra_mejor']} · p = {s['mcnemar']['p']:.4f}", ""]
    if r["avisos"]:
        lineas += ["## Avisos", "", *[f"- {a}" for a in r["avisos"]], ""]
    return "\n".join(lineas)


def main(argv: list[str] | None = None, *, decir: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ciego", type=Path, required=True, help="muestra_ciega.csv ya etiquetado")
    p.add_argument("--clave", type=Path, required=True, help="clave_veredictos.csv de la misma exportación")
    p.add_argument("--modalidades", type=Path, help="modalidades.csv respondido por el equipo de CR")
    p.add_argument("--sombra", type=Path, help="CSV id,es_relevante con la variante sombra de una capa")
    p.add_argument("--minimo-etiquetadas", type=int, default=ETIQUETADAS_MIN)
    p.add_argument("--salida-md", type=Path)
    p.add_argument("--salida-json", type=Path)
    args = p.parse_args(argv)
    try:
        ciego = leer_csv(args.ciego, COLUMNAS_CIEGO[:1] + (COL_RELEVANTE, COL_MARCAS, COL_OFERTABLE, COL_COMENTARIO))
        clave = leer_csv(args.clave, COLUMNAS_CLAVE)
        modalidades = leer_csv(args.modalidades, ("codigo", COL_MOD_OFERTABLE)) if args.modalidades else None
        sombra = leer_csv(args.sombra, ("id", "es_relevante")) if args.sombra else None
        resultado = evaluar(ciego, clave, modalidades, minimo=args.minimo_etiquetadas, ruta_ciego=args.ciego,
                            sombra=sombra)
    except CsvInvalido as exc:
        decir(f"ERROR: {exc}")
        return 2
    base = args.ciego.parent
    md = args.salida_md or base / "informe_precision_cr.md"
    js = args.salida_json or base / "informe_precision_cr.json"
    md.write_text(informe_markdown(resultado), encoding="utf-8")
    js.write_text(json.dumps(resultado, ensure_ascii=False, indent=2, default=list) + "\n", encoding="utf-8")
    c = resultado["conteos"]
    decir(f"Etiquetadas {c['etiquetadas']}/{c['muestra']} (sin etiquetar: {c['sin_etiquetar']})")
    decir(f"Decisión «relevante»: {resultado['decision_relevante']['estado']}")
    decir(f"Informe: {md} · {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
