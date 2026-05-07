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


@dataclass
class MarcaRecomendada:
    marca: str = ""
    razon: str = ""


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
        )
