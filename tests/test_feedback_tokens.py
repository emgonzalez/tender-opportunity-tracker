"""Verifica `src.feedback.tokens`: los enlaces del correo no se pueden falsificar ni alterar."""
import pytest

from src.feedback import tokens

SECRETO = "s3creto-de-prueba"


def _token(proceso="96-2026-P0011", cliente="multicomputos", correo="ana@multicomputos.com"):
    return tokens.firmar(SECRETO, proceso, cliente, tokens.destinatario_id(correo))


class TestIdaYVuelta:
    def test_verifica_lo_que_firmo(self):
        payload = tokens.verificar(SECRETO, _token())
        assert payload == tokens.Payload(
            "96-2026-P0011", "multicomputos", tokens.destinatario_id("ana@multicomputos.com")
        )

    def test_es_determinista(self):
        """Un reenvío del reporte tiene que producir los mismos enlaces."""
        assert _token() == _token()

    def test_destinatarios_distintos_tokens_distintos(self):
        assert _token(correo="ana@x.com") != _token(correo="luis@x.com")

    def test_el_correo_no_viaja_en_claro(self):
        assert "ana@multicomputos.com" not in _token()
        assert "multicomputos.com" not in tokens.destinatario_id("ana@multicomputos.com")

    def test_el_id_ignora_mayusculas_y_espacios(self):
        assert tokens.destinatario_id(" Ana@X.com ") == tokens.destinatario_id("ana@x.com")

    def test_procesos_con_caracteres_raros_sobreviven(self):
        assert tokens.verificar(SECRETO, _token(proceso="1000000005-2026/P0039 ñ")).proceso_id \
            == "1000000005-2026/P0039 ñ"


class TestRechazo:
    def test_secreto_distinto(self):
        assert tokens.verificar("otro-secreto", _token()) is None

    def test_payload_alterado(self):
        cuerpo, firma = _token().split(".")
        falso = tokens.firmar(SECRETO, "OTRO-PROCESO", "multicomputos", "abc")
        assert tokens.verificar(SECRETO, f"{falso.split('.')[0]}.{firma}") is None
        assert tokens.verificar(SECRETO, f"{cuerpo}.{firma[:-1]}A") is None

    @pytest.mark.parametrize("basura", [
        "", ".", "..", "sin-punto", "a.b.c", "!!!.???", "x" * 5000, None,
    ])
    def test_basura_da_none_y_no_lanza(self, basura):
        assert tokens.verificar(SECRETO, basura) is None

    def test_sin_secreto_no_verifica(self):
        assert tokens.verificar("", _token()) is None

    def test_firma_valida_de_un_payload_sin_campos(self):
        payload_b64 = tokens._b64(b'{"p":"x"}')
        assert tokens.verificar(SECRETO, f"{payload_b64}.{tokens._firma(SECRETO, payload_b64)}") is None


class TestFirmar:
    def test_sin_secreto_lanza(self):
        with pytest.raises(ValueError):
            tokens.firmar("", "p", "c", "d")

    @pytest.mark.parametrize("args", [("", "c", "d"), ("p", "", "d"), ("p", "c", "")])
    def test_campos_vacios_lanzan(self, args):
        with pytest.raises(ValueError):
            tokens.firmar(SECRETO, *args)


class TestUrl:
    def test_arma_la_ruta(self):
        assert tokens.url("https://f.example.com/", "TOK", "relevante") == "https://f.example.com/f/TOK/relevant"

    def test_voto_desconocido(self):
        with pytest.raises(ValueError):
            tokens.url("https://f.example.com", "TOK", "quizas")
