"""src.oportunidades — pipeline comercial de una oportunidad (plan de identidad, Entrega 5).

La persona mueve una oportunidad por NUEVO → EVALUANDO → OFERTADO → GANADO | PERDIDO (o la
DESCARTA). Cada cambio deja el estado en `oportunidad_estado` (origen MANUAL), un evento en
`oportunidad_evento` y una fila de auditoría, todo en la misma transacción.

E11 suma dos escrituras con el mismo patrón (`service.asignar_responsable` y
`service.recuperar_descartado`): un proceso que AMALIA descartó entra al pipeline sin tocar su
clasificación, y por eso la lista de oportunidades es «relevante O con fila en `oportunidad_estado`».
"""
