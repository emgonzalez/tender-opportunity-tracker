"""src.feedback.catalogo — los motivos de "Irrelevante" que ofrece el formulario.

Se guardan los CÓDIGOS, no las etiquetas: la etiqueta se puede reescribir o
traducir sin invalidar el histórico. Cada cliente puede definir los suyos en
`clients/<id>.yaml`:

    feedback:
      motivos:
        - {id: fuera_de_rubro, label: "No es de nuestro rubro"}

Un cliente que define `motivos` reemplaza el catálogo entero (no se mezcla): así
puede quitar los que no le sirven.
"""
from __future__ import annotations

MOTIVOS_DEFECTO: tuple[dict, ...] = (
    {"id": "fuera_de_rubro", "label": "Fuera de nuestro rubro"},
    {"id": "monto_muy_bajo", "label": "El monto es demasiado bajo"},
    {"id": "monto_muy_alto", "label": "El monto es demasiado alto para nosotros"},
    {"id": "plazo_insuficiente", "label": "El plazo no alcanza para ofertar"},
    {"id": "requisitos_no_cumplibles", "label": "Exige requisitos que no podemos cumplir"},
    {"id": "institucion_no_interesa", "label": "La institución no nos interesa"},
    {"id": "marca_no_representada", "label": "Piden una marca que no representamos"},
    {"id": "ya_conocido_o_duplicado", "label": "Ya lo conocíamos o está duplicado"},
    {"id": "otro", "label": "Otro motivo (explicar abajo)"},
)

COMENTARIO_MAX = 1000  # caracteres; la columna es VARCHAR2(4000) bytes


def motivos_de(perfil) -> list[dict]:
    """Motivos vigentes para un cliente: los suyos si definió, si no los de por defecto."""
    propios = getattr(perfil, "feedback_motivos", None) or []
    validos = [
        {"id": str(m["id"]), "label": str(m["label"])}
        for m in propios
        if isinstance(m, dict) and m.get("id") and m.get("label")
    ]
    return validos or [dict(m) for m in MOTIVOS_DEFECTO]


def validar_motivos(codigos: list[str], catalogo: list[dict]) -> list[str]:
    """Deja solo los códigos que existen en el catálogo, sin repetidos y en orden."""
    permitidos = {m["id"] for m in catalogo}
    vistos: list[str] = []
    for c in codigos:
        if c in permitidos and c not in vistos:
            vistos.append(c)
    return vistos


def limpiar_comentario(texto: str | None) -> str:
    """Recorta espacios y largo. No escapa HTML: eso lo hace Jinja al mostrarlo."""
    return (texto or "").strip()[:COMENTARIO_MAX]
