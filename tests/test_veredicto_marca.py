"""Tabla de precedencias y regresiones del veredicto puro (sin servicios)."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from src.aprendizaje.semilla import terminos_semilla
from src.notifier.veredicto_marca import (
    UMBRAL_CONFIRMADA,
    UMBRAL_POSIBLE,
    a_json,
    veredicto_marca,
    veredictos_de_proceso,
)
from src.scraper.models import MarcaRecomendada, ProcesoClasificado, ResultadoAnalisis


def proceso(titulo="Plataforma de gestión", **marca):
    return ProcesoClasificado(
        institucion="Institución de ejemplo", descripcion=titulo,
        fecha_publicacion=datetime(2026, 1, 1), url_directa="",
        marcas_recomendadas=[MarcaRecomendada(marca="Oracle", **marca)],
    )


def analisis(**kwargs):
    return ResultadoAnalisis(proceso_uuid="ejemplo", estado="ANALIZADO", **kwargs)


@pytest.mark.parametrize("titulo,marca,ra,nivel", [
    ("Renovación y soporte de licencias COGNOS", {}, dict(
        marca_exigida={"marca": "IBM/Cognos", "cita": "Licencias Cognos"},
        carta_fabricante=True, competible=False), "fuera"),
    ("Renovación licenciamiento de productos Oracle", {}, {}, "confirmada"),
    ("Plataforma de gestión", dict(score=0.1), {}, "por_confirmar"),
    ("Plataforma de gestión", dict(score=0.5), {}, "por_confirmar"),
    ("Migración de Oracle a PostgreSQL", dict(score=0.99), {}, "por_confirmar"),
    ("Oracle → PostgreSQL: migración de Oracle a PostgreSQL", {}, {}, "por_confirmar"),
    ("Mantenimiento Sun V880", {}, {}, "confirmada"),
    ("Migración fuera de Oracle", {}, {}, "por_confirmar"),
    ("Plataforma de gestión", {}, dict(competible=False, motivo_bloqueo="proveedor_unico"), "no_competible"),
    ("Plataforma de gestión", {}, {}, "por_confirmar"),
    ("Renovación NetSuite", {}, {}, "confirmada"),
    ("Oracle e IBM", {}, {}, "confirmada"),
    ("Oraclexyz", dict(score=0.01), {}, "por_confirmar"),
])
def test_tabla(titulo, marca, ra, nivel):
    p = proceso(titulo, **marca)
    r = veredicto_marca(p, analisis(**ra), "oracle", terminos=terminos_semilla())
    assert r.nivel == nivel
    if "Migración" in titulo:
        assert "Migración fuera" in r.por_que


@pytest.mark.parametrize("nombre", ["Oracle NetSuite", "MySQL", "Java", "WebLogic", "Solaris", "Sun", "SPARC", "PeopleSoft", "JD Edwards"])
def test_productos_oracle_son_entradas_propias(nombre):
    p = proceso()
    p.marcas_recomendadas = [MarcaRecomendada(marca=nombre)]
    assert veredicto_marca(p, None, "oracle").nivel == "por_confirmar"
    p.descripcion = f"Soporte de {nombre}"
    assert veredicto_marca(p, None, "oracle").nivel == "confirmada"


@pytest.mark.parametrize("titulo,nivel", [
    ("Plataforma", "fuera"), ("Oracle", "confirmada"),
    ("licenciamiento", "por_confirmar"), ("Exadata", "confirmada"),
])
def test_sin_entrada_diccionario(titulo, nivel):
    p = proceso(titulo)
    p.marcas_recomendadas = []
    assert veredicto_marca(p, None, "oracle", terminos=terminos_semilla()).nivel == nivel


@pytest.mark.parametrize("respuesta,valida,nivel", [
    ("sí", True, "confirmada"), ("no", True, "fuera"),
    ("sí", False, "por_confirmar"), ("no", False, "por_confirmar"),
    ("incierta", True, "por_confirmar"),
])
def test_verificacion(respuesta, valida, nivel):
    v = dict(respuesta=respuesta, cita="Objeto de contratación", evidencia_valida=valida)
    assert veredicto_marca(proceso(), None, "oracle", verificacion=v).nivel == nivel


def test_cita_se_valida_con_original_y_no_con_cita_inventada():
    v = dict(respuesta="no", cita="No requiere Oracle")
    assert veredicto_marca(proceso(), None, "oracle", verificacion=v,
                           textos_pliego=["No requiere Oracle en este proceso"]).nivel == "fuera"
    assert veredicto_marca(proceso(), None, "oracle", verificacion=v,
                           textos_pliego=["Otro contenido"]).nivel == "por_confirmar"
    v["cita"] = ""
    v["evidencia_valida"] = True
    assert veredicto_marca(proceso(), None, "oracle", verificacion=v).nivel == "por_confirmar"


@pytest.mark.parametrize("segunda,nivel", [(False, "por_confirmar"), (True, "fuera")])
def test_vision_negativa(segunda, nivel):
    v = dict(respuesta="no", cita="No aplica", evidencia_valida=True,
             fuente_texto="vision_llm", segunda_fuente=segunda)
    assert veredicto_marca(proceso("Oracle"), None, "oracle", verificacion=v).nivel == nivel


def test_vision_bloqueo_y_marca_exigida():
    soporte = dict(cita="Exclusivo IBM", evidencia_valida=True, fuente_texto="vision_llm")
    ra = analisis(competible=False, motivo_bloqueo="proveedor_unico",
                  anexos={"competibilidad": soporte})
    v = veredicto_marca(proceso("Oracle"), ra, "oracle")
    assert v.nivel == "por_confirmar" and v.competible is None
    ra.marca_exigida = dict(marca="IBM", **soporte)
    assert veredicto_marca(proceso(), ra, "oracle").nivel == "por_confirmar"
    ra.marca_exigida["segunda_fuente"] = dict(cita="Exclusivo IBM", fuente_texto="nativo")
    assert veredicto_marca(proceso(), ra, "oracle", textos_pliego=["Exclusivo IBM"]).nivel == "fuera"


def test_exigida_equivalente_y_cita():
    ra = analisis(marca_exigida=dict(marca="Cognos", cita="Solo Cognos"))
    p = proceso("Oracle")
    assert veredicto_marca(p, ra, "oracle").nivel == "confirmada"
    assert veredicto_marca(p, ra, "oracle", textos_pliego=["Solo Cognos"]).nivel == "fuera"
    ra.equivalente_admitido = True
    assert veredicto_marca(p, ra, "oracle", textos_pliego=["Solo Cognos"]).nivel == "confirmada"


@pytest.mark.parametrize("score,estado,umbral,nivel", [
    (0.75, None, 0.9, "confirmada"), (0.74, None, 0.6, "por_confirmar"),
    (0.8, "activa", 0.9, "por_confirmar"), (0.65, "activa", 0.6, "confirmada"),
    (0.65, "candidata", 0.6, "por_confirmar"),
])
def test_calibracion(score, estado, umbral, nivel):
    p = proceso(score=score, role="Recomendada", evidencia="Licencias MySQL", evidencia_valida=True)
    c = dict(estado=estado, umbral_confirmada=umbral, umbral_posible=0.4)
    assert veredicto_marca(p, None, "oracle", calibracion=c).nivel == nivel
    assert (UMBRAL_CONFIRMADA, UMBRAL_POSIBLE) == (0.75, 0.40)


@pytest.mark.parametrize("cambio", [dict(evidencia_valida=False), dict(role="Alternativa"), dict(evidencia="plataforma")])
def test_score_no_basta(cambio):
    m = dict(score=1, role="Recomendada", evidencia="Oracle", evidencia_valida=True)
    m.update(cambio)
    assert veredicto_marca(proceso(**m), None, "oracle").nivel == "por_confirmar"


@pytest.mark.parametrize("score", [0, -1, float("nan"), float("inf"), True, "incorrecto"])
def test_aprendido_no_descarta(score):
    assert veredicto_marca(proceso(score=score, origen="knn"), None, "oracle").nivel == "por_confirmar"


def test_bloqueo_guarda_evidencia_y_precede_confirmacion():
    ra = analisis(competible=False, motivo_bloqueo="proveedor_unico",
                  anexos={"competibilidad": dict(evidencia="Exclusividad contractual")})
    v = veredicto_marca(proceso("Oracle"), ra, "oracle")
    assert v.nivel == "no_competible"
    assert v.por_que == "proveedor_unico"
    assert v.evidencia == "Exclusividad contractual"
    assert v.competible is False


def test_diccionario_inactivo_no_confirma():
    terminos = [dict(marca_id="oracle", termino="Producto ejemplo", tipo="fuerte", activo=False)]
    assert veredicto_marca(proceso("Producto ejemplo"), None, "oracle", terminos=terminos).nivel == "por_confirmar"


def test_helpers_puros_inmutables_y_json():
    p, ra = proceso("Oracle"), analisis()
    antes = deepcopy((p, ra))
    salida = veredictos_de_proceso(p, ra, ["oracle", "ibm"], terminos=iter(terminos_semilla()))
    assert salida["oracle"].nivel == "confirmada"
    assert salida["ibm"].nivel == "fuera"
    assert (p, ra) == antes
    with pytest.raises(FrozenInstanceError):
        salida["oracle"].nivel = "fuera"
    assert a_json(salida["oracle"]) == dict(veredicto="confirmada", por_que=salida["oracle"].por_que,
                                          evidencia="Oracle", origen="título")


@pytest.mark.parametrize("estado,posible,motivo", [
    ("activa", 0.6, "Verificación pendiente o evidencia insuficiente"),
    ("activa", 0.3, "Score posible"),
    ("candidata", 0.6, "Score posible"),
])
def test_umbral_posible(estado, posible, motivo):
    c = dict(estado=estado, umbral_confirmada=0.9, umbral_posible=posible)
    v = veredicto_marca(proceso(score=0.5), None, "oracle", calibracion=c)
    assert v.nivel == "por_confirmar" and v.por_que == motivo


@pytest.mark.parametrize("c", [
    dict(estado="activa", marca_id="ibm", umbral_confirmada=0.9, umbral_posible=0.4),
    dict(estado="activa", umbral_confirmada=0.3, umbral_posible=0.9),
    dict(estado="activa", umbral_confirmada=float("nan"), umbral_posible=0.4),
])
def test_calibracion_ajena_o_invalida_usa_defaults(c):
    p = proceso(score=0.8, role="Recomendada", evidencia="Oracle", evidencia_valida=True)
    assert veredicto_marca(p, None, "oracle", calibracion=c).nivel == "confirmada"


def test_contrapatron_diccionario_y_tuplas():
    terminos = [("oracle", "retirar Oracle", "excluye")]
    assert veredicto_marca(proceso("Retirar Oracle"), None, "oracle", terminos=terminos).nivel == "por_confirmar"
