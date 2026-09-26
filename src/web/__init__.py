"""src.web — servicio web residente de AMALIA.

Hoy sirve un solo router público (`feedback`: los botones "Relevante"/"Irrelevante" del
correo). Está armado como fábrica de app con routers porque es la semilla del portal
administrativo: cuando llegue `/admin` (dashboards, roles) será un router más, no un
servicio nuevo. La capa de datos NO vive acá sino en `src.feedback`, para que el portal
la reuse sin arrastrar HTTP.
"""
