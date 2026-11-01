"""Servicio de administración de clientes y perfiles para el portal (Fase G2). Sin HTTP.

Cada función recibe la `conn` que abrió la ruta (`db.connection`, una transacción) y devuelve un resultado de
vocabulario cerrado, el mismo de `src.web.admin` (los literales coinciden a propósito para no importar la web
desde acá): `OK | NO_ENCONTRADO | INVALIDO | CONFLICTO` (+ `SIN_CAMBIOS`). Nada hace commit ni audita: la
plantilla `escritura_admin` audita SOLO si el resultado es `OK`, en la misma transacción.

Reglas que concentra:

- Alcance: `admin_plataforma` ve todos los clientes; `admin_organizacion`, solo los de su organización. Lo ajeno es
  `NO_ENCONTRADO` (el 404 uniforme), igual que lo inexistente.
- Cada edición de perfil parte de la versión ACTIVA de la ADB, reemplaza SOLO su sección y se guarda con la
  `version_base` que vio quien editó: si la activa cambió, `CONFLICTO` y no se escribe nada.
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


class _Invalido(Exception):
    """Dato del formulario que no cumple la regla; `str(exc)` no se muestra al usuario (solo se loguea si hace falta)."""


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

def _texto(valor: Any, maximo: int, *, una_linea: bool = False, obligatorio: bool = False) -> str:
    if not isinstance(valor, str):
        raise _Invalido("no es texto")
    t = valor.strip()
    if obligatorio and not t:
        raise _Invalido("vacío")
    if len(t) > maximo:
        raise _Invalido("demasiado largo")
    if una_linea and any(ord(c) < 32 or ord(c) == 127 for c in t):
        raise _Invalido("no admite saltos de línea ni caracteres de control")
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
            raise _Invalido("línea demasiado larga")
        salida.append(linea)
    if len(salida) > _MAX_LINEAS:
        raise _Invalido("demasiadas líneas")
    return salida


def _lista_de_str(valor: Any, que: str) -> list[str]:
    if not isinstance(valor, list) or not all(isinstance(x, str) and x.strip() for x in valor):
        raise _Invalido(f"{que} debe ser una lista de textos")
    return [x.strip() for x in valor]


def _categorias(valor: Any) -> list[dict]:
    if not isinstance(valor, str) or len(valor) > _MAX_JSON_CATEGORIAS:
        raise _Invalido("categorías: texto ausente o demasiado grande")
    try:
        datos = json.loads(valor) if valor.strip() else []
    except ValueError as exc:
        raise _Invalido("categorías: JSON inválido") from exc
    if not isinstance(datos, list):
        raise _Invalido("categorías debe ser una lista")
    ids: set[str] = set()
    limpias = []
    for cat in datos:
        if not isinstance(cat, dict):
            raise _Invalido("cada categoría es un objeto")
        cid = _texto(cat.get("id"), 60, una_linea=True, obligatorio=True)
        if cid in ids:
            raise _Invalido("id de categoría repetido")
        ids.add(cid)
        limpia: dict[str, Any] = {"id": cid, "label": _texto(cat.get("label"), 120, una_linea=True, obligatorio=True),
                                  "items": _lista_de_str(cat.get("items", []), "items")}
        marcas = cat.get("brands", [])
        if not isinstance(marcas, list):
            raise _Invalido("brands debe ser una lista")
        limpia["brands"] = []
        for m in marcas:
            if not isinstance(m, dict):
                raise _Invalido("cada marca es un objeto")
            limpia["brands"].append({
                "id": _texto(m.get("id"), 60, una_linea=True, obligatorio=True),
                "name": _texto(m.get("name"), 120, una_linea=True, obligatorio=True),
                "products": _lista_de_str(m.get("products", []), "products"),
            })
        limpias.append(limpia)
    return limpias


def _umbral(valor: Any) -> float:
    if not isinstance(valor, str):
        raise _Invalido("umbral no numérico")
    try:
        n = float(valor.strip().replace(",", "."))
    except ValueError as exc:
        raise _Invalido("umbral no numérico") from exc
    if not 0.0 <= n <= 1.0:      # también descarta nan e inf
        raise _Invalido("el umbral va de 0 a 1")
    return round(n, 4)


def _motivos(valor: Any) -> list[dict]:
    motivos: list[dict] = []
    for linea in _lineas(valor):
        codigo, sep, etiqueta = linea.partition("|")
        codigo, etiqueta = codigo.strip(), etiqueta.strip()
        if not sep or not _RE_ID_MOTIVO.match(codigo) or not etiqueta or len(etiqueta) > 120:
            raise _Invalido("cada motivo es «codigo | etiqueta»")
        if any(m["id"] == codigo for m in motivos):
            raise _Invalido("código de motivo repetido")
        motivos.append({"id": codigo, "label": etiqueta})
    return motivos


# ── Secciones: formulario → trozo del perfil, y perfil → valores del formulario ─────────────────────────────

def _aplicar(seccion: str, datos: dict, campos: dict) -> None:
    """Reemplaza SOLO la sección en `datos` (copia ya hecha por quien llama). Lanza `_Invalido`."""
    if seccion == "catalog":
        cat = datos.setdefault("catalog", {})
        cat["categories"] = _categorias(campos.get("categories", ""))
        cat["inclusion_signals"] = _lineas(campos.get("inclusion_signals", ""))
        cat["exclusion_signals"] = _lineas(campos.get("exclusion_signals", ""))
        cat["tag_taxonomy"] = _lineas(campos.get("tag_taxonomy", ""))
    elif seccion == "rules":
        reglas = datos.setdefault("rules", {})
        reglas["context_note"] = _texto(campos.get("context_note", ""), 5000)
        reglas["categories"] = _texto(campos.get("categories", ""), 500, una_linea=True, obligatorio=True)
    elif seccion == "thresholds":
        clasif = datos.setdefault("classification", {})
        clasif["relevance_threshold"] = _umbral(campos.get("relevance_threshold", ""))
        clasif["confidence_threshold"] = _umbral(campos.get("confidence_threshold", ""))
    elif seccion == "email":
        notif = datos.setdefault("notification", {})
        notif["report_title"] = _texto(campos.get("report_title", ""), 200, una_linea=True, obligatorio=True)
        notif["subject_prefix"] = _texto(campos.get("subject_prefix", ""), 100, una_linea=True, obligatorio=True)
        notif["intro_message"] = _texto(campos.get("intro_message", ""), 2000)
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


def valores_formulario(seccion: str, datos: dict) -> dict:
    """Los valores actuales de la sección, listos para el formulario (texto plano; Jinja los escapa)."""
    cat = datos.get("catalog") or {}
    reglas = datos.get("rules") or {}
    clasif = datos.get("classification") or {}
    notif = datos.get("notification") or {}
    if seccion == "catalog":
        return {"categories": json.dumps(cat.get("categories") or [], ensure_ascii=False, indent=2),
                "inclusion_signals": "\n".join(cat.get("inclusion_signals") or []),
                "exclusion_signals": "\n".join(cat.get("exclusion_signals") or []),
                "tag_taxonomy": "\n".join(cat.get("tag_taxonomy") or [])}
    if seccion == "rules":
        return {"context_note": reglas.get("context_note", ""),
                "categories": reglas.get("categories",
                                         "hardware, software, servicios_ti, ciberseguridad, cableado, redes, audiovisual, otro")}
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
    if visible(conn, alcance, cliente_id) is None:
        return NO_ENCONTRADO
    if seccion not in SECCIONES:
        return NO_ENCONTRADO
    base = _entero(version_base)
    if base is None:
        return INVALIDO
    try:
        _, actual = repo.requerir_base(conn, cliente_id, base)
        nuevo = copy.deepcopy(actual)
        _aplicar(seccion, nuevo, campos)
        version = repo.guardar_perfil(conn, cliente_id, nuevo, creado_por(conn, alcance), base)
    except ConflictoVersion:
        return CONFLICTO
    except (_Invalido, ErrorClientes):
        return INVALIDO
    return OK if version is not None else SIN_CAMBIOS


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
