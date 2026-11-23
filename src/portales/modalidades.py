"""Modalidades de contratación por portal y su competibilidad: fuente única (D11).

`taxonomia_modalidad` de la ADB es un espejo para analytics que se genera desde `MODALIDADES`; la
competibilidad vive solo acá porque la corrida no puede depender de la ADB. Diseño en
`docs/arquitectura/portales.md` §9.

- **SICOP** publica un código (`proceType`, catálogo BD01): la tabla de abajo decide.
- **COMPRASAL** publica solo texto (`forma_contratacion`): se conserva **exactamente** la regla que vivía
  en `drilldown/analysis.py` (`"directa"` en la forma normalizada ⇒ condicional; forma no vacía ⇒
  competible; vacía ⇒ condicional).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

COMPETIBLE = "competible"
CONDICIONAL = "condicional"        # que decida el pliego (reglas de texto de `evaluar_competibilidad`)
NO_COMPETIBLE = "no_competible"    # excepción, remate o equivalente: no se descarga pliego

# Vocabulario del CHECK de `taxonomia_modalidad.modalidad_norm` (db/schema_v2.sql).
NORMAS = (
    "LICITACION_ABIERTA", "COMPARACION_PRECIOS", "SUBASTA_INVERSA",
    "CONTRATACION_DIRECTA", "CONVENIO_MARCO", "CONSULTORIA", "OTRO",
)


@dataclass(frozen=True)
class Modalidad:
    portal_id: str
    codigo: str          # SICOP: proceType; COMPRASAL: "" (publica texto, no código)
    nombre: str          # texto tal como lo publica el portal (= proceso.modalidad_raw en v2)
    norm: str            # vocabulario CHECK de taxonomia_modalidad (`NORMAS`)
    competibilidad: str


def _sicop(codigo: str, nombre: str, norm: str, competibilidad: str) -> Modalidad:
    return Modalidad("sicop_cr", codigo, nombre, norm, competibilidad)


# Catálogo BD01 de SICOP (nombres tal como los devuelve `coCommonCd/list`, ver
# tests/fixtures/sicop/coCommonCd_list_BD01.json) y la decisión de competibilidad de §9.1 / §12 (A-3).
MODALIDADES: tuple[Modalidad, ...] = (
    # Ley 9986
    _sicop("LY", "LICITACIÓN MAYOR", "LICITACION_ABIERTA", COMPETIBLE),
    _sicop("LE", "LICITACIÓN MENOR", "LICITACION_ABIERTA", COMPETIBLE),
    _sicop("LD", "LICITACIÓN REDUCIDA", "LICITACION_ABIERTA", COMPETIBLE),
    _sicop("SE", "SUBASTA INVERSA ELECTRÓNICA", "SUBASTA_INVERSA", COMPETIBLE),
    _sicop("PE", "PROCEDIMIENTOS ESPECIALES", "CONVENIO_MARCO", CONDICIONAL),
    _sicop("PX", "PROCEDIMIENTO POR EXCEPCIÓN", "CONTRATACION_DIRECTA", NO_COMPETIBLE),
    # Ley 7494 (histórico)
    _sicop("LN", "LICITACIÓN PÚBLICA NACIONAL", "LICITACION_ABIERTA", COMPETIBLE),
    _sicop("LI", "LICITACIÓN PÚBLICA INTERNACIONAL", "LICITACION_ABIERTA", COMPETIBLE),
    _sicop("LA", "LICITACIÓN ABREVIADA", "LICITACION_ABIERTA", COMPETIBLE),
    _sicop("CD", "CONTRATACIÓN DIRECTA", "CONTRATACION_DIRECTA", CONDICIONAL),
    _sicop("CE", "CONTRATACIÓN ESPECIAL", "CONTRATACION_DIRECTA", CONDICIONAL),
    _sicop("PP", "PROCEDIMIENTO POR PRINCIPIO", "OTRO", NO_COMPETIBLE),
    # La institución vende: nunca es una oportunidad de venta.
    _sicop("RE", "REMATE", "OTRO", NO_COMPETIBLE),
)

_POR_CODIGO: dict[tuple[str, str], Modalidad] = {(m.portal_id, m.codigo): m for m in MODALIDADES}

_RE_ESCAPE_LITERAL = re.compile(r"\\u[0-9a-fA-F]{0,4}")


def _normalizar(texto: str) -> str:
    """Igual que `drilldown.analysis._normalizar`: sin tildes, minúsculas, espacios colapsados (sin strip)."""
    texto = _RE_ESCAPE_LITERAL.sub("", texto or "")
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower())


def _codigo(codigo: str) -> str:
    return (codigo or "").strip().upper()


def _por_nombre(portal_id: str, forma: str) -> Modalidad | None:
    """La modalidad del portal cuyo nombre coincide con la forma publicada (sin tildes ni mayúsculas)."""
    norm = _normalizar(forma).strip()
    if not norm:
        return None
    for m in MODALIDADES:
        if m.portal_id == portal_id and _normalizar(m.nombre) == norm:
            return m
    return None


def modalidad_de(proceso) -> Modalidad | None:
    """Modalidad del proceso según su `portal_id` y `modalidad_codigo`.

    None si el proceso no trae código (COMPRASAL). Un código que la tabla no conoce da una modalidad
    sintética `OTRO`/`CONDICIONAL` (que decida el pliego), con el código como nombre.
    """
    from src.portales import PORTAL_ID_DEFAULT

    portal_id = getattr(proceso, "portal_id", "") or PORTAL_ID_DEFAULT
    codigo = _codigo(getattr(proceso, "modalidad_codigo", ""))
    if not codigo:
        return None
    return _POR_CODIGO.get((portal_id, codigo)) or Modalidad(portal_id, codigo, codigo, "OTRO", CONDICIONAL)


def competibilidad_de(portal_id: str, codigo: str = "", forma_contratacion: str = "") -> str:
    """`COMPETIBLE`, `CONDICIONAL` o `NO_COMPETIBLE` para una modalidad.

    1. Con código: la tabla `MODALIDADES`; un código desconocido es `CONDICIONAL`.
    2. Sin código, si la forma es el nombre de una modalidad conocida del portal: la de esa modalidad.
    3. Si no, la regla histórica de COMPRASAL sobre el texto (idéntica a la de `analysis.py`).
    """
    cod = _codigo(codigo)
    if cod:
        m = _POR_CODIGO.get((portal_id, cod))
        return m.competibilidad if m else CONDICIONAL
    m = _por_nombre(portal_id, forma_contratacion)
    if m is not None:
        return m.competibilidad
    forma = _normalizar(forma_contratacion)
    if forma and "directa" not in forma:
        return COMPETIBLE
    return CONDICIONAL
