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
import os
from dataclasses import dataclass, field
from typing import Optional



import yaml


@dataclass
class Brand:
    id: str
    name: str
    products: list[str]


@dataclass
class CatalogCategory:
    id: str
    label: str
    items: list[str]
    brands: list[Brand] = field(default_factory=list)


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


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_client_profile(client_id: str, clients_dir: str) -> ClientProfile:
    """
    Carga clients/{client_id}.yaml y lo parsea en un ClientProfile.

    Raises:
        FileNotFoundError: si el archivo no existe.
        KeyError: si faltan campos requeridos.
    """
    path = os.path.join(clients_dir, f"{client_id}.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Perfil de cliente no encontrado: {path}\n"
            f"Crear el archivo clients/{client_id}.yaml antes de ejecutar el agente."
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return _parse_profile(data, path)


def load_all_active_profiles(clients_dir: str) -> list[ClientProfile]:
    """
    Escanea clients_dir, carga todos los YAMLs y retorna únicamente
    los perfiles con `client.active: true`.

    Retorna lista vacía (no falla) si no hay clientes activos.
    """
    if not os.path.isdir(clients_dir):
        raise FileNotFoundError(
            f"Directorio de clientes no encontrado: {clients_dir}\n"
            "Verificar que el volumen clients/ esté montado correctamente."
        )

    profiles = []
    for filename in sorted(os.listdir(clients_dir)):
        if not filename.endswith(".yaml"):
            continue
        client_id = filename[:-5]
        try:
            profile = load_client_profile(client_id, clients_dir)
            if profile.active:
                profiles.append(profile)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "No se pudo cargar el perfil '%s': %s", filename, exc
            )

    return profiles


def save_intelligence(client_id: str, clients_dir: str, profile_text: str, processes_analyzed: int) -> None:
    """
    Actualiza SOLO la sección intelligence del YAML existente.
    Preserva todo el contenido del catálogo (editado manualmente).
    """
    from datetime import datetime, timezone, timedelta

    path = os.path.join(clients_dir, f"{client_id}.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    tz_sv = timezone(timedelta(hours=-6))
    data["intelligence"] = {
        "generated_at": datetime.now(tz_sv).isoformat(),
        "processes_analyzed": processes_analyzed,
        "profile": profile_text,
    }

    with open(path, "w", encoding="utf-8") as f:
        header = (
            f"# Client Profile — {data['client']['name']}\n"
            f"# Para regenerar intelligence: python -m src.classifier.trainer --client {client_id}\n"
            "# NO editar la sección intelligence manualmente.\n\n"
        )
        f.write(header)
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


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
                )
                for b in cat.get("brands", [])
            ],
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
        greeting=notif_data.get("greeting", f"Estimado cliente,"),
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
        catalog=catalog,
        rules=rules,
        notification=notification,
        intelligence=intelligence,
        relevance_threshold=float(classification_data.get("relevance_threshold", 0.60)),
        confidence_threshold=float(classification_data.get("confidence_threshold", 0.70)),
    )
