"""Tests del servicio web del feedback (`src.web`), sin ADB ni disco real.

Se parchean `src.feedback.service` (`guardar_voto`, `leer_reportada`, `leer_voto`) y el
cargador de perfiles: lo que se prueba es el contrato HTTP —qué escribe cada método, qué
responde ante un token malo, qué encabezados salen— no la capa de datos, que ya tiene los
suyos.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.feedback import catalogo, tokens
from src.web import feedback as web_feedback
from src.web.app import crear_app

SECRETO = "secreto-de-prueba"
CLIENTE = "cliente_x"
PROCESO = "96-2026-P0011"
EMAIL = "ana@cliente.com"
ADMIN = "admin@amalia.com"
OVERRIDE = "validacion@gmail.com"


def _cfg(**cambios):
    base = dict(
        enable_feedback=True,
        feedback_base_url="https://feedback.example.com",
        feedback_hmac_secret=SECRETO,
        clients_dir="/no/existe",
        data_dir="/no/existe",
        override_recipients=[],
    )
    base.update(cambios)
    return SimpleNamespace(**base)


def _perfil(motivos=None):
    return SimpleNamespace(
        notification=SimpleNamespace(email_to=[EMAIL], system_admins_email=[ADMIN]),
        feedback_motivos=motivos or [],
    )


def _token(email=EMAIL, proceso=PROCESO, cliente=CLIENTE):
    return tokens.firmar(SECRETO, proceso, cliente, tokens.destinatario_id(email))


class _Servicio:
    """Sustituto de las tres funciones de `service` que toca la web; registra las llamadas."""

    def __init__(self):
        self.votos: list[dict] = []
        self.guardado = True
        self.reportada = {"titulo": "Compra de servidores", "institucion": "Ministerio de X"}
        self.voto_previo = None

    def guardar_voto(self, cfg, **kwargs):
        self.votos.append(kwargs)
        return self.guardado

    def leer_reportada(self, cfg, client_id, proceso_id):
        return self.reportada

    def leer_voto(self, cfg, client_id, proceso_id, dest_id):
        return self.voto_previo


@pytest.fixture
def servicio(monkeypatch):
    s = _Servicio()
    monkeypatch.setattr("src.feedback.service.guardar_voto", s.guardar_voto)
    monkeypatch.setattr("src.feedback.service.leer_reportada", s.leer_reportada)
    monkeypatch.setattr("src.feedback.service.leer_voto", s.leer_voto)
    return s


@pytest.fixture
def perfil(monkeypatch):
    """Perfil que devuelve el cargador; el test puede reemplazarlo con `perfil.actual`."""
    caja = SimpleNamespace(actual=_perfil())

    def cargar(client_id, clients_dir):
        if client_id != CLIENTE:
            raise FileNotFoundError(client_id)
        return caja.actual

    monkeypatch.setattr(web_feedback, "load_client_profile", cargar)
    return caja


@pytest.fixture
def cliente(servicio, perfil):
    return TestClient(crear_app(_cfg()))


class TestGetNoEscribe:
    def test_get_relevante_no_guarda_y_muestra_boton_de_confirmacion(self, cliente, servicio):
        r = cliente.get(f"/f/{_token()}/relevante")
        assert r.status_code == 200
        assert servicio.votos == []
        assert "Confirmar: Relevante" in r.text
        assert 'method="post"' in r.text
        assert "Compra de servidores" in r.text and "Ministerio de X" in r.text

    def test_get_irrelevante_no_guarda_y_muestra_formulario(self, cliente, servicio):
        r = cliente.get(f"/f/{_token()}/irrelevante")
        assert r.status_code == 200
        assert servicio.votos == []
        assert 'name="motivos"' in r.text
        assert f'maxlength="{catalogo.COMENTARIO_MAX}"' in r.text
        for m in catalogo.MOTIVOS_DEFECTO:
            assert m["id"] in r.text

    def test_el_token_no_se_repite_en_la_pagina(self, cliente):
        token = _token()
        for voto in ("relevante", "irrelevante"):
            assert token not in cliente.get(f"/f/{token}/{voto}").text

    def test_proceso_sin_foto_degrada_al_codigo(self, cliente, servicio):
        servicio.reportada = None
        r = cliente.get(f"/f/{_token()}/relevante")
        assert r.status_code == 200 and PROCESO in r.text


class TestPostRelevante:
    def test_guarda_relevante_con_email_resuelto(self, cliente, servicio):
        r = cliente.post(f"/f/{_token()}/relevante")
        assert r.status_code == 200
        assert "Gracias" in r.text and "Relevante" in r.text
        assert servicio.votos == [dict(
            client_id=CLIENTE, proceso_id=PROCESO, dest_id=tokens.destinatario_id(EMAIL),
            email=EMAIL, voto="RELEVANTE", motivos=[], comentario="",
        )]

    def test_resuelve_el_email_entre_los_admins(self, cliente, servicio):
        cliente.post(f"/f/{_token(ADMIN)}/relevante")
        assert servicio.votos[0]["email"] == ADMIN

    def test_resuelve_el_email_del_override_en_modo_validacion(self, servicio, perfil):
        app = crear_app(_cfg(override_recipients=[OVERRIDE]))
        TestClient(app).post(f"/f/{_token(OVERRIDE)}/relevante")
        assert servicio.votos[0]["email"] == OVERRIDE

    def test_destinatario_que_ya_no_esta_guarda_con_email_none(self, cliente, servicio):
        r = cliente.post(f"/f/{_token('ex@cliente.com')}/relevante")
        assert r.status_code == 200
        assert servicio.votos[0]["email"] is None
        assert servicio.votos[0]["dest_id"] == tokens.destinatario_id("ex@cliente.com")


class TestPostIrrelevante:
    def test_guarda_motivos_y_comentario(self, cliente, servicio):
        r = cliente.post(
            f"/f/{_token()}/irrelevante",
            data={"motivos": ["fuera_de_rubro", "monto_muy_bajo"], "comentario": "  no aplica "},
        )
        assert r.status_code == 200 and "Gracias" in r.text
        assert servicio.votos == [dict(
            client_id=CLIENTE, proceso_id=PROCESO, dest_id=tokens.destinatario_id(EMAIL),
            email=EMAIL, voto="IRRELEVANTE", motivos=["fuera_de_rubro", "monto_muy_bajo"],
            comentario="no aplica",
        )]

    def test_motivos_y_comentario_son_opcionales(self, cliente, servicio):
        r = cliente.post(f"/f/{_token()}/irrelevante", data={})
        assert r.status_code == 200
        assert servicio.votos[0]["motivos"] == [] and servicio.votos[0]["comentario"] == ""

    def test_filtra_motivos_desconocidos_y_repetidos(self, cliente, servicio):
        cliente.post(
            f"/f/{_token()}/irrelevante",
            data={"motivos": ["otro", "inventado", "otro", "fuera_de_rubro"]},
        )
        assert servicio.votos[0]["motivos"] == ["otro", "fuera_de_rubro"]

    def test_usa_el_catalogo_propio_del_cliente(self, cliente, servicio, perfil):
        perfil.actual = _perfil([{"id": "propio", "label": "Motivo propio"}])
        cliente.post(f"/f/{_token()}/irrelevante", data={"motivos": ["propio", "fuera_de_rubro"]})
        assert servicio.votos[0]["motivos"] == ["propio"]

    def test_recorta_el_comentario_largo(self, cliente, servicio):
        cliente.post(
            f"/f/{_token()}/irrelevante", data={"comentario": "x" * (catalogo.COMENTARIO_MAX + 500)}
        )
        assert servicio.votos[0]["comentario"] == "x" * catalogo.COMENTARIO_MAX


class TestPrecarga:
    def test_precarga_el_voto_anterior_y_lo_dice(self, cliente, servicio):
        servicio.voto_previo = {
            "voto": "IRRELEVANTE", "motivos": ["monto_muy_bajo"], "comentario": "muy chico",
        }
        r = cliente.get(f"/f/{_token()}/irrelevante")
        assert "Ya respondiste" in r.text and "puedes cambiarla" in r.text
        assert "muy chico" in r.text
        assert 'value="monto_muy_bajo"\n                 checked' in r.text
        assert 'value="fuera_de_rubro"\n                 checked' not in r.text

    def test_comentario_con_script_sale_escapado(self, cliente, servicio):
        servicio.voto_previo = {
            "voto": "IRRELEVANTE", "motivos": [],
            "comentario": '</textarea><script>alert("xss")</script>',
        }
        r = cliente.get(f"/f/{_token()}/irrelevante")
        assert "<script>" not in r.text and "</textarea><script" not in r.text
        assert "&lt;script&gt;" in r.text

    def test_previo_relevante_avisa_pero_no_precarga(self, cliente, servicio):
        servicio.voto_previo = {"voto": "RELEVANTE", "motivos": [], "comentario": ""}
        r = cliente.get(f"/f/{_token()}/irrelevante")
        assert "Ya respondiste" in r.text and "Relevante" in r.text

    def test_sin_voto_previo_no_hay_aviso(self, cliente):
        assert "Ya respondiste" not in cliente.get(f"/f/{_token()}/irrelevante").text


class TestTokenInvalido:
    """Todas las causas de rechazo tienen que verse IDÉNTICAS desde afuera."""

    def _rechazos(self, servicio, perfil):
        firmado = _token()
        alterado = firmado[:-2] + ("aa" if not firmado.endswith("aa") else "bb")
        con_otro_secreto = tokens.firmar("otro-secreto", PROCESO, CLIENTE, "abc")
        return {
            "basura": "esto-no-es-un-token",
            "vacio_de_firma": "abc.",
            "firma_alterada": alterado,
            "otro_secreto": con_otro_secreto,
            "cliente_desconocido": _token(cliente="no_existe"),
        }

    @pytest.mark.parametrize("metodo", ["get", "post"])
    @pytest.mark.parametrize("voto", ["relevante", "irrelevante"])
    def test_todas_las_causas_dan_el_mismo_404(self, cliente, servicio, perfil, metodo, voto):
        respuestas = {
            causa: getattr(cliente, metodo)(f"/f/{tok}/{voto}")
            for causa, tok in self._rechazos(servicio, perfil).items()
        }
        assert {r.status_code for r in respuestas.values()} == {404}
        assert len({r.text for r in respuestas.values()}) == 1
        assert servicio.votos == []

    def test_feedback_apagado_da_el_mismo_404(self, servicio, perfil):
        normal = TestClient(crear_app(_cfg())).get("/f/basura/relevante")
        apagado = TestClient(crear_app(_cfg(enable_feedback=False)))
        r = apagado.post(f"/f/{_token()}/relevante")
        assert r.status_code == 404 and r.text == normal.text
        assert servicio.votos == []

    def test_sin_secreto_da_404(self, servicio, perfil):
        c = TestClient(crear_app(_cfg(feedback_hmac_secret="")))
        assert c.get(f"/f/{_token()}/relevante").status_code == 404

    def test_ruta_inexistente_se_ve_igual_que_un_token_malo(self, cliente):
        malo = cliente.get("/f/basura/relevante")
        assert cliente.get("/no/existe").text == malo.text
        assert cliente.get("/f/solo-token").status_code == 404

    def test_el_404_no_menciona_la_causa(self, cliente):
        r = cliente.get("/f/basura/relevante")
        assert "firma" not in r.text.lower() and "cliente" not in r.text.lower()


class TestFalloAlGuardar:
    def test_relevante_devuelve_503_y_no_dice_gracias(self, cliente, servicio):
        servicio.guardado = False
        r = cliente.post(f"/f/{_token()}/relevante")
        assert r.status_code == 503
        assert "gracias" not in r.text.lower()
        assert "NO se guardó" in r.text and "inténtalo de nuevo" in r.text

    def test_irrelevante_devuelve_503_y_no_dice_gracias(self, cliente, servicio):
        servicio.guardado = False
        r = cliente.post(f"/f/{_token()}/irrelevante", data={"comentario": "hola"})
        assert r.status_code == 503
        assert "gracias" not in r.text.lower() and "NO se guardó" in r.text


class TestEndurecimiento:
    ESPERADOS = {
        "referrer-policy": "no-referrer",
        "x-robots-tag": "noindex",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        "strict-transport-security": "max-age=31536000",
    }

    @pytest.mark.parametrize("ruta,metodo", [
        ("/healthz", "get"), ("/f/basura/relevante", "get"), ("/f/__TOKEN__/relevante", "get"),
        ("/f/__TOKEN__/irrelevante", "get"), ("/f/__TOKEN__/relevante", "post"),
    ])
    def test_encabezados_en_todas_las_respuestas(self, cliente, ruta, metodo):
        r = getattr(cliente, metodo)(ruta.replace("__TOKEN__", _token()))
        for nombre, valor in self.ESPERADOS.items():
            assert r.headers[nombre] == valor

    def test_no_hay_cookies(self, cliente):
        r = cliente.post(f"/f/{_token()}/relevante")
        assert "set-cookie" not in r.headers

    def test_cuerpo_mayor_a_16kb_da_413_con_encabezados(self, cliente, servicio):
        r = cliente.post(f"/f/{_token()}/irrelevante", data={"comentario": "x" * (17 * 1024)})
        assert r.status_code == 413
        assert servicio.votos == []
        assert r.headers["referrer-policy"] == "no-referrer"

    def test_cuerpo_sin_content_length_tambien_se_corta(self, cliente, servicio):
        def trozos():
            for _ in range(20):
                yield b"comentario=" + b"x" * 1024

        r = cliente.post(
            f"/f/{_token()}/irrelevante", content=trozos(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 413 and servicio.votos == []

    def test_cuerpo_de_tamano_normal_pasa(self, cliente, servicio):
        r = cliente.post(f"/f/{_token()}/irrelevante", data={"comentario": "x" * 2000})
        assert r.status_code == 200
        assert len(servicio.votos[0]["comentario"]) == catalogo.COMENTARIO_MAX

    def test_no_se_publican_docs_ni_openapi(self, cliente):
        for ruta in ("/docs", "/redoc", "/openapi.json"):
            assert cliente.get(ruta).status_code == 404


class TestLogging:
    def test_no_registra_el_token_ni_el_comentario(self, cliente, servicio, caplog):
        token = _token()
        with caplog.at_level("DEBUG"):
            cliente.post(f"/f/{token}/irrelevante", data={"comentario": "texto-privado-123"})
            cliente.get(f"/f/{token[:-3]}xyz/relevante")
        # Solo lo que registra el servicio: el cliente de pruebas (httpx) loguea la URL.
        registro = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("src."))
        assert registro
        assert token not in registro and token.split(".")[0] not in registro
        assert "texto-privado-123" not in registro
        assert PROCESO in registro and CLIENTE in registro
        assert tokens.destinatario_id(EMAIL) in registro


class TestHealthz:
    def test_responde_ok_sin_tocar_la_adb(self, monkeypatch):
        def explota(*a, **k):
            raise AssertionError("healthz no debe tocar la ADB")

        monkeypatch.setattr("src.feedback.service.leer_reportada", explota)
        monkeypatch.setattr("src.feedback.service.leer_voto", explota)
        monkeypatch.setattr("src.feedback.service.guardar_voto", explota)
        r = TestClient(crear_app(_cfg())).get("/healthz")
        assert r.status_code == 200 and r.text == "ok"

    def test_responde_ok_con_feedback_apagado(self):
        r = TestClient(crear_app(_cfg(enable_feedback=False))).get("/healthz")
        assert r.status_code == 200
