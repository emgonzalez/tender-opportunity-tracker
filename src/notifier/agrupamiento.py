"""src.notifier.agrupamiento — la regla de agrupamiento del correo, sin SMTP, Jinja ni Config.

Vive aparte de `email_sender.py` para que el correo y el portal (vista de país, E11)
agrupen con UNA SOLA regla: el correo la importa de aquí y el portal también.

REGLA DE AGRUPAMIENTO (explícita y testeable; `grupo_de_valores` la implementa). Sobre
las relevantes, en este orden de precedencia (la primera que aplica gana):
  1. NO COMPETIBLES  ← `competible is False` (bloqueo DETECTADO por el drill-down:
     proveedor único, marca exigida fuera del portafolio, directa nominada).
  2. BAJA PRIORIDAD  ← triaje NO-GO o match < UMBRAL_MATCH_DECIDIR.
  3. PARA DECIDIR    ← el resto. `competible is None` (desconocido, o sin análisis)
     NO excluye.

URGENCIA UNIFICADA: rojo hasta DIAS_ESTA_SEMANA días (incluido hoy), ámbar hasta
DIAS_PRONTO, verde después. Sin fecha o con el plazo vencido no hay urgencia.
"""
from __future__ import annotations

from datetime import datetime

from src.scraper.models import ProcesoClasificado, ResultadoAnalisis

UMBRAL_MATCH_DECIDIR = 80
DIAS_ESTA_SEMANA = 7
DIAS_PRONTO = 14

GRUPO_DECIDIR = "decidir"
GRUPO_NO_COMPETIBLE = "no_competible"
GRUPO_BAJA = "baja"

URGENCIA_ROJO = "rojo"
URGENCIA_AMBAR = "ambar"
URGENCIA_VERDE = "verde"

DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_LARGOS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_CORTOS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                "septiembre", "octubre", "noviembre", "diciembre"]


def _num(valor) -> float:
    """Score como float; `None` (o basura) cuenta como 0, igual que el default del dataclass."""
    try:
        return float(valor) if valor is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def match_de_valores(match_tecnico, match_categoria, factibilidad) -> int:
    """Match 0-100: promedio ponderado (0.4 técnico, 0.3 categoría, 0.3 factibilidad)."""
    return round(
        (_num(match_tecnico) * 0.4 + _num(match_categoria) * 0.3 + _num(factibilidad) * 0.3) * 100
    )


def match_de(proc: ProcesoClasificado) -> int:
    """Match 0-100 de un proceso clasificado (ver `match_de_valores`)."""
    return match_de_valores(proc.match_tecnico, proc.match_categoria, proc.factibilidad)


def grupo_de_valores(match: int, competible: bool | None, descartado_por_triaje: bool) -> str:
    """Grupo de una relevante a partir de valores sueltos (lo que tiene el portal)."""
    if competible is False:
        return GRUPO_NO_COMPETIBLE
    if descartado_por_triaje or match < UMBRAL_MATCH_DECIDIR:
        return GRUPO_BAJA
    return GRUPO_DECIDIR


def grupo_de(match: int, ra: ResultadoAnalisis | None) -> str:
    """Grupo del correo de una relevante; delega en `grupo_de_valores`."""
    if ra is None:
        return grupo_de_valores(match, None, False)
    return grupo_de_valores(match, ra.competible, ra.descartado_por_triaje)


def dias_hasta(limite: datetime | None, hoy: datetime) -> int | None:
    """Días de calendario de `hoy` al cierre (negativo si ya venció).

    Si ambos traen zona, el cierre se lleva a la zona de `hoy` antes de tomar la fecha.
    """
    if limite is None:
        return None
    if limite.tzinfo is not None and hoy.tzinfo is not None:
        limite = limite.astimezone(hoy.tzinfo)
    return (limite.date() - hoy.date()).days


def urgencia(dias_restantes: int | None) -> str | None:
    """'rojo' | 'ambar' | 'verde' según los días al cierre; None sin fecha o vencido."""
    if dias_restantes is None or dias_restantes < 0:
        return None
    if dias_restantes <= DIAS_ESTA_SEMANA:
        return URGENCIA_ROJO
    if dias_restantes <= DIAS_PRONTO:
        return URGENCIA_AMBAR
    return URGENCIA_VERDE


def fecha_corta(d: datetime) -> str:
    """«jue 22 oct»."""
    return f"{DIAS_CORTOS[d.weekday()]} {d.day} {MESES_CORTOS[d.month - 1]}"


def texto_cierre(limite: datetime | None, dias: int | None) -> str:
    """El texto del cierre; lleva el significado (el color solo lo repite)."""
    if limite is None or dias is None:
        return "Cierre: sin fecha registrada"
    fecha = fecha_corta(limite)
    if dias < 0:
        return f"Plazo vencido ({fecha})"
    if dias == 0:
        return f"Cierra hoy, {fecha}"
    return f"Cierra {fecha} · {'1 día' if dias == 1 else f'{dias} días'}"


def cierre(limite: datetime | None, dias: int | None) -> tuple[str, str]:
    """(texto, urgencia del correo): el correo solo pinta rojo y ámbar; lo demás va en ""."""
    if limite is None or dias is None:
        return texto_cierre(limite, dias), ""
    color = urgencia(dias)
    return texto_cierre(limite, dias), color if color in (URGENCIA_ROJO, URGENCIA_AMBAR) else ""
