"""Casos sintéticos del filtro y su gancho, sin servicios externos."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.classifier import engine, filtro_ruido
from src.config import Config
from src.scraper.models import MarcaRecomendada, ProcesoClasificado, UsoModelo


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for var in ('COMPRASAL_EMAIL', 'COMPRASAL_PASSWORD', 'SMTP_USER', 'SMTP_PASSWORD'):
        monkeypatch.setenv(var, 'x')


@pytest.mark.parametrize('termino', [t for ts in filtro_ruido.CLARAS.values() for t in ts])
def test_familias_claras(termino):
    r = filtro_ruido.evaluar_ruido(f'Adquisición y mantenimiento de {termino}')
    assert r.ruido and r.nivel == 'claro' and r.termino in termino


@pytest.mark.parametrize('indicio', filtro_ruido.INDICIOS_TI)
def test_indicios_conservan_compras_mixtas(indicio):
    assert not filtro_ruido.evaluar_ruido(f'Aire acondicionado y {indicio}').ruido


@pytest.mark.parametrize('termino', [t for ts in filtro_ruido.POSIBLES.values() for t in ts])
def test_posible_nunca_descarta(termino):
    r = filtro_ruido.evaluar_ruido(f'Compra de {termino}')
    assert not r.ruido and r.nivel == 'posible'


@pytest.mark.parametrize('titulo', ['inversionista', 'microinversor', 'softwarex', '', 'Otros servicios'])
def test_sin_subcadenas(titulo):
    assert not filtro_ruido.evaluar_ruido(titulo).ruido


def test_tildes_espacios_puntuacion_e_institucion():
    assert filtro_ruido.evaluar_ruido('  REFRIGERACIÓN   industrial ').ruido
    assert not filtro_ruido.evaluar_ruido('UPS – aire acondicionado y conexiones').ruido
    assert not filtro_ruido.evaluar_ruido('Refrigeración y Wi-Fi').ruido
    assert filtro_ruido.evaluar_ruido('Aire acondicionado de precisión HUAWEI').ruido
    assert filtro_ruido.evaluar_ruido('Equipo médico', 'Instituto de software ficticio').ruido
    assert filtro_ruido.evaluar_ruido('inversores para softwarex').ruido


@pytest.mark.parametrize('modo', ['off', 'sombra', 'activo', 'invalido'])
def test_config_flag(monkeypatch, modo):
    monkeypatch.setenv('INTEL_FILTRO_RUIDO', modo)
    assert Config().intel_filtro_ruido == (modo if modo != 'invalido' else 'off')


def test_config_default(monkeypatch):
    monkeypatch.delenv('INTEL_FILTRO_RUIDO', raising=False)
    assert Config().intel_filtro_ruido == 'off'


@pytest.mark.parametrize('modo', ['off', 'sombra', 'activo'])
@pytest.mark.parametrize('v2', [False, True])
def test_engine_aplica_antes_de_devolver(monkeypatch, modo, v2):
    from src.aprendizaje import service
    from src.drilldown import quota

    pcs = [ProcesoClasificado(
        institucion='Entidad ficticia', descripcion=titulo, fecha_publicacion=datetime(2026, 1, 1),
        url_directa='https://example.com/proceso', numero_referencia=str(i),
        es_relevante=True, marcas_recomendadas=[MarcaRecomendada(marca='Oracle')])
        for i, titulo in enumerate(['Equipo médico', 'Televisores', 'Software y aire acondicionado'])]
    cfg = SimpleNamespace(intel_filtro_ruido=modo, intel_clasif='activo' if v2 else 'off', app_name='Prueba')
    profile = SimpleNamespace(client_id='prueba', portal_id='prueba')
    monkeypatch.setattr(engine, '_clasificar_v2', lambda *a: (pcs, UsoModelo()))
    monkeypatch.setattr(engine, '_classify_batch', lambda *a: (pcs, UsoModelo()))
    monkeypatch.setattr(engine, 'get_system_prompt', lambda *a: '')
    monkeypatch.setattr(engine, '_batch_size', lambda *a: 40)
    monkeypatch.setattr(engine.llm, 'proveedor_activo', lambda *a: 'falso')
    monkeypatch.setattr(engine.llm, 'modelo_de', lambda *a: 'falso')
    monkeypatch.setattr(engine, 'filtrar_marcas_por_portafolio', lambda *a: None)
    monkeypatch.setattr(quota, 'cobrar', lambda *a: None)
    guardar = Mock()
    monkeypatch.setattr(service, 'guardar_decision', guardar)
    assert engine.classify_procesos(pcs, cfg, profile) == pcs
    assert pcs[0].es_relevante == (modo != 'activo')
    assert bool(pcs[0].marcas_recomendadas) == (modo != 'activo')
    assert all(p.es_relevante and p.marcas_recomendadas for p in pcs[1:])
    assert guardar.call_count == (0 if modo == 'off' else 2)
    if modo == 'sombra':
        assert guardar.call_args_list[0].args[3] == 'ruido'
        assert guardar.call_args_list[0].kwargs['variante'] == 'sombra'


def test_excepcion_por_proceso_no_detiene_gancho(monkeypatch):
    evaluar = Mock(side_effect=[RuntimeError('fallo sintético'), filtro_ruido.Ruido(True, 'claro', 'medico', 'equipo medico')])
    monkeypatch.setattr(filtro_ruido, 'evaluar_ruido', evaluar)
    from src.aprendizaje import service
    monkeypatch.setattr(service, 'guardar_decision', Mock(side_effect=RuntimeError('fallo sintético')))
    pcs = [SimpleNamespace(descripcion='Equipo médico', institucion='', numero_referencia=str(i),
                           es_relevante=True, marcas_recomendadas=['Oracle']) for i in range(2)]
    engine._aplicar_filtro_ruido(pcs, SimpleNamespace(intel_filtro_ruido='activo'),
                               SimpleNamespace(client_id='prueba', portal_id='prueba'))
    assert pcs[0].es_relevante and pcs[0].marcas_recomendadas
    assert not pcs[1].es_relevante and not pcs[1].marcas_recomendadas
