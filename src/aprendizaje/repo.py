"""src.aprendizaje.repo — SQL de la memoria de aprendizaje (`db/schema_v2.sql` §7) sobre una `conn` ya abierta.

Mismas reglas que `src.identidad.repo` y `src.clientes.repo`: nada acá hace commit (decide quien abrió la
conexión), todo valor viaja como bind variable y las funciones SÍ propagan los errores de la base — la
tolerancia a una ADB caída vive en `src.aprendizaje.service`, que es lo que llama la corrida diaria.

Toda escritura que cambia términos o calibraciones deja su fila en `auditoria`, en la misma transacción.
La acción se verifica ANTES de escribir: si no está en `ACCIONES_AUDITADAS`, no se toca nada.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from src.identidad import repo as identidad_repo
from src.identidad.politica import AccionNoAuditable
from src.memoria.vectorizer import _validar_nombre_modelo

logger = logging.getLogger(__name__)

MODELO_EMBEDDING = "MULTILINGUAL_E5_SMALL"

ETIQUETAS = frozenset({"positivo", "negativo", "ajena"})
FUENTES = frozenset({
    "voto_usuario", "confirmacion", "resultado_comercial", "participacion",
    "pliego", "verificacion_llm", "humana", "semilla",
})
SPLITS = frozenset({"entrenamiento", "prueba", "critico"})
TIPOS_TERMINO = frozenset({"fuerte", "debil", "excluye", "ajena"})
VARIANTES = frozenset({"activa", "sombra"})

# Las que este módulo escribe en `auditoria`. Hoy NO están en `src.identidad.politica.ACCIONES_AUDITADAS`
# (territorio congelado de este PR): ver la SOLICITUD DE INTEGRACIÓN del PR.
ACCION_TERMINOS_SEMBRADOS = "TERMINOS_SEMBRADOS"
ACCION_TERMINO_PROPUESTO = "TERMINO_PROPUESTO"
ACCION_TERMINO_ACTIVADO = "TERMINO_ACTIVADO"
ACCION_TERMINO_RECHAZADO = "TERMINO_RECHAZADO"
ACCION_CALIBRACION_REGISTRADA = "CALIBRACION_REGISTRADA"
ACCION_CALIBRACION_PROMOVIDA = "CALIBRACION_PROMOVIDA"
ACCION_CALIBRACION_ARCHIVADA = "CALIBRACION_ARCHIVADA"
ACCIONES = frozenset({
    ACCION_TERMINOS_SEMBRADOS, ACCION_TERMINO_PROPUESTO, ACCION_TERMINO_ACTIVADO, ACCION_TERMINO_RECHAZADO,
    ACCION_CALIBRACION_REGISTRADA, ACCION_CALIBRACION_PROMOVIDA, ACCION_CALIBRACION_ARCHIVADA,
})

# Tope del JSON de `decision_capa.salida` que viaja como bind de texto: más grande se reemplaza por un
# marcador (un JSON recortado sería inválido y la fila no se guardaría).
_MAX_SALIDA_BYTES = 32000


# ── Utilidades ─────────────────────────────────────────────────────────────

def recortar_bytes(texto: Optional[str], max_bytes: int) -> Optional[str]:
    """Recorta a ``max_bytes`` BYTES en UTF-8 (VARCHAR2 de la ADB cuenta bytes, no caracteres)."""
    if texto is None:
        return None
    crudo = texto.encode("utf-8")
    if len(crudo) <= max_bytes:
        return texto
    return crudo[:max_bytes].decode("utf-8", errors="ignore")


def _exigir(valor: Any, permitidos: frozenset, campo: str) -> None:
    if valor not in permitidos:
        raise ValueError(f"{campo} inválido: {valor!r} (válidos: {sorted(permitidos)})")


def _a_json(valor: Any) -> Any:
    """Columna JSON leída con JSON_SERIALIZE(... RETURNING CLOB): str o LOB → objeto Python."""
    if valor is None or isinstance(valor, (dict, list)):
        return valor
    if hasattr(valor, "read"):
        valor = valor.read()
    if isinstance(valor, bytes):
        valor = valor.decode("utf-8")
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        return None


def _json_bind(valor: Any) -> Optional[str]:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str) if valor is not None else None


def _devuelto(var: Any) -> Optional[int]:
    """Valor de un ``RETURNING ... INTO :v`` (oracledb lo entrega como lista de una fila)."""
    valor = var.getvalue()
    if isinstance(valor, list):
        valor = valor[0] if valor else None
    return int(valor) if valor is not None else None


def _ejecutar(conn: Any, sql: str, binds: dict[str, Any]) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        return cursor.rowcount
    finally:
        cursor.close()


def _uno(conn: Any, sql: str, binds: dict[str, Any]) -> Optional[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        return cursor.fetchone()
    finally:
        cursor.close()


def _todos(conn: Any, sql: str, binds: dict[str, Any]) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, binds)
        return cursor.fetchall()
    finally:
        cursor.close()


def _exigir_auditable(accion: str) -> None:
    """Falla ANTES de escribir si `auditoria` rechazaría la acción (P6: nada cambia sin su fila)."""
    if accion not in identidad_repo.ACCIONES_AUDITADAS:
        raise AccionNoAuditable(accion)


def _org_de(conn: Any, cliente_id: str) -> str:
    fila = _uno(conn, "SELECT org_id FROM cliente WHERE cliente_id = :cliente_id", {"cliente_id": cliente_id})
    if not fila:
        raise ValueError(f"el cliente {cliente_id!r} no existe")
    return fila[0]


def existe_cliente(conn: Any, cliente_id: str) -> bool:
    return _uno(conn, "SELECT 1 FROM cliente WHERE cliente_id = :cliente_id", {"cliente_id": cliente_id}) is not None


def _auditar(conn: Any, cliente_id: str, accion: str, actor: Optional[str], recurso: str) -> None:
    identidad_repo.registrar_auditoria(
        conn, _org_de(conn, cliente_id), accion,
        usuario_id=recortar_bytes(actor, 64) if actor else None, recurso=recurso,
    )


# ── ejemplo_marca ──────────────────────────────────────────────────────────

def agregar_ejemplo(
    conn: Any,
    cliente_id: str,
    marca_id: str,
    etiqueta: str,
    fuente: str,
    titulo_texto: str,
    *,
    proceso_gid: Optional[str] = None,
    peso: float = 1.0,
    evidencia: Optional[str] = None,
    pagina: Optional[int] = None,
    split: str = "entrenamiento",
    creado_por: Optional[str] = None,
    calcular_embedding: bool = True,
    modelo: str = MODELO_EMBEDDING,
) -> dict[str, Any]:
    """Inserta un ejemplo etiquetado; el embedding se calcula DENTRO de la base con ``VECTOR_EMBEDDING``.

    Idempotente cuando hay ``proceso_gid``: si ya existe un ejemplo activo con el mismo (cliente, proceso,
    marca, fuente, etiqueta) devuelve ese y no inserta otro. Si el modelo ONNX no está cargado (o falla),
    inserta con ``embedding`` NULL y lo informa en el log y en el resultado: nunca revienta por eso.

    :return: ``{"ejemplo_id", "nuevo", "embedding"}`` — ``embedding`` es True si quedó calculado.
    :raises ValueError: etiqueta, fuente, split o nombre de modelo fuera de lo permitido.
    """
    _exigir(etiqueta, ETIQUETAS, "etiqueta")
    _exigir(fuente, FUENTES, "fuente")
    _exigir(split, SPLITS, "split")
    if not titulo_texto or not titulo_texto.strip():
        raise ValueError("titulo_texto vacío: el ejemplo no tendría qué vectorizar")
    nombre_modelo = _validar_nombre_modelo(modelo) if calcular_embedding else None

    if proceso_gid is not None:
        previo = _uno(
            conn,
            "SELECT ejemplo_id FROM ejemplo_marca WHERE cliente_id = :cliente_id AND proceso_gid = :proceso_gid "
            "AND marca_id = :marca_id AND fuente = :fuente AND etiqueta = :etiqueta AND activo = 1 "
            "FETCH FIRST 1 ROWS ONLY",
            {"cliente_id": cliente_id, "proceso_gid": proceso_gid, "marca_id": marca_id,
             "fuente": fuente, "etiqueta": etiqueta},
        )
        if previo:
            return {"ejemplo_id": int(previo[0]), "nuevo": False, "embedding": None}

    binds = {
        "cliente_id": cliente_id, "proceso_gid": proceso_gid, "marca_id": marca_id, "etiqueta": etiqueta,
        "fuente": fuente, "peso": peso, "evidencia": recortar_bytes(evidencia, 1000), "pagina": pagina,
        "titulo": recortar_bytes(titulo_texto.strip(), 1000), "split": split,
        "creado_por": recortar_bytes(creado_por, 64),
    }
    columnas = ("INSERT INTO ejemplo_marca (cliente_id, proceso_gid, marca_id, etiqueta, fuente, peso, evidencia, "
                "pagina, titulo_texto, embedding, split, creado_por) VALUES (:cliente_id, :proceso_gid, :marca_id, "
                ":etiqueta, :fuente, :peso, :evidencia, :pagina, :titulo, ")
    cola = ", :split, :creado_por) RETURNING ejemplo_id INTO :ejemplo_id"

    cursor = conn.cursor()
    try:
        if nombre_modelo is not None:
            salida = cursor.var(int)
            try:
                cursor.execute(columnas + f"VECTOR_EMBEDDING({nombre_modelo} USING :titulo AS data)" + cola,
                               {**binds, "ejemplo_id": salida})
                return {"ejemplo_id": _devuelto(salida), "nuevo": True, "embedding": True}
            except Exception as exc:  # noqa: BLE001 — sin modelo ONNX se guarda igual, sin vector
                logger.warning(
                    "agregar_ejemplo: VECTOR_EMBEDDING(%s) no disponible (%s); se inserta con embedding NULL",
                    nombre_modelo, type(exc).__name__,
                )
        salida = cursor.var(int)
        cursor.execute(columnas + "NULL" + cola, {**binds, "ejemplo_id": salida})
        return {"ejemplo_id": _devuelto(salida), "nuevo": True, "embedding": False}
    finally:
        cursor.close()


def listar_ejemplos(
    conn: Any,
    cliente_id: str,
    marca_id: Optional[str] = None,
    *,
    etiqueta: Optional[str] = None,
    split: Optional[str] = None,
    solo_activos: bool = True,
    limite: int = 500,
) -> list[dict[str, Any]]:
    """Ejemplos de un cliente (sin el vector: solo si lo tiene), del más nuevo al más viejo."""
    filtros = ["cliente_id = :cliente_id"]
    binds: dict[str, Any] = {"cliente_id": cliente_id, "limite": int(limite)}
    if marca_id is not None:
        filtros.append("marca_id = :marca_id")
        binds["marca_id"] = marca_id
    if etiqueta is not None:
        _exigir(etiqueta, ETIQUETAS, "etiqueta")
        filtros.append("etiqueta = :etiqueta")
        binds["etiqueta"] = etiqueta
    if split is not None:
        _exigir(split, SPLITS, "split")
        filtros.append("split = :split")
        binds["split"] = split
    if solo_activos:
        filtros.append("activo = 1")
    filas = _todos(
        conn,
        "SELECT ejemplo_id, proceso_gid, marca_id, etiqueta, fuente, peso, evidencia, pagina, titulo_texto, "
        "split, activo, creado_por, creado_en, CASE WHEN embedding IS NULL THEN 0 ELSE 1 END "
        "FROM ejemplo_marca WHERE " + " AND ".join(filtros) +
        " ORDER BY creado_en DESC, ejemplo_id DESC FETCH FIRST :limite ROWS ONLY",
        binds,
    )
    return [{
        "ejemplo_id": int(f[0]), "proceso_gid": f[1], "marca_id": f[2], "etiqueta": f[3], "fuente": f[4],
        "peso": float(f[5]) if f[5] is not None else None, "evidencia": f[6], "pagina": f[7],
        "titulo_texto": f[8], "split": f[9], "activo": bool(f[10]), "creado_por": f[11], "creado_en": f[12],
        "tiene_embedding": bool(f[13]),
    } for f in filas]


def desactivar_ejemplo(conn: Any, ejemplo_id: int, *, proteger_humanos: bool = False) -> bool:
    """Apaga un ejemplo (nunca se borra). Con ``proteger_humanos`` —lo que usa el ciclo automático— no toca
    los de ``fuente='humana'`` ni los del split ``critico``: lo humano es inmutable para el ciclo.

    :return: True si cambió algo.
    """
    sql = "UPDATE ejemplo_marca SET activo = 0 WHERE ejemplo_id = :ejemplo_id AND activo = 1"
    if proteger_humanos:
        sql += " AND fuente != 'humana' AND split != 'critico'"
    return _ejecutar(conn, sql, {"ejemplo_id": ejemplo_id}) > 0


# ── marca_termino ──────────────────────────────────────────────────────────

_COLUMNAS_TERMINO = "termino_id, marca_id, termino, tipo, estado, soporte, precision_est, origen, version, activo"


def _termino(f: tuple) -> dict[str, Any]:
    return {
        "termino_id": int(f[0]), "marca_id": f[1], "termino": f[2], "tipo": f[3], "estado": f[4],
        "soporte": f[5], "precision_est": float(f[6]) if f[6] is not None else None, "origen": f[7],
        "version": int(f[8]) if f[8] is not None else None, "activo": bool(f[9]),
    }


def listar_terminos_activos(conn: Any, cliente_id: str, marca_id: Optional[str] = None) -> list[dict[str, Any]]:
    """El diccionario vigente: ``activo = 1`` y estado ``semilla`` o ``activo``."""
    binds: dict[str, Any] = {"cliente_id": cliente_id}
    filtro_marca = ""
    if marca_id is not None:
        filtro_marca = " AND marca_id = :marca_id"
        binds["marca_id"] = marca_id
    filas = _todos(
        conn,
        f"SELECT {_COLUMNAS_TERMINO} FROM marca_termino WHERE cliente_id = :cliente_id AND activo = 1 "
        f"AND estado IN ('semilla','activo'){filtro_marca} ORDER BY marca_id, tipo, termino",
        binds,
    )
    return [_termino(f) for f in filas]


def terminos_existentes(conn: Any, cliente_id: str) -> set[tuple[str, str, str]]:
    """(marca_id, termino, tipo) de TODAS las filas del cliente, en cualquier estado."""
    filas = _todos(conn, "SELECT marca_id, termino, tipo FROM marca_termino WHERE cliente_id = :cliente_id",
                   {"cliente_id": cliente_id})
    return {(f[0], f[1], f[2]) for f in filas}


def _validar_termino(marca_id: str, termino: str, tipo: str) -> str:
    _exigir(tipo, TIPOS_TERMINO, "tipo")
    limpio = (termino or "").strip()
    if not limpio or not marca_id:
        raise ValueError("marca_id y termino son obligatorios")
    if len(limpio.encode("utf-8")) > 120:
        raise ValueError(f"término demasiado largo para VARCHAR2(120 BYTE): {limpio[:40]!r}…")
    return limpio


def sembrar_terminos(conn: Any, cliente_id: str, terminos: Iterable[tuple[str, str, str]],
                     creado_por: Optional[str] = None) -> int:
    """Inserta términos de semilla (``estado='semilla'``, ``activo=1``) que todavía no existan.

    Idempotente: el MERGE solo tiene rama de INSERT, así que una fila existente —incluida una que una persona
    rechazó— no se toca. Una sola fila de auditoría por llamada, con la cantidad insertada.
    :return: filas insertadas.
    """
    lote = []
    for marca_id, termino, tipo in terminos:
        lote.append({"cliente_id": cliente_id, "marca_id": marca_id, "termino": _validar_termino(marca_id, termino, tipo),
                     "tipo": tipo, "creado_por": recortar_bytes(creado_por, 64)})
    if not lote:
        return 0
    _exigir_auditable(ACCION_TERMINOS_SEMBRADOS)
    cursor = conn.cursor()
    try:
        cursor.executemany(
            """
            MERGE INTO marca_termino t
            USING (SELECT :cliente_id AS cliente_id, :marca_id AS marca_id, :termino AS termino, :tipo AS tipo
                     FROM dual) s
               ON (t.cliente_id = s.cliente_id AND t.marca_id = s.marca_id
                   AND t.termino = s.termino AND t.tipo = s.tipo)
            WHEN NOT MATCHED THEN
              INSERT (cliente_id, marca_id, termino, tipo, estado, origen, activo, creado_por)
              VALUES (s.cliente_id, s.marca_id, s.termino, s.tipo, 'semilla', 'semilla', 1, :creado_por)
            """,
            lote,
        )
        insertados = cursor.rowcount or 0
    finally:
        cursor.close()
    if insertados:
        _auditar(conn, cliente_id, ACCION_TERMINOS_SEMBRADOS, creado_por,
                 f"cliente={cliente_id};insertados={insertados}")
    return insertados


def proponer_termino(
    conn: Any,
    cliente_id: str,
    marca_id: str,
    termino: str,
    tipo: str,
    *,
    soporte: float = 0,
    precision_est: Optional[float] = None,
    origen: str = "minado",
    creado_por: Optional[str] = None,
) -> Optional[int]:
    """Deja un término en ``estado='propuesto'`` (inactivo) para revisión o activación por el ciclo.

    Si ya existe y sigue propuesto, refresca ``soporte`` y ``precision_est``; si ya fue activado, rechazado
    o es de semilla, no lo toca (un rechazo humano no se deshace solo).
    :return: ``termino_id`` si se insertó o actualizó; None si no cambió nada.
    """
    limpio = _validar_termino(marca_id, termino, tipo)
    _exigir_auditable(ACCION_TERMINO_PROPUESTO)
    binds = {"cliente_id": cliente_id, "marca_id": marca_id, "termino": limpio, "tipo": tipo}
    cambiadas = _ejecutar(
        conn,
        """
        MERGE INTO marca_termino t
        USING (SELECT :cliente_id AS cliente_id, :marca_id AS marca_id, :termino AS termino, :tipo AS tipo
                 FROM dual) s
           ON (t.cliente_id = s.cliente_id AND t.marca_id = s.marca_id
               AND t.termino = s.termino AND t.tipo = s.tipo)
        WHEN MATCHED THEN
          UPDATE SET t.soporte = :soporte, t.precision_est = :precision_est, t.actualizado_en = SYSTIMESTAMP
           WHERE t.estado = 'propuesto'
        WHEN NOT MATCHED THEN
          INSERT (cliente_id, marca_id, termino, tipo, soporte, precision_est, estado, origen, activo, creado_por)
          VALUES (s.cliente_id, s.marca_id, s.termino, s.tipo, :soporte, :precision_est, 'propuesto', :origen, 0,
                  :creado_por)
        """,
        {**binds, "soporte": soporte, "precision_est": precision_est, "origen": recortar_bytes(origen, 32),
         "creado_por": recortar_bytes(creado_por, 64)},
    )
    if not cambiadas:
        return None
    fila = _uno(conn, "SELECT termino_id FROM marca_termino WHERE cliente_id = :cliente_id AND marca_id = :marca_id "
                      "AND termino = :termino AND tipo = :tipo", binds)
    termino_id = int(fila[0]) if fila else None
    _auditar(conn, cliente_id, ACCION_TERMINO_PROPUESTO, creado_por,
             f"cliente={cliente_id};marca={marca_id};tipo={tipo};termino_id={termino_id}")
    return termino_id


def _cambiar_estado_termino(conn: Any, termino_id: int, estado: str, activo: int, desde: tuple[str, ...],
                            accion: str, actor: Optional[str]) -> bool:
    _exigir_auditable(accion)
    fila = _uno(conn, "SELECT cliente_id, marca_id, estado FROM marca_termino WHERE termino_id = :termino_id "
                      "FOR UPDATE", {"termino_id": termino_id})
    if not fila or fila[2] not in desde:
        return False
    cliente_id, marca_id, previo = fila
    cambiadas = _ejecutar(
        conn,
        "UPDATE marca_termino SET estado = :estado, activo = :activo, version = version + 1, "
        "actualizado_en = SYSTIMESTAMP WHERE termino_id = :termino_id",
        {"estado": estado, "activo": activo, "termino_id": termino_id},
    )
    if cambiadas:
        _auditar(conn, cliente_id, accion, actor,
                 f"cliente={cliente_id};marca={marca_id};termino_id={termino_id};de={previo}")
    return cambiadas > 0


def activar_termino(conn: Any, termino_id: int, actor: Optional[str] = None) -> bool:
    """propuesto o rechazado → activo. :return: True si cambió."""
    return _cambiar_estado_termino(conn, termino_id, "activo", 1, ("propuesto", "rechazado"),
                                   ACCION_TERMINO_ACTIVADO, actor)


def rechazar_termino(conn: Any, termino_id: int, actor: Optional[str] = None) -> bool:
    """semilla, propuesto o activo → rechazado (inactivo). :return: True si cambió."""
    return _cambiar_estado_termino(conn, termino_id, "rechazado", 0, ("semilla", "propuesto", "activo"),
                                   ACCION_TERMINO_RECHAZADO, actor)


# ── marca_calibracion ──────────────────────────────────────────────────────

_COLUMNAS_CALIBRACION = (
    "version, cliente_id, marca_id, umbral_confirmada, umbral_posible, k, "
    "JSON_SERIALIZE(pesos_fuentes RETURNING CLOB), JSON_SERIALIZE(parametros RETURNING CLOB), "
    "JSON_SERIALIZE(metricas RETURNING CLOB), estado, creado_por, creado_en"
)


def _calibracion(f: tuple) -> dict[str, Any]:
    def num(v: Any) -> Optional[float]:
        return float(v) if v is not None else None
    return {
        "version": int(f[0]), "cliente_id": f[1], "marca_id": f[2], "umbral_confirmada": num(f[3]),
        "umbral_posible": num(f[4]), "k": int(f[5]) if f[5] is not None else None,
        "pesos_fuentes": _a_json(f[6]), "parametros": _a_json(f[7]), "metricas": _a_json(f[8]),
        "estado": f[9], "creado_por": f[10], "creado_en": f[11],
    }


def calibracion_activa(conn: Any, cliente_id: str, marca_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """La calibración activa de la marca; si la marca no tiene, la global del cliente (``marca_id`` NULL)."""
    fila = _uno(
        conn,
        f"SELECT {_COLUMNAS_CALIBRACION} FROM marca_calibracion WHERE cliente_id = :cliente_id "
        "AND estado = 'activa' AND (marca_id = :marca_id OR marca_id IS NULL) "
        "ORDER BY CASE WHEN marca_id IS NULL THEN 1 ELSE 0 END FETCH FIRST 1 ROWS ONLY",
        {"cliente_id": cliente_id, "marca_id": marca_id},
    )
    return _calibracion(fila) if fila else None


def obtener_calibracion(conn: Any, version: int) -> Optional[dict[str, Any]]:
    fila = _uno(conn, f"SELECT {_COLUMNAS_CALIBRACION} FROM marca_calibracion WHERE version = :version",
                {"version": version})
    return _calibracion(fila) if fila else None


def registrar_candidata(
    conn: Any,
    cliente_id: str,
    marca_id: Optional[str] = None,
    *,
    umbral_confirmada: Optional[float] = None,
    umbral_posible: Optional[float] = None,
    k: Optional[int] = None,
    pesos_fuentes: Optional[dict] = None,
    parametros: Optional[dict] = None,
    metricas: Optional[dict] = None,
    creado_por: Optional[str] = None,
) -> int:
    """Inserta una calibración en ``estado='candidata'`` (no la lee nadie hasta que se promueva).

    :return: la ``version`` asignada.
    """
    _exigir_auditable(ACCION_CALIBRACION_REGISTRADA)
    cursor = conn.cursor()
    try:
        salida = cursor.var(int)
        cursor.execute(
            """
            INSERT INTO marca_calibracion (cliente_id, marca_id, umbral_confirmada, umbral_posible, k,
                                           pesos_fuentes, parametros, metricas, estado, creado_por)
            VALUES (:cliente_id, :marca_id, :umbral_confirmada, :umbral_posible, :k,
                    JSON(:pesos_fuentes), JSON(:parametros), JSON(:metricas), 'candidata', :creado_por)
            RETURNING version INTO :version
            """,
            {
                "cliente_id": cliente_id, "marca_id": marca_id, "umbral_confirmada": umbral_confirmada,
                "umbral_posible": umbral_posible, "k": k, "pesos_fuentes": _json_bind(pesos_fuentes),
                "parametros": _json_bind(parametros), "metricas": _json_bind(metricas),
                "creado_por": recortar_bytes(creado_por, 64), "version": salida,
            },
        )
        version = _devuelto(salida)
    finally:
        cursor.close()
    _auditar(conn, cliente_id, ACCION_CALIBRACION_REGISTRADA, creado_por,
             f"cliente={cliente_id};marca={marca_id or '*'};version={version}")
    return version


def promover(conn: Any, version: int, actor: Optional[str] = None) -> Optional[int]:
    """Activa ``version`` (candidata o archivada: reactivar una archivada es el rollback) y archiva la que
    estaba activa para el mismo (cliente, marca), en la MISMA transacción y en ese orden (el índice único
    `ux_marca_calibracion_activa` no admite dos activas ni por un instante).

    :return: la versión que quedó archivada (None si no había ninguna activa).
    :raises ValueError: si ``version`` no existe o ya es la activa.
    """
    _exigir_auditable(ACCION_CALIBRACION_PROMOVIDA)
    fila = _uno(conn, "SELECT cliente_id, marca_id, estado FROM marca_calibracion WHERE version = :version "
                      "FOR UPDATE", {"version": version})
    if not fila:
        raise ValueError(f"no existe la calibración {version}")
    cliente_id, marca_id, estado = fila
    if estado == "activa":
        raise ValueError(f"la calibración {version} ya está activa")
    binds = {"cliente_id": cliente_id, "marca_id": marca_id}
    anterior = _uno(
        conn,
        "SELECT version FROM marca_calibracion WHERE cliente_id = :cliente_id "
        "AND NVL(marca_id, '*') = NVL(:marca_id, '*') AND estado = 'activa' FOR UPDATE",
        binds,
    )
    if anterior:
        _ejecutar(conn, "UPDATE marca_calibracion SET estado = 'archivada' WHERE version = :version",
                  {"version": anterior[0]})
    _ejecutar(conn, "UPDATE marca_calibracion SET estado = 'activa' WHERE version = :version", {"version": version})
    archivada = int(anterior[0]) if anterior else None
    _auditar(conn, cliente_id, ACCION_CALIBRACION_PROMOVIDA, actor,
             f"cliente={cliente_id};marca={marca_id or '*'};version={version};archivada={archivada}")
    return archivada


def archivar(conn: Any, version: int, actor: Optional[str] = None) -> bool:
    """Archiva una calibración (candidata descartada o activa que se retira sin reemplazo).

    :return: True si cambió algo.
    """
    _exigir_auditable(ACCION_CALIBRACION_ARCHIVADA)
    fila = _uno(conn, "SELECT cliente_id, marca_id, estado FROM marca_calibracion WHERE version = :version "
                      "FOR UPDATE", {"version": version})
    if not fila or fila[2] == "archivada":
        return False
    cliente_id, marca_id, previo = fila
    _ejecutar(conn, "UPDATE marca_calibracion SET estado = 'archivada' WHERE version = :version", {"version": version})
    _auditar(conn, cliente_id, ACCION_CALIBRACION_ARCHIVADA, actor,
             f"cliente={cliente_id};marca={marca_id or '*'};version={version};de={previo}")
    return True


# ── decision_capa ──────────────────────────────────────────────────────────

def _salida_bind(salida: Any) -> Optional[str]:
    texto = _json_bind(salida)
    if texto is not None and len(texto.encode("utf-8")) > _MAX_SALIDA_BYTES:
        logger.warning("guardar_decision: salida de %d bytes, se guarda un marcador", len(texto.encode("utf-8")))
        return json.dumps({"truncado": True, "bytes": len(texto.encode("utf-8"))})
    return texto


def guardar_decision(
    conn: Any,
    cliente_id: str,
    proceso_gid: str,
    capa: str,
    *,
    version: Optional[str] = None,
    variante: str = "activa",
    salida: Any = None,
    score: Optional[float] = None,
    propension: Optional[float] = None,
    costo_usd: Optional[float] = None,
    latencia_ms: Optional[int] = None,
    entrada_hash: Optional[str] = None,
) -> int:
    """Registra lo que decidió una capa sobre un proceso (append-only). :return: el ``id`` de la fila."""
    _exigir(variante, VARIANTES, "variante")
    cursor = conn.cursor()
    try:
        salida_id = cursor.var(int)
        cursor.execute(
            """
            INSERT INTO decision_capa (cliente_id, proceso_gid, capa, version, variante, salida, score,
                                       propension, costo_usd, latencia_ms, entrada_hash)
            VALUES (:cliente_id, :proceso_gid, :capa, :version, :variante, JSON(:salida), :score,
                    :propension, :costo_usd, :latencia_ms, :entrada_hash)
            RETURNING id INTO :id
            """,
            {
                "cliente_id": cliente_id, "proceso_gid": proceso_gid, "capa": recortar_bytes(capa, 16),
                "version": recortar_bytes(version, 32), "variante": variante, "salida": _salida_bind(salida),
                "score": score, "propension": propension, "costo_usd": costo_usd, "latencia_ms": latencia_ms,
                "entrada_hash": entrada_hash, "id": salida_id,
            },
        )
        return _devuelto(salida_id)
    finally:
        cursor.close()


def buscar_decision_por_hash(
    conn: Any,
    entrada_hash: str,
    capa: str,
    *,
    version: Optional[str] = None,
    variante: str = "activa",
    cliente_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Caché: la decisión más reciente de esa capa (y versión, si se da) para la misma entrada."""
    _exigir(variante, VARIANTES, "variante")
    filtros = ["entrada_hash = :entrada_hash", "capa = :capa", "variante = :variante"]
    binds: dict[str, Any] = {"entrada_hash": entrada_hash, "capa": capa, "variante": variante}
    if version is not None:
        filtros.append("version = :version")
        binds["version"] = version
    if cliente_id is not None:
        filtros.append("cliente_id = :cliente_id")
        binds["cliente_id"] = cliente_id
    fila = _uno(
        conn,
        "SELECT id, cliente_id, proceso_gid, capa, version, variante, JSON_SERIALIZE(salida RETURNING CLOB), "
        "score, propension, costo_usd, latencia_ms, entrada_hash, creado_en FROM decision_capa WHERE "
        + " AND ".join(filtros) + " ORDER BY creado_en DESC, id DESC FETCH FIRST 1 ROWS ONLY",
        binds,
    )
    if not fila:
        return None

    def num(v: Any) -> Optional[float]:
        return float(v) if v is not None else None
    return {
        "id": int(fila[0]), "cliente_id": fila[1], "proceso_gid": fila[2], "capa": fila[3], "version": fila[4],
        "variante": fila[5], "salida": _a_json(fila[6]), "score": num(fila[7]), "propension": num(fila[8]),
        "costo_usd": num(fila[9]), "latencia_ms": int(fila[10]) if fila[10] is not None else None,
        "entrada_hash": fila[11], "creado_en": fila[12],
    }


# ── estado (solo lectura) ──────────────────────────────────────────────────

TABLAS = ("ejemplo_marca", "marca_termino", "marca_calibracion", "decision_capa", "pagina_pliego", "senal_mercado")


def contar_por_tabla(conn: Any, cliente_id: Optional[str] = None) -> dict[str, int]:
    """Filas por tabla (las que tienen ``cliente_id`` se filtran si se da; `pagina_pliego` no tiene)."""
    conteos: dict[str, int] = {}
    for tabla in TABLAS:  # nombres fijos de esta tupla, nunca de la entrada
        if cliente_id is not None and tabla != "pagina_pliego":
            fila = _uno(conn, f"SELECT COUNT(*) FROM {tabla} WHERE cliente_id = :cliente_id",
                        {"cliente_id": cliente_id})
        else:
            fila = _uno(conn, f"SELECT COUNT(*) FROM {tabla}", {})
        conteos[tabla] = int(fila[0]) if fila else 0
    return conteos


def contar_terminos_por_estado(conn: Any, cliente_id: str) -> dict[tuple[str, str], int]:
    """(tipo, estado) → filas del diccionario del cliente."""
    filas = _todos(conn, "SELECT tipo, estado, COUNT(*) FROM marca_termino WHERE cliente_id = :cliente_id "
                         "GROUP BY tipo, estado", {"cliente_id": cliente_id})
    return {(f[0], f[1]): int(f[2]) for f in filas}
