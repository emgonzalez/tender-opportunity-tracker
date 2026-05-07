"""
Construcción dinámica del system prompt a partir de un ClientProfile.

Todo el contenido específico del cliente (catálogo, marcas, señales de inclusión/exclusión,
nota de contexto, categorías válidas, taxonomía de tags) proviene del YAML del cliente.
Este módulo no tiene texto hardcodeado de ningún cliente.
"""
from src.classifier.client_profile import ClientProfile


def get_system_prompt(profile: ClientProfile, app_name: str = "Monitor") -> str:
    catalog_text = _format_catalog(profile)
    brands_text = _format_brands(profile)
    tag_taxonomy_text = _format_tag_taxonomy(profile)
    inclusion_text = _format_signals(profile.catalog.inclusion_signals)
    exclusion_text = _format_signals(profile.catalog.exclusion_signals)
    intelligence_section = _format_intelligence(profile)

    return f"""Eres {app_name}, el agente de detección de oportunidades de {profile.client_name}, \
{profile.client_description}.

Tu tarea es analizar procesos de compra pública del portal COMPRASAL (El Salvador) y determinar si \
cada uno es una oportunidad de negocio relevante para {profile.client_name}.

## Portafolio de {profile.client_name}

{catalog_text}

## Marcas y partnerships

{brands_text}

## Señales de inclusión (el proceso SÍ es relevante si contiene)

{inclusion_text}

## Señales de exclusión (el proceso NO es relevante si contiene, salvo que también haya señales de inclusión)

{exclusion_text}

## Contexto del mercado

{profile.rules.context_note}
{intelligence_section}
## Escala de puntuación multi-dimensional

Para cada proceso, proporciona tres sub-scores entre 0.0 y 1.0:

1. **match_tecnico**: ¿Qué tan bien coinciden los requerimientos técnicos del proceso con las marcas \
y productos del portafolio? (1.0 = match directo con un producto/marca específica; 0.0 = sin relación)

2. **match_categoria**: ¿Qué tan bien encaja la categoría del proceso con las áreas de negocio del \
portafolio? (1.0 = encaja perfectamente en una categoría; 0.0 = categoría completamente ajena)

3. **factibilidad**: Puntuación general de viabilidad comercial combinando match técnico, match de \
categoría, capacidad competitiva y probabilidad de éxito. (1.0 = oportunidad ideal; 0.0 = sin viabilidad)

Umbrales de decisión basados en factibilidad:
- 0.85-1.0: Claramente relevante (match directo con el portafolio y marcas)
- 0.70-0.84: Probable relevante (fuerte afinidad con el portafolio)
- 0.50-0.69: Dudoso, requiere revisión humana
- 0.00-0.49: No relevante

## Taxonomía de tags válidos

{tag_taxonomy_text}

Asigna entre 1 y 4 tags de esta taxonomía a cada proceso. Los tags deben reflejar las sub-áreas \
técnicas específicas del proceso. Solo usa tags de esta lista.

## Recomendación de marcas

Para cada proceso relevante (es_relevante=true), sugiere qué marca(s) del portafolio de \
{profile.client_name} son las más adecuadas para ofertar, con una razón breve. \
Para procesos no relevantes, marcas_recomendadas debe ser una lista vacía.

## Categorías de respuesta válidas

{profile.rules.categories}

## Instrucciones de respuesta

Responde ÚNICAMENTE con un JSON válido siguiendo exactamente este esquema:
{{
  "clasificaciones": [
    {{
      "indice": 0,
      "es_relevante": true,
      "categoria_detectada": "infraestructura",
      "match_tecnico": 0.92,
      "match_categoria": 0.95,
      "factibilidad": 0.93,
      "tags": ["Servidores", "Almacenamiento", "Flash"],
      "marcas_recomendadas": [
        {{"marca": "HPE", "razon": "Requiere servidores rack de alto rendimiento"}},
        {{"marca": "PureStorage", "razon": "Almacenamiento all-flash mencionado explícitamente"}}
      ],
      "razon": "Licitación para infraestructura de data center con servidores y almacenamiento."
    }}
  ]
}}

No incluyas texto fuera del JSON. No uses markdown. El campo "indice" corresponde \
al índice (0-based) del proceso en el array de entrada."""


def get_user_prompt(procesos: list[dict]) -> str:
    """Genera el prompt de usuario con la lista de procesos a clasificar."""
    lines = ["Clasifica los siguientes procesos de compra pública:\n"]
    for i, p in enumerate(procesos):
        lines.append(
            f"[{i}] Referencia: {p.get('referencia', 'N/D')}\n"
            f"    Institución: {p['institucion']}\n"
            f"    Descripción: {p['descripcion']}\n"
            f"    Método: {p.get('metodo', 'N/D')}\n"
            f"    Fecha: {p['fecha']}\n"
        )
    lines.append("\nResponde con el JSON de clasificaciones.")
    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_catalog(profile: ClientProfile) -> str:
    lines = []
    for cat in profile.catalog.categories:
        lines.append(f"**{cat.label}**")
        for item in cat.items:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_brands(profile: ClientProfile) -> str:
    lines = []
    for cat in profile.catalog.categories:
        for brand in cat.brands:
            products_str = ", ".join(brand.products)
            lines.append(f"- **{brand.name}** ({cat.label}): {products_str}")
    return "\n".join(lines) if lines else "(sin marcas definidas)"


def _format_tag_taxonomy(profile: ClientProfile) -> str:
    tags = profile.catalog.tag_taxonomy
    if not tags:
        return "(sin taxonomía definida)"
    return ", ".join(tags)


def _format_signals(signals: list[str]) -> str:
    if not signals:
        return "(ninguna definida)"
    return "\n".join(f"- {s}" for s in signals)


def _format_intelligence(profile: ClientProfile) -> str:
    """Sección de inteligencia histórica. Vacía si el trainer no se ha ejecutado."""
    intel = profile.intelligence
    if not intel.is_trained:
        return "\n"
    return f"""

## Inteligencia histórica ({intel.processes_analyzed} procesos participados)

La siguiente síntesis fue generada analizando el historial de participación de \
{profile.client_name} en COMPRASAL. Úsala para calibrar tu clasificación, \
NO como lista de exclusión rígida.

{intel.profile}

"""
