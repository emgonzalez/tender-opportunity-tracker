"""Competibilidad por modalidad (docs/arquitectura/portales.md §9.2). Sin red ni LLM.

- SICOP: LD/LY/LE/SE → competible; PX/PP/RE → `MOTIVO_MODALIDAD_NO_COMPETIBLE` sin mirar el pliego;
  PE/CD/CE → condicional (deciden las reglas de texto, como una directa).
- COMPRASAL: la regla anterior se reproduce EXACTA. El oráculo de abajo es una copia literal de
  `analysis.py` antes del cambio (`_legado*`), y se compara contra la versión nueva en una grilla
  de formas de contratación, matrices, portafolios y rutas (anterior y C0).
"""
# ruff: noqa: E501
import re
import types

import pytest

from src.classifier.marcas import contiene
from src.drilldown import analysis, pipeline
from src.drilldown.analysis import (
    _RE_DIRECTA,
    _RE_MARCA_ESPECIFICA,
    _RE_NOMINADA,
    _RE_PROVEEDOR_UNICO,
    _fragmentos,
    _normalizar,
)
from src.scraper.models import (
    MOTIVO_DIRECTA_NOMINADA,
    MOTIVO_MARCA_ESPECIFICA,
    MOTIVO_MODALIDAD_NO_COMPETIBLE,
    MOTIVO_PROVEEDOR_UNICO,
    MatrizRiesgos,
    Proceso,
    ResultadoAnalisis,
    RiesgoItem,
)

# ── Oráculo: copia literal de la regla anterior (src/drilldown/analysis.py antes de §9.2) ──────────

def _legado_anterior(
    matriz: MatrizRiesgos | None,
    forma_contratacion: str = "",
    marcas_cliente: tuple[str, ...] | list[str] | None = None,
) -> tuple[bool | None, str, str]:
    """Devuelve `(competible, motivo_bloqueo, evidencia)` para un pliego analizado.

    Reglas, en orden de precedencia (la primera que dispara gana):

    0. Sin matriz → `(None, "", "")`: no hay de que derivar nada.
    1. Si `forma_contratacion` (el dato de COMPRASAL) viene y NO es directa,
       el proceso es competitivo por definicion → `True`. Un pliego de subasta
       inversa que mencione "proveedor unico" en la ley citada no es un bloqueo.
    2. Todo bloqueo exige ver "contratacion directa" en el propio texto del
       analisis (o en `forma_contratacion`).
    3. `proveedor_unico`: "proveedor/oferente/fuente unico|exclusivo".
    4. `contratacion_directa_nominada`: el contratista ya esta nombrado
       ("con el consultor", "titular de los derechos [patrimoniales]").
    5. `marca_especifica`: "marca especifica". Depende del portafolio:
       - `marcas_cliente is None` (no se sabe el portafolio) → `None`, con el
         motivo puesto: es una señal a verificar, no un bloqueo probado.
       - alguna marca del portafolio aparece en el texto → `True`: el cliente
         puede ofertar esa marca.
       - ninguna aparece → `False`.
    6. Nada de lo anterior → `True`.
    """
    if matriz is None:
        return None, "", ""
    fragmentos = _fragmentos(matriz)

    forma = _normalizar(forma_contratacion)
    if forma and "directa" not in forma:
        return True, "", ""

    normalizados = [(_normalizar(t), t) for t in fragmentos]
    todo = " ".join(n for n, _ in normalizados)
    if not (_RE_DIRECTA.search(todo) or "directa" in forma):
        return True, "", ""

    def _cita(patron: re.Pattern) -> str:
        for norm, original in normalizados:
            if patron.search(norm):
                return original.strip()[:300]
        return ""

    if _RE_PROVEEDOR_UNICO.search(todo):
        return False, MOTIVO_PROVEEDOR_UNICO, _cita(_RE_PROVEEDOR_UNICO)
    if _RE_NOMINADA.search(todo):
        return False, MOTIVO_DIRECTA_NOMINADA, _cita(_RE_NOMINADA)
    if _RE_MARCA_ESPECIFICA.search(todo):
        cita = _cita(_RE_MARCA_ESPECIFICA)
        if marcas_cliente is None:
            return None, MOTIVO_MARCA_ESPECIFICA, cita
        for marca in marcas_cliente:
            m = _normalizar(marca).strip()
            if m and re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", todo):
                return True, "", ""
        return False, MOTIVO_MARCA_ESPECIFICA, cita
    return True, "", ""



_LEGADO_CARTA = re.compile(
    r"carta\s+(?:del\s+fabricante|de\s+(?:autorizacion|respaldo)\s+del\s+fabricante)|"
    r"(?:distribuidor|canal|partner)\s+(?:autorizado|certificado)|exclusiv[oa]\s+de\s+la\s+marca"
)
_LEGADO_ALIAS = {"oracle": ("oracle", "netsuite", "mysql", "java", "weblogic", "solaris", "sun", "sparc", "peoplesoft", "jd edwards"),
          "ibm": ("ibm", "cognos")}


def _legado_contiene_marca(texto: str, marca: str) -> bool:
    return any(contiene(texto, alias)
               for alias in _LEGADO_ALIAS.get(_normalizar(marca), (_normalizar(marca),)) if alias)


def _legado(
    matriz: MatrizRiesgos | None, forma_contratacion: str = "",
    marcas_cliente: tuple[str, ...] | list[str] | None = None, *,
    marca_exigida: dict | None = None, proveedor_unico: bool | None = None,
    carta_fabricante: bool | None = None, equivalente_admitido: bool | None = None,
    nuevo: bool = False, titulo: str = "",
) -> tuple[bool | None, str, str]:
    """Evalúa restricciones de marca sin confundir menciones con la marca exigida.

    En la ruta nueva, proveedor único o carta del fabricante para una marca
    representada deja una alerta a verificar (None); una marca ajena o
    desconocida conserva el bloqueo. La marca exigida precede a las menciones.
    """
    if not nuevo or matriz is None:
        return _legado_anterior(matriz, forma_contratacion, marcas_cliente)
    fragmentos = _fragmentos(matriz)
    todo = _normalizar(" ".join(fragmentos))
    if equivalente_admitido is True or re.search(r"\bo (?:equivalente|similar)\b", todo):
        return True, "", ""
    exigida = marca_exigida or {}
    cita = str(exigida.get("cita") or "")
    nombre = str(exigida.get("marca") or "")
    carta = bool(_LEGADO_CARTA.search(todo))
    especifica = bool(_RE_MARCA_ESPECIFICA.search(todo))
    directa = "directa" in _normalizar(forma_contratacion) if forma_contratacion else bool(
        _RE_DIRECTA.search(todo))
    # Un dato estructurado del LLM no prueba un bloqueo: exige cita presente.
    if cita and _normalizar(cita) not in todo:
        nombre = ""
        cita = ""
    # Solo la ruta nueva relaja exclusividad para un partner del portafolio.
    unico = bool(_RE_PROVEEDOR_UNICO.search(todo))
    if (directa and unico) or carta:
        objetivo = _normalizar(nombre) if nombre else _normalizar(" ".join((todo, titulo)))
        propia = bool(marcas_cliente and any(
            _legado_contiene_marca(objetivo, m) for m in marcas_cliente))
        evidencia = cita or next((t for t in fragmentos if
            _RE_PROVEEDOR_UNICO.search(_normalizar(t)) or
            _LEGADO_CARTA.search(_normalizar(t))), "")
        if propia:
            return None, MOTIVO_PROVEEDOR_UNICO, evidencia[:300]
        # Las marcas ajenas conservan también el motivo histórico específico.
        if not (nombre and carta or especifica and (directa or carta)):
            return False, MOTIVO_PROVEEDOR_UNICO, evidencia[:300]
    if nombre and carta or especifica and (directa or carta):
        evidencia = cita or next((t for t in fragmentos if _RE_MARCA_ESPECIFICA.search(
            _normalizar(t))), "")
        if marcas_cliente is None:
            return None, MOTIVO_MARCA_ESPECIFICA, evidencia[:300]
        objetivo = _normalizar(nombre or evidencia)
        if not nombre:
            coincidencia = _RE_MARCA_ESPECIFICA.search(objetivo)
            if coincidencia:
                resto = objetivo[coincidencia.end():].lstrip(" :,-")
                # La marca es la primera entidad tras la restricción; una mención
                # posterior del portafolio no autoriza a sustituirla.
                resto = re.sub(
                    r"^(?:(?:de|la|para|el|del|renovacion|licenciamiento|licencias|"
                    r"soporte|tecnico|servicio|servicios|producto|productos|software)\s+)+",
                    "", resto)
                objetivo = resto.split(" ", 1)[0].rstrip(".,;:")
        coincide = any(_legado_contiene_marca(objetivo, m) for m in marcas_cliente)
        if nombre or not coincide:
            return (True, "", "") if coincide else (False, MOTIVO_MARCA_ESPECIFICA, evidencia[:300])
        return True, "", ""
    return _legado_anterior(matriz, forma_contratacion, marcas_cliente)


# ── Datos ────────────────────────────────────────────────────────────────────

def _matriz(*textos):
    return MatrizRiesgos(resumen=textos[0] if textos else "",
                         items=[RiesgoItem("legal", t, "alta", t) for t in textos[1:]])


MATRICES = [
    None,
    _matriz("Adquisición ordinaria de servidores."),
    _matriz("Contratación directa con proveedor único del fabricante.", "Proveedor único: Oracle"),
    _matriz("Contratación directa con el consultor nombrado.", "titular de los derechos patrimoniales"),
    _matriz("Contratacion directa por marca específica VMware.", "marca específica VMware vSphere"),
    _matriz("Contratación directa por marca específica Oracle Exadata.", "Se admite Oracle o equivalente"),
    _matriz("Se exige carta del fabricante Fortinet.", "distribuidor autorizado Fortinet"),
    _matriz("Licitación con proveedor exclusivo de la marca.", "exclusivo de la marca Cisco"),
    _matriz("Contrataci\\u00ifn directa con proveedor \\u00fanico"),
]
FORMAS = [
    "", "   ", "Contratación Directa", "CONTRATACION DIRECTA", "Contratación directa por excepción",
    "Licitación competitiva", "Subasta inversa", "Comparación de precios", "Libre gestión",
    "Convenio marco", "Concurso público", "\\u00f3", "directa", "Contrataci\\u00f3n directa",
]
PORTAFOLIOS = [None, (), ("Oracle", "Fortinet"), ("VMware",)]


@pytest.mark.parametrize("forma", FORMAS)
@pytest.mark.parametrize("indice", range(len(MATRICES)))
@pytest.mark.parametrize("marcas", PORTAFOLIOS)
def test_comprasal_identico_en_la_ruta_anterior(forma, indice, marcas):
    matriz = MATRICES[indice]
    esperado = _legado_anterior(matriz, forma, marcas)
    assert analysis._evaluar_competibilidad_anterior(matriz, forma, marcas) == esperado
    assert analysis.evaluar_competibilidad(matriz, forma, marcas) == esperado
    assert analysis.evaluar_competibilidad(matriz, forma, marcas, portal_id="comprasal_sv") == esperado


@pytest.mark.parametrize("forma", FORMAS)
@pytest.mark.parametrize("indice", range(len(MATRICES)))
@pytest.mark.parametrize("marcas", PORTAFOLIOS)
@pytest.mark.parametrize("senales", [
    {}, {"equivalente_admitido": True},
    {"marca_exigida": {"marca": "Oracle", "cita": "Proveedor único: Oracle"}},
    {"marca_exigida": {"marca": "VMware", "cita": "no aparece en el texto"}},
])
def test_comprasal_identico_en_la_ruta_c0(forma, indice, marcas, senales):
    matriz = MATRICES[indice]
    for titulo in ("", "Licencias Oracle"):
        esperado = _legado(matriz, forma, marcas, nuevo=True, titulo=titulo, **senales)
        assert analysis.evaluar_competibilidad(
            matriz, forma, marcas, nuevo=True, titulo=titulo, **senales) == esperado
        assert analysis.evaluar_competibilidad(
            matriz, forma, marcas, nuevo=True, titulo=titulo, portal_id="comprasal_sv",
            modalidad_codigo="", **senales) == esperado


# ── SICOP ────────────────────────────────────────────────────────────────────

ORDINARIA = MATRICES[1]
PROVEEDOR_UNICO = _matriz("Se adjudica al proveedor único del fabricante.")


@pytest.mark.parametrize("nuevo", [False, True])
@pytest.mark.parametrize("codigo", ["LD", "LY", "LE", "SE", "LN", "LI", "LA"])
def test_sicop_competibles(codigo, nuevo):
    assert analysis.evaluar_competibilidad(
        PROVEEDOR_UNICO if not nuevo else ORDINARIA, "", None, nuevo=nuevo,
        portal_id="sicop_cr", modalidad_codigo=codigo) == (True, "", "")


@pytest.mark.parametrize("nuevo", [False, True])
@pytest.mark.parametrize("codigo,nombre", [("PX", "PROCEDIMIENTO POR EXCEPCIÓN"),
                                           ("PP", "PROCEDIMIENTO POR PRINCIPIO"), ("RE", "REMATE")])
def test_sicop_no_competibles(codigo, nombre, nuevo):
    assert analysis.evaluar_competibilidad(
        ORDINARIA, "", ("Oracle",), nuevo=nuevo, equivalente_admitido=True,
        portal_id="sicop_cr", modalidad_codigo=codigo) == (False, MOTIVO_MODALIDAD_NO_COMPETIBLE, nombre)


def test_sicop_no_competible_por_nombre_sin_codigo():
    assert analysis.evaluar_competibilidad(
        ORDINARIA, "Procedimiento por excepción", None, portal_id="sicop_cr")[1] == MOTIVO_MODALIDAD_NO_COMPETIBLE


@pytest.mark.parametrize("codigo", ["PE", "CD", "CE", "ZZ"])
def test_sicop_condicionales_deciden_por_el_texto(codigo):
    # Sin «contratación directa» en el texto: la modalidad condicional igual activa las reglas 3-6.
    assert analysis.evaluar_competibilidad(
        PROVEEDOR_UNICO, "", None, portal_id="sicop_cr", modalidad_codigo=codigo)[:2] == (
        False, MOTIVO_PROVEEDOR_UNICO)
    assert analysis.evaluar_competibilidad(
        ORDINARIA, "", None, portal_id="sicop_cr", modalidad_codigo=codigo) == (True, "", "")
    # Ruta C0: proveedor único de una marca ajena bloquea; con equivalente, compite.
    assert analysis.evaluar_competibilidad(
        PROVEEDOR_UNICO, "", ("HPE",), nuevo=True, portal_id="sicop_cr",
        modalidad_codigo=codigo)[:2] == (False, MOTIVO_PROVEEDOR_UNICO)
    assert analysis.evaluar_competibilidad(
        PROVEEDOR_UNICO, "", ("HPE",), nuevo=True, equivalente_admitido=True,
        portal_id="sicop_cr", modalidad_codigo=codigo) == (True, "", "")


def test_aplicar_competibilidad_lleva_la_modalidad():
    res = ResultadoAnalisis(proceso_uuid="2026PX-1", estado="ANALIZADO", matriz=ORDINARIA)
    analysis.aplicar_competibilidad(res, portal_id="sicop_cr", modalidad_codigo="PX")
    assert res.competible is False and res.motivo_bloqueo == MOTIVO_MODALIDAD_NO_COMPETIBLE
    assert res.anexos["competibilidad"]["evidencia"] == "PROCEDIMIENTO POR EXCEPCIÓN"
    viejo = ResultadoAnalisis(proceso_uuid="P", estado="ANALIZADO", matriz=ORDINARIA)
    analysis.aplicar_competibilidad(viejo, "Contratación directa")  # firma histórica
    assert viejo.competible is True


def test_pipeline_toma_la_modalidad_del_proceso():
    from types import SimpleNamespace

    proceso = Proceso(institucion="I", descripcion="d", fecha_publicacion=None, url_directa="u",
                      numero_referencia="2026PE-1", portal_id="sicop_cr", modalidad_codigo="PE")
    assert pipeline._modalidad(proceso, "otro") == {"portal_id": "sicop_cr", "modalidad_codigo": "PE"}
    assert pipeline._modalidad(SimpleNamespace(), "sicop_cr") == {"portal_id": "sicop_cr",
                                                                  "modalidad_codigo": ""}
    assert pipeline._modalidad(SimpleNamespace()) == {"portal_id": "comprasal_sv", "modalidad_codigo": ""}


@pytest.mark.parametrize("codigo,esperado", [("PX", True), ("RE", True), ("PP", True), ("PE", False),
                                             ("LD", False), ("", False)])
def test_no_competible_por_modalidad(codigo, esperado):
    p = types.SimpleNamespace(portal_id="sicop_cr", modalidad_codigo=codigo, forma_contratacion="")
    assert pipeline.no_competible_por_modalidad(p) is esperado


@pytest.mark.parametrize("forma", FORMAS)
def test_comprasal_nunca_se_omite_por_modalidad(forma):
    p = types.SimpleNamespace(portal_id="comprasal_sv", modalidad_codigo="", forma_contratacion=forma)
    assert pipeline.no_competible_por_modalidad(p) is False
