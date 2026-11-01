"""Respuestas sintéticas de análisis y restricciones de marca, sin proveedores reales."""
import json
from types import SimpleNamespace

import pytest

from src.drilldown import analysis
from src.llm.contrato import Respuesta, Uso
from src.scraper.models import MOTIVO_MARCA_ESPECIFICA, MatrizRiesgos


@pytest.mark.parametrize('forma', ['Contratación directa', 'Licitación pública'])
def test_cognos_ajeno_con_carta(forma):
    cita = 'Se exige IBM Cognos y carta de autorización del fabricante.'
    matriz = MatrizRiesgos(resumen=(
        'Contratación Directa con Proveedor Único por Marca Específica. ' + cita
        + ' La plataforma existente incluye Oracle.'))
    assert analysis.evaluar_competibilidad(
        matriz, forma, ('Oracle',), nuevo=True,
        marca_exigida={'marca': 'IBM Cognos', 'cita': cita, 'pagina': 1})[:2] == (
            False, MOTIVO_MARCA_ESPECIFICA)


def test_equivalente_admitido():
    matriz = MatrizRiesgos(resumen=(
        'Contratación directa por marca específica IBM Cognos o equivalente.'))
    assert analysis.evaluar_competibilidad(matriz, '', ('Oracle',), nuevo=True) == (True, '', '')


def test_cita_ajena_a_matriz_no_prueba_restriccion():
    matriz = MatrizRiesgos(resumen='Licitación pública de software.')
    assert analysis.evaluar_competibilidad(
        matriz, 'Licitación pública', ('Oracle',), nuevo=True, marca_exigida={
            'marca': 'IBM', 'cita': 'Texto inventado', 'pagina': 1},
        carta_fabricante=True) == (True, '', '')


def _respuesta(texto):
    return Respuesta(texto=texto, uso=Uso(input_tokens=2, output_tokens=3),
                     modelo='sintetico', proveedor='anthropic')


@pytest.mark.parametrize('repara', [True, False])
def test_reparacion_analisis(monkeypatch, repara):
    data = {'resumen': 'Compra de software.', 'items': [], 'monto_estimado': None,
            'monto_estimado_clausula': '', 'marca_exigida': None,
            'proveedor_unico': None, 'carta_fabricante': None,
            'equivalente_admitido': None, 'marcas_involucradas': []}
    cassettes = iter(['JSON incompleto', json.dumps(data) if repara else 'roto'])
    pedidos = []

    def completar(cfg, pedido, *, cliente=None):
        pedidos.append(pedido)
        return _respuesta(next(cassettes))

    monkeypatch.setattr(analysis.llm, 'completar', completar)
    cfg = SimpleNamespace(llm_provider='anthropic', intel_c0='activo', llm_temperatura=0.0)
    resultado = analysis.analizar_pliego('sintetico.pdf', cfg, 'contexto', 'proceso-prueba',
                                        n_paginas=1)
    assert resultado.estado == ('ANALIZADO' if repara else 'ERROR_LLM')
    if not repara:
        assert 'parse_error' in resultado.error
    assert len(pedidos) == 2
    assert pedidos[0].esquema == analysis.ESQUEMA_ANALISIS
    assert pedidos[0].temperatura == 0.0
    assert pedidos[0].contexto == {
        'proceso_gid': 'proceso-prueba', 'capa': 'C0', 'variante': 'activo'}
    assert 'JSON incompleto' in pedidos[1].contenido[0].texto
    assert resultado.uso_analisis.llamadas == 2


def test_senales_con_citas_verificadas(monkeypatch):
    from src.drilldown import pdf

    cita = 'Se exige IBM Cognos y carta de autorización del fabricante.'
    monkeypatch.setattr(pdf, 'extract_text_first_pages', lambda *args: cita)
    data = {'marca_exigida': {'marca': 'IBM Cognos', 'cita': cita, 'pagina': None},
            'marcas_involucradas': [
                {'marca': 'Oracle', 'cita': 'Cita inventada', 'pagina': 1,
                 'fuente_texto': 'pliego'}], 'proveedor_unico': True}
    senales = analysis._senales_validadas(data, 'sintetico.pdf', 'NATIVO', 1)
    assert senales['marca_exigida'] == data['marca_exigida']
    assert senales['carta_fabricante'] is True
    assert senales['proveedor_unico'] is None
    assert senales['marcas_involucradas'] == []
    assert analysis._senales_validadas(data, 'sintetico.pdf', 'ESCANEADO', 1)[
        'marca_exigida'] is None


def test_off_no_envia_esquema_ni_repara(monkeypatch):
    pedidos = []

    def completar(cfg, pedido, *, cliente=None):
        pedidos.append(pedido)
        return _respuesta('respuesta rota')

    monkeypatch.setattr(analysis.llm, 'completar', completar)
    resultado = analysis.analizar_pliego('sintetico.pdf', SimpleNamespace(
        llm_provider='anthropic', intel_c0='off'), '', 'proceso-prueba', n_paginas=1)
    assert resultado.estado == 'ERROR_LLM'
    assert len(pedidos) == 1
    assert pedidos[0].esquema is None


def test_contexto_solo_metadatos_y_senales_en_contenido(monkeypatch):
    pedidos = []

    def completar(cfg, pedido, *, cliente=None):
        pedidos.append(pedido)
        return _respuesta('{"resumen":"Software", "items":[]}')

    monkeypatch.setattr(analysis.llm, 'completar', completar)
    analysis.analizar_pliego('sintetico.pdf', SimpleNamespace(
        llm_provider='anthropic', intel_c0='activo'), '', 'proceso-prueba', n_paginas=1,
        metadatos={'cliente_id': 'cliente-prueba', 'texto': 'contenido de pliego'},
        senales_triaje={'es_ti': True})
    assert pedidos[0].contexto['cliente_id'] == 'cliente-prueba'
    assert 'texto' not in pedidos[0].contexto
    assert 'contexto no confiable' in pedidos[0].contenido[1].texto


def test_sombra_conserva_matriz_anterior_y_suma_consumo(monkeypatch):
    pedidos = []
    cassettes = iter([
        '{"resumen":"Resultado anterior", "items":[]}',
        '{"resumen":"Resultado nuevo distinto", "items":[]}',
    ])

    def completar(cfg, pedido, *, cliente=None):
        pedidos.append(pedido)
        return _respuesta(next(cassettes))

    monkeypatch.setattr(analysis.llm, 'completar', completar)
    cfg = SimpleNamespace(llm_provider='anthropic', intel_c0='sombra')
    resultado = analysis.analizar_pliego('sintetico.pdf', cfg, '', 'prueba', n_paginas=1)
    assert resultado.matriz.resumen == 'Resultado anterior'
    assert resultado.uso_analisis.llamadas == 2
    assert resultado.uso_analisis.input_tokens == 4
    assert resultado.uso_analisis.output_tokens == 6
    assert pedidos[0].esquema is None
    assert pedidos[1].esquema == analysis.ESQUEMA_ANALISIS
    assert cfg.intel_c0 == 'sombra'


def test_mencion_accesoria_no_sustituye_marca_exigida():
    matriz = MatrizRiesgos(resumen=(
        'Contratación directa por marca específica Cognos y Oracle accesorio.'))
    assert analysis.evaluar_competibilidad(matriz, '', ('Oracle',), nuevo=True)[0] is False


def test_validacion_marca_y_pagina(tmp_path):
    import fitz

    ruta = tmp_path / 'sintetico.pdf'
    documento = fitz.open()
    documento.new_page().insert_text((72, 72), 'Se exige IBM Cognos.')
    documento.new_page().insert_text((72, 72), 'Otra pagina.')
    documento.save(ruta)
    documento.close()
    data = {'marca_exigida': {'marca': 'IBM', 'cita': 'Se exige IBM Cognos.', 'pagina': 1}}
    assert analysis._senales_validadas(data, str(ruta), 'NATIVO', 2)['marca_exigida']
    data['marca_exigida']['pagina'] = 2
    assert analysis._senales_validadas(data, str(ruta), 'NATIVO', 2)['marca_exigida'] is None
    data['marca_exigida']['pagina'] = 1
    data['marca_exigida']['marca'] = 'Oracle'
    assert analysis._senales_validadas(data, str(ruta), 'NATIVO', 2)['marca_exigida'] is None
