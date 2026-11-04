"""Las citas deben estar en el pliego, incluso cuando la similitud engaña."""
import pytest

from src.drilldown.citas import validar_cita

TEXTO = "Se exige una carta del fabricante para renovar las licencias del producto."


@pytest.mark.parametrize("cita", [TEXTO, TEXTO.upper(), TEXTO.replace(" ", "  \n")])
def test_literal(cita):
    assert validar_cita(cita, "Encabezado. " + TEXTO + " Fin.").ok


def test_mojibake_y_tildes():
    texto = "La contratación requiere renovación de las licencias vigentes."
    assert validar_cita(texto.encode().decode("latin1"), texto).ok


def test_ocr_conocido():
    assert validar_cita("La Un¡dad solicita carta del fabricante original.",
                        "La Unidad solicita carta del fabricante original.").ok


@pytest.mark.parametrize("cita", [
    "Es obligatoria una comunicación del fabricante para prorrogar las licencias.",
    "Se exige una carta del fabricante para renovar las licencias de otro producto.",
    "No se exige una carta del fabricante para renovar las licencias del producto.",
    "El servicio incluye diez servidores y soporte durante cinco años.",
    "carta del fabricante", "", None,
])
def test_cita_no_literal_o_trivial(cita):
    assert not validar_cita(cita, TEXTO).ok


def test_procedencia():
    cita = validar_cita(TEXTO, {"texto": TEXTO, "fuente_texto": "vision_llm"})
    assert cita.ok and cita.fuente_texto == "vision_llm"


def test_puntuacion_sin_cambiar_palabras():
    assert validar_cita(TEXTO.replace("fabricante", "fabricante,"), TEXTO).ok


def test_palabra_incompleta_no_cuenta():
    assert not validar_cita("competible para todos los oferentes registrados",
                            "incompetible para todos los oferentes registrados").ok


@pytest.mark.parametrize("pagina", [1, 2, None, 90])
def test_busqueda_documental_corrige_pagina(pagina):
    from src.drilldown.citas import validar_en_documento

    assert validar_en_documento(TEXTO, {1: "Otro contenido", 2: TEXTO}, pagina) == {
        "ok": True, "pagina_real": 2, "motivo": None}
    assert validar_en_documento(TEXTO, {2: TEXTO, 1: TEXTO}, 1)["pagina_real"] == 1


@pytest.mark.parametrize("cita", ["carta del fabricante", "No " + TEXTO,
    "Se exige una comunicación del fabricante para renovar las licencias del producto."])
def test_busqueda_documental_no_relaja_literalidad(cita):
    from src.drilldown.citas import validar_en_documento

    assert not validar_en_documento(cita, {1: TEXTO}, None)["ok"]


def test_busqueda_documental_mojibake_y_bordes():
    from src.drilldown.citas import validar_en_documento

    texto = "La contratación requiere renovación de las licencias vigentes."
    assert validar_en_documento(texto.encode().decode("latin1"), {3: texto})["pagina_real"] == 3
    assert not validar_en_documento("competible para todos los oferentes registrados",
        {1: "incompetible para todos los oferentes registrados"})["ok"]
    assert not validar_en_documento(TEXTO, {1: TEXTO[:30], 2: TEXTO[30:]})["ok"]


@pytest.mark.parametrize("pagina,motivo", [(None, "sin_pagina"), (3, "pagina_inexistente"),
    (1, "sin_texto_de_pagina"), (2, "texto_no_coincide")])
def test_busqueda_documental_motivos(pagina, motivo):
    from src.drilldown.citas import validar_en_documento

    assert validar_en_documento(TEXTO, {1: "", 2: "Otro texto"}, pagina)["motivo"] == motivo


@pytest.mark.parametrize("pagina", [None, 1, 20])
def test_senales_corrigen_pagina_sin_mutar_respuesta(tmp_path, pagina):
    import fitz

    from src.drilldown.analysis import _senales_validadas

    ruta = tmp_path / "pliego.pdf"
    cita = "Se exige licencia de marca Ejemplo para renovar el producto."
    with fitz.open() as documento:
        documento.new_page()
        documento.new_page().insert_text((50, 50), cita)
        documento.save(ruta)
    item = dict(marca="Ejemplo", cita=cita, pagina=pagina)
    salida = _senales_validadas(dict(marca_exigida=item, marcas_involucradas=[item]), str(ruta), "NATIVO", 2)
    assert salida["marca_exigida"]["pagina"] == 2
    assert salida["marcas_involucradas"][0]["pagina"] == 2
    assert item["pagina"] == pagina


def test_config_replay_en_ambas_ramas_sombra(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.drilldown import analysis, replay
    from src.scraper.models import ResultadoAnalisis

    cfg = replay.ConfigReplay(SimpleNamespace(llm_timeout_s=10, atributo_delegado="conservado"), [], tmp_path)
    cfg.intel_c0 = "sombra"
    original = analysis.analizar_pliego
    llamadas = []

    def inferir(ruta, local, *args, **kwargs):
        llamadas.append(local)
        assert local.atributo_delegado == "conservado"
        return ResultadoAnalisis(proceso_uuid="REF-DEMO", estado="ANALIZADO")

    monkeypatch.setattr(analysis, "analizar_pliego", inferir)
    resultado = original("no-se-abre.pdf", cfg, "Contexto ficticio", "REF-DEMO", formato="NATIVO")
    assert resultado.estado == "ANALIZADO"
    assert [local.intel_c0 for local in llamadas] == ["off", "activo"]
    assert llamadas[0] is not llamadas[1] and cfg.intel_c0 == "sombra"
