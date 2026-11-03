"""Capacidades de marca y confirmación HTTP; sin conexiones externas."""
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.feedback import tokens
from src.web.app import crear_app

SECRETO = 'prueba-marca'


def token(voto='si', **kwargs):
    return tokens.firmar_marca(SECRETO, 'proceso-demo', 'cliente-demo',
                               tokens.destinatario_id('persona@example.com'),
                               '<Oracle>', voto, **kwargs)


def test_token_firma_tipo_expiracion():
    t = token(ahora=100, ttl=20)
    assert tokens.verificar_marca(SECRETO, t, ahora=119).marca == '<Oracle>'
    assert tokens.verificar_marca(SECRETO, t, ahora=120) is None
    assert tokens.verificar_marca(SECRETO, t + 'x', ahora=101) is None
    assert tokens.verificar(SECRETO, t) is None
    assert tokens.verificar_marca(SECRETO, tokens.firmar(SECRETO, 'p', 'c', 'd')) is None
    assert '@' not in tokens._des_b64(t.split('.')[0]).decode()
    assert tokens.verificar_marca(SECRETO, 'ñ.x') is None


@pytest.fixture
def web(monkeypatch):
    cfg = SimpleNamespace(enable_feedback=True, feedback_hmac_secret=SECRETO,
                          feedback_base_url='https://feedback.example.com', clients_dir='inexistente')
    monkeypatch.setattr('src.web.feedback.load_client_profile', lambda *a: object())
    votos = []
    monkeypatch.setattr('src.feedback.service.guardar_marca', lambda cfg, p: votos.append(p) or True)
    return TestClient(crear_app(cfg)), votos


@pytest.mark.parametrize(('voto', 'ruta'), [('si', 'brand-yes'), ('no', 'brand-no')])
def test_get_post(web, voto, ruta):
    cliente, votos = web
    path = f'/f/{token(voto)}/{ruta}'
    r = cliente.get(path)
    assert r.status_code == 200 and not votos
    assert '&lt;Oracle&gt;' in r.text and '<Oracle>' not in r.text
    assert 'persona@example.com' not in r.text
    csrf = re.search('name="csrf_token" value="([^"]+)"', r.text)[1]
    r = cliente.post(path, data={'csrf_token': csrf, 'comentario': 'ignorar'},
                     headers={'origin': 'https://feedback.example.com'})
    assert r.status_code == 200 and 'Gracias' in r.text
    assert len(votos) == 1 and votos[0].voto == voto


def test_error_uniforme_y_csrf(web):
    c, votos = web
    malo = c.get('/f/invalido/brand-yes')
    assert malo.status_code == 404
    assert c.get(f'/f/{token("no")}/brand-yes').text == malo.text
    assert c.get(f'/f/{token(ahora=1)}/brand-yes').text == malo.text
    assert c.post(f'/f/{token()}/brand-yes').status_code == 404
    assert not votos


def test_adb_caida(web, monkeypatch):
    c, _ = web
    monkeypatch.setattr('src.feedback.service.guardar_marca', lambda *a: False)
    path = f'/f/{token()}/brand-yes'
    csrf = re.search('name="csrf_token" value="([^"]+)"', c.get(path).text)[1]
    r = c.post(path, data={'csrf_token': csrf}, headers={'origin': 'https://feedback.example.com'})
    assert r.status_code == 503 and 'No se pudo registrar ahora' in r.text


def test_limite_y_flag(web):
    c, votos = web
    for _ in range(10):
        assert c.get(f'/f/{token()}/brand-yes').status_code == 200
    assert c.get(f'/f/{token()}/brand-yes').status_code == 404
    c.app.state.cfg.enable_feedback = False
    assert c.post(f'/f/{token()}/brand-yes').status_code == 404
    assert not votos
