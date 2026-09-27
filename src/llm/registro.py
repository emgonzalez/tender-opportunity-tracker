"""src.llm.registro — el libro append-only de lo que consume cada llamada al LLM.

Una línea JSON por llamada, durable, que **no se resetea nunca**. Existe para
responder con datos una pregunta que hoy no tiene respuesta en ningún archivo
del sistema.

── La pregunta abierta que este libro tiene que poder responder ───────────────

Oracle documenta que factura los modelos de Generative AI **por carácter**
(*"1 character is calculated as 1 transaction"*) pero **no dice cómo cuenta una
imagen**. Medido: una página de pliego rasterizada son **45.329 caracteres de
base64** contra **2.491 tokens** reportados — un factor **3,3×**.

Si OCI factura el base64 y no los tokens, el triaje de pliegos escaneados sale
3,3 veces más caro que con Anthropic, justo para el **66% del corpus** que está
escaneado, y eso **invierte el argumento económico de toda la migración**. La
única forma de cerrar la pregunta es registrar los caracteres de base64
efectivamente enviados junto a los tokens reportados, dejar correr el sistema, y
contrastar después contra la factura real en Cost Analysis. Ése es el dato que
`caracteres_base64` guarda y que hoy no existe en ningún lado.

── Por qué no alcanzaba con lo que ya se registra ─────────────────────────────

| Archivo | Qué guarda | Por qué no sirve acá |
|---|---|---|
| `data/gasto_diario.json` (`drilldown/quota.py`) | agregado del día por modelo | **se resetea cada día**; sin caracteres, latencia ni familia |
| `data/history/*.analisis.jsonl` | `UsoModelo` por proceso | solo tokens; no hay entrada cruda ni fallos |

Ninguno de los dos distingue familias de OCI, ninguno guarda latencia, y
ninguno conserva el `usage` **tal como lo devolvió el proveedor**, que es lo que
permite auditar después si la normalización de `contrato.Uso` perdió algo.

── ⚠️ Privacidad: se registran TAMAÑOS, nunca CONTENIDO ───────────────────────

Los pliegos son documentos de clientes. En este archivo **no entra ni un
fragmento** de texto del prompt, ni el base64 de una imagen, ni la respuesta del
modelo: solo sus longitudes. Es exactamente la clase de archivo que alguien va a
querer enriquecer *"con un cachito del prompt para depurar"* — no se hace. Si
hace falta depurar contenido, eso va a `cfg.debug_dir`, que ya existe y no se
sube a ningún lado.

La única excepción es `error`, que guarda el mensaje del proveedor acotado a
`MAX_ERROR` caracteres: es diagnóstico del transporte, no del documento.

── Nunca puede tumbar la corrida ──────────────────────────────────────────────

El registro es **contabilidad, no la tarea**. Si el disco está lleno, el
directorio no existe o el JSON no serializa, se loguea y se sigue: el mismo
patrón de degradación elegante de `memoria/service.py`, por la misma razón que
allá. Una corrida diaria perdida por no poder escribir una línea de
contabilidad sería un intercambio absurdo.

**Las llamadas que fallan también se registran** (`ok: false`). Una llamada que
falló igual consumió latencia y a veces tokens, y el patrón de fallos de cada
proveedor —cuántos, de qué clase, después de cuánto tiempo— es parte de la
comparación que motiva el libro.

── Sobre `uso_crudo` y `razonamiento_oculto` ──────────────────────────────────

Los dos campos salen del objeto `usage` **sin normalizar** del proveedor, que
viaja en `Respuesta.uso_crudo` si el adaptador lo adjunta. Mientras ese atributo
no exista, el registro **reconstruye** `uso_crudo` desde el `Uso` ya normalizado
usando los nombres nativos del proveedor y marca la línea con
`"_reconstruido": true`, para que nadie audite una normalización contra su
propio resultado. En ese caso `razonamiento_oculto` queda en 0 por construcción:
`total_tokens` no sobrevive a la normalización (ver la SOLICITUD DE INTEGRACIÓN
del reporte de esta rama).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from src.llm.modelos import familia_activa, max_tokens_de, modelo_de, proveedor_activo

logger = logging.getLogger(__name__)

# La VM corre en America/El_Salvador y los runners de CI en UTC. Se sella con
# offset fijo -06:00, igual que `scraper/extractor.py` y `main.py`: El Salvador
# no tiene horario de verano, así que el offset fijo es exacto y no arrastra
# `pytz` ni `zoneinfo` hasta acá.
TZ_ELSALVADOR = timezone(timedelta(hours=-6))

NOMBRE_DEFAULT = "llm_consumo.jsonl"

# Los mensajes de error de un SDK pueden traer el request completo de vuelta —o
# sea, el prompt. Se acotan: alcanza para reconocer la clase de fallo y no
# alcanza para filtrar un pliego.
MAX_ERROR = 300


# ── Configuración ────────────────────────────────────────────────────────────


def ruta_del_libro(cfg) -> str:
    """Dónde vive el JSONL. `cfg.llm_registro_path`, o el default bajo `data_dir`.

    Se lee con `getattr` y con fallback propio —no acceso directo— para que un
    `types.SimpleNamespace` de tres campos sirva de `cfg` en los tests, que es
    el idioma del resto de `src/llm`.
    """
    ruta = str(getattr(cfg, "llm_registro_path", "") or "").strip()
    if ruta:
        return ruta
    data_dir = str(getattr(cfg, "data_dir", "") or "data")
    return os.path.join(data_dir, NOMBRE_DEFAULT)


def habilitado(cfg) -> bool:
    """¿Se escribe el libro? Default **sí**: un libro vacío no responde nada."""
    return bool(getattr(cfg, "llm_registro_habilitado", True))


# ── Medición de la entrada (tamaños, nunca contenido) ────────────────────────


def _medir_entrada(pedido) -> dict:
    """Longitudes del pedido, desagregadas por lo que factura distinto.

    Se recorre por `bloque.tipo` y no por `isinstance`: los discriminantes son
    parte del contrato (`contrato.py` los fija en los strings de Anthropic) y
    duck-tipear deja este módulo sin importar los bloques, que es lo que permite
    testearlo con objetos de utilería.
    """
    caracteres_texto = 0
    caracteres_b64 = 0
    bloques = {"text": 0, "image": 0, "document": 0}

    for bloque in getattr(pedido, "contenido", None) or ():
        tipo = str(getattr(bloque, "tipo", "") or "desconocido")
        bloques[tipo] = bloques.get(tipo, 0) + 1
        if tipo == "text":
            caracteres_texto += len(getattr(bloque, "texto", "") or "")
        elif tipo == "image":
            caracteres_b64 += len(getattr(bloque, "datos_b64", "") or "")

    return {
        # El texto del contenido y el prompt de sistema van SEPARADOS: en OCI el
        # sistema también se factura, y saber cuánto pesa cada uno es la
        # diferencia entre recortar el prompt correcto y recortar el otro.
        "caracteres_texto": caracteres_texto,
        "caracteres_sistema": len(str(getattr(pedido, "sistema", "") or "")),
        # LA razón de ser del libro. Ver el docstring del módulo.
        "caracteres_base64": caracteres_b64,
        # Las dos lecturas del mismo dato: si OCI factura el transporte son los
        # caracteres; si factura el pixel, los bytes. Guardar los dos evita
        # tener que reconstruir uno cuando llegue la factura.
        "bytes_imagen": caracteres_b64 * 3 // 4,
        "bloques": bloques,
    }


# ── Uso: lo normalizado, lo crudo y lo que nadie reporta ─────────────────────

# Nombres que usa cada proveedor en su `usage`, en el orden (entrada, salida).
# Solo se usan para RECONSTRUIR `uso_crudo` cuando el adaptador no adjuntó el
# objeto original; nunca para traducir tokens (eso pasa en un solo lugar, los
# constructores de `contrato.Uso`).
_NOMBRES_NATIVOS: dict[str, tuple[str, str]] = {
    "anthropic": ("input_tokens", "output_tokens"),
    "oci": ("prompt_tokens", "completion_tokens"),
}

# Todo lo que puede venir en un `usage` y que interesa guardar verbatim.
_CAMPOS_CRUDOS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _entero(valor) -> int | None:
    """`int(valor)` o `None` si no es un número. Nunca lanza."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def razonamiento_oculto(crudo) -> int:
    """Tokens que el proveedor cobró y **no reportó** como salida.

    `total_tokens - prompt_tokens - completion_tokens` cuando los tres están y
    la resta da positivo; 0 en cualquier otro caso.

    No es una curiosidad. Medido el 2026-09-19 en `us-chicago-1`, con el mismo
    prompt y `max_tokens=20`: `xai.grok-4.6` informó `completion_tokens=18`
    sobre un `total_tokens=514`. Grok y Gemini gastan —y facturan— tokens de
    razonamiento invisibles; este campo los deja medidos por llamada en vez de
    inferidos a fin de mes.
    """
    if crudo is None:
        return 0
    prompt = _entero(getattr(crudo, "prompt_tokens", None))
    completion = _entero(getattr(crudo, "completion_tokens", None))
    total = _entero(getattr(crudo, "total_tokens", None))
    if prompt is None or completion is None or total is None:
        return 0
    return max(0, total - prompt - completion)


def _uso_crudo(crudo, proveedor: str, uso) -> dict:
    """El `usage` del proveedor **con sus nombres originales**, sin normalizar.

    Sin esto no se puede auditar la normalización después: el libro tendría el
    resultado de `contrato.Uso` y nada contra qué contrastarlo.
    """
    if crudo is not None:
        campos = {
            campo: _entero(getattr(crudo, campo, None))
            for campo in _CAMPOS_CRUDOS
            if getattr(crudo, campo, None) is not None
        }
        campos = {k: v for k, v in campos.items() if v is not None}
        if campos:
            return campos

    # Reconstrucción: el adaptador no adjuntó el objeto original. Se marca, para
    # que nadie confunda esto con el dato del proveedor (ver el docstring).
    entrada, salida = _NOMBRES_NATIVOS.get(proveedor, _NOMBRES_NATIVOS["anthropic"])
    return {
        entrada: int(getattr(uso, "input_tokens", 0) or 0) if uso is not None else 0,
        salida: int(getattr(uso, "output_tokens", 0) or 0) if uso is not None else 0,
        "_reconstruido": True,
    }


def _costo_estimado_usd(modelo: str, input_tokens: int, output_tokens: int) -> float:
    """NUESTRA estimación en dólares, no la factura — el nombre del campo lo dice.

    Import perezoso a propósito: `src/llm` no se acopla a `src/drilldown` a
    nivel de módulo. El drill-down es un feature flag y el LLM se usa igual con
    el flag apagado; un import de arriba ataría los dos ciclos de vida sin
    ninguna razón.
    """
    try:
        from src.drilldown.quota import costo_usd

        return round(costo_usd(modelo, input_tokens, output_tokens), 8)
    except Exception as exc:  # noqa: BLE001 — estimar un costo no tumba nada
        logger.debug("registro: no se pudo estimar el costo de %s (%s)", modelo, exc)
        return 0.0


# ── Espejo en la ADB (best-effort, nunca la fuente de verdad) ────────────────
#
# El JSONL sigue siendo lo único que este módulo promete escribir siempre: no
# está en ningún respaldo hoy (plan del portal, §0), así que este espejo es lo
# que le da continuidad a la comparación de costo si se pierde la VM. Se gatea
# igual que el resto de Track B (`enable_memoria`) y nunca puede tumbar la
# corrida ni siquiera cuando ADB está prendida — mismo patrón exacto que
# `memoria/service.py`.


def _espejo_adb(cfg, linea: dict) -> None:
    """Escribe `linea` (la misma que se escribió al JSONL) en `llm_llamada`.

    No-op si `enable_memoria` está apagado. Cualquier fallo (ADB caída, tabla
    ausente en un ambiente viejo, lo que sea) se loguea y se ignora: perder el
    espejo es aceptable, perder la corrida no.
    """
    if not getattr(cfg, "enable_memoria", False):
        return
    try:
        from src.memoria import db

        entrada = linea.get("entrada") or {}
        uso = linea.get("uso") or {}
        detalle = {"bloques": entrada.get("bloques"), "uso_crudo": linea.get("uso_crudo")}

        sql = """
            INSERT INTO llm_llamada (
                ts, proveedor, familia, modelo, etapa, ok, error_msg,
                latencia_ms, max_tokens_pedido, max_tokens_efectivo, truncada,
                caracteres_texto, caracteres_sistema, caracteres_base64, bytes_imagen,
                caracteres_salida, input_tokens, output_tokens, razonamiento_oculto,
                costo_estimado_usd, version, detalle
            ) VALUES (
                :ts, :proveedor, :familia, :modelo, :etapa, :ok, :error_msg,
                :latencia_ms, :max_tokens_pedido, :max_tokens_efectivo, :truncada,
                :caracteres_texto, :caracteres_sistema, :caracteres_base64, :bytes_imagen,
                :caracteres_salida, :input_tokens, :output_tokens, :razonamiento_oculto,
                :costo_estimado_usd, :version, JSON(:detalle)
            )
        """
        # `ts` (columna TIMESTAMP, hora de pared de El Salvador) se pasa como
        # datetime de Python, no como TO_TIMESTAMP(string): así se evita
        # depender de un formato NLS y de TZH:TZM, que TO_TIMESTAMP (a
        # diferencia de TO_TIMESTAMP_TZ) ni siquiera admite. `linea["ts"]` es
        # el ISO de `_linea()`, con offset -06:00; se descarta el tzinfo
        # porque la columna no es WITH TIME ZONE.
        ts = datetime.fromisoformat(linea["ts"]).replace(tzinfo=None)
        binds = {
            "ts": ts,
            "proveedor": linea.get("proveedor") or None,
            "familia": linea.get("familia") or None,
            "modelo": linea.get("modelo") or None,
            "etapa": linea.get("etapa") or None,
            "ok": 1 if linea.get("ok") else 0,
            "error_msg": linea.get("error"),
            "latencia_ms": linea.get("latencia_ms"),
            "max_tokens_pedido": linea.get("max_tokens_pedido"),
            "max_tokens_efectivo": linea.get("max_tokens_efectivo"),
            "truncada": 1 if linea.get("truncada") else 0,
            "caracteres_texto": entrada.get("caracteres_texto"),
            "caracteres_sistema": entrada.get("caracteres_sistema"),
            "caracteres_base64": entrada.get("caracteres_base64"),
            "bytes_imagen": entrada.get("bytes_imagen"),
            "caracteres_salida": (linea.get("salida") or {}).get("caracteres"),
            "input_tokens": uso.get("input_tokens"),
            "output_tokens": uso.get("output_tokens"),
            "razonamiento_oculto": linea.get("razonamiento_oculto"),
            "costo_estimado_usd": linea.get("costo_estimado_usd"),
            "version": linea.get("version") or None,
            "detalle": json.dumps(detalle, ensure_ascii=False, default=str),
        }

        with db.connection(cfg) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, binds)
            finally:
                cursor.close()
    except Exception as exc:  # noqa: BLE001 — el espejo nunca tumba la corrida
        logger.warning("registro: no se pudo espejar la llamada en la ADB (%s).", exc)


# ── Escritura ────────────────────────────────────────────────────────────────


def _escribir(ruta: str, linea: dict) -> None:
    """Append de una línea JSON. Crea el directorio si falta.

    Append en modo `"a"`: una escritura sola, de una línea corta, con `O_APPEND`
    — dos procesos que escriban a la vez no se pisan a mitad de línea en POSIX.
    No hay write-to-tmp + `os.replace` como en `quota._guardar_libro` porque acá
    no se reescribe el archivo entero: reemplazarlo sería, justamente, perder
    todo lo acumulado.
    """
    carpeta = os.path.dirname(ruta)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(json.dumps(linea, ensure_ascii=False) + "\n")


def _linea(cfg, pedido, *, proveedor, modelo, respuesta, error, latencia_ms) -> dict:
    """Arma el dict de una llamada. Esquema estable: hay un script que lo lee."""
    uso = getattr(respuesta, "uso", None) if respuesta is not None else None
    crudo = getattr(respuesta, "uso_crudo", None) if respuesta is not None else None
    entrada = int(getattr(uso, "input_tokens", 0) or 0) if uso is not None else 0
    salida = int(getattr(uso, "output_tokens", 0) or 0) if uso is not None else 0

    etapa = str(getattr(pedido, "etapa", "") or "")
    pedido_max = int(getattr(pedido, "max_tokens", 0) or 0)

    return {
        "ts": datetime.now(TZ_ELSALVADOR).isoformat(),
        "proveedor": proveedor,
        "familia": _familia(cfg, proveedor),
        "modelo": modelo,
        "etapa": etapa,
        "ok": error is None,
        "error": _mensaje_de_error(error),
        "latencia_ms": int(latencia_ms),
        "max_tokens_pedido": pedido_max,
        # Comparado con el pedido, muestra cuándo el techo del modelo está
        # recortando la respuesta sin que nadie se entere.
        "max_tokens_efectivo": _max_tokens_efectivo(cfg, etapa, pedido_max),
        "truncada": bool(getattr(respuesta, "truncada", False)) if respuesta else False,
        "entrada": _medir_entrada(pedido),
        "salida": {"caracteres": len(str(getattr(respuesta, "texto", "") or ""))},
        "uso": {"input_tokens": entrada, "output_tokens": salida},
        "uso_crudo": _uso_crudo(crudo, proveedor, uso),
        "razonamiento_oculto": razonamiento_oculto(crudo),
        "costo_estimado_usd": _costo_estimado_usd(modelo, entrada, salida),
        "version": str(getattr(cfg, "app_version", "") or ""),
    }


def _mensaje_de_error(error) -> str | None:
    if error is None:
        return None
    texto = error if isinstance(error, str) else str(error)
    return texto[:MAX_ERROR] or error.__class__.__name__


def _familia(cfg, proveedor: str) -> str:
    """La familia de OCI activa. Vacía con Anthropic: allá el eje no existe."""
    if proveedor != "oci":
        return ""
    try:
        return familia_activa(cfg)
    except Exception:  # noqa: BLE001 — una familia ilegible no tumba el registro
        return ""


def _max_tokens_efectivo(cfg, etapa: str, deseado: int) -> int:
    """Lo que queda del `max_tokens` pedido tras el clamp de `modelos`.

    Vuelve a llamar a `max_tokens_de` en vez de recibirlo del adaptador: la
    función es pura y barata, y pedírselo al adaptador obligaría a cambiarle la
    firma a los dos, que es justo lo que este módulo no debe costar.
    """
    try:
        return int(max_tokens_de(cfg, etapa, deseado))
    except Exception:  # noqa: BLE001 — sin clamp conocido, queda lo pedido
        return deseado


def _modelo(cfg, respuesta, etapa: str) -> str:
    """El modelo que atendió la llamada; con la llamada fallida, el que le tocaba."""
    modelo = str(getattr(respuesta, "modelo", "") or "") if respuesta is not None else ""
    if modelo:
        return modelo
    try:
        return modelo_de(cfg, etapa)
    except Exception:  # noqa: BLE001 — un modelo sin resolver no tumba el registro
        return ""


# ── La puerta: nunca lanza ───────────────────────────────────────────────────


def registrar(
    cfg,
    pedido,
    *,
    proveedor: str = "",
    respuesta=None,
    error: BaseException | str | None = None,
    latencia_ms: int = 0,
) -> None:
    """Anota una llamada en el libro. **No lanza jamás.**

    Es contabilidad, no la tarea: cualquier fallo acá se loguea y se sigue. Un
    disco lleno no puede costar la corrida de las 07:00, que no se repite.

    `respuesta` es la `Respuesta` del adaptador cuando la llamada salió bien;
    `error`, la excepción cuando no. Las dos cosas se registran: una llamada
    fallida igual consumió latencia, a veces tokens, y siempre información sobre
    el proveedor.
    """
    try:
        if not habilitado(cfg):
            return
        proveedor = proveedor or _proveedor(cfg, respuesta)
        etapa = str(getattr(pedido, "etapa", "") or "")
        linea = _linea(
            cfg,
            pedido,
            proveedor=proveedor,
            modelo=_modelo(cfg, respuesta, etapa),
            respuesta=respuesta,
            error=error,
            latencia_ms=latencia_ms,
        )
        _escribir(ruta_del_libro(cfg), linea)
        _espejo_adb(cfg, linea)
    except Exception as exc:  # noqa: BLE001 — el libro nunca tumba la corrida
        logger.warning(
            "registro: no se pudo anotar el consumo de la llamada (%s). La corrida sigue; "
            "la comparación entre proveedores va a tener un hueco.",
            exc,
        )


def _proveedor(cfg, respuesta) -> str:
    proveedor = str(getattr(respuesta, "proveedor", "") or "") if respuesta is not None else ""
    if proveedor:
        return proveedor
    try:
        return proveedor_activo(cfg)
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "MAX_ERROR",
    "NOMBRE_DEFAULT",
    "habilitado",
    "razonamiento_oculto",
    "registrar",
    "ruta_del_libro",
]
