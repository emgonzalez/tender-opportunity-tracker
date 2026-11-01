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
