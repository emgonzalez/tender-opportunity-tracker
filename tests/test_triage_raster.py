"""Tests del rasterizado acotado del triaje (control de costo de tokens).

Contexto: en la validación del 2026-08-03 el triaje enviaba el PDF COMPLETO de
los pliegos escaneados a Haiku. Con ~66% del corpus escaneado, eso convirtió al
triaje en el mayor consumidor de tokens del pipeline. Estos tests fijan el
comportamiento nuevo: solo N páginas, rasterizadas y acotadas en tamaño.

Desde la Fase 9 cubren además lo que la abstracción de proveedor habilitó y
puede romper en silencio: que el `UsoModelo` declare el modelo del proveedor
ACTIVO (por ahí tarifa `quota.cobrar`), que todo error del LLM siga saliendo
como `TriageError`, y que un truncamiento se distinga de un JSON inválido en el
log. Toda la red está mockeada parcheando `triage.llm.completar`.
"""
import base64
import logging
import types

import fitz  # pymupdf
import pytest

from src.drilldown import triage
from src.drilldown.pdf import MAX_LADO_PX, PdfCorruptoError, rasterizar_paginas
from src.drilldown.triage import _RETRY_SUFFIX, _build_user_content
from src.llm import modelos
from src.llm.contrato import LLMError, Respuesta, Uso


@pytest.fixture
def pdf_20_paginas(tmp_path):
    """PDF de 20 páginas tamaño carta con algo de texto."""
    path = tmp_path / "pliego.pdf"
    doc = fitz.open()
    for i in range(20):
        page = doc.new_page(width=612, height=792)  # Letter
        page.insert_text((72, 100), f"Pagina {i} — objeto de la contratacion")
    doc.save(str(path))
    doc.close()
    return str(path)


class TestRasterizarPaginas:
    def test_respeta_el_limite_de_paginas(self, pdf_20_paginas):
        assert len(rasterizar_paginas(pdf_20_paginas, 5)) == 5

    def test_no_excede_el_total_del_documento(self, pdf_20_paginas):
        # pedir más páginas de las que hay no debe reventar
        assert len(rasterizar_paginas(pdf_20_paginas, 999)) == 20

    def test_cero_paginas_devuelve_vacio(self, pdf_20_paginas):
        assert rasterizar_paginas(pdf_20_paginas, 0) == []

    def test_devuelve_jpeg_base64_valido(self, pdf_20_paginas):
        img = rasterizar_paginas(pdf_20_paginas, 1)[0]
        crudo = base64.standard_b64decode(img)
        assert crudo[:2] == b"\xff\xd8"  # cabecera JPEG (SOI)
        assert "\n" not in img

    def test_nunca_excede_el_lado_objetivo(self, pdf_20_paginas):
        """Tope de costo: por encima de MAX_LADO_PX el servidor reescala igual."""
        img = rasterizar_paginas(pdf_20_paginas, 1)[0]
        pix = fitz.Pixmap(base64.standard_b64decode(img))
        assert max(pix.width, pix.height) <= MAX_LADO_PX

    def test_alcanza_el_lado_objetivo(self, pdf_20_paginas):
        """Regresión del bug de 72 DPI: una carta (612x792 PUNTOS) se renderizaba
        a 612x792 px porque el zoom se topaba contra los puntos, no los píxeles.
        Debe llegar al lado objetivo, no quedarse en el tamaño de la página."""
        img = rasterizar_paginas(pdf_20_paginas, 1)[0]
        pix = fitz.Pixmap(base64.standard_b64decode(img))
        assert max(pix.width, pix.height) == MAX_LADO_PX

    def test_pdf_corrupto_lanza_error(self, tmp_path):
        malo = tmp_path / "malo.pdf"
        malo.write_bytes(b"no soy un pdf")
        with pytest.raises(PdfCorruptoError):
            rasterizar_paginas(str(malo), 3)


class TestContenidoDelTriaje:
    def test_escaneado_manda_imagenes_no_el_pdf(self, pdf_20_paginas):
        content = _build_user_content(
            "ctx", texto=None, pdf_path=pdf_20_paginas, scanned=True, raster_pages=3
        )
        tipos = [b.tipo for b in content]
        assert tipos.count("image") == 3
        # REGRESIÓN: jamás volver a mandar el PDF completo en el triaje
        assert "document" not in tipos

    def test_nativo_manda_solo_texto(self, pdf_20_paginas):
        content = _build_user_content(
            "ctx", texto="servidor rack firewall", pdf_path=pdf_20_paginas, scanned=False
        )
        assert [b.tipo for b in content] == ["text"]
        assert "servidor rack" in content[0].texto

    def test_raster_pages_controla_el_costo(self, pdf_20_paginas):
        pocas = _build_user_content("c", texto=None, pdf_path=pdf_20_paginas,
                                    scanned=True, raster_pages=2)
        muchas = _build_user_content("c", texto=None, pdf_path=pdf_20_paginas,
                                     scanned=True, raster_pages=8)
        n_pocas = sum(1 for b in pocas if b.tipo == "image")
        n_muchas = sum(1 for b in muchas if b.tipo == "image")
        assert (n_pocas, n_muchas) == (2, 8)

    def test_el_texto_declara_cuantas_paginas_van(self, pdf_20_paginas):
        content = _build_user_content("c", texto=None, pdf_path=pdf_20_paginas,
                                      scanned=True, raster_pages=4)
        texto = content[-1].texto
        assert "4 pagina" in texto


class TestModeloTarifado:
    """El `uso.modelo` que devuelve `triage()` es el del proveedor ACTIVO.

    No es cosmético: `quota.cobrar` tarifa por ese string contra
    `PRECIOS_USD_MTOK`. Antes de la Fase 9 salía de `cfg.model_triage`, que solo
    gobierna a Anthropic — con `LLM_PROVIDER=oci` el gasto de Llama se habría
    contabilizado a precio de Haiku, y el freno de presupuesto habría mentido en
    la dirección peligrosa (más barato de lo real).
    """

    @staticmethod
    def _cfg(proveedor):
        # Solo los campos que mira la ruta bajo test: el proveedor y el tope de
        # rasterizado. `modelo_de` lee todo con getattr(..., ""), así que no hace
        # falta construir un Config entero.
        return types.SimpleNamespace(llm_provider=proveedor, triage_raster_pages=2)

    @staticmethod
    def _respuesta_go(proveedor, modelo):
        return Respuesta(
            texto='{"viabilidad": "GO", "confianza": 0.9, "motivo": "servidores"}',
            uso=Uso(input_tokens=1200, output_tokens=30),
            modelo=modelo,
            proveedor=proveedor,
        )

    def test_con_oci_el_uso_declara_el_modelo_de_oci(self, monkeypatch):
        cfg = self._cfg("oci")
        esperado = modelos.DEFAULTS[("oci", "triaje")]
        monkeypatch.setattr(
            triage.llm, "completar",
            lambda *a, **k: self._respuesta_go("oci", esperado),
        )

        veredicto, confianza, motivo, uso = triage.triage(cfg, "ctx", texto="servidores")

        assert (veredicto, confianza, motivo) == ("GO", 0.9, "servidores")
        assert uso.modelo == esperado
        assert "claude" not in uso.modelo  # REGRESIÓN: tarifar OCI como Haiku
        assert (uso.input_tokens, uso.output_tokens, uso.llamadas) == (1200, 30, 1)

    def test_con_anthropic_el_uso_declara_el_modelo_de_claude(self, monkeypatch):
        cfg = self._cfg("anthropic")
        esperado = modelos.DEFAULTS[("anthropic", "triaje")]
        monkeypatch.setattr(
            triage.llm, "completar",
            lambda *a, **k: self._respuesta_go("anthropic", esperado),
        )

        _, _, _, uso = triage.triage(cfg, "ctx", texto="servidores")

        assert uso.modelo == esperado

    def test_cfg_pisa_el_default_del_proveedor(self, monkeypatch):
        """`modelo_de` da precedencia al campo de cfg; `uso` tiene que seguirlo."""
        cfg = types.SimpleNamespace(
            llm_provider="oci", triage_raster_pages=2, oci_model_triage="meta.otro-modelo"
        )
        monkeypatch.setattr(
            triage.llm, "completar",
            lambda *a, **k: self._respuesta_go("oci", "meta.otro-modelo"),
        )

        _, _, _, uso = triage.triage(cfg, "ctx", texto="servidores")

        assert uso.modelo == "meta.otro-modelo"


class TestPedidoQueSeEnvia:
    def test_la_etapa_es_triaje_y_lleva_system_prompt(self, monkeypatch):
        """La etapa es lo único que el call site dice sobre el modelo."""
        capturado = {}

        def fake(cfg, pedido, *, cliente=None):
            capturado["pedido"] = pedido
            capturado["cliente"] = cliente
            return Respuesta(
                texto='{"viabilidad": "NO-GO", "confianza": 0.1, "motivo": "obra civil"}',
                uso=Uso(input_tokens=10, output_tokens=5),
                modelo="x", proveedor="anthropic",
            )

        monkeypatch.setattr(triage.llm, "completar", fake)
        centinela = object()
        triage.triage(
            types.SimpleNamespace(llm_provider="anthropic"),
            "ctx", texto="pavimentación", client=centinela,
        )

        pedido = capturado["pedido"]
        assert pedido.etapa == "triaje"
        assert pedido.max_tokens == 512
        assert "GO" in pedido.sistema
        assert [b.tipo for b in pedido.contenido] == ["text"]
        # El cliente crudo se reenvía tal cual: es la inyección de los tests.
        assert capturado["cliente"] is centinela

    def test_escaneado_viaja_como_bloques_de_imagen(self, monkeypatch, pdf_20_paginas):
        capturado = {}

        def fake(cfg, pedido, *, cliente=None):
            capturado["pedido"] = pedido
            return Respuesta(
                texto='{"viabilidad": "GO", "confianza": 1, "motivo": "ok"}',
                uso=None, modelo="x", proveedor="oci",
            )

        monkeypatch.setattr(triage.llm, "completar", fake)
        triage.triage(
            types.SimpleNamespace(llm_provider="oci", triage_raster_pages=3),
            "ctx", pdf_path=pdf_20_paginas, scanned=True,
        )

        tipos = [b.tipo for b in capturado["pedido"].contenido]
        assert tipos == ["image", "image", "image", "text"]
        # REGRESIÓN: el PDF completo jamás vuelve a salir por el triaje.
        assert "document" not in tipos
        assert all(b.datos_b64 for b in capturado["pedido"].contenido if b.tipo == "image")


class TestErroresYTruncamiento:
    @staticmethod
    def _cfg():
        return types.SimpleNamespace(llm_provider="anthropic")

    def test_llmerror_se_traduce_a_triageerror(self, monkeypatch):
        """El pipeline mapea TriageError a ERROR_LLM; nunca se silencia como NO-GO."""
        def explota(*a, **k):
            raise LLMError("429 rate limited", proveedor="oci", etapa="triaje")

        monkeypatch.setattr(triage.llm, "completar", explota)
        with pytest.raises(triage.TriageError):
            triage.triage(self._cfg(), "ctx", texto="x")

    def test_error_inesperado_tambien_es_triageerror(self, monkeypatch):
        monkeypatch.setattr(
            triage.llm, "completar",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("socket cerrado")),
        )
        with pytest.raises(triage.TriageError):
            triage.triage(self._cfg(), "ctx", texto="x")

    def test_parse_error_tras_reintento_devuelve_no_go_y_cobra_las_dos_llamadas(
        self, monkeypatch
    ):
        llamadas = []

        def basura(cfg, pedido, *, cliente=None):
            llamadas.append(pedido)
            return Respuesta(
                texto="Claro, acá va el análisis:",
                uso=Uso(input_tokens=100, output_tokens=512),
                modelo="m", proveedor="oci", truncada=True,
            )

        monkeypatch.setattr(triage.llm, "completar", basura)
        veredicto, confianza, motivo, uso = triage.triage(self._cfg(), "ctx", texto="x")

        assert (veredicto, confianza, motivo) == ("NO-GO", 0.0, "parse_error")
        # El reintento se cobra: si no, el libro de gasto subestima el triaje.
        assert uso.llamadas == 2
        assert uso.input_tokens == 200
        assert _RETRY_SUFFIX in llamadas[1].contenido[-1].texto

    def test_el_log_del_parse_error_distingue_truncamiento(self, monkeypatch, caplog):
        """Con el techo de 4096 de OCI, un truncamiento deja de ser una rareza:
        el operador tiene que poder separarlo de 'el modelo dijo una barbaridad'
        sin abrir el código."""
        monkeypatch.setattr(
            triage.llm, "completar",
            lambda *a, **k: Respuesta(
                texto="{ viabilidad: GO", uso=None, modelo="m",
                proveedor="oci", truncada=True,
            ),
        )
        with caplog.at_level(logging.ERROR, logger="src.drilldown.triage"):
            triage.triage(self._cfg(), "ctx", texto="x")

        assert "truncada=True" in caplog.text

    def test_reintento_recupera_el_json(self, monkeypatch):
        respuestas = iter([
            Respuesta(texto="no es json", uso=Uso(input_tokens=10, output_tokens=3),
                      modelo="m", proveedor="anthropic"),
            Respuesta(texto='prefacio {"viabilidad": "GO", "confianza": 0.7, "motivo": "r"} ',
                      uso=Uso(input_tokens=12, output_tokens=8),
                      modelo="m", proveedor="anthropic"),
        ])
        monkeypatch.setattr(triage.llm, "completar", lambda *a, **k: next(respuestas))

        veredicto, confianza, motivo, uso = triage.triage(self._cfg(), "ctx", texto="x")

        assert (veredicto, confianza, motivo) == ("GO", 0.7, "r")
        assert (uso.llamadas, uso.input_tokens, uso.output_tokens) == (2, 22, 11)
