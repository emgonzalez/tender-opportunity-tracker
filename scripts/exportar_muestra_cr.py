#!/usr/bin/env python3
"""Muestra ciega y estratificada de concursos de Costa Rica para etiquetado humano (solo lectura).

Procedimiento completo en `docs/planes/evaluacion-cr.md`. Lee `proceso` y `cliente_proceso` de la ADB dentro de
una transacción `SET TRANSACTION READ ONLY` (Oracle rechaza cualquier DML posterior) y escribe tres CSV en
`--salida` (por defecto `data/evaluacion/cr-<fecha>/`, que no se versiona):

- `muestra_ciega.csv`: lo que ve quien etiqueta. **Nada** del veredicto de AMALIA (ni relevancia, ni match, ni
  marcas, ni estrato, ni razón): solo datos públicos del concurso y cuatro columnas vacías para la etiqueta.
- `clave_veredictos.csv`: el veredicto de AMALIA y el estrato por `id`. Se queda con quien evalúa.
- `modalidades.csv`: plantilla para que el equipo de CR diga qué modalidades de SICOP son ofertables (también
  ciega: no muestra lo que hoy decide el código).

La ADB no guarda el UNSPSC (vive en `Proceso.datos_portal["unspsc"]`, ver `docs/arquitectura/sicop-api.md`): con
`--historial data/history/multicomputos_cr.jsonl` se toma de ahí, junto con el código de modalidad y el `detalle`
de las partidas. Sin historial, la señal de TI del estrato de falsos negativos se apoya solo en palabras clave.

Nunca escribe en la base: no tiene modo `--aplicar` porque no hay nada que aplicar.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics.tablero import _json, es_relevante  # noqa: E402
from src.notifier.agrupamiento import UMBRAL_MATCH_DECIDIR, match_de_valores  # noqa: E402
from src.portales.modalidades import MODALIDADES, competibilidad_de  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
CLIENTE_DEFAULT = "multicomputos_cr"
PORTAL_DEFAULT = "sicop_cr"
SEMILLA_DEFAULT = 20261124

# Estratos excluyentes: cada concurso cae en exactamente uno, así los pesos (población / muestra) son válidos.
RELEVANTE_ALTO = "relevante_alto"            # relevante para AMALIA y match >= 80
BORDE = "borde"                              # relevante con match < 80, o no relevante con match en [50, 80)
NO_RELEVANTE_SENAL_TI = "no_relevante_senal_ti"  # no relevante con UNSPSC 43/81 o palabra del catálogo
NO_RELEVANTE_AZAR = "no_relevante_azar"      # no relevante sin señal de TI
ESTRATOS = (RELEVANTE_ALTO, BORDE, NO_RELEVANTE_SENAL_TI, NO_RELEVANTE_AZAR)
OBJETIVOS_DEFAULT = {RELEVANTE_ALTO: 60, BORDE: 40, NO_RELEVANTE_SENAL_TI: 30, NO_RELEVANTE_AZAR: 50}
MATCH_BORDE_MIN = 50

# Segmentos UNSPSC de TI: 43 (equipos y software de TI y telecomunicaciones), 81 (servicios de ingeniería e
# informática). Los 8 dígitos son segmento(2)+familia(2)+clase(2)+producto(2).
SEGMENTOS_TI = ("43", "81")

COL_RELEVANTE = "relevante (sí/no)"
COL_MARCAS = "marcas"
COL_OFERTABLE = "ofertable (sí/no)"
COL_COMENTARIO = "comentario"
COLUMNAS_ETIQUETA = (COL_RELEVANTE, COL_MARCAS, COL_OFERTABLE, COL_COMENTARIO)
# Lista BLANCA del ciego: un test verifica que no se cuele ninguna columna del veredicto.
COLUMNAS_CIEGO = (
    "id", "numero_procedimiento", "institucion", "descripcion", "detalle", "unspsc", "modalidad",
    "monto", "moneda", "fecha_publicacion", *COLUMNAS_ETIQUETA,
)
COLUMNAS_CLAVE = (
    "id", "estrato", "poblacion_estrato", "muestra_estrato", "peso", "es_relevante_amalia", "match",
    "es_relevante_col", "triaje", "estado", "categoria_detectada", "marcas_amalia", "modalidad_codigo",
    "competibilidad_codigo", "unspsc", "senal_ti",
)
COL_MOD_OFERTABLE = "ofertable (sí/no/depende)"
COLUMNAS_MODALIDADES = ("codigo", "nombre", COL_MOD_OFERTABLE, "condiciones", COL_COMENTARIO)

# Palabras del catálogo demasiado cortas o ambiguas en textos de compras públicas de CR («SAN» de San José,
# «red» vial, «acceso» a una finca…). Solo afinan el estrato de falsos negativos: un falso acierto aquí solo
# agrega un concurso más a etiquetar.
PALABRAS_AMBIGUAS = frozenset({
    "red", "acceso", "identidad", "implementacion", "migracion", "recuperacion", "respaldo", "monitoreo",
    "torre", "disco", "componentes", "suscripcion", "conectividad empresarial", "redundancia electrica",
    "respaldo energetico", "rendimiento de aplicaciones", "amenaza interna", "onboarding digital",
})
LARGO_MINIMO_PALABRA = 4


def normalizar(texto: Any) -> str:
    """Minúsculas, sin tildes y con espacios colapsados (para comparar, nunca para mostrar)."""
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


# ── Perfil → palabras clave ───────────────────────────────────────────────────────────────────────────────

def cargar_perfil(cliente: str, clients_dir: Path) -> dict:
    """El perfil YAML del cliente con `extends` resuelto (sin ADB: es una herramienta de evaluación)."""
    import yaml

    from src.classifier.client_profile import resolver_perfil

    ruta = clients_dir / f"{cliente}.yaml"
    with open(ruta, encoding="utf-8") as f:
        return resolver_perfil(yaml.safe_load(f), str(clients_dir))


def palabras_clave(perfil: dict) -> list[str]:
    """Señales de inclusión, taxonomía de tags y nombres de marca del catálogo, normalizadas y sin ambiguas."""
    catalogo = perfil.get("catalog") or {}
    crudas: list[str] = []
    for senal in catalogo.get("inclusion_signals") or []:
        crudas.extend(str(senal).split(","))
    crudas.extend(str(t) for t in catalogo.get("tag_taxonomy") or [])
    for categoria in catalogo.get("categories") or []:
        crudas.extend(str(m.get("name") or "") for m in categoria.get("brands") or [] if isinstance(m, dict))
    salida = []
    for palabra in crudas:
        p = normalizar(palabra)
        if len(p) >= LARGO_MINIMO_PALABRA and p not in PALABRAS_AMBIGUAS and p not in salida:
            salida.append(p)
    return salida


def compilar_patron(palabras: Iterable[str]) -> re.Pattern | None:
    palabras = sorted(set(palabras), key=len, reverse=True)
    if not palabras:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(p) for p in palabras) + r")(?![a-z0-9])")


def senal_ti(fila: dict, patron: re.Pattern | None) -> str:
    """`unspsc:<código>`, `palabra:<palabra>` o "" si el concurso no muestra ninguna señal de TI."""
    for codigo in fila.get("unspsc") or []:
        if str(codigo)[:2] in SEGMENTOS_TI:
            return f"unspsc:{codigo}"
    if patron is not None:
        m = patron.search(normalizar(f"{fila.get('titulo', '')} {fila.get('detalle', '')}"))
        if m:
            return f"palabra:{m.group(1)}"
    return ""


# ── Lectura ───────────────────────────────────────────────────────────────────────────────────────────────

SQL_POBLACION = """
SELECT cp.proceso_gid, p.numero_referencia, p.institucion, p.titulo, p.modalidad_raw, p.fecha_pub_utc,
       p.monto_original, p.moneda, cp.es_relevante, cp.triaje, cp.estado, cp.match_tecnico,
       cp.match_categoria, cp.factibilidad, cp.categoria_detectada,
       JSON_SERIALIZE(cp.marcas RETURNING CLOB), cp.clasificado_en
  FROM cliente_proceso cp
  JOIN proceso p ON p.proceso_gid = cp.proceso_gid
 WHERE cp.cliente_id = :cliente_id
   AND p.portal_id = :portal_id
"""
_COLUMNAS_SQL = (
    "proceso_gid", "numero_referencia", "institucion", "titulo", "modalidad_raw", "fecha_pub_utc",
    "monto_original", "moneda", "es_relevante", "triaje", "estado_cp", "match_tecnico", "match_categoria",
    "factibilidad", "categoria_detectada", "marcas", "clasificado_en",
)


def leer_poblacion(conn, cliente: str, portal: str, desde: date | None = None,
                   hasta: date | None = None) -> list[dict]:
    """Todos los concursos del cliente en el portal (relevantes y no), en una transacción de solo lectura."""
    sql, binds = SQL_POBLACION, {"cliente_id": cliente, "portal_id": portal}
    if desde:
        sql += "   AND p.fecha_pub_utc >= :desde\n"
        binds["desde"] = datetime.combine(desde, datetime.min.time())
    if hasta:
        sql += "   AND p.fecha_pub_utc < :hasta\n"
        binds["hasta"] = datetime.combine(hasta + timedelta(days=1), datetime.min.time())
    cursor = conn.cursor()
    try:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(sql + " ORDER BY cp.proceso_gid", binds)
        crudas = cursor.fetchall()
    finally:
        cursor.close()
    return [normalizar_fila(dict(zip(_COLUMNAS_SQL, c))) for c in crudas]


def _float(valor: Any) -> float | None:
    try:
        return None if valor is None else float(valor)
    except (TypeError, ValueError):
        return None


def _marcas_amalia(valor: Any) -> list[dict]:
    """`cliente_proceso.marcas` → [{marca, veredicto, score}]. Sin veredicto = `por_confirmar`, como el portal."""
    datos = _json(valor)
    salida = []
    for m in datos if isinstance(datos, list) else []:
        m = m if isinstance(m, dict) else {"marca": m}
        nombre = m.get("marca")
        if isinstance(nombre, str) and nombre.strip():
            salida.append({"marca": nombre.strip(), "veredicto": str(m.get("veredicto") or "por_confirmar"),
                           "score": _float(m.get("score"))})
    return salida


def normalizar_fila(f: dict) -> dict:
    """Fila cruda (ADB o simulada) → campos tipados que usan el muestreo y los CSV."""
    for clave in ("match_tecnico", "match_categoria", "factibilidad", "monto_original"):
        f[clave] = _float(f.get(clave))
    f["match"] = match_de_valores(f["match_tecnico"], f["match_categoria"], f["factibilidad"])
    f["relevante"] = es_relevante(f)
    f["clasificado"] = f.get("es_relevante") is not None or f.get("clasificado_en") is not None or f["relevante"]
    f["marcas"] = _marcas_amalia(f.get("marcas"))
    f["moneda"] = str(f.get("moneda") or "").strip().upper()
    f.setdefault("unspsc", [])
    f.setdefault("detalle", "")
    f.setdefault("modalidad_codigo", codigo_de_modalidad(f.get("modalidad_raw")))
    return f


def codigo_de_modalidad(nombre: Any, portal: str = PORTAL_DEFAULT) -> str:
    """El código SICOP (`LD`, `PX`…) a partir del nombre publicado; "" si no se reconoce."""
    n = normalizar(nombre)
    if not n:
        return ""
    for m in MODALIDADES:
        if m.portal_id == portal and (normalizar(m.nombre) == n or m.codigo.lower() == n):
            return m.codigo
    return ""


def leer_historial(ruta: Path) -> dict[str, dict]:
    """`data/history/<cliente>.jsonl` → {numero_referencia: {unspsc, detalle, modalidad_codigo}}; la última gana."""
    indice: dict[str, dict] = {}
    with open(ruta, encoding="utf-8") as f:
        for n, linea in enumerate(f, 1):
            if not linea.strip():
                continue
            try:
                corrida = json.loads(linea)
            except ValueError as exc:
                raise ValueError(f"{ruta}:{n}: línea JSON inválida ({exc})") from exc
            for p in corrida.get("procesos") or []:
                ref = str(p.get("numero_referencia") or "").strip()
                if not ref:
                    continue
                datos = p.get("datos_portal") if isinstance(p.get("datos_portal"), dict) else {}
                indice[ref] = {
                    "unspsc": [str(c) for c in datos.get("unspsc") or []],
                    "detalle": str(p.get("detalle") or ""),
                    "modalidad_codigo": str(p.get("modalidad_codigo") or "").strip().upper(),
                }
    return indice


def enriquecer(filas: list[dict], historial: dict[str, dict]) -> int:
    """Agrega UNSPSC, detalle y código de modalidad del historial; devuelve cuántas filas se enriquecieron."""
    n = 0
    for f in filas:
        extra = historial.get(str(f.get("numero_referencia") or ""))
        if not extra:
            continue
        n += 1
        f["unspsc"] = extra["unspsc"] or f.get("unspsc") or []
        f["detalle"] = extra["detalle"] or f.get("detalle") or ""
        f["modalidad_codigo"] = extra["modalidad_codigo"] or f.get("modalidad_codigo") or ""
    return n


# ── Muestreo ──────────────────────────────────────────────────────────────────────────────────────────────

def estrato_de(f: dict, patron: re.Pattern | None) -> str:
    if f["relevante"]:
        return RELEVANTE_ALTO if f["match"] >= UMBRAL_MATCH_DECIDIR else BORDE
    if MATCH_BORDE_MIN <= f["match"] < UMBRAL_MATCH_DECIDIR:
        return BORDE
    return NO_RELEVANTE_SENAL_TI if senal_ti(f, patron) else NO_RELEVANTE_AZAR


def muestrear(filas: list[dict], objetivos: dict[str, int], semilla: int,
              patron: re.Pattern | None) -> tuple[list[dict], dict]:
    """Muestra estratificada reproducible: misma población + misma semilla ⇒ misma muestra y mismo orden.

    Un estrato con menos concursos que su objetivo se censa (todos) y el faltante NO se redistribuye: los
    pesos quedan bien definidos. El orden final se baraja para mezclar estratos ante quien etiqueta.
    """
    rng = random.Random(semilla)
    poblacion: dict[str, list[dict]] = {e: [] for e in ESTRATOS}
    sin_clasificar = 0
    for f in sorted(filas, key=lambda x: str(x["proceso_gid"])):
        if not f["clasificado"]:
            sin_clasificar += 1
            continue
        f["estrato"] = estrato_de(f, patron)
        f["senal_ti"] = senal_ti(f, patron)
        poblacion[f["estrato"]].append(f)
    muestra: list[dict] = []
    resumen = {"semilla": semilla, "sin_clasificar_excluidos": sin_clasificar, "estratos": {}}
    for estrato in ESTRATOS:
        candidatos = poblacion[estrato]
        k = min(max(0, int(objetivos.get(estrato, 0))), len(candidatos))
        elegidos = rng.sample(candidatos, k)
        for f in elegidos:
            f["poblacion_estrato"], f["muestra_estrato"] = len(candidatos), k
            f["peso"] = len(candidatos) / k
        muestra.extend(elegidos)
        resumen["estratos"][estrato] = {"poblacion": len(candidatos), "objetivo": objetivos.get(estrato, 0),
                                        "muestra": k, "faltante": max(0, objetivos.get(estrato, 0) - k)}
    rng.shuffle(muestra)
    resumen["total"] = len(muestra)
    return muestra, resumen


# ── Salida ────────────────────────────────────────────────────────────────────────────────────────────────

def _fecha(valor: Any) -> str:
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%Y-%m-%d")
    return str(valor or "")[:10]


def fila_ciega(f: dict) -> dict:
    """Solo datos públicos del concurso. Construida desde la lista blanca, nunca copiando la fila entera."""
    monto = f.get("monto_original")
    return {
        "id": f["proceso_gid"],
        "numero_procedimiento": f.get("numero_referencia") or "",
        "institucion": f.get("institucion") or "",
        "descripcion": f.get("titulo") or "",
        "detalle": f.get("detalle") or "",
        "unspsc": " ".join(f.get("unspsc") or []),
        "modalidad": f.get("modalidad_raw") or f.get("modalidad_codigo") or "",
        "monto": "" if monto is None else f"{monto:.2f}",
        "moneda": f.get("moneda") or "",
        "fecha_publicacion": _fecha(f.get("fecha_pub_utc")),
        **{c: "" for c in COLUMNAS_ETIQUETA},
    }


def fila_clave(f: dict, portal: str = PORTAL_DEFAULT) -> dict:
    return {
        "id": f["proceso_gid"],
        "estrato": f["estrato"],
        "poblacion_estrato": f["poblacion_estrato"],
        "muestra_estrato": f["muestra_estrato"],
        "peso": f"{f['peso']:.6f}",
        "es_relevante_amalia": "sí" if f["relevante"] else "no",
        "match": f["match"],
        "es_relevante_col": "" if f.get("es_relevante") is None else int(f["es_relevante"]),
        "triaje": f.get("triaje") or "",
        "estado": f.get("estado_cp") or "",
        "categoria_detectada": f.get("categoria_detectada") or "",
        "marcas_amalia": json.dumps(f["marcas"], ensure_ascii=False),
        "modalidad_codigo": f.get("modalidad_codigo") or "",
        "competibilidad_codigo": competibilidad_de(portal, f.get("modalidad_codigo") or "",
                                                   f.get("modalidad_raw") or ""),
        "unspsc": " ".join(f.get("unspsc") or []),
        "senal_ti": f.get("senal_ti") or "",
    }


def filas_modalidades(portal: str = PORTAL_DEFAULT) -> list[dict]:
    """Plantilla ciega: código y nombre; la competibilidad del código NO se muestra (la compara el evaluador)."""
    return [{"codigo": m.codigo, "nombre": m.nombre, COL_MOD_OFERTABLE: "", "condiciones": "", COL_COMENTARIO: ""}
            for m in MODALIDADES if m.portal_id == portal]


def escribir_csv(ruta: Path, columnas: Iterable[str], filas: Iterable[dict]) -> None:
    """UTF-8 con BOM: Excel en español abre bien las tildes."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(columnas), extrasaction="raise")
        w.writeheader()
        w.writerows(filas)


def exportar(filas: list[dict], salida: Path, *, objetivos: dict[str, int], semilla: int,
             patron: re.Pattern | None, portal: str = PORTAL_DEFAULT) -> dict:
    muestra, resumen = muestrear(filas, objetivos, semilla, patron)
    escribir_csv(salida / "muestra_ciega.csv", COLUMNAS_CIEGO, (fila_ciega(f) for f in muestra))
    escribir_csv(salida / "clave_veredictos.csv", COLUMNAS_CLAVE, (fila_clave(f, portal) for f in muestra))
    escribir_csv(salida / "modalidades.csv", COLUMNAS_MODALIDADES, filas_modalidades(portal))
    (salida / "muestra_resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
    return resumen


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────────────

def configuracion():
    # Config llama a load_dotenv al importarse: esta herramienta solo usa el entorno.
    with patch("dotenv.load_dotenv", return_value=False):
        from src.config import get_config
    return get_config()


def _fecha_arg(texto: str) -> date:
    return date.fromisoformat(texto)


def main(argv: list[str] | None = None, *, conectar: Callable | None = None,
         decir: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cliente", default=CLIENTE_DEFAULT)
    p.add_argument("--portal", default=PORTAL_DEFAULT)
    p.add_argument("--semilla", type=int, default=SEMILLA_DEFAULT)
    p.add_argument("--desde", type=_fecha_arg, help="fecha de publicación mínima (AAAA-MM-DD)")
    p.add_argument("--hasta", type=_fecha_arg, help="fecha de publicación máxima, inclusive (AAAA-MM-DD)")
    p.add_argument("--n-relevantes", type=int, default=OBJETIVOS_DEFAULT[RELEVANTE_ALTO])
    p.add_argument("--n-borde", type=int, default=OBJETIVOS_DEFAULT[BORDE])
    p.add_argument("--n-senal-ti", type=int, default=OBJETIVOS_DEFAULT[NO_RELEVANTE_SENAL_TI])
    p.add_argument("--n-azar", type=int, default=OBJETIVOS_DEFAULT[NO_RELEVANTE_AZAR])
    p.add_argument("--historial", type=Path, help="data/history/<cliente>.jsonl, para UNSPSC y detalle")
    p.add_argument("--clients-dir", type=Path, default=RAIZ / "clients")
    p.add_argument("--salida", type=Path, help="carpeta de salida (por defecto data/evaluacion/cr-<hoy>/)")
    args = p.parse_args(argv)
    objetivos = {RELEVANTE_ALTO: args.n_relevantes, BORDE: args.n_borde,
                 NO_RELEVANTE_SENAL_TI: args.n_senal_ti, NO_RELEVANTE_AZAR: args.n_azar}
    if any(v < 0 for v in objetivos.values()):
        p.error("los tamaños de estrato no pueden ser negativos")
    salida = args.salida or RAIZ / "data" / "evaluacion" / f"cr-{date.today().isoformat()}"

    patron = compilar_patron(palabras_clave(cargar_perfil(args.cliente, args.clients_dir)))
    if conectar is None:
        from src.memoria import db
        cfg = configuracion()

        def conectar():
            return db.connection(cfg)
    with conectar() as conn:
        filas = leer_poblacion(conn, args.cliente, args.portal, args.desde, args.hasta)
    if args.historial:
        n = enriquecer(filas, leer_historial(args.historial))
        decir(f"Historial: {n}/{len(filas)} concursos con UNSPSC/detalle")
    else:
        decir("Sin --historial: la señal de TI usa solo palabras clave (la ADB no guarda el UNSPSC)")
    resumen = exportar(filas, salida, objetivos=objetivos, semilla=args.semilla, patron=patron,
                       portal=args.portal)
    decir(f"Población {len(filas)} · muestra {resumen['total']} · semilla {args.semilla}")
    for estrato, r in resumen["estratos"].items():
        aviso = f" (faltan {r['faltante']}: se censó el estrato)" if r["faltante"] else ""
        decir(f"  {estrato}: {r['muestra']}/{r['poblacion']}{aviso}")
    if resumen["total"] < 150:
        decir("AVISO: la muestra tiene menos de 150 concursos; esperar más días de datos o ampliar --desde")
    decir(f"Entregar a quien etiqueta: {salida / 'muestra_ciega.csv'} y {salida / 'modalidades.csv'}")
    decir(f"NO entregar (queda con quien evalúa): {salida / 'clave_veredictos.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
