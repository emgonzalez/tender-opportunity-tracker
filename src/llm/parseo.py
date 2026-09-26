"""src.llm.parseo — leer el JSON que un modelo dijo que iba a emitir.

Los tres sitios que piden JSON a un LLM —`drilldown/triage.py`,
`drilldown/analysis.py` y `classifier/engine.py`— hacían `json.loads` directo.
Funciona casi siempre, y cuando no, el pliego entero se pierde: el análisis cae
a `ERROR_LLM` después de haber pagado la llamada.

── El escape que rompe, y por qué es inevitable ──────────────────────────────

El análisis le pide al modelo una **cita textual** del pliego en `clausula_ref`.
Los pliegos de COMPRASAL son PDFs escaneados y su capa de texto trae ruido, entre
otras cosas barras invertidas sueltas. Cuando el modelo copia la cita tal cual y
la barra queda delante de una palabra que empieza con `u` —`\\unidad`,
`\\ubicacion`, `\\ultimos`—, el resultado es un `\\u` que no abre un escape
Unicode válido, y Python corta con:

    json.decoder.JSONDecodeError: Invalid \\uXXXX escape

Ese mensaje es específico: Python lo reserva para `\\u` seguido de algo que no
son cuatro dígitos hexadecimales. Cualquier otro escape inválido da
`Invalid \\escape`. Observado en producción el 2026-09-23 y el 2026-09-24, un
pliego cada día.

No se arregla pidiéndoselo mejor al modelo: la cita **tiene** que ser textual
para poder verificarla contra el PDF, y el texto de origen contiene la barra.

── Qué hace la reparación ───────────────────────────────────────────────────

Solo se intenta si el parseo estricto ya falló, así que un JSON bien formado
nunca se toca. Escapa la barra invertida cuando **no** abre un escape válido de
JSON, que es justo lo que el modelo quiso decir: una barra literal.

Lo que deliberadamente NO hace: inventar comillas, cerrar llaves ni completar
una respuesta truncada. Eso es adivinar qué dijo el modelo, y una matriz de
riesgos a medio adivinar es peor que un `ERROR_LLM` — el operador ve el error,
no ve la invención.

⚠️ Hay una corrupción hermana que esto **no puede** tocar: `C:\\temp` es JSON
*válido* —`\\t` es un tabulador— así que se parsea sin error y con el texto
cambiado en silencio. No hay forma de distinguirlo de un tabulador querido.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Toda secuencia que empieza con barra invertida, válida o no. La alternancia
# está ordenada de más larga a más corta y **consume el escape entero**, que es
# el punto: un `\\` válido ocupa DOS caracteres, y si el escáner avanzara de a
# uno volvería a mirar la segunda barra y la "repararía" — convirtiendo
# `"a\\ b"`, que es JSON válido, en `"a\\\ b"`, que no lo es.
#
# Ese error importa más de lo que parece: la reparación solo corre cuando el
# parseo estricto YA falló, así que se aplica sobre textos que suelen tener más
# de una rareza. Romper las partes sanas al arreglar la rota deja el pliego
# perdido igual, que es justo lo que este módulo existe para evitar.
_SECUENCIA_ESCAPE = re.compile(
    r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt]|u|.|\Z)', re.DOTALL
)

# Los escapes de un solo carácter que JSON acepta (además de `\uXXXX`).
_SIMPLES_VALIDOS = frozenset('"\\/bfnrt')

# Cuántas clases distintas de escape roto se nombran en el WARNING.
_MAX_MUESTRAS = 5


def reparar_escapes(texto: str) -> tuple[str, Counter]:
    """Escapa las barras invertidas que no abren un escape válido.

    Devuelve `(texto_reparado, cuenta_por_secuencia)`. La cuenta se usa solo
    para el log: dice QUÉ clase de escape vino roto (`\\un`, `\\ub`) sin volcar
    el texto del pliego, que no tiene por qué terminar en un archivo de log.
    """
    vistos: Counter = Counter()

    def _revisar(m: re.Match) -> str:
        seq = m.group(1)
        # `\uXXXX` bien formado, o un escape simple válido: se deja intacto.
        if (len(seq) == 5 and seq[0] == "u") or seq in _SIMPLES_VALIDOS:
            return m.group(0)

        # Inválido. Para el log se espía el carácter siguiente sin consumirlo:
        # `\u` a secas es poco diagnóstico, `\un` dice de dónde viene.
        siguiente = m.string[m.end() : m.end() + 1] if seq == "u" else ""
        vistos[m.group(0) + siguiente] += 1
        return "\\" + m.group(0)

    return _SECUENCIA_ESCAPE.sub(_revisar, texto), vistos


def cargar(texto: str) -> Any:
    """`json.loads` que sobrevive a los escapes que los modelos emiten mal.

    Intenta el parseo estricto primero: si el modelo emitió JSON bien formado
    —el caso normal— no se toca nada. Solo ante un `JSONDecodeError` se repara
    y se reintenta **una** vez.

    Si el reintento también falla, se relanza el error **original**, no el del
    texto reparado: el primero describe lo que el modelo realmente dijo, que es
    lo que quien lee el log necesita.
    """
    try:
        return json.loads(texto)
    except json.JSONDecodeError as original:
        reparado, vistos = reparar_escapes(texto)
        if not vistos:
            raise

        try:
            datos = json.loads(reparado)
        except json.JSONDecodeError:
            logger.debug(
                "parseo: la reparación de escapes no alcanzó (%s escape(s) inválido(s))",
                sum(vistos.values()),
            )
            raise original from None

        muestras = ", ".join(
            f"{seq!r}×{n}" for seq, n in vistos.most_common(_MAX_MUESTRAS)
        )
        logger.warning(
            "parseo: el modelo emitió %s escape(s) inválido(s) y el JSON se "
            "recuperó reparándolos (%s). Sin esto, la respuesta se perdía entera.",
            sum(vistos.values()), muestras,
        )
        return datos


def guardar_respuesta_fallida(
    debug_dir: str | None,
    etapa: str,
    referencia: str,
    texto: str,
    error: BaseException,
) -> str | None:
    """Deja en `debug_dir` la respuesta cruda que no se pudo leer. Nunca lanza.

    Existe porque el libro de consumo solo guarda el LARGO de la respuesta: si el
    modelo emite JSON roto de una forma que `cargar` no repara, el `ERROR_LLM`
    dice `Expecting ',' delimiter: line 2 column 470` y no hay manera de ver qué
    había en esa columna. Observado el 2026-09-26.

    Va a un archivo y no al log a propósito: el texto trae citas del pliego, y
    el journal no es el lugar para volcarlas. `data/debug/` ya está en
    `.gitignore`. Devuelve la ruta escrita, o `None` si no se pudo (sin
    `debug_dir`, disco lleno, permisos): depurar no puede tumbar la corrida.
    """
    if not debug_dir or not texto:
        return None
    try:
        os.makedirs(debug_dir, exist_ok=True)
        seguro = re.sub(r"[^A-Za-z0-9._-]", "_", referencia)[:80] or "sin_ref"
        marca = datetime.now().strftime("%Y%m%d-%H%M%S")
        ruta = os.path.join(debug_dir, f"respuesta_fallida_{etapa}_{seguro}_{marca}.txt")
        posicion = getattr(error, "pos", None)
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(f"# etapa: {etapa}\n# referencia: {referencia}\n")
            f.write(f"# error: {error}\n")
            if posicion is not None:
                f.write(f"# posicion: {posicion} de {len(texto)} caracteres\n")
            f.write("# --- respuesta cruda del modelo ---\n")
            f.write(texto)
        return ruta
    except OSError as e:
        logger.warning("parseo: no se pudo guardar la respuesta fallida: %s", e)
        return None


__all__ = ["cargar", "guardar_respuesta_fallida", "reparar_escapes"]
