"""Modelos de datos para los procesos de compra extraídos de COMPRASAL."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Proceso:
    institucion: str
    descripcion: str
    fecha_publicacion: datetime
    url_directa: str
    numero_referencia: str = ""
    forma_contratacion: str = ""
    tipo_actividad: str = ""
    monto: Optional[float] = None
    fecha_limite: Optional[datetime] = None


@dataclass
class MarcaRecomendada:
    marca: str = ""
    razon: str = ""
    role: str = "Alternativa"  # "Recomendada" o "Alternativa"


@dataclass
class RiesgoItem:
    """Un riesgo detectado en el pliego durante el análisis profundo (Fase 2)."""
    categoria: str          # "financiero" | "tecnico" | "legal" | "plazos"
    descripcion: str
    severidad: str          # "alta" | "media" | "baja"
    clausula_ref: str = ""  # cita textual de la cláusula del pliego


@dataclass
class MatrizRiesgos:
    """Matriz de riesgos financieros y técnicos extraída de un pliego (Fase 2)."""
    items: list["RiesgoItem"] = field(default_factory=list)
    resumen: str = ""


@dataclass
class Precedente:
    """Pliego histórico semánticamente similar al actual (Fase 2, Track B).

    Es el valor diferencial de la memoria institucional: responde "¿ya vimos
    algo así antes?" cruzando el corpus completo, no un solo documento.
    """
    proceso_id: str
    institucion: str = ""
    titulo: str = ""
    fecha_pub: str = ""          # ISO 8601
    url_comprasal: str = ""
    extracto: str = ""           # fragmento del pliego histórico que hizo match
    distancia: float = 0.0       # menor = más similar (COSINE)


@dataclass
class ResultadoAnalisis:
    """Resultado del drill-down de un pliego (Fase 2, MVP Track A).

    `estado` sigue la máquina de estados de inventario_diagramas_amalia.md §4:
      DESCUBIERTO, DESCARGADO, DESCARTADO_TAMANO, DESCARTADO_REGEX,
      DESCARTADO_TRIAJE, REQUIERE_REVISION_MANUAL, ANALIZADO, NOTIFICADO,
      ERROR_DESCARGA, ERROR_PARSEO, ERROR_LLM.
    """
    proceso_uuid: str                       # numero_referencia del Proceso
    estado: str
    formato: str = ""                       # "NATIVO" | "ESCANEADO"
    triaje: str = ""                        # "GO" | "NO-GO"
    triaje_confianza: float = 0.0
    matriz: Optional["MatrizRiesgos"] = None
    anexos: dict = field(default_factory=dict)
    modelo_analisis: str = ""
    error: str = ""
    generado_en: str = ""                   # ISO 8601
    # Track B · memoria institucional: pliegos históricos similares.
    # Default vacío → retrocompatible con Track A (que nunca lo puebla).
    precedentes: list["Precedente"] = field(default_factory=list)


@dataclass
class ProcesoClasificado(Proceso):
    es_relevante: bool = False
    categoria_detectada: str = ""
    confianza: float = 0.0          # Backward compat: se setea igual a factibilidad
    razon: str = ""
    # Scoring multidimensional (v2)
    match_tecnico: float = 0.0      # 0.0–1.0: requerimientos técnicos vs marcas/productos
    match_categoria: float = 0.0    # 0.0–1.0: categoría del proceso vs áreas de negocio
    factibilidad: float = 0.0       # 0.0–1.0: viabilidad comercial general
    tags: list[str] = field(default_factory=list)
    marcas_recomendadas: list[MarcaRecomendada] = field(default_factory=list)

    @classmethod
    def from_proceso(cls, proceso: Proceso) -> "ProcesoClasificado":
        return cls(
            institucion=proceso.institucion,
            descripcion=proceso.descripcion,
            fecha_publicacion=proceso.fecha_publicacion,
            url_directa=proceso.url_directa,
            numero_referencia=proceso.numero_referencia,
            forma_contratacion=proceso.forma_contratacion,
            monto=proceso.monto,
            fecha_limite=proceso.fecha_limite,
        )
