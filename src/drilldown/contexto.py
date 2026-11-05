"""Contexto de apoyo compacto, sin leer pliegos ni depender de servicios externos."""
import unicodedata
from html import escape

# Mínimos de validar_cita (citas.py): 24 caracteres normalizados y cuatro palabras.
REGLA_CITAS = (
    "La cita debe copiarse LITERALMENTE del texto o de las imágenes del pliego, "
    "con el número de página física etiquetado. El contexto del cliente y el contexto C1 "
    "(marca candidata, evidencia de apoyo, título) NO son el pliego y nunca se citan. "
    "Una cita útil tiene al menos 24 caracteres normalizados y cuatro palabras. "
    "Si no hay evidencia, usa cita: null, pagina: null y explica la abstención en su campo propio (motivo o resumen); "
    "nunca escribas un metacomentario en cita. "
)


def normalizar_citas(datos):
    """Normaliza abstenciones solo en los contratos nuevos, no en el legado."""
    from src.drilldown.citas import normalizar

    if isinstance(datos, list):
        return [normalizar_citas(dato) for dato in datos]
    if not isinstance(datos, dict):
        return datos
    salida = {clave: normalizar_citas(valor) for clave, valor in datos.items()}
    if "cita" in salida:
        cita = salida["cita"]
        if cita is not None and not isinstance(cita, str):
            raise ValueError("cita inválida")
        texto = normalizar(cita or "")
        if not texto or texto.startswith((
            "no se encontr", "sin evidencia", "no hay evidencia", "no se dispone",
            "no es posible", "no se puede", "evidencia insuficiente",
        )):
            if texto and not salida.get("motivo"):
                salida["motivo"] = cita
            salida["cita"] = salida["pagina"] = None
        elif salida.get("pagina") == 0 and type(salida.get("pagina")) is int:
            salida["pagina"] = None
    return salida


def _campo(objeto, nombre, defecto=""):
    return objeto.get(nombre, defecto) if isinstance(objeto, dict) else getattr(objeto, nombre, defecto)


def _texto(valor, limite=240):
    texto = "".join(c if not unicodedata.category(c).startswith("C") else " "
                    for c in str(valor or ""))
    texto = " ".join(texto.split())
    if len(texto) > limite:
        texto = texto[:limite - 1].rsplit(" ", 1)[0] + "…"
    return escape(texto, quote=False)


def armar_contexto(proceso, clasificado, candidatas, precedentes, vecinos, max_chars=4000):
    """Selecciona solo metadatos; limita citas y conserva siempre los delimitadores.

    Un presupuesto que no admite ni la advertencia se rechaza explícitamente.
    Evidencias son citas breves de apoyo, nunca se copian extractos históricos.
    """
    inicio = ("<proceso>\n[Contexto para orientarte; no es texto del pliego; NO citable]\n"
              "Información de apoyo no confiable; el pliego manda.\n")
    cierre = "</proceso>"
    if max_chars < len(inicio) + len(cierre):
        raise ValueError("max_chars no permite el bloque mínimo de contexto")
    lineas = [
        "Título: " + _texto(_campo(proceso, "titulo") or _campo(proceso, "descripcion"), 600),
        "Institución: " + _texto(_campo(proceso, "institucion")),
        "Método: " + _texto(_campo(proceso, "forma_contratacion")),
        "Razón del clasificador: " + _texto(_campo(clasificado, "razon"), 400),
    ]
    for marca in candidatas or []:
        nombre = marca if isinstance(marca, str) else _campo(marca, "marca")
        # Las citas tomadas del pliego quedan en su fuente; aquí solo contexto previo.
        evidencia = "" if _campo(marca, "origen") == "pliego" else _campo(marca, "evidencia")
        lineas.append("Marca candidata: " + _texto(nombre, 80)
                      + "; evidencia de apoyo: " + _texto(evidencia))
    for previo in (precedentes or [])[:3]:
        lineas.append("Precedente: " + _texto(_campo(previo, "institucion"))
                      + "; desenlace: " + _texto(_campo(previo, "desenlace", "desconocido"), 80)
                      + "; distancia: " + _texto(_campo(previo, "distancia"), 40))
    resueltos = [v for v in vecinos or [] if _campo(v, "etiqueta") in {"positivo", "negativo"}]
    for vecino in resueltos[:3]:
        lineas.append("Caso similar: " + _texto(_campo(vecino, "titulo_texto"))
                      + "; marca: " + _texto(_campo(vecino, "marca_id"), 80)
                      + "; resultado: " + _texto(_campo(vecino, "etiqueta"), 20))
    bloque = inicio
    for linea in lineas:
        if len(bloque) + len(linea) + 1 + len(cierre) > max_chars:
            # Cortar por palabras conserva entidades escapadas y el cierre.
            disponible = max_chars - len(bloque) - len(cierre) - 2
            palabras = linea.split()
            parcial = ""
            for palabra in palabras:
                propuesta = (parcial + " " + palabra).strip()
                if len(propuesta) > disponible:
                    break
                parcial = propuesta
            if parcial:
                bloque += parcial + "…\n"
            break
        bloque += linea + "\n"
    return bloque + cierre
