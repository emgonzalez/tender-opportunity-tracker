"""Servicio de administración de clientes y perfiles para el portal (Fase G2). Sin HTTP.

Cada función recibe la `conn` que abrió la ruta (`db.connection`, una transacción) y devuelve un resultado de
vocabulario cerrado, el mismo de `src.web.admin` (los literales coinciden a propósito para no importar la web
desde acá): `OK | NO_ENCONTRADO | INVALIDO | CONFLICTO` (+ `SIN_CAMBIOS`). Nada hace commit ni audita: la
plantilla `escritura_admin` audita SOLO si el resultado es `OK`, en la misma transacción.

Reglas que concentra:

- Alcance: `admin_plataforma` ve todos los clientes; `admin_organizacion`, solo los de su organización. Lo ajeno es
  `NO_ENCONTRADO` (el 404 uniforme), igual que lo inexistente.
- Cada edición de perfil parte de la versión ACTIVA de la ADB, reemplaza SOLO lo que edita el formulario de su
  sección y se guarda con la `version_base` que vio quien editó: si la activa cambió, `CONFLICTO` y no se
  escribe nada. Sin pérdida: las claves que el editor no conoce (de la raíz, de cada sección, de cada categoría y
  de cada marca) se conservan tal cual, y guardar sin tocar nada no inventa claves (⇒ `SIN_CAMBIOS`).
- Un `INVALIDO` lleva un motivo de la lista cerrada `MOTIVOS` (`editar_seccion_detalle`).
- Todo se valida con `_parse_profile` (vía `repo.validar`) ANTES de escribir, y los tipos de cada campo se revisan
  acá porque el parser es tolerante (un string donde va una lista no falla, y sí rompería la corrida).
- `cliente.activo` y `client.active` solo los escribe `repo.fijar_activo`.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.classifier.alias_clientes import id_canonico
from src.clientes import repo
from src.clientes.repo import ConflictoVersion, ErrorClientes
from src.portales import PORTALES

OK = "ok"
NO_ENCONTRADO = "no_encontrado"
INVALIDO = "invalido"
CONFLICTO = "conflicto"
SIN_CAMBIOS = "sin_cambios"

# Secciones editables del perfil: el slug de la URL → (clave en el JSON, título). `inteligencia` es solo lectura.
SECCIONES = {
    "catalog": "Catálogo",
    "rules": "Reglas",
    "thresholds": "Umbrales",
    "email": "Textos del correo",
    "feedback": "Motivos de feedback",
}
SECCION_INTELIGENCIA = "intelligence"

_RE_ID_CLIENTE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
_RE_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_RE_ID_MOTIVO = re.compile(r"^[a-z0-9_]{2,40}$")
_MAX_JSON_CATEGORIAS = 200_000
_MAX_LINEAS = 300
_MAX_LARGO_LINEA = 200
_MAX_DESCRIPCION = 500
_MAX_MARCAS = 500

# Motivo de un INVALIDO: lista CERRADA de códigos → texto fijo. Es lo único que la URL lleva de vuelta
# (`?reason=<código>`); nunca el contenido del usuario ni el detalle de la excepción.
MOTIVOS = {
    "datos": "Algún campo no tiene el formato esperado.",
    "obligatorio": "Falta un campo obligatorio.",
    "largo": "Un texto supera el largo permitido.",
    "caracteres": "Un campo de una sola línea trae saltos de línea o caracteres de control.",
    "lineas": "Demasiadas líneas, o alguna línea demasiado larga.",
    "catalogo_json": "El JSON de categorías no se pudo leer: revisa comas, comillas y corchetes.",
    "catalogo_estructura": "El catálogo debe ser una lista de categorías (objetos), y `brands` una lista de marcas (objetos).",
    "catalogo_id": "Falta el id de una categoría o marca, o está repetido.",
    "catalogo_lista": "`items` y `products` deben ser listas de textos.",
    "descripcion": "La descripción de una marca no es válida (una línea, hasta 500 caracteres).",
    "umbral": "Los umbrales son números de 0 a 1.",
    "motivos": "Cada motivo es «codigo | etiqueta», con códigos en minúscula y sin repetir.",
    "version": "La versión de partida no es válida: recarga la página.",
    "perfil": "El perfil resultante no pasa la validación del clasificador.",
}


class _Invalido(Exception):
    """Dato del formulario que no cumple la regla. `motivo` es un código de `MOTIVOS` (lo único que ve el usuario);
    `str(exc)` no se muestra."""

    def __init__(self, detalle: str = "", motivo: str = "datos"):
        super().__init__(detalle)
        self.motivo = motivo if motivo in MOTIVOS else "datos"


# ── Quién escribe ──────────────────────────────────────────────────────────────

def creado_por(conn, alcance) -> str:
    """El correo del admin (columna `creado_por`, VARCHAR2(64) = 64 BYTES); si no se halla, su `usuario_id`."""
    from src.identidad import repo as identidad_repo
    usuario = identidad_repo.obtener_usuario(conn, alcance.usuario_id)
    texto = (usuario or {}).get("email") or alcance.usuario_id
    return texto.encode("utf-8")[:64].decode("utf-8", "ignore")


def _visible(alcance, fila: Optional[dict]) -> bool:
    return bool(fila) and (alcance.es_plataforma or fila["org_id"] == alcance.org_id)


def visible(conn, alcance, cliente_id: str) -> Optional[dict]:
    """La fila de `cliente` si este admin puede administrarlo; None si no existe o es ajeno."""
    fila = repo.obtener(conn, cliente_id) if isinstance(cliente_id, str) and 0 < len(cliente_id) <= 64 else None
    return fila if _visible(alcance, fila) else None


def listar(conn, alcance) -> list[dict]:
    return repo.listar(conn, None if alcance.es_plataforma else alcance.org_id)


# ── Parseo de campos del formulario ─────────────────────────────────────────────

def _texto(valor: Any, maximo: int, *, una_linea: bool = False, obligatorio: bool = False,
           motivo: Optional[str] = None) -> str:
    if not isinstance(valor, str):
        raise _Invalido("no es texto", motivo or "datos")
    t = valor.replace("\r\n", "\n").replace("\r", "\n").strip()    # el navegador envía los saltos como CRLF
    if obligatorio and not t:
        raise _Invalido("vacío", motivo or "obligatorio")
    if len(t) > maximo:
        raise _Invalido("demasiado largo", motivo or "largo")
    if una_linea and any(ord(c) < 32 or ord(c) == 127 for c in t):
        raise _Invalido("no admite saltos de línea ni caracteres de control", motivo or "caracteres")
    return t


def _lineas(valor: Any) -> list[str]:
    if not isinstance(valor, str):
        raise _Invalido("no es texto")
    salida: list[str] = []
    for crudo in valor.splitlines():
        linea = crudo.strip()
        if not linea or linea in salida:
            continue
        if len(linea) > _MAX_LARGO_LINEA:
            raise _Invalido("línea demasiado larga", "lineas")
        salida.append(linea)
    if len(salida) > _MAX_LINEAS:
        raise _Invalido("demasiadas líneas", "lineas")
    return salida


def _lista_de_str(valor: Any, que: str) -> list[str]:
    if not isinstance(valor, list) or not all(isinstance(x, str) and x.strip() for x in valor):
        raise _Invalido(f"{que} debe ser una lista de textos", "catalogo_lista")
    return [x.strip() for x in valor]


def _descripcion(valor: Any) -> str:
    return _texto(valor, _MAX_DESCRIPCION, una_linea=True, motivo="descripcion")


def _marca(m: Any, ids: set[str]) -> dict:
    """Valida las claves conocidas de una marca y conserva TODAS las demás tal cual (round-trip sin pérdida)."""
    if not isinstance(m, dict):
        raise _Invalido("cada marca es un objeto", "catalogo_estructura")
    limpia = copy.deepcopy(m)
    limpia["id"] = _texto(m.get("id"), 60, una_linea=True, obligatorio=True, motivo="catalogo_id")
    if limpia["id"] in ids:
        raise _Invalido("id de marca repetido en la categoría", "catalogo_id")
    ids.add(limpia["id"])
    limpia["name"] = _texto(m.get("name"), 120, una_linea=True, obligatorio=True)
    if "products" in m:
        limpia["products"] = _lista_de_str(m["products"], "products")
    if "descripcion" in m:
        limpia["descripcion"] = _descripcion(m["descripcion"])
        if not limpia["descripcion"]:
            del limpia["descripcion"]
    return limpia


def _categorias(valor: Any) -> list[dict]:
    """El JSON del formulario → lista de categorías. Las claves conocidas se validan; las desconocidas (de la
    categoría y de cada marca) se conservan sin tocar. Una clave conocida ausente sigue ausente."""
    if not isinstance(valor, str) or len(valor) > _MAX_JSON_CATEGORIAS:
        raise _Invalido("categorías: texto ausente o demasiado grande", "catalogo_json")
    try:
        datos = json.loads(valor) if valor.strip() else []
    except ValueError as exc:
        raise _Invalido("categorías: JSON inválido", "catalogo_json") from exc
    if not isinstance(datos, list):
        raise _Invalido("categorías debe ser una lista", "catalogo_estructura")
    ids: set[str] = set()
    limpias = []
    for cat in datos:
        if not isinstance(cat, dict):
            raise _Invalido("cada categoría es un objeto", "catalogo_estructura")
        limpia = copy.deepcopy(cat)
        limpia["id"] = _texto(cat.get("id"), 60, una_linea=True, obligatorio=True, motivo="catalogo_id")
        if limpia["id"] in ids:
            raise _Invalido("id de categoría repetido", "catalogo_id")
        ids.add(limpia["id"])
        limpia["label"] = _texto(cat.get("label"), 120, una_linea=True, obligatorio=True)
        if "items" in cat:
            limpia["items"] = _lista_de_str(cat["items"], "items")
        if "brands" in cat:
            if not isinstance(cat["brands"], list):
                raise _Invalido("brands debe ser una lista", "catalogo_estructura")
            ids_marca: set[str] = set()
            limpia["brands"] = [_marca(m, ids_marca) for m in cat["brands"]]
        limpias.append(limpia)
    return limpias


def _descripciones_previas(datos: dict) -> dict[tuple[str, str], str]:
    """{(categoría, marca): descripción} del perfil activo (solo las bien formadas)."""
    salida: dict[tuple[str, str], str] = {}
    for cat in (datos.get("catalog") or {}).get("categories") or []:
        if not isinstance(cat, dict):
            continue
        for m in cat.get("brands") or []:
            if isinstance(m, dict) and isinstance(m.get("descripcion"), str) and m["descripcion"].strip():
                salida[(str(cat.get("id")), str(m.get("id")))] = m["descripcion"]
    return salida


def _descripciones_formulario(campos: dict) -> dict[tuple[str, str], str]:
    """Los campos `marca_ref_<i>` (JSON `[categoría, marca]`) + `marca_desc_<i>` del formulario."""
    salida: dict[tuple[str, str], str] = {}
    for i in range(_MAX_MARCAS + 1):
        ref = campos.get(f"marca_ref_{i}")
        if ref is None:
            return salida
        if i == _MAX_MARCAS:
            raise _Invalido("demasiadas marcas", "descripcion")
        try:
            par = json.loads(ref) if isinstance(ref, str) and len(ref) <= 300 else None
        except ValueError:
            par = None
        if not (isinstance(par, list) and len(par) == 2 and all(isinstance(x, str) for x in par)):
            raise _Invalido("referencia de marca mal formada", "descripcion")
        salida[(par[0], par[1])] = _descripcion(campos.get(f"marca_desc_{i}", ""))
    return salida


def _fusionar_descripciones(categorias: list[dict], previas: dict, del_formulario: dict) -> None:
    """La descripción de cada marca: la del campo propio si vino; si no, la del JSON si la trae; si no, la que
    ya tenía el perfil activo (ausente = se conserva). Vacía en su campo = se borra."""
    for cat in categorias:
        for m in cat.get("brands") or []:
            clave = (cat["id"], m["id"])
            if clave in del_formulario:
                texto = del_formulario[clave]
            elif "descripcion" in m:
                continue
            else:
                texto = previas.get(clave, "")
            if texto:
                m["descripcion"] = texto
            else:
                m.pop("descripcion", None)


def _umbral(valor: Any) -> float:
    if not isinstance(valor, str):
        raise _Invalido("umbral no numérico", "umbral")
    try:
        n = float(valor.strip().replace(",", "."))
    except ValueError as exc:
        raise _Invalido("umbral no numérico", "umbral") from exc
    if not 0.0 <= n <= 1.0:      # también descarta nan e inf
        raise _Invalido("el umbral va de 0 a 1", "umbral")
    return round(n, 4)


def _motivos(valor: Any) -> list[dict]:
    motivos: list[dict] = []
    for linea in _lineas(valor):
        codigo, sep, etiqueta = linea.partition("|")
        codigo, etiqueta = codigo.strip(), etiqueta.strip()
        if not sep or not _RE_ID_MOTIVO.match(codigo) or not etiqueta or len(etiqueta) > 120:
            raise _Invalido("cada motivo es «codigo | etiqueta»", "motivos")
        if any(m["id"] == codigo for m in motivos):
            raise _Invalido("código de motivo repetido", "motivos")
        motivos.append({"id": codigo, "label": etiqueta})
    return motivos


# ── Secciones: formulario → trozo del perfil, y perfil → valores del formulario ─────────────────────────────

def _poner(d: dict, clave: str, valor: Any, defecto: Any) -> None:
    """`d[clave] = valor`, salvo que la clave no estuviera y el valor sea el que el formulario muestra por defecto:
    guardar sin tocar nada no debe inventar claves (cambiaría el hash y crearía una versión vacía)."""
    previo = d.get(clave)
    if clave in d and _normal(previo) == _normal(valor):
        return      # equivalente a lo guardado (p. ej. el salto final de un bloque YAML): no se toca
    if clave in d or valor != defecto:
        d[clave] = valor


def _normal(valor: Any) -> Any:
    """Forma comparable de lo que el formulario normaliza: textos sin espacios en los bordes ni CRLF, y las
    listas de líneas sin vacías ni repetidas."""
    if isinstance(valor, str):
        return valor.replace("\r\n", "\n").strip()
    if isinstance(valor, list) and all(isinstance(x, str) for x in valor):
        salida: list[str] = []
        for x in (x.strip() for x in valor):
            if x and x not in salida:
                salida.append(x)
        return salida
    return valor


def _seccion(datos: dict, clave: str, llenar) -> None:
    """Aplica `llenar` sobre una copia de `datos[clave]` y solo la cuelga si existía o quedó con algo."""
    existia = isinstance(datos.get(clave), dict)
    trozo = dict(datos[clave]) if existia else {}
    llenar(trozo)
    if existia or trozo:
        datos[clave] = trozo


_CATEGORIAS_DEFECTO = "hardware, software, servicios_ti, ciberseguridad, cableado, redes, audiovisual, otro"


def _aplicar(seccion: str, datos: dict, campos: dict) -> None:
    """Reemplaza SOLO lo que edita el formulario de la sección en `datos` (copia ya hecha por quien llama): todo lo
    demás del perfil —otras claves de la sección, y las claves desconocidas de categorías y marcas— se conserva
    tal cual. Lanza `_Invalido`."""
    if seccion == "catalog":
        previas = _descripciones_previas(datos)
        categorias = _categorias(campos.get("categories", ""))
        _fusionar_descripciones(categorias, previas, _descripciones_formulario(campos))
        inclusion = _lineas(campos.get("inclusion_signals", ""))
        exclusion = _lineas(campos.get("exclusion_signals", ""))
        taxonomia = _lineas(campos.get("tag_taxonomy", ""))

        def llenar(cat: dict) -> None:
            _poner(cat, "categories", categorias, [])
            _poner(cat, "inclusion_signals", inclusion, [])
            _poner(cat, "exclusion_signals", exclusion, [])
            _poner(cat, "tag_taxonomy", taxonomia, [])
        _seccion(datos, "catalog", llenar)
    elif seccion == "rules":
        nota = _texto(campos.get("context_note", ""), 5000)
        categorias_txt = _texto(campos.get("categories", ""), 500, una_linea=True, obligatorio=True)

        def llenar(reglas: dict) -> None:
            _poner(reglas, "context_note", nota, "")
            _poner(reglas, "categories", categorias_txt, _CATEGORIAS_DEFECTO)
        _seccion(datos, "rules", llenar)
    elif seccion == "thresholds":
        relevancia = _umbral(campos.get("relevance_threshold", ""))
        confianza = _umbral(campos.get("confidence_threshold", ""))

        def llenar(clasif: dict) -> None:
            _poner(clasif, "relevance_threshold", relevancia, 0.60)
            _poner(clasif, "confidence_threshold", confianza, 0.70)
        _seccion(datos, "classification", llenar)
    elif seccion == "email":
        titulo = _texto(campos.get("report_title", ""), 200, una_linea=True, obligatorio=True)
        prefijo = _texto(campos.get("subject_prefix", ""), 100, una_linea=True, obligatorio=True)
        intro = _texto(campos.get("intro_message", ""), 2000)

        def llenar(notif: dict) -> None:
            notif["report_title"] = titulo
            _poner(notif, "subject_prefix", prefijo, "[Monitor]")
            _poner(notif, "intro_message", intro, "")
        _seccion(datos, "notification", llenar)
    elif seccion == "feedback":
        motivos = _motivos(campos.get("motivos", ""))
        fb = dict(datos.get("feedback") or {})
        if motivos:
            fb["motivos"] = motivos
        else:
            fb.pop("motivos", None)     # vacío = vuelve al catálogo por defecto
        if fb:
            datos["feedback"] = fb
        else:
            datos.pop("feedback", None)
    else:  # pragma: no cover — la ruta ya filtra por SECCIONES
        raise _Invalido("sección desconocida")


def _sin_descripcion(categorias: Any) -> Any:
    """Las categorías para el JSON del formulario: igual que en el perfil, salvo `descripcion` de las marcas
    (texto), que se edita en su propio campo."""
    if not isinstance(categorias, list):
        return categorias
    salida = []
    for cat in categorias:
        if isinstance(cat, dict) and isinstance(cat.get("brands"), list):
            cat = {**cat, "brands": [
                {k: v for k, v in m.items() if not (k == "descripcion" and isinstance(v, str))}
                if isinstance(m, dict) else m for m in cat["brands"]]}
        salida.append(cat)
    return salida


def _marcas_formulario(categorias: Any) -> list[dict]:
    """Un campo de descripción por marca: `ref` (JSON `[categoría, marca]`), etiquetas y el texto actual."""
    salida: list[dict] = []
    if not isinstance(categorias, list):
        return salida
    for cat in categorias:
        if not isinstance(cat, dict) or not isinstance(cat.get("id"), str) or not isinstance(cat.get("brands"), list):
            continue
        for m in cat["brands"]:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                continue
            desc = m.get("descripcion", "")
            if not isinstance(desc, str):
                continue    # se queda en el JSON, donde la validación lo señala
            salida.append({"ref": json.dumps([cat["id"], m["id"]], ensure_ascii=False),
                           "categoria": cat.get("label") or cat["id"], "nombre": m.get("name") or m["id"],
                           "descripcion": desc})
    return salida[:_MAX_MARCAS]


def valores_formulario(seccion: str, datos: dict) -> dict:
    """Los valores actuales de la sección, listos para el formulario (texto plano; Jinja los escapa)."""
    cat = datos.get("catalog") or {}
    reglas = datos.get("rules") or {}
    clasif = datos.get("classification") or {}
    notif = datos.get("notification") or {}
    if seccion == "catalog":
        return {"categories": json.dumps(_sin_descripcion(cat.get("categories") or []), ensure_ascii=False, indent=2),
                "marcas": _marcas_formulario(cat.get("categories")),
                "inclusion_signals": "\n".join(cat.get("inclusion_signals") or []),
                "exclusion_signals": "\n".join(cat.get("exclusion_signals") or []),
                "tag_taxonomy": "\n".join(cat.get("tag_taxonomy") or [])}
    if seccion == "rules":
        return {"context_note": reglas.get("context_note", ""),
                "categories": reglas.get("categories", _CATEGORIAS_DEFECTO)}
    if seccion == "thresholds":
        return {"relevance_threshold": clasif.get("relevance_threshold", 0.60),
                "confidence_threshold": clasif.get("confidence_threshold", 0.70)}
    if seccion == "email":
        return {"report_title": notif.get("report_title", ""), "subject_prefix": notif.get("subject_prefix", "[Monitor]"),
                "intro_message": notif.get("intro_message", "")}
    if seccion == "feedback":
        motivos = (datos.get("feedback") or {}).get("motivos") or []
        return {"motivos": "\n".join(f"{m.get('id')} | {m.get('label')}" for m in motivos if isinstance(m, dict))}
    return {}


def inteligencia(datos: dict) -> dict:
    """Solo lectura: la genera `classifier/trainer.py`."""
    i = datos.get("intelligence") or {}
    return {"generated_at": i.get("generated_at"), "processes_analyzed": i.get("processes_analyzed", 0),
            "profile": i.get("profile") or ""}


# ── Escrituras ──────────────────────────────────────────────────────────────────

def _entero(valor: Any) -> Optional[int]:
    try:
        n = int(str(valor).strip())
    except ValueError:
        return None
    return n if 0 < n < 10**6 else None


def editar_seccion(conn, alcance, cliente_id: str, seccion: str, campos: dict, version_base: Any) -> str:
    """Reemplaza una sección del perfil activo y registra la versión nueva (si cambió)."""
    return editar_seccion_detalle(conn, alcance, cliente_id, seccion, campos, version_base)[0]


def editar_seccion_detalle(conn, alcance, cliente_id: str, seccion: str, campos: dict,
                           version_base: Any) -> tuple[str, Optional[str]]:
    """Como `editar_seccion`, más el motivo (código de `MOTIVOS`) cuando el resultado es `INVALIDO`."""
    if visible(conn, alcance, cliente_id) is None:
        return NO_ENCONTRADO, None
    if seccion not in SECCIONES:
        return NO_ENCONTRADO, None
    base = _entero(version_base)
    if base is None:
        return INVALIDO, "version"
    try:
        _, actual = repo.requerir_base(conn, cliente_id, base)
        nuevo = copy.deepcopy(actual)
        _aplicar(seccion, nuevo, campos)
        version = repo.guardar_perfil(conn, cliente_id, nuevo, creado_por(conn, alcance), base)
    except ConflictoVersion:
        return CONFLICTO, None
    except _Invalido as exc:
        return INVALIDO, exc.motivo
    except ErrorClientes:
        return INVALIDO, "perfil"
    return (OK if version is not None else SIN_CAMBIOS), None


def editar_general(conn, alcance, cliente_id: str, nombre: Any, locale: Any, zona: Any, version_base: Any) -> str:
    if visible(conn, alcance, cliente_id) is None:
        return NO_ENCONTRADO
    base = _entero(version_base)
    try:
        nombre = _texto(nombre, 200, una_linea=True, obligatorio=True)
        locale = _texto(locale, 8, una_linea=True, obligatorio=True)
        zona = _texto(zona, 64, una_linea=True, obligatorio=True)
        if base is None or not _RE_LOCALE.match(locale):
            raise _Invalido("locale o versión")
        try:
            ZoneInfo(zona)
        except Exception as exc:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError (ruta insegura)…
            raise _Invalido("zona horaria desconocida") from exc
        repo.editar_general(conn, cliente_id, nombre, locale, zona, creado_por(conn, alcance), base)
    except ConflictoVersion:
        return CONFLICTO
    except (_Invalido, ErrorClientes):
        return INVALIDO
    return OK


def crear_cliente(conn, alcance, cliente_id: Any, nombre: Any, portal: Any, org_id: Any) -> str:
    """Solo `admin_plataforma` (la ruta lo exige). Inserta `cliente` + versión 1 de `cliente_config`."""
    try:
        cid = _texto(cliente_id, 48, una_linea=True, obligatorio=True)
        nombre = _texto(nombre, 200, una_linea=True, obligatorio=True)
        if not _RE_ID_CLIENTE.match(cid) or id_canonico(cid) != cid or portal not in PORTALES:
            raise _Invalido("id o portal")
        if not isinstance(org_id, str) or org_id not in {o["org_id"] for o in repo.organizaciones(conn)}:
            raise _Invalido("organización")
        repo.crear(conn, cid, nombre, portal, org_id, creado_por(conn, alcance))
    except (_Invalido, ErrorClientes):
        return INVALIDO
    return OK


def fijar_activo(conn, alcance, cliente_id: str, valor: bool, version_base: Any) -> str:
    """Activa o desactiva: `cliente.activo` y `client.active` juntos. Solo `admin_plataforma` (la ruta)."""
    if visible(conn, alcance, cliente_id) is None:
        return NO_ENCONTRADO
    base = _entero(version_base)
    if base is None:
        return INVALIDO
    try:
        repo.fijar_activo(conn, cliente_id, valor, creado_por(conn, alcance), base)
    except ConflictoVersion:
        return CONFLICTO
    except ErrorClientes:
        return INVALIDO
    return OK


def revertir(conn, alcance, cliente_id: str, numero: Any, version_base: Any) -> str:
    """Una versión nueva con el contenido de la `numero` (nunca borra). Sin cambios = `SIN_CAMBIOS`."""
    if visible(conn, alcance, cliente_id) is None:
        return NO_ENCONTRADO
    n, base = _entero(numero), _entero(version_base)
    if n is None or base is None:
        return INVALIDO
    try:
        if repo.version(conn, cliente_id, n) is None:
            return NO_ENCONTRADO
        version = repo.revertir(conn, cliente_id, n, creado_por(conn, alcance), base)
    except ConflictoVersion:
        return CONFLICTO
    except ErrorClientes:
        return INVALIDO
    return OK if version is not None else SIN_CAMBIOS
