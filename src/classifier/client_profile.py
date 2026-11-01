"""
Carga y valida perfiles de cliente desde archivos YAML en clients/.

Cada cliente tiene su propio YAML que es la única fuente de verdad para:
  - Información del cliente (nombre, descripción)
  - Catálogo de productos/servicios
  - Reglas de negocio (context_note, categorías válidas)
  - Configuración de notificaciones (email, asunto, mensaje personalizado)
  - Perfil de inteligencia histórica (generado por el trainer)

Para activar/desactivar un cliente: cambiar `client.active` en su YAML.
"""
import copy
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from src.classifier.alias_clientes import id_canonico

logger = logging.getLogger(__name__)


# Claves que el parser conoce; cualquier otra viaja en `extra` y `a_dict()` la devuelve tal cual (el editor del
# portal y quien serialice un perfil no pierden campos que todavía no tienen dataclass).
_CLAVES_BRAND = ("id", "name", "products", "descripcion")
_CLAVES_CATEGORIA = ("id", "label", "items", "brands")


@dataclass
class Brand:
    id: str
    name: str
    products: list[str]
    # Texto breve de qué cubre la marca (opcional; lo usará el prompt).
    descripcion: str = ""
    extra: dict = field(default_factory=dict)

    def a_dict(self) -> dict:
        """La marca como en el perfil: claves conocidas + `extra`; `descripcion` solo si tiene texto."""
        salida = {**copy.deepcopy(self.extra), "id": self.id, "name": self.name, "products": list(self.products)}
        if self.descripcion:
            salida["descripcion"] = self.descripcion
        return salida


@dataclass
class CatalogCategory:
    id: str
    label: str
    items: list[str]
    brands: list[Brand] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def a_dict(self) -> dict:
        """La categoría como en el perfil: claves conocidas + `extra`."""
        return {**copy.deepcopy(self.extra), "id": self.id, "label": self.label, "items": list(self.items),
                "brands": [b.a_dict() for b in self.brands]}


@dataclass
class Catalog:
    categories: list[CatalogCategory]
    inclusion_signals: list[str]
    exclusion_signals: list[str]
    tag_taxonomy: list[str] = field(default_factory=list)


@dataclass
class Rules:
    context_note: str       # Contexto del mercado local para inyectar en el prompt
    categories: str         # Categorías de respuesta válidas (ej: "hardware, software, ...")


@dataclass
class Notification:
    email_to: list[str]
    system_admins_email: list[str]
    report_title: str
    subject_prefix: str
    greeting: str
    intro_message: str


@dataclass
class Intelligence:
    generated_at: Optional[str]
    processes_analyzed: int
    profile: Optional[str]

    @property
    def is_trained(self) -> bool:
        return bool(self.profile)


@dataclass
class ClientProfile:
    client_id: str
    client_name: str
    client_description: str
    active: bool
    catalog: Catalog
    rules: Rules
    notification: Notification
    intelligence: Intelligence
    relevance_threshold: float = 0.60
    confidence_threshold: float = 0.70
    # Portal de compras del que lee (`client.portal`); COMPRASAL si no lo declara.
    portal_id: str = "comprasal_sv"
    # Motivos de "Irrelevante" propios del cliente: [{id, label}]. Vacío = los
    # del catálogo por defecto (`src.feedback.catalogo`).
    feedback_motivos: list[dict] = field(default_factory=list)


# ── Fuente de los perfiles (archivo | sombra | adb) ───────────────────────────
#
# `CLIENTES_FUENTE` (src/persistencia/fuente.py): `archivo` (default) = comportamiento de siempre, sin tocar la
# ADB. `sombra` = manda el YAML, pero lee la ADB y loguea las diferencias. `adb` = manda `cliente_config`
# (última versión activa); si la ADB no responde, la caché local `data/cache/clientes/<id>.json`; si tampoco,
# el YAML horneado en la imagen. La ADB jamás hace fallar la corrida.

DATO = "CLIENTES"
_TTL_ADB = 60.0            # s: una corrida lee los perfiles varias veces; una sola consulta basta
_ESPERA_TRAS_FALLO = 60.0  # s: con la ADB caída no se reintenta en cada lectura
_memo_adb: dict[str, Any] = {"t": 0.0, "datos": None}
_diferencias_avisadas: set[tuple] = set()


def reiniciar_estado() -> None:
    """Vacía la memoria de la consulta a la ADB y los avisos de diferencias (tests, y tras escribir)."""
    _memo_adb.update(t=0.0, datos=None)
    _diferencias_avisadas.clear()


def hash_contenido(data: dict) -> str:
    """sha256 del contenido normalizado a JSON (claves ordenadas): lo que decide si hay versión nueva."""
    normal = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


def _cfg():
    try:
        from src.config import get_config
        return get_config()
    except Exception:  # noqa: BLE001 — sin config no hay ADB, pero la corrida sigue
        logger.debug("sin config para leer perfiles de la ADB", exc_info=True)
        return None


def _fuente() -> str:
    from src.persistencia.fuente import fuente
    return fuente(DATO)


def _adb_configurada(cfg) -> bool:
    return bool(cfg is not None and getattr(cfg, "adb_dsn", "") and getattr(cfg, "adb_password", ""))


def _a_dict(valor) -> Optional[dict]:
    # JSON_SERIALIZE sin RETURNING CLOB corta en 4000 caracteres (ORA-40478): se pide CLOB y llega como LOB.
    if hasattr(valor, "read"):
        valor = valor.read()
    if isinstance(valor, (bytes, bytearray)):
        valor = valor.decode("utf-8")
    if isinstance(valor, str):
        valor = json.loads(valor)
    return valor if isinstance(valor, dict) else None


_SQL_ACTIVAS = """
    SELECT cc.cliente_id, JSON_SERIALIZE(cc.contenido RETURNING CLOB)
      FROM cliente_config cc
     WHERE cc.activa = 1
       AND cc.version = (SELECT MAX(c2.version) FROM cliente_config c2
                          WHERE c2.cliente_id = cc.cliente_id AND c2.activa = 1)
"""


def _leer_adb(cfg) -> Optional[dict[str, dict]]:
    """{cliente_id canónico: contenido} de la última versión activa; None si la ADB no respondió. Nunca lanza."""
    ahora = time.monotonic()
    if _memo_adb["t"]:
        vigencia = _TTL_ADB if _memo_adb["datos"] is not None else _ESPERA_TRAS_FALLO
        if ahora - _memo_adb["t"] < vigencia:
            return _memo_adb["datos"]
    if not _adb_configurada(cfg):
        return None
    datos: Optional[dict[str, dict]]
    try:
        from src.memoria import db
        with db.connection(cfg) as conn:
            cur = conn.cursor()
            cur.execute(_SQL_ACTIVAS)
            filas = cur.fetchall()
            cur.close()
        datos = {}
        for cid, crudo in filas:
            contenido = _a_dict(crudo)
            if contenido is None:
                logger.warning("cliente_config de %s no es un objeto JSON; se ignora", cid)
                continue
            canon = id_canonico(cid)
            # Si conviven el id viejo y el canónico, gana el canónico.
            if canon in datos and cid != canon:
                continue
            datos[canon] = contenido
    except Exception as exc:  # noqa: BLE001 — la ADB nunca tumba la corrida
        logger.warning("perfiles: ADB no disponible (%s); se usa caché o YAML", type(exc).__name__)
        logger.debug("detalle del fallo de la ADB", exc_info=True)
        datos = None
    _memo_adb.update(t=ahora, datos=datos)
    return datos


def _dir_cache(cfg) -> Optional[str]:
    base = getattr(cfg, "data_dir", "") if cfg is not None else ""
    return os.path.join(base, "cache", "clientes") if base else None


def _escribir_cache(cfg, client_id: str, data: dict) -> None:
    carpeta = _dir_cache(cfg)
    if not carpeta:
        return
    try:
        from src.persistencia.cache import escribir_json
        escribir_json(os.path.join(carpeta, f"{client_id}.json"), data)
    except Exception:  # noqa: BLE001
        logger.warning("perfiles: no se pudo escribir la caché de %s", client_id, exc_info=True)


def _leer_cache(cfg, client_id: str) -> Optional[dict]:
    carpeta = _dir_cache(cfg)
    if not carpeta:
        return None
    from src.persistencia.cache import leer_json
    return _a_dict(leer_json(os.path.join(carpeta, f"{client_id}.json")))


def _leer_yaml(client_id: str, clients_dir: str) -> tuple[Optional[dict], str]:
    path = os.path.join(clients_dir, f"{client_id}.yaml")
    if not os.path.exists(path):
        return None, path
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f), path


def _comparar_sombra(client_id: str, de_yaml: dict, de_adb: Optional[dict]) -> None:
    if de_adb is None:
        if (de_yaml.get("client") or {}).get("active") is False:
            return  # un perfil inactivo a propósito (p. ej. multicomputos_cr) no tiene por qué estar en la ADB
        clave = (client_id, "sin-adb")
        if clave not in _diferencias_avisadas:
            _diferencias_avisadas.add(clave)
            logger.info("perfiles[sombra]: %s no está en cliente_config", client_id)
        return
    hy, ha = hash_contenido(de_yaml), hash_contenido(de_adb)
    if hy == ha:
        return
    clave = (client_id, hy, ha)
    if clave in _diferencias_avisadas:
        return
    _diferencias_avisadas.add(clave)
    campos = sorted(
        k for k in set(de_yaml) | set(de_adb)
        if hash_contenido({"v": de_yaml.get(k)}) != hash_contenido({"v": de_adb.get(k)})
    )
    logger.warning(
        "perfiles[sombra]: %s difiere entre YAML (%s) y ADB (%s); campos: %s",
        client_id, hy[:12], ha[:12], ", ".join(campos) or "?",
    )


def _datos_perfil(client_id: str, clients_dir: str, cfg=None) -> tuple[dict, str]:
    """Contenido crudo del perfil y su origen, según `CLIENTES_FUENTE`.

    Raises:
        FileNotFoundError: si ninguna fuente tiene el perfil.
    """
    client_id = id_canonico(client_id)
    modo = _fuente()
    if modo == "archivo":
        data, path = _leer_yaml(client_id, clients_dir)
        if data is None:
            raise FileNotFoundError(
                f"Perfil de cliente no encontrado: {path}\n"
                f"Crear el archivo clients/{client_id}.yaml antes de ejecutar el agente."
            )
        return data, path

    cfg = cfg if cfg is not None else _cfg()
    de_adb = (_leer_adb(cfg) or {}).get(client_id)
    if modo == "sombra":
        data, path = _leer_yaml(client_id, clients_dir)
        if data is not None:
            _comparar_sombra(client_id, data, de_adb)
            return data, path
        if de_adb is not None:  # sin YAML no hay con qué comparar; el dato de la ADB es mejor que nada
            return de_adb, f"adb:{client_id}"
        raise FileNotFoundError(f"Perfil de cliente no encontrado: {path}")

    # modo adb
    if de_adb is not None:
        _escribir_cache(cfg, client_id, de_adb)
        return de_adb, f"adb:{client_id}"
    en_cache = _leer_cache(cfg, client_id)
    if en_cache is not None:
        logger.info("perfiles: %s desde la caché local", client_id)
        return en_cache, f"cache:{client_id}"
    data, path = _leer_yaml(client_id, clients_dir)
    if data is None:
        raise FileNotFoundError(
            f"Perfil de cliente no encontrado en ADB, caché ni {path}\n"
            f"Crear el archivo clients/{client_id}.yaml antes de ejecutar el agente."
        )
    logger.info("perfiles: %s desde el YAML de la imagen", client_id)
    return data, path


def _ids_candidatos(clients_dir: str, cfg=None) -> list[str]:
    """Ids a cargar: los YAML del directorio y, con fuente `adb`, los de la ADB y la caché."""
    ids: set[str] = set()
    if os.path.isdir(clients_dir):
        ids.update(id_canonico(f[:-5]) for f in os.listdir(clients_dir) if f.endswith(".yaml"))
    if _fuente() == "adb":
        cfg = cfg if cfg is not None else _cfg()
        ids.update((_leer_adb(cfg) or {}).keys())
        carpeta = _dir_cache(cfg)
        if carpeta and os.path.isdir(carpeta):
            ids.update(id_canonico(f[:-5]) for f in os.listdir(carpeta) if f.endswith(".json"))
    return sorted(ids)


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_client_profile(client_id: str, clients_dir: str) -> ClientProfile:
    """
    Carga el perfil de `client_id` (ADB, caché o clients/{client_id}.yaml según `CLIENTES_FUENTE`).

    Raises:
        FileNotFoundError: si ninguna fuente lo tiene.
        KeyError: si faltan campos requeridos.
    """
    data, origen = _datos_perfil(client_id, clients_dir)
    return _parse_profile(data, origen)


def load_all_active_profiles(clients_dir: str) -> list[ClientProfile]:
    """
    Carga todos los perfiles (clients_dir y, con `CLIENTES_FUENTE=adb`, ADB y caché) y retorna únicamente
    los que tienen `client.active: true`.

    Retorna lista vacía (no falla) si no hay clientes activos.
    """
    if _fuente() == "archivo" and not os.path.isdir(clients_dir):
        raise FileNotFoundError(
            f"Directorio de clientes no encontrado: {clients_dir}\n"
            "Verificar que el volumen clients/ esté montado correctamente."
        )

    profiles = []
    for client_id in _ids_candidatos(clients_dir):
        try:
            profile = load_client_profile(client_id, clients_dir)
            if profile.active:
                profiles.append(profile)
        except Exception as exc:
            logger.warning("No se pudo cargar el perfil '%s': %s", client_id, exc)

    return profiles


# ── Escritura en la ADB (versionado append-only) ──────────────────────────────

def registrar_version(conn, client_id: str, contenido: dict, creado_por: str) -> Optional[int]:
    """Inserta una versión nueva de `cliente_config` solo si el hash difiere de la activa.

    Devuelve el número de versión creado, o None si el contenido no cambió. No hace commit: lo hace quien
    abrió la conexión (`db.connection`). Apaga la versión anterior en la misma transacción.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT version, JSON_SERIALIZE(contenido RETURNING CLOB) FROM cliente_config "
            "WHERE cliente_id = :c AND activa = 1 ORDER BY version DESC",
            c=client_id,
        )
        activa = cur.fetchone()
        if activa is not None:
            previo = _a_dict(activa[1])
            if previo is not None and hash_contenido(previo) == hash_contenido(contenido):
                return None
        cur.execute("SELECT NVL(MAX(version), 0) + 1 FROM cliente_config WHERE cliente_id = :c", c=client_id)
        (nueva,) = cur.fetchone()
        cur.execute("UPDATE cliente_config SET activa = 0 WHERE cliente_id = :c AND activa = 1", c=client_id)
        cur.execute(
            "INSERT INTO cliente_config (cliente_id, version, contenido, activa, creado_por) "
            "VALUES (:c, :v, JSON(:j), 1, :p)",
            c=client_id, v=int(nueva),
            j=json.dumps(contenido, ensure_ascii=False, default=str), p=creado_por,
        )
        return int(nueva)
    finally:
        cur.close()


def _con_inteligencia(data: dict, profile_text: str, processes_analyzed: int) -> dict:
    from datetime import datetime, timedelta, timezone

    data = dict(data)
    data["intelligence"] = {
        "generated_at": datetime.now(timezone(timedelta(hours=-6))).isoformat(),
        "processes_analyzed": processes_analyzed,
        "profile": profile_text,
    }
    return data


def _save_intelligence_yaml(client_id: str, clients_dir: str, profile_text: str, processes_analyzed: int) -> None:
    """Rama `archivo` (hasta la Fase F): actualiza SOLO la sección intelligence del YAML existente."""
    path = os.path.join(clients_dir, f"{client_id}.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data = _con_inteligencia(data, profile_text, processes_analyzed)
    with open(path, "w", encoding="utf-8") as f:
        header = (
            f"# Client Profile — {data['client']['name']}\n"
            f"# Para regenerar intelligence: python -m src.classifier.trainer --client {client_id}\n"
            "# NO editar la sección intelligence manualmente.\n\n"
        )
        f.write(header)
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def save_intelligence(client_id: str, clients_dir: str, profile_text: str, processes_analyzed: int) -> str:
    """Guarda el perfil de inteligencia y devuelve dónde quedó: `archivo`, `adb` o `cache`.

    Con `CLIENTES_FUENTE=archivo` actualiza el YAML (comportamiento de siempre). Con `sombra` o `adb`
    NO lee ni escribe el YAML: parte de la versión ACTIVA de la ADB (así no pisa ediciones hechas solo
    allá), reemplaza solo `intelligence` y registra una versión nueva (`trainer`). Si la ADB falla, la
    inteligencia generada no se pierde: queda en la caché local `data/cache/clientes/<id>.json`, que es
    la que lee el modo `adb` mientras la ADB no responda, y se avisa por log.
    """
    client_id = id_canonico(client_id)
    if _fuente() == "archivo":
        _save_intelligence_yaml(client_id, clients_dir, profile_text, processes_analyzed)
        return "archivo"

    cfg = _cfg()
    base: Optional[dict] = None
    try:
        if not _adb_configurada(cfg):
            raise RuntimeError("ADB no configurada")
        from src.memoria import db
        with db.connection(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT JSON_SERIALIZE(contenido RETURNING CLOB) FROM cliente_config "
                    "WHERE cliente_id = :c AND activa = 1 ORDER BY version DESC", c=client_id,
                )
                fila = cur.fetchone()
            finally:
                cur.close()
            base = _a_dict(fila[0]) if fila else None
            if base is None:
                raise LookupError(f"{client_id} no tiene versión activa en cliente_config")
            data = _con_inteligencia(base, profile_text, processes_analyzed)
            version = registrar_version(conn, client_id, data, "trainer")
        logger.info("perfiles: inteligencia de %s → cliente_config %s", client_id,
                    f"v{version}" if version else "sin cambios")
        reiniciar_estado()
        _escribir_cache(cfg, client_id, data)
        return "adb"
    except Exception as exc:  # noqa: BLE001 — la inteligencia generada (20 min de scrape + LLM) no se pierde
        logger.warning("perfiles: no se pudo registrar la inteligencia de %s en la ADB (%s); "
                       "queda en la caché local", client_id, type(exc).__name__)
        logger.debug("detalle", exc_info=True)
    if base is None:  # sin ADB: lo último conocido localmente (caché; el YAML solo se lee, nunca se escribe)
        base = _leer_cache(cfg, client_id)
        if base is None:
            base, _ = _leer_yaml(client_id, clients_dir)
    if base is None:
        raise FileNotFoundError(
            f"No hay perfil de {client_id} (ADB, caché ni YAML) donde guardar la inteligencia generada"
        )
    _escribir_cache(cfg, client_id, _con_inteligencia(base, profile_text, processes_analyzed))
    return "cache"


# ── Parser interno ────────────────────────────────────────────────────────────

def _parse_profile(data: dict, path: str) -> ClientProfile:
    client_data = data["client"]
    catalog_data = data["catalog"]
    rules_data = data.get("rules", {})
    notif_data = data.get("notification", {})
    intel_data = data.get("intelligence", {})
    classification_data = data.get("classification", {})

    categories = [
        CatalogCategory(
            id=cat["id"],
            label=cat["label"],
            items=cat.get("items", []),
            brands=[
                Brand(
                    id=b["id"],
                    name=b["name"],
                    products=b.get("products", []),
                    descripcion=str(b.get("descripcion") or "").strip(),
                    extra={k: copy.deepcopy(v) for k, v in b.items() if k not in _CLAVES_BRAND},
                )
                for b in cat.get("brands", [])
            ],
            extra={k: copy.deepcopy(v) for k, v in cat.items() if k not in _CLAVES_CATEGORIA},
        )
        for cat in catalog_data.get("categories", [])
    ]

    catalog = Catalog(
        categories=categories,
        inclusion_signals=catalog_data.get("inclusion_signals", []),
        exclusion_signals=catalog_data.get("exclusion_signals", []),
        tag_taxonomy=catalog_data.get("tag_taxonomy", []),
    )

    rules = Rules(
        context_note=rules_data.get("context_note", "").strip(),
        categories=rules_data.get("categories", "hardware, software, servicios_ti, ciberseguridad, cableado, redes, audiovisual, otro"),
    )

    raw_email_to = notif_data.get("email_to", [])
    if isinstance(raw_email_to, str):
        raw_email_to = [raw_email_to]

    raw_system_admins = notif_data.get("system_admins_email", [])
    if isinstance(raw_system_admins, str):
        raw_system_admins = [raw_system_admins]

    notification = Notification(
        email_to=raw_email_to,
        system_admins_email=raw_system_admins,
        report_title=notif_data.get("report_title", f"Reporte — {client_data['name']}"),
        subject_prefix=notif_data.get("subject_prefix", "[Monitor]"),
        greeting=notif_data.get("greeting", "Estimado cliente,"),
        intro_message=notif_data.get("intro_message", "").strip(),
    )

    intelligence = Intelligence(
        generated_at=intel_data.get("generated_at"),
        processes_analyzed=intel_data.get("processes_analyzed", 0),
        profile=intel_data.get("profile"),
    )

    return ClientProfile(
        client_id=client_data["id"],
        client_name=client_data["name"],
        client_description=client_data.get("description", ""),
        active=bool(client_data.get("active", True)),
        portal_id=str(client_data.get("portal") or "comprasal_sv").strip(),
        catalog=catalog,
        rules=rules,
        notification=notification,
        intelligence=intelligence,
        relevance_threshold=float(classification_data.get("relevance_threshold", 0.60)),
        confidence_threshold=float(classification_data.get("confidence_threshold", 0.70)),
        feedback_motivos=list((data.get("feedback") or {}).get("motivos") or []),
    )
