"""src.analytics — capa de datos del portal AMALIA. Ver docs/planes/portal-amalia.md.

Separado de ``src.web`` por el mismo motivo que ``src.feedback``: el portal
reusa esta lógica sin arrastrar HTTP, y queda testeable sin un cliente FastAPI.

Cada función recibe una conexión ``conn`` ya abierta (oracledb) como primer
parámetro — igual que ``src.memoria.repo`` — y devuelve datos ya en la forma
que la vista necesita (``{"valor", "delta_pct", "serie"}`` para un stat tile;
``list[dict]`` ordenado para un chart), nunca filas crudas: la plantilla
Jinja2 no debería tener que saber de SQL.
"""
