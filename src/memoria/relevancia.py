"""Definiciones SQL de «relevante» y «apto», con un único hogar (E11.1).

Las comparten la analítica del portal (`src/analytics/`) y los lectores de memoria (`src/memoria/`); `memoria`
no puede importar de `analytics` sin riesgo de ciclo, así que la constante vive aquí. Todas asumen el alias
`cp` para `cliente_proceso`.
"""
from __future__ import annotations

from src.config import get_config
from src.notifier.agrupamiento import UMBRAL_MATCH_DECIDIR
from src.notifier.veredicto_marca import EQUIVALENCIAS

# «Relevante» combina tres señales con OR, no solo `es_relevante`/`triaje`: verificado en vivo contra
# multicomputos_dev (2026-10-01) que las 37 filas migradas tenían `es_relevante`/`triaje` NULL (y lo mismo en la v1
# de origen), así que ese campo rara vez se puebla en producción real. `estado IN ('ANALIZADO','NOTIFICADO')` es la
# señal que sí está siempre presente (llegar a ANALIZADO implica haber pasado el filtro de relevancia del pipeline).
# Las tres condiciones conviven a propósito: el día que `es_relevante`/`triaje` se pueblen de verdad, siguen sumando
# (nunca restan). Desde E11.1 el pipeline también guarda los NO relevantes (`es_relevante = 0`, estado `DESCUBIERTO`):
# esos quedan fuera de esta condición.
SQL_ES_RELEVANTE = """(cp.es_relevante = 1 OR cp.triaje = 'GO'
          OR cp.estado IN ('ANALIZADO', 'NOTIFICADO'))"""

# Match 0-100, igual que `agrupamiento.match_de_valores` (0,4 técnico · 0,3 categoría · 0,3 factibilidad).
# Un test comprueba que ambas fórmulas coinciden.
SQL_MATCH = ("(0.4 * NVL(cp.match_tecnico, 0) + 0.3 * NVL(cp.match_categoria, 0) "
             "+ 0.3 * NVL(cp.factibilidad, 0)) * 100")

# «Apto para decidir»: relevante, con match suficiente y sin NO-GO de triaje. No mira el pliego (el correo además
# descarta los «no competibles», que se derivan de la matriz de riesgos): por eso el KPI se rotula «aptos».
SQL_ES_APTO = (f"({SQL_ES_RELEVANTE} AND NVL(cp.triaje, 'GO') <> 'NO-GO' "
               f"AND ROUND({SQL_MATCH}) >= {UMBRAL_MATCH_DECIDIR})")

# Día de la corrida a la que pertenece un proceso: la de las 07:00 de El Salvador (13:00 UTC) mira las 24 h previas,
# así que `fecha_pub_utc + 11 h` truncado al día da la fecha de esa corrida.
SQL_DIA_DE_CORRIDA = "TRUNC(p.fecha_pub_utc + INTERVAL '11' HOUR)"


# E14 · restricción por fabricante. Las vistas de los roles Oracle solo ven oportunidades que lo involucran.
# Cada clave es una marca restringible del `Alcance.solo_marcas`; el valor es el prefijo del nombre de marca en
# `cliente_proceso.marcas` (cubre «Oracle» y «Oracle NetSuite») y la palabra que se busca en el título como
# respaldo (el pitch: Oracle bajo un nombre genérico). Lista CERRADA: la cláusula se arma con literales de aquí,
# jamás con texto que llegue de fuera.
MARCAS_RESTRINGIBLES = {"oracle": "oracle"}


def sql_marca(solo_marcas, alias: str = "cp", *, modo: str | None = None) -> str:
    """Condición SQL «la oportunidad involucra alguna de `solo_marcas`»; `1 = 1` si no hay restricción.

    Va con AND, FUERA de los paréntesis de cualquier OR (como las demás definiciones de este módulo). Solo
    necesita el alias de `cliente_proceso`: el título se consulta con un EXISTS sobre `proceso`, así que sirve
    también donde la consulta no une `proceso`. No se mezcla con `SQL_ES_RELEVANTE`: `_SQL_DESDE_DESCARTADOS`
    lo niega, y los no-Oracle pasarían a contar como descartados.
    """
    if not solo_marcas:
        return "1 = 1"
    activo = (modo if modo is not None else get_config().marca_veredicto) == "activo"
    ramas = []
    for marca in sorted(solo_marcas):
        if marca not in MARCAS_RESTRINGIBLES:
            raise ValueError(f"marca no restringible: {marca!r}")
        pref = MARCAS_RESTRINGIBLES[marca]
        if activo:
            # Solo aliases del diccionario cerrado; ni el título ni el score deciden la pertenencia.
            import re
            prefijos = "|".join(re.escape(a) for a in EQUIVALENCIAS.get(marca, (pref,)))
            ramas.append(
                f"EXISTS (SELECT 1 FROM JSON_TABLE(JSON_SERIALIZE({alias}.marcas RETURNING CLOB), "
                "'$[*]' COLUMNS (m VARCHAR2(200) PATH '$.marca', "
                "v VARCHAR2(40) PATH '$.veredicto')) jm "
                f"WHERE REGEXP_LIKE(jm.m, '^({prefijos})([^[:alnum:]_]|$)', 'i') "
                "AND (jm.v IN ('confirmada', 'por_confirmar', 'no_competible') OR jm.v IS NULL))")
            continue
        ramas.append(
            f"EXISTS (SELECT 1 FROM JSON_TABLE({alias}.marcas, '$[*]' COLUMNS (m VARCHAR2(200) PATH '$.marca')) jm "
            f"WHERE REGEXP_LIKE(jm.m, '^{pref}([^[:alnum:]_]|$)', 'i')) "
            f"OR EXISTS (SELECT 1 FROM proceso pm WHERE pm.proceso_gid = {alias}.proceso_gid "
            f"AND REGEXP_LIKE(pm.titulo, '(^|[^[:alnum:]_]){pref}([^[:alnum:]_]|$)', 'i'))")
    return "(" + " OR ".join(ramas) + ")"


def es_de_marca(nombre: str, solo_marcas, *, modo: str | None = None) -> bool:
    """Lo mismo que la rama de marcas de `sql_marca`, en Python: «Oracle» y «Oracle NetSuite» sí, «Oraclexyz» no."""
    import re
    activo = (modo if modo is not None else get_config().marca_veredicto) == "activo"
    return any(re.match(rf"{re.escape(pref)}(?![A-Za-z0-9_])", nombre, re.I)
               for m in solo_marcas
               for pref in (EQUIVALENCIAS.get(m, (MARCAS_RESTRINGIBLES[m],)) if activo
                            else (MARCAS_RESTRINGIBLES[m],)))
