"""Verifica `src/llm/parseo.py` — el JSON que el modelo dijo que iba a emitir.

Existe por una falla observada dos días seguidos en producción (2026-09-23 y
2026-09-24): un pliego por día se perdía con

    json.decoder.JSONDecodeError: Invalid \\uXXXX escape

después de haber pagado la llamada al modelo. La causa es la cita textual que el
análisis pide en `clausula_ref`: los pliegos escaneados traen barras invertidas
sueltas en su capa de texto, y una barra delante de una palabra que empieza con
`u` —abundantes en español: unidad, ubicación, últimos, único— produce un `\\u`
que no abre un escape Unicode válido.
"""
from __future__ import annotations

import json
import os

import pytest

from src.llm import parseo


class TestElCasoDeProduccion:
    """`Invalid \\uXXXX escape` es un mensaje específico, no genérico."""

    @pytest.mark.parametrize("cita", [
        r"el precio por \unidad sera fijo",
        r"la \ubicacion del suministro",
        r"durante los \ultimos doce meses",
        r"el \unico oferente admitido",
    ])
    def test_una_barra_suelta_ante_palabra_con_u_ya_no_pierde_el_pliego(self, cita):
        crudo = json.dumps({"clausula_ref": cita}).replace("\\\\", "\\")

        with pytest.raises(json.JSONDecodeError) as exc:
            json.loads(crudo)
        assert exc.value.msg == "Invalid \\uXXXX escape", (
            "si este mensaje cambia, la causa diagnosticada ya no es la misma"
        )

        assert parseo.cargar(crudo)["clausula_ref"] == cita

    def test_la_barra_sobrevive_como_barra(self):
        """Reparar no es borrar: la cita tiene que seguir siendo textual.

        `clausula_ref` se verifica contra el PDF. Si la reparación se comiera la
        barra, la cita dejaría de coincidir con el original y el campo perdería
        su única razón de ser.
        """
        crudo = r'{"clausula_ref": "el plazo por \unidad"}'
        assert parseo.cargar(crudo)["clausula_ref"] == "el plazo por \\unidad"


class TestNoTocaLoQueEstaBien:
    """Un JSON bien formado no pasa por la reparación: se parsea estricto."""

    @pytest.mark.parametrize("crudo,esperado", [
        (r'{"x": "N° 5"}', "N° 5"),
        (r'{"x": "linea1\nlinea2"}', "linea1\nlinea2"),
        (r'{"x": "comillas \"dentro\""}', 'comillas "dentro"'),
        (r'{"x": "barra escapada \\ sola"}', "barra escapada \\ sola"),
        (r'{"x": "ruta \/ con slash"}', "ruta / con slash"),
        (r'{"x": "tab\there"}', "tab\there"),
    ])
    def test_los_escapes_validos_se_respetan(self, crudo, esperado):
        assert parseo.cargar(crudo)["x"] == esperado

    def test_una_barra_escapada_se_consume_entera(self):
        """Regresión: el escáner no puede volver a mirar la segunda barra.

        `"a\\\\ b"` es JSON válido —una barra literal seguida de espacio—. Una
        primera versión de este módulo avanzaba de a un carácter: descartaba la
        primera barra (porque la sigue otra barra) y después examinaba la
        SEGUNDA, a la que sigue un espacio, y la "reparaba".

        El daño no era teórico: la reparación solo corre cuando el parseo
        estricto ya falló, o sea sobre textos que suelen traer más de una
        rareza. Romper lo sano al arreglar lo roto deja el pliego perdido igual.
        """
        crudo = r'{"x": "a\\ b", "y": "por \unidad"}'
        datos = parseo.cargar(crudo)
        assert datos["x"] == "a\\ b"
        assert datos["y"] == "por \\unidad"

    @pytest.mark.parametrize("crudo", [
        r'{"x": "a\\ b"}',
        r'{"x": "a\\\\b"}',
        r'{"x": "termina en barra \\"}',
        r'{"x": "\\u00b0 y \\\\ y \n"}',
    ])
    def test_ningun_json_valido_se_altera(self, crudo):
        """La invariante dura: lo que ya parsea, parsea igual después."""
        reparado, vistos = parseo.reparar_escapes(crudo)
        assert vistos == {}, f"se reparó JSON válido: {dict(vistos)}"
        assert json.loads(reparado) == json.loads(crudo)

    def test_un_json_valido_no_reporta_reparaciones(self):
        _, vistos = parseo.reparar_escapes(r'{"x": "N° y \n y \\ y \"q\""}')
        assert vistos == {}, f"se reparó algo que estaba bien: {vistos}"


class TestLoQueNoHace:
    """La reparación no adivina. Una respuesta rota sigue siendo un error."""

    @pytest.mark.parametrize("crudo", [
        '{"x": "sin cerrar',          # truncada por max_tokens
        '{"x": 1,}',                  # coma colgante
        'no soy json',
        '{"x": "ok"} basura {"y": 2}',
    ])
    def test_no_inventa_json_donde_no_lo_hay(self, crudo):
        with pytest.raises(json.JSONDecodeError):
            parseo.cargar(crudo)

    def test_relanza_el_error_ORIGINAL_no_el_del_texto_reparado(self):
        """Quien lee el log necesita saber qué dijo el modelo, no qué dijo el parche.

        Un texto que tiene un escape inválido Y está truncado se repara y sigue
        fallando; el error que sale tiene que describir el texto del modelo.
        """
        crudo = r'{"clausula_ref": "por \unidad'  # escape roto + sin cerrar
        with pytest.raises(json.JSONDecodeError) as exc:
            parseo.cargar(crudo)
        assert exc.value.msg == "Invalid \\uXXXX escape"


class TestElLogNoFiltraElPliego:
    def test_avisa_cuando_repara_sin_volcar_el_texto(self, caplog):
        cita = "el suministro de firewalls para la \\ubicacion central del ministerio"
        crudo = '{"clausula_ref": "%s"}' % cita
        with caplog.at_level("WARNING"):
            parseo.cargar(crudo)

        assert "escape(s) inválido(s)" in caplog.text
        assert "'\\\\ub'" in caplog.text, "el log tiene que decir QUÉ escape vino roto"
        assert "firewalls" not in caplog.text, (
            "el texto del pliego no puede terminar en un archivo de log"
        )


class TestGuardarRespuestaFallida:
    """El JSON roto que `cargar` no repara tiene que poder verse después.

    Observado el 2026-09-26: `Expecting ',' delimiter: line 2 column 470` sin
    forma de saber qué había en esa columna, porque el libro de consumo solo
    guarda el largo de la respuesta.
    """

    def _error(self, texto):
        with pytest.raises(json.JSONDecodeError) as exc:
            parseo.cargar(texto)
        return exc.value

    def test_escribe_la_respuesta_cruda_y_la_posicion(self, tmp_path):
        crudo = '{"resumen": "x",\n "items": [{"a": "b" "c": "d"}]}'
        error = self._error(crudo)

        ruta = parseo.guardar_respuesta_fallida(
            str(tmp_path), "analisis", "1000000005-2026-P0039", crudo, error
        )

        contenido = open(ruta, encoding="utf-8").read()
        assert crudo in contenido
        assert "1000000005-2026-P0039" in contenido
        assert f"posicion: {error.pos}" in contenido

    def test_la_referencia_no_puede_escapar_del_directorio(self, tmp_path):
        crudo = "{no es json"
        ruta = parseo.guardar_respuesta_fallida(
            str(tmp_path), "analisis", "../../etc/passwd", crudo, self._error(crudo)
        )

        assert os.path.dirname(ruta) == str(tmp_path)

    def test_sin_directorio_o_sin_texto_no_hace_nada(self, tmp_path):
        error = self._error("{roto")
        assert parseo.guardar_respuesta_fallida(None, "analisis", "x", "{roto", error) is None
        assert parseo.guardar_respuesta_fallida(str(tmp_path), "analisis", "x", "", error) is None
        assert list(tmp_path.iterdir()) == []

    def test_un_disco_que_falla_no_tumba_la_corrida(self, tmp_path):
        archivo = tmp_path / "no_es_directorio"
        archivo.write_text("x")
        error = self._error("{roto")

        assert parseo.guardar_respuesta_fallida(
            str(archivo / "debug"), "analisis", "x", "{roto", error
        ) is None


class TestAnalisisGuardaLaRespuestaQueNoPudoLeer:
    def _correr(self, monkeypatch, tmp_path, texto):
        from types import SimpleNamespace

        from src import llm
        from src.drilldown import analysis
        from src.llm.contrato import Respuesta

        monkeypatch.setattr(llm, "modelo_de", lambda cfg, etapa: "modelo-falso")
        monkeypatch.setattr(
            llm, "completar",
            lambda cfg, pedido, cliente=None: Respuesta(
                texto=texto, uso=None, modelo="modelo-falso", proveedor="oci"
            ),
        )
        monkeypatch.setattr(analysis, "_paginas_del_pliego", lambda *a, **k: [])
        cfg = SimpleNamespace(debug_dir=str(tmp_path))
        return analysis.analizar_pliego("/x.pdf", cfg, "ctx", "P-1", formato="NATIVO")

    def test_json_roto_deja_el_archivo_y_sigue_siendo_error_llm(self, monkeypatch, tmp_path):
        crudo = '{"resumen": "x",\n "items": [{"a": "b" "c": "d"}]}'
        res = self._correr(monkeypatch, tmp_path, crudo)

        assert res.estado == "ERROR_LLM"
        archivos = list(tmp_path.glob("respuesta_fallida_analisis_P-1_*.txt"))
        assert len(archivos) == 1
        assert crudo in archivos[0].read_text(encoding="utf-8")
        assert str(archivos[0]) in res.error

    def test_una_respuesta_buena_no_deja_nada(self, monkeypatch, tmp_path):
        res = self._correr(monkeypatch, tmp_path, '{"resumen": "ok", "items": []}')

        assert res.estado == "ANALIZADO"
        assert list(tmp_path.iterdir()) == []
