"""Decisiones de negocio sobre datos sintéticos y funciones sin servicios."""
from copy import deepcopy

import pytest

from scripts import replay_marca
from src.aprendizaje.recalcular import calcular
from src.drilldown.analysis import evaluar_competibilidad
from src.notifier.veredicto_marca import veredicto_marca
from src.scraper.models import MatrizRiesgos


@pytest.mark.parametrize("titulo,razon,nivel", [
    ("Gestión de nóminas", "Solución empresarial", "fuera"),
    ("Servicios Oracle", "Solución empresarial", "confirmada"),
    # La mención en la razón es especulación, no evidencia del proceso.
    ("Gestión de nóminas", "Requiere WebLogic", "fuera"),
    ("Adquisición de software informático para unidad ejecutora",
     "Oracle es líder en software empresarial", "fuera"),
    ("Adquisición de Software para la Oficina de Gestión de infraestructura",
     "Oracle es líder en software empresarial", "fuera"),
    ("Servicios Oracle", "Oracle es líder en software empresarial", "confirmada"),
    ("Soporte WebLogic", "Oracle es líder en software empresarial", "confirmada"),
    ("Gestión de nóminas", "Requiere Oraclexyz", "fuera"),
    ("Servicios ÓRACLE", "", "confirmada"),
    ("Licencias IBM Cognos", "Alternativa Oracle", "fuera"),
])
def test_regla_a_en_vivo_y_recalculo(titulo, razon, nivel):
    entrada = dict(marca="Oracle", razon=razon)
    proc = dict(titulo=titulo, marcas_recomendadas=[entrada])
    antes = deepcopy(proc)
    v = veredicto_marca(proc, None, "oracle")
    assert v.nivel == nivel
    if nivel == "fuera" and "Cognos" not in titulo:
        assert v.por_que == "sugerida sin evidencia en el título (la razón del LLM no cuenta como evidencia)"
    assert calcular(dict(titulo=titulo, marcas=[entrada]), [])["oracle"].nivel == nivel
    assert proc == antes


@pytest.mark.parametrize("campos,nivel", [
    (dict(score=0.8, role="Recomendada", evidencia="WebLogic", evidencia_valida=True), "confirmada"),
    (dict(score=0), "por_confirmar"),
    (dict(evidencia="Sin validar"), "por_confirmar"),
    (dict(origen="llm"), "por_confirmar"),
    (dict(evidencia_valida=False), "por_confirmar"),
])
def test_a_no_modifica_entradas_v2(campos, nivel):
    entrada = dict(marca="Oracle", **campos)
    assert veredicto_marca(dict(titulo="Gestión de nóminas", marcas_recomendadas=[entrada]),
                           None, "oracle").nivel == nivel
    assert calcular(dict(titulo="Gestión de nóminas", marcas=[entrada]), [])["oracle"].nivel == nivel


@pytest.mark.parametrize("marca,esperado", [("Oracle", None), ("Fortinet", None),
                                            ("IBM Cognos", False), ("Desconocida", False)])
@pytest.mark.parametrize("restriccion", ["proveedor único", "carta del fabricante",
                                       "distribuidor autorizado"])
def test_b_marca_exigida_precede_menciones(marca, esperado, restriccion):
    texto = f"Contratación directa para {marca}: {restriccion}. Incluye integración Oracle."
    comp, motivo, evidencia = evaluar_competibilidad(
        MatrizRiesgos(resumen=texto, items=[]), "Contratación directa", ["Oracle", "Fortinet"],
        marca_exigida=dict(marca=marca, cita=texto), nuevo=True)
    assert comp is esperado
    assert motivo in {"proveedor_unico", "marca_especifica"} and evidencia
    if esperado is None:
        assert motivo == "proveedor_unico"
    proc = dict(titulo="Servicios Oracle", marcas_recomendadas=[dict(marca="Oracle")])
    v = veredicto_marca(proc, dict(competible=comp, motivo_bloqueo=motivo), "oracle")
    assert v.nivel == ("confirmada" if esperado is None else "no_competible")
    if esperado is None:
        assert "contratación directa por proveedor único: verificar que se acepte partner autorizado" in v.por_que


@pytest.mark.parametrize("texto,esperado", [
    ("Soporte ÓRACLE", None), ("Soporte Fortinet", None),
    ("Soporte Oraclexyz", False), ("Soporte sin marca", False),
])
def test_b_fallback_sin_marca_exigida(texto, esperado):
    matriz = MatrizRiesgos(resumen=f"Contratación directa por proveedor único. {texto}", items=[])
    assert evaluar_competibilidad(matriz, marcas_cliente=["Oracle", "Fortinet"], nuevo=True)[0] is esperado
    # El flag apagado conserva el bloqueo histórico.
    assert evaluar_competibilidad(matriz, marcas_cliente=["Oracle", "Fortinet"])[0] is False


def test_b_recalculo_reemplaza_bloqueo_previo_por_alerta():
    texto = "Contratación directa por proveedor único: soporte Oracle"
    fila = dict(titulo="Servicios de Soporte Técnico ORACLE", marcas=[dict(marca="Oracle")],
                analisis=dict(estado="ANALIZADO", competible=False, motivo_bloqueo="proveedor_unico",
                              matriz=dict(resumen=texto, items=[]),
                              marca_exigida=dict(marca="Oracle", cita=texto)))
    v = calcular(fila, [])["oracle"]
    assert v.nivel == "confirmada" and v.competible is None
    assert "verificar que se acepte partner autorizado" in v.por_que


def test_metrica_no_competible_llega():
    filas = [dict(proceso_gid="sintetico:1", titulo="Soporte Oracle", marcas=[dict(marca="Oracle")],
                  analisis=dict(competible=False, motivo_bloqueo="proveedor_unico"))]
    ejemplos = [dict(proceso_gid="sintetico:1", marca_id="oracle", etiqueta="positivo")]
    a = replay_marca.etapa_a(filas, [], ejemplos)
    assert a["oracle_dejan_de_llegar"] == 0
    assert a["metricas"]["oracle"]["nueva"]["matriz"]["tp"] == 1
    assert a["niveles_oracle"]["no_competible"] == 1
    assert a["transiciones"][0]["nuevo"] == "no_competible"
    assert a["transiciones"][0]["nuevo_alcance"] == "llega"


@pytest.mark.parametrize("valida,nivel", [(True, "por_confirmar"), (False, "fuera")])
def test_a_respeta_marcas_involucradas_con_cita_validada(valida, nivel):
    entrada = dict(marca="Oracle", razon="Oracle es líder en software empresarial")
    ra = dict(marcas_involucradas=[dict(marca="Oracle", cita="Soporte Oracle",
                                        evidencia_valida=valida)])
    proc = dict(titulo="Servicios de soporte", marcas_recomendadas=[entrada])
    assert veredicto_marca(proc, ra, "oracle").nivel == nivel
    fila = dict(titulo=proc["titulo"], marcas=[entrada], analisis=ra)
    assert calcular(fila, [])["oracle"].nivel == nivel


@pytest.mark.parametrize("campos", [
    dict(evidencia="Soporte Oracle", evidencia_valida=True, origen="pliego"),
    dict(origen="diccionario"),
    dict(origen="pliego"),
])
def test_a_conserva_sustento_previo(campos):
    entrada = dict(marca="Oracle", razon="Oracle es líder en software empresarial", **campos)
    proc = dict(titulo="Servicios de soporte", marcas_recomendadas=[entrada])
    assert veredicto_marca(proc, None, "oracle").nivel == "por_confirmar"
    assert calcular(dict(titulo=proc["titulo"], marcas=[entrada]), [])["oracle"].nivel == "por_confirmar"
