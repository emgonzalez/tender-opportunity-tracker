"""Memoria de aprendizaje por marca en la ADB: ejemplos etiquetados, diccionario de términos,
calibraciones versionadas y decisiones por capa (`db/schema_v2.sql` §7).

`repo` es SQL puro sobre una conexión inyectada; `service` es la fachada tolerante para la corrida
diaria; `semilla` trae los términos públicos iniciales; `python -m src.aprendizaje` es la CLI.
"""
