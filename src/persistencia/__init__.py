"""Piezas comunes para mover el estado de la VM a la ADB sin romper la corrida si la ADB falla.

Ver docs/planes/persistencia-adb.md. Tres ideas, todas sin dependencias nuevas:
  · `fuente`: qué origen manda para un dato (`archivo` = comportamiento actual, `adb`, o `sombra`).
  · `cache` y `spool`: copia local mínima y cola de salida para lo que la ADB no alcanzó a recibir.
  · `cifrado`: blobs cifrados en reposo (la sesión de COMPRASAL) con la clave de `IDENTIDAD_SECRET`.
"""
