"""src.mercado — datos de mercado (adjudicaciones) y resultado propio.

Ver el plan del portal, §3.5: genera datos de resultado de adjudicación
—tanto de terceros (mercado completo) como propios (¿nos lo dieron o no?)—
para entrenar a AMALIA (perfil de inteligencia del clasificador, precedentes
con desenlace en RAG, y el futuro dataset de "probabilidad de éxito").

A diferencia de `src/portal/` (esquema v2, todavía sin arrancar), este
paquete es **aditivo sobre el esquema actual** (`db/schema.sql`): no depende
de `proceso_gid`/`cliente_proceso` ni de la migración grande.

Módulos:
    api_publica    — cliente HTTP del registro público de adjudicaciones
                     (sin sesión; `requests`, no Playwright)
    identidad      — quién es MULTICOMPUTOS en COMPRASAL (id_proveedor)
    repo           — acceso a datos: `adjudicacion_item`, `proceso_participado`
    participacion  — qué procesos participó el cliente (API autenticada,
                     reutiliza `classifier.trainer.extract_all_participated`)
    service        — orquesta la sincronización y la resolución de resultado
"""
