"""src.oportunidades — pipeline comercial de una oportunidad (plan de identidad, Entrega 5).

La persona mueve una oportunidad por NUEVO → EVALUANDO → OFERTADO → GANADO | PERDIDO (o la
DESCARTA). Cada cambio deja el estado en `oportunidad_estado` (origen MANUAL), un evento en
`oportunidad_evento` y una fila de auditoría, todo en la misma transacción.
"""
