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
class UsoModelo:
    """Consumo real de tokens que reportó la API para las llamadas a un modelo.

    Se registra POR MODELO y no agregado porque Haiku y Sonnet tienen precios
    distintos ($1/$5 vs $3/$15 por MTok): un contador único de tokens no se
    puede traducir a dinero. `llamadas` > 1 cuando hubo reintento.
    """
    modelo: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    llamadas: int = 0

    def acumular(self, usage) -> None:
        """Suma el bloque `usage` de una respuesta de la API (tolera None)."""
        if usage is None:
            return
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.llamadas += 1


# Vocabulario CERRADO de `ResultadoAnalisis.motivo_bloqueo`. Pequeño a
# propósito: lo consume el armado del correo (grupo «No competibles») y una
# etiqueta nueva tiene que ser una decisión, no algo que el código invente.
MOTIVO_PROVEEDOR_UNICO = "proveedor_unico"
MOTIVO_MARCA_ESPECIFICA = "marca_especifica"
MOTIVO_DIRECTA_NOMINADA = "contratacion_directa_nominada"
MOTIVOS_BLOQUEO: dict[str, str] = {
    MOTIVO_PROVEEDOR_UNICO: "Contratación directa con proveedor único",
    MOTIVO_MARCA_ESPECIFICA: "Marca específica exigida, fuera del portafolio",
    MOTIVO_DIRECTA_NOMINADA: "Contratación directa con un contratista ya nombrado",
}


@dataclass
class ResultadoAnalisis:
    """Resultado del drill-down de un pliego (Fase 2, MVP Track A).

    `estado` sigue la máquina de estados de docs/arquitectura/modelo-de-datos.md §4:
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
    # Monto estimado/presupuesto: COMPRASAL no lo publica en ningún endpoint
    # (verificado exhaustivamente contra la API real, pública y autenticada —
    # ver la investigación de monto), así que la única fuente es que el propio
    # pliego lo declare y el LLM lo extraiga aquí. `None` = el pliego no lo
    # declara explícitamente (nunca se estima ni se infiere). La cláusula que
    # lo respalda viaja en `anexos["monto_estimado_clausula"]`, no como campo
    # aparte, para no tocar el esquema de `licitaciones_analisis` (`anexos` ya
    # es JSON de paso libre hacia la ADB).
    monto_estimado: Optional[float] = None
    modelo_analisis: str = ""
    error: str = ""
    generado_en: str = ""                   # ISO 8601
    # ── Instrumentación de costo ────────────────────────────────────────────
    # Sin estos campos la heurística de cuota (n_paginas * 2000 + 5000) no se
    # puede calibrar: no habría con qué contrastar lo estimado contra lo real.
    n_paginas: int = 0                      # 0 si el PDF no se pudo abrir
    tamano_bytes: int = 0
    uso_triaje: Optional["UsoModelo"] = None
    uso_analisis: Optional["UsoModelo"] = None
    # Track B · memoria institucional: pliegos históricos similares.
    # Default vacío → retrocompatible con Track A (que nunca lo puebla).
    precedentes: list["Precedente"] = field(default_factory=list)
    # ── Competibilidad (P4 del rediseño del correo) ─────────────────────────
    # ¿Puede el cliente competir en este proceso, o está cerrado de antemano
    # (proveedor único nombrado, marca exigida fuera del portafolio)? Lo deriva
    # de forma DETERMINISTA `drilldown.analysis.evaluar_competibilidad` sobre
    # la matriz ya extraída; no es una segunda opinión del LLM.
    #   True  → no se detectó ningún bloqueo.
    #   False → bloqueado; `motivo_bloqueo` dice por qué (vocabulario cerrado en
    #           `MOTIVOS_BLOQUEO`).
    #   None  → desconocido: no se analizó (descartes, errores, NOTIFICADO) o
    #           el bloqueo depende de un dato que no se tuvo (el portafolio).
    # La cita que lo respalda viaja en `anexos["competibilidad"]`, igual que
    # `monto_estimado_clausula`, para no tocar el esquema de la ADB.
    competible: Optional[bool] = None
    motivo_bloqueo: str = ""

    @property
    def descartado_por_triaje(self) -> bool:
        """El triaje dijo NO-GO: el pliego no se analizó por no ser del cliente.

        Se mira el estado Y el veredicto: `DESCARTADO_TRIAJE` es el desenlace
        que escribe el pipeline, y `triaje == "NO-GO"` cubre filas reconstruidas
        que solo conservaron el veredicto. NO cambia `es_relevante` del
        proceso: qué hacer con esto en el correo lo decide quien lo arma.
        """
        return self.estado == "DESCARTADO_TRIAJE" or self.triaje == "NO-GO"

    @property
    def no_competible(self) -> bool:
        """True solo si hay un bloqueo DETECTADO (`competible is False`)."""
        return self.competible is False

    @classmethod
    def desde_dict(cls, d: dict) -> "ResultadoAnalisis":
        """Inverso tolerante de `dataclasses.asdict` (una línea de `*.analisis.jsonl`).

        Las claves faltantes toman el default del dataclass: las líneas
        anteriores a P4 no traen `competible`/`motivo_bloqueo` y quedan en
        None/"" (desconocido), que es exactamente lo que significan. Las claves
        desconocidas se ignoran, para que una línea escrita por una versión
        más nueva no tumbe a una más vieja.
        """
        d = d or {}

        def _uso(u):
            if not isinstance(u, dict):
                return None
            return UsoModelo(
                modelo=str(u.get("modelo") or ""),
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                llamadas=int(u.get("llamadas") or 0),
            )

        matriz = None
        m = d.get("matriz")
        if isinstance(m, dict):
            matriz = MatrizRiesgos(
                items=[
                    RiesgoItem(
                        categoria=str(it.get("categoria") or ""),
                        descripcion=str(it.get("descripcion") or ""),
                        severidad=str(it.get("severidad") or ""),
                        clausula_ref=str(it.get("clausula_ref") or ""),
                    )
                    for it in (m.get("items") or [])
                    if isinstance(it, dict)
                ],
                resumen=str(m.get("resumen") or ""),
            )

        competible = d.get("competible")
        if not isinstance(competible, bool):
            competible = None

        return cls(
            proceso_uuid=str(d.get("proceso_uuid") or ""),
            estado=str(d.get("estado") or ""),
            formato=str(d.get("formato") or ""),
            triaje=str(d.get("triaje") or ""),
            triaje_confianza=float(d.get("triaje_confianza") or 0.0),
            matriz=matriz,
            anexos=d.get("anexos") if isinstance(d.get("anexos"), dict) else {},
            monto_estimado=d.get("monto_estimado"),
            modelo_analisis=str(d.get("modelo_analisis") or ""),
            error=str(d.get("error") or ""),
            generado_en=str(d.get("generado_en") or ""),
            n_paginas=int(d.get("n_paginas") or 0),
            tamano_bytes=int(d.get("tamano_bytes") or 0),
            uso_triaje=_uso(d.get("uso_triaje")),
            uso_analisis=_uso(d.get("uso_analisis")),
            precedentes=[
                Precedente(
                    proceso_id=str(p.get("proceso_id") or ""),
                    institucion=str(p.get("institucion") or ""),
                    titulo=str(p.get("titulo") or ""),
                    fecha_pub=str(p.get("fecha_pub") or ""),
                    url_comprasal=str(p.get("url_comprasal") or ""),
                    extracto=str(p.get("extracto") or ""),
                    distancia=float(p.get("distancia") or 0.0),
                )
                for p in (d.get("precedentes") or [])
                if isinstance(p, dict)
            ],
            competible=competible,
            motivo_bloqueo=str(d.get("motivo_bloqueo") or ""),
        )


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
