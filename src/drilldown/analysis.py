"""src.drilldown.analysis — Fase 2 (MVP Track A). Ver docs/planes/fase2-track-a.md.

Analisis profundo de pliegos: genera una matriz de riesgos financieros,
tecnicos, legales y de plazos a partir del PDF del pliego, con severidad y cita
textual de la clausula relevante, mas un resumen ejecutivo.

Desde la Fase 9 el modelo lo elige `src.llm` segun `LLM_PROVIDER`; este modulo
no nombra proveedor. Lo que manda es un `BloqueDocumento`, que transporta la
**ruta** del PDF y no su base64: antes se hacia `read_pdf_b64(pdf_path)` apenas
entrando a la funcion, y un pliego de 32 MB quedaba en RAM como base64 (~43 MB)
dentro de una VM con `mem_limit: 800m`, aunque la llamada fallara antes de
usarlo. Quien lee el archivo —si es que hace falta leerlo— es el adaptador.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone

from src import llm
from src.llm import parseo
from src.llm.contrato import BloqueDocumento, BloqueTexto, Pedido
from src.scraper.models import (
    MOTIVO_DIRECTA_NOMINADA,
    MOTIVO_MARCA_ESPECIFICA,
    MOTIVO_PROVEEDOR_UNICO,
    MatrizRiesgos,
    ResultadoAnalisis,
    RiesgoItem,
    UsoModelo,
)

logger = logging.getLogger(__name__)

# Salida DESEADA. Es el techo de Anthropic para esta etapa; con OCI el adaptador
# lo recorta a 4096 (`modelos.max_tokens_de`), que es lo maximo que acepta su
# API. Por eso se pide lo ideal y no el minimo comun: recortar es trabajo del
# adaptador, que sabe contra que techo esta hablando.
MAX_TOKENS_ANALISIS = 16000

CATEGORIAS_VALIDAS = {"financiero", "tecnico", "legal", "plazos"}
SEVERIDADES_VALIDAS = {"alta", "media", "baja"}

SYSTEM_PROMPT = """Eres un analista experto en licitaciones publicas (COMPRASAL, El Salvador).

Se te adjunta el pliego de bases de un proceso de contratacion en formato PDF. \
Tu tarea es leerlo con cuidado y extraer una MATRIZ DE RIESGOS financieros, \
tecnicos, legales y de plazos para una empresa integradora de tecnologia que \
evalua si participar.

Para cada riesgo detectado:
- Clasifica la categoria como una de: "financiero", "tecnico", "legal", "plazos".
- Describe el riesgo de forma clara y accionable para quien decide si ofertar.
- Asigna una severidad: "alta", "media" o "baja".
- Incluye una CITA TEXTUAL (copiada literalmente del pliego) de la clausula \
que origina el riesgo, en "clausula_ref".

Ademas, redacta un resumen ejecutivo breve (2-4 oraciones) de los hallazgos \
mas relevantes para la decision de participar o no.

Ademas, busca si el pliego declara un MONTO ESTIMADO o PRESUPUESTO para esta \
contratacion (frases como "monto estimado", "presupuesto asignado", "valor \
estimado del contrato", "monto maximo de la oferta", "disponibilidad \
presupuestaria"). Reportalo SOLO si el pliego lo declara explicitamente como \
un numero: nunca lo calcules, estimes ni infieras a partir de otros datos \
(cantidades, precios unitarios, etc.). Si no aparece declarado, \
"monto_estimado" debe ser null y "monto_estimado_clausula" una cadena vacia.

Responde UNICAMENTE con JSON valido, sin texto adicional antes ni despues, \
con este formato EXACTO:
{"resumen": "...", "items": [{"categoria": "financiero|tecnico|legal|plazos", \
"descripcion": "...", "severidad": "alta|media|baja", "clausula_ref": "cita textual"}], \
"monto_estimado": 12345.67, "monto_estimado_clausula": "cita textual o cadena vacia"}"""


def _extraer_json(texto: str) -> dict:
    """Parsea el primer objeto JSON de `texto`, con regex de respaldo.

    Usa `llm.parseo.cargar` y no `json.loads`: el modelo copia citas textuales
    del pliego en `clausula_ref`, y una barra invertida suelta del PDF delante de
    una palabra con `u` —`\\unidad`— rompe el parseo y tira el pliego entero a
    ERROR_LLM despues de haber pagado la llamada. Ver `src/llm/parseo.py`.
    """
    try:
        return parseo.cargar(texto)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if match:
        return parseo.cargar(match.group(0))

    raise ValueError("No se encontro un objeto JSON valido en la respuesta del modelo.")


def _construir_matriz(data: dict) -> MatrizRiesgos:
    """Construye MatrizRiesgos tolerando items con claves faltantes o invalidas."""
    items = []
    for it in data.get("items", []) or []:
        if not isinstance(it, dict):
            continue

        categoria = it.get("categoria", "tecnico")
        if categoria not in CATEGORIAS_VALIDAS:
            categoria = "tecnico"

        severidad = it.get("severidad", "media")
        if severidad not in SEVERIDADES_VALIDAS:
            severidad = "media"

        items.append(RiesgoItem(
            categoria=categoria,
            descripcion=it.get("descripcion", ""),
            severidad=severidad,
            clausula_ref=it.get("clausula_ref", ""),
        ))

    return MatrizRiesgos(items=items, resumen=data.get("resumen", ""))


def _extraer_monto(data: dict) -> tuple[float | None, str]:
    """Extrae monto_estimado/monto_estimado_clausula del JSON del modelo.

    Tolera formatos sucios (moneda, separadores de miles: el modelo a veces
    devuelve "$12,345.67" en vez de un numero). Cualquier valor no parseable
    o ausente se trata como "el pliego no lo declaro" (None), nunca como un
    error que tire el analisis entero: la matriz de riesgos ya se extrajo
    bien y un campo opcional roto no debe perderla.
    """
    crudo = data.get("monto_estimado")
    clausula = str(data.get("monto_estimado_clausula") or "").strip()

    if crudo is None or crudo == "":
        return None, ""
    if isinstance(crudo, bool):  # bool es subclase de int; nunca es un monto
        return None, ""
    if isinstance(crudo, (int, float)):
        return float(crudo), clausula

    limpio = re.sub(r"[^0-9.]", "", str(crudo))
    if not limpio:
        return None, ""
    try:
        return float(limpio), clausula
    except ValueError:
        return None, ""


# ── Competibilidad (P4) ──────────────────────────────────────────────────────
# Heuristica DETERMINISTA sobre lo que el analisis ya extrajo (resumen + items
# + citas). No se le pregunta al LLM: el analisis ya cita la clausula del
# metodo de contratacion cuando es raro, y una regex sobre esa cita se puede
# verificar contra los datos reales; una opinion mas del modelo, no.
#
# El texto llega sucio (ver P3): `espec\u00ifico` literal, mojibake como
# `PºbLICAS`. Por eso se normaliza a ASCII sin tildes y se borran los `\uXXXX`
# literales antes de buscar, y los patrones toleran 0-8 caracteres basura donde
# va la vocal acentuada (`\S{0,8}`).

_RE_DIRECTA = re.compile(r"contrataci\S{0,8}n\s+directa")
_RE_PROVEEDOR_UNICO = re.compile(r"(?:proveedor|oferente|fuente)\s+(?:\S{0,8}nic[oa]|exclusiv[oa])")
_RE_MARCA_ESPECIFICA = re.compile(r"marca\s+espec\S{0,8}fica")
_RE_NOMINADA = re.compile(
    r"con\s+el\s+consultor\b|titular\s+de\s+(?:los\s+)?derechos|derechos\s+patrimoniales"
)
_RE_ESCAPE_LITERAL = re.compile(r"\\u[0-9a-fA-F]{0,4}")


def _normalizar(texto: str) -> str:
    texto = _RE_ESCAPE_LITERAL.sub("", texto or "")
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower())


def _fragmentos(matriz: MatrizRiesgos | None) -> list[str]:
    """Textos donde buscar, con su forma original para citarla como evidencia."""
    if matriz is None:
        return []
    out = [matriz.resumen or ""]
    for it in matriz.items:
        out.append(it.clausula_ref or "")
        out.append(it.descripcion or "")
    return [t for t in out if t.strip()]


def evaluar_competibilidad(
    matriz: MatrizRiesgos | None,
    forma_contratacion: str = "",
    marcas_cliente: tuple[str, ...] | list[str] | None = None,
) -> tuple[bool | None, str, str]:
    """Devuelve `(competible, motivo_bloqueo, evidencia)` para un pliego analizado.

    Reglas, en orden de precedencia (la primera que dispara gana):

    0. Sin matriz → `(None, "", "")`: no hay de que derivar nada.
    1. Si `forma_contratacion` (el dato de COMPRASAL) viene y NO es directa,
       el proceso es competitivo por definicion → `True`. Un pliego de subasta
       inversa que mencione "proveedor unico" en la ley citada no es un bloqueo.
    2. Todo bloqueo exige ver "contratacion directa" en el propio texto del
       analisis (o en `forma_contratacion`).
    3. `proveedor_unico`: "proveedor/oferente/fuente unico|exclusivo".
    4. `contratacion_directa_nominada`: el contratista ya esta nombrado
       ("con el consultor", "titular de los derechos [patrimoniales]").
    5. `marca_especifica`: "marca especifica". Depende del portafolio:
       - `marcas_cliente is None` (no se sabe el portafolio) → `None`, con el
         motivo puesto: es una señal a verificar, no un bloqueo probado.
       - alguna marca del portafolio aparece en el texto → `True`: el cliente
         puede ofertar esa marca.
       - ninguna aparece → `False`.
    6. Nada de lo anterior → `True`.
    """
    if matriz is None:
        return None, "", ""
    fragmentos = _fragmentos(matriz)

    forma = _normalizar(forma_contratacion)
    if forma and "directa" not in forma:
        return True, "", ""

    normalizados = [(_normalizar(t), t) for t in fragmentos]
    todo = " ".join(n for n, _ in normalizados)
    if not (_RE_DIRECTA.search(todo) or "directa" in forma):
        return True, "", ""

    def _cita(patron: re.Pattern) -> str:
        for norm, original in normalizados:
            if patron.search(norm):
                return original.strip()[:300]
        return ""

    if _RE_PROVEEDOR_UNICO.search(todo):
        return False, MOTIVO_PROVEEDOR_UNICO, _cita(_RE_PROVEEDOR_UNICO)
    if _RE_NOMINADA.search(todo):
        return False, MOTIVO_DIRECTA_NOMINADA, _cita(_RE_NOMINADA)
    if _RE_MARCA_ESPECIFICA.search(todo):
        cita = _cita(_RE_MARCA_ESPECIFICA)
        if marcas_cliente is None:
            return None, MOTIVO_MARCA_ESPECIFICA, cita
        for marca in marcas_cliente:
            m = _normalizar(marca).strip()
            if m and re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", todo):
                return True, "", ""
        return False, MOTIVO_MARCA_ESPECIFICA, cita
    return True, "", ""


def _limpiar_texto_modelo(o):
    """Aplica `parseo.normalizar_texto_modelo` a todo texto del JSON del modelo.

    `parseo.cargar` ya decodifica los escapes truncados; esto cubre lo que el
    parseo no puede ver: la ß por «í», el mojibake de los pliegos escaneados y
    el OCR de las citas. Es idempotente y no toca el texto correcto.
    """
    if isinstance(o, str):
        return parseo.normalizar_texto_modelo(o)
    if isinstance(o, dict):
        return {k: _limpiar_texto_modelo(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_limpiar_texto_modelo(v) for v in o]
    return o


def aplicar_competibilidad(
    res: ResultadoAnalisis,
    forma_contratacion: str = "",
    marcas_cliente: tuple[str, ...] | list[str] | None = None,
) -> ResultadoAnalisis:
    """Escribe la señal en `res` (campos + `anexos["competibilidad"]`). Idempotente.

    Solo actua sobre `ANALIZADO`: cualquier otro estado no tiene matriz de la
    que derivar, y su `competible` queda en None (desconocido). La evidencia
    va a `anexos` y no a un campo propio porque `anexos` ya viaja como JSON
    libre a `licitaciones_analisis.anexos`: la ADB la recibe sin DDL nuevo.
    """
    if res.estado != "ANALIZADO":
        return res
    competible, motivo, evidencia = evaluar_competibilidad(
        res.matriz, forma_contratacion=forma_contratacion, marcas_cliente=marcas_cliente)
    res.competible = competible
    res.motivo_bloqueo = motivo
    anexos = dict(res.anexos or {})
    anexos["competibilidad"] = {
        "competible": competible,
        "motivo_bloqueo": motivo,
        "evidencia": evidencia,
    }
    res.anexos = anexos
    return res


def _paginas_del_pliego(pdf_path: str, n_paginas: int) -> int:
    """Cuantas paginas tiene el pliego, contandolas solo si nadie las conto ya.

    El pipeline ya las tiene (`n_pages`) y las pasa; pero `analizar_pliego` es
    una funcion publica y un `paginas=0` no es neutro: el adaptador de OCI lo
    lee para decidir cuantas paginas rasterizar o extraer, y con 0 cae a su
    default generoso. Contar de nuevo cuesta un `fitz.open` por mmap —la misma
    operacion que ya hizo el pipeline, microsegundos y cero RAM— asi que la
    funcion se basta sola en vez de depender de que el caller se acuerde.
    """
    if n_paginas > 0:
        return n_paginas
    try:
        from src.drilldown.pdf import page_count

        return page_count(pdf_path)
    except Exception as exc:  # noqa: BLE001 - contar paginas no puede tumbar el analisis
        logger.warning("No se pudo contar las paginas de %s: %s", pdf_path, exc)
        return 0


def analizar_pliego(
    pdf_path: str,
    cfg,
    contexto_cliente: str,
    proceso_uuid: str,
    formato: str = "",
    client=None,
    n_paginas: int = 0,
) -> ResultadoAnalisis:
    """Analiza un pliego con el proveedor de LLM activo y devuelve la matriz de riesgos.

    En caso de error del proveedor, timeout o parseo irrecuperable, NO relanza
    la excepcion — devuelve un ResultadoAnalisis con estado="ERROR_LLM" para
    que el pipeline continue con el siguiente pliego. `LLMError` cae en ese
    mismo catch-all: la capa nueva no agrega un modo de falla, lo unifica.

    `client` es el cliente CRUDO del proveedor activo y es opcional; si es None
    lo construye —y cachea— el adaptador.
    """
    # El modelo sale de la tabla de despacho, NO de `cfg.model_analysis`: ese
    # campo solo gobierna a Anthropic. `quota.cobrar` tarifa por este string, y
    # es el mismo que viaja en `ResultadoAnalisis.modelo_analisis`; si dijera el
    # modelo equivocado, el gasto de OCI se cobraria a la tarifa de Sonnet.
    #
    # Se declara fuera del try para que un fallo de parseo POSTERIOR a la
    # llamada siga reportando los tokens que ya se pagaron.
    modelo = llm.modelo_de(cfg, "analisis")
    uso = UsoModelo(modelo=modelo)
    truncada = False
    texto_respuesta = ""

    try:
        user_text = (
            f"Contexto del cliente que evalua este proceso:\n{contexto_cliente}\n\n"
            "Analiza el pliego adjunto y devuelve la matriz de riesgos en el "
            "formato JSON indicado en las instrucciones."
        )

        # El orden importa y es parte del prompt: primero el pliego, despues la
        # consigna. `escaneado` y `paginas` viajan con el bloque porque el
        # adaptador de OCI decide con ellos como traducirlo (capa de texto si es
        # nativo, N imagenes si esta escaneado) sin volver a abrir el PDF.
        contenido = [
            BloqueDocumento(
                ruta=pdf_path,
                paginas=_paginas_del_pliego(pdf_path, n_paginas),
                escaneado=(formato == "ESCANEADO"),
            ),
            BloqueTexto(texto=user_text),
        ]

        respuesta = llm.completar(
            cfg,
            Pedido(
                etapa="analisis",
                contenido=contenido,
                sistema=SYSTEM_PROMPT,
                max_tokens=MAX_TOKENS_ANALISIS,
            ),
            cliente=client,
        )

        uso.acumular(respuesta.uso)
        truncada = respuesta.truncada
        texto_respuesta = respuesta.texto or ""

        if not respuesta.texto:
            raise ValueError("La respuesta del modelo no contiene un bloque de texto.")

        data = _limpiar_texto_modelo(_extraer_json(respuesta.texto))
        matriz = _construir_matriz(data)
        monto_estimado, monto_clausula = _extraer_monto(data)

        anexos = {}
        if monto_estimado is not None:
            anexos["monto_estimado_clausula"] = monto_clausula

        # Sin portafolio a mano (`marcas_cliente=None`): una "marca especifica"
        # queda en None. El pipeline, que si conoce el portafolio, la reevalua.
        return aplicar_competibilidad(ResultadoAnalisis(
            proceso_uuid=proceso_uuid,
            estado="ANALIZADO",
            formato=formato,
            matriz=matriz,
            anexos=anexos,
            monto_estimado=monto_estimado,
            modelo_analisis=modelo,
            uso_analisis=uso,
            generado_en=datetime.now(timezone.utc).isoformat(),
        ))
    except Exception as e:
        # `truncada` separa dos causas que hasta la Fase 9 se veian iguales: el
        # modelo dijo algo invalido, o se quedo sin tokens a mitad del JSON. Con
        # el techo de 4096 de OCI el segundo caso pasa de rareza a evento
        # esperado, y quien lee el log necesita distinguirlos.
        detalle = " (respuesta truncada por max_tokens)" if truncada else ""
        # Un `ValueError` con texto de respuesta es un fallo de lectura (JSON roto
        # o matriz inválida): la respuesta cruda se guarda para poder ver QUÉ
        # emitió el modelo. Ver `parseo.guardar_respuesta_fallida`.
        if texto_respuesta and isinstance(e, ValueError):
            ruta = parseo.guardar_respuesta_fallida(
                getattr(cfg, "debug_dir", None), "analisis", proceso_uuid, texto_respuesta, e
            )
            if ruta:
                detalle += f" [respuesta cruda en {ruta}]"
        logger.error(
            "Error en analizar_pliego para proceso_uuid=%s: %s%s", proceso_uuid, e, detalle
        )
        return ResultadoAnalisis(
            proceso_uuid=proceso_uuid,
            estado="ERROR_LLM",
            error=f"{e}{detalle}",
            modelo_analisis=modelo,
            uso_analisis=uso,
            generado_en=datetime.now(timezone.utc).isoformat(),
        )
