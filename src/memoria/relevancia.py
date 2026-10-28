"""Definiciones SQL de «relevante» y «apto», con un único hogar (E11.1).

Las comparten la analítica del portal (`src/analytics/`) y los lectores de memoria (`src/memoria/`); `memoria`
no puede importar de `analytics` sin riesgo de ciclo, así que la constante vive aquí. Todas asumen el alias
`cp` para `cliente_proceso`.
"""
from __future__ import annotations

from src.notifier.agrupamiento import UMBRAL_MATCH_DECIDIR

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
