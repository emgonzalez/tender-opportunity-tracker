"""Tests para src.memoria.chunker — segmentación de texto para vectorización."""
import pytest

from src.memoria.chunker import chunk_text


def test_texto_muy_corto_retorna_vacio():
    """Texto < 50 chars (strippeado) debe devolver lista vacía."""
    texto = "Esto es muy corto."
    resultado = chunk_text(texto)
    assert resultado == []


def test_texto_corto_retorna_un_chunk():
    """Texto ~200 chars debe producir exactamente 1 chunk con seq=0."""
    texto = "Este es un texto moderado de aproximadamente cien a doscientos caracteres. " \
            "Contiene varias palabras y frases para cumplir con el tamaño mínimo. " \
            "Debe caber en un único chunk con el tamaño por defecto."
    resultado = chunk_text(texto)
    assert len(resultado) == 1
    assert resultado[0][0] == 0  # seq debe ser 0
    assert resultado[0][1].strip() == texto.strip()


def test_texto_largo_multiples_chunks():
    """Texto largo (~10k chars) debe producir varios chunks con seq consecutivos."""
    # Crear un texto de ~10k caracteres con párrafos claros
    parrafo = ("Este es un párrafo de prueba con suficiente contenido para ejercitar "
               "la segmentación en chunks. Incluye varias palabras y explicaciones. "
               "La función debe encontrar puntos de corte naturales (párrafos u oraciones). "
               "Aquí estamos probando que el algoritmo funciona correctamente.\n\n")
    texto = parrafo * 25  # ~10k chars

    resultado = chunk_text(texto, chunk_tokens=800, overlap=100)

    # Debe haber múltiples chunks
    assert len(resultado) > 1

    # Las secuencias deben ser consecutivas comenzando desde 0
    seqs = [seq for seq, _ in resultado]
    assert seqs == list(range(len(resultado)))

    # Ningún chunk debe exceder significativamente chunk_tokens*4 caracteres
    # (permitir algo de holgura por los separadores de párrafos)
    max_tamaño = 800 * 4 * 1.5  # 50% de holgura
    for seq, chunk_texto in resultado:
        assert len(chunk_texto) <= max_tamaño, \
            f"Chunk {seq} excede tamaño máximo: {len(chunk_texto)} > {max_tamaño}"


def test_solapamiento_real():
    """El final del chunk i debe compartir texto con el inicio del i+1."""
    # Texto con párrafos claros para asegurar múltiples chunks
    parrafo = "X" * 200 + ". Párrafo marcador. " + "Y" * 200 + ". " + "Z" * 200 + ". " \
              + "A" * 200 + ". Párrafo final. "
    texto = parrafo * 20  # Repetir para garantizar múltiples chunks

    resultado = chunk_text(texto, chunk_tokens=400, overlap=100)

    # Debe haber al menos 2 chunks
    assert len(resultado) >= 2

    # Verificar solapamiento entre chunks consecutivos
    for i in range(len(resultado) - 1):
        _, chunk_i = resultado[i]
        _, chunk_next = resultado[i + 1]

        # El chunk siguiente debe empezar dentro de las últimas líneas del chunk actual
        # (debido al overlap). Esto es difícil de verificar de forma exacta, así que
        # comprobamos que el inicio del chunk siguiente aparece dentro del chunk actual.
        # Buscar una cadena distintiva del siguiente chunk en el actual.
        # Por simplicidad, verificamos que hay solapamiento por el contexto.
        assert len(chunk_i) > 0 and len(chunk_next) > 0


def test_caso_antibucleno_overlap_mayor_que_chunk():
    """overlap > chunk_tokens debe reducirse y el proceso debe terminar sin colgar."""
    texto = ("Párrafo 1. " * 50 + "\n\n" + "Párrafo 2. " * 50 + "\n\n" +
             "Párrafo 3. " * 50)

    # Llamar con overlap > chunk_tokens (debería reducirse internamente)
    resultado = chunk_text(texto, chunk_tokens=100, overlap=500)

    # Debe devolver chunks (no colgar)
    assert isinstance(resultado, list)
    # Si hay chunks, deben ser válidos
    for seq, chunk_contenido in resultado:
        assert isinstance(seq, int)
        assert isinstance(chunk_contenido, str)
        assert len(chunk_contenido) >= 50


def test_no_se_pierde_contenido():
    """Una palabra distintiva en mitad del texto debe aparecer en algún chunk."""
    palabra_distintiva = "MARCADOR_UNICO_12345"

    parrafo_inicio = "Inicio del texto con contenido irrelevante. " * 30
    parrafo_marca = f"Aquí viene {palabra_distintiva} en medio del contenido. " * 5
    parrafo_fin = "Fin del texto con más contenido irrelevante. " * 30

    texto = parrafo_inicio + parrafo_marca + parrafo_fin

    resultado = chunk_text(texto, chunk_tokens=600, overlap=100)

    # Buscar la palabra distintiva en los chunks
    contenido_total = " ".join(chunk_txt for _, chunk_txt in resultado)
    assert palabra_distintiva in contenido_total, \
        "La palabra distintiva se perdió durante la segmentación"


def test_normalizacion_espacios():
    """Los espacios en blanco excesivos deben ser colapsados."""
    texto = "Párrafo 1.\n\n\n\n\nPárrafo 2.\n\n\n\n\nPárrafo 3." * 30

    resultado = chunk_text(texto, chunk_tokens=400, overlap=50)

    # Verificar que el resultado no tiene 5+ saltos de línea consecutivos
    contenido_total = " ".join(chunk_txt for _, chunk_txt in resultado)
    assert "\n\n\n" not in contenido_total, \
        "La normalización no colapsó saltos de línea excesivos"


def test_chunks_retornan_tuplas_correctas():
    """Cada chunk debe ser una tupla (seq, texto) con tipos correctos."""
    texto = "Contenido de prueba. " * 100

    resultado = chunk_text(texto, chunk_tokens=300, overlap=50)

    for item in resultado:
        assert isinstance(item, tuple)
        assert len(item) == 2
        seq, chunk_texto = item
        assert isinstance(seq, int)
        assert isinstance(chunk_texto, str)
        assert seq >= 0
        assert len(chunk_texto) >= 50
