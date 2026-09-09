"""src.memoria.chunker — Chunker de texto para vectorización (Fase 2 Track B).

Implementa segmentación inteligente de texto con solape, optimizado para
vectorización y recuperación de contexto.
"""

import logging
import re

logger = logging.getLogger(__name__)


def chunk_text(
    texto: str,
    chunk_tokens: int = 800,
    overlap: int = 100,
) -> list[tuple[int, str]]:
    """Segmenta un texto en chunks con solape para vectorización.

    La aproximación de tokens es: 1 token ≈ 4 caracteres.
    El tamaño objetivo del chunk es chunk_tokens * 4 caracteres.
    El solape es overlap * 4 caracteres.

    La función busca puntos de corte naturales en este orden:
    1. Párrafos completos (separador "\n\n")
    2. Final de oración (".", "?", "!")
    3. Límite duro de caracteres (como último recurso)

    Devuelve una lista de tuplas (seq, texto_del_chunk) donde seq empieza en 0.
    Los chunks con menos de 50 caracteres (tras strip) se descartan.

    Args:
        texto: El texto a segmentar.
        chunk_tokens: Tamaño objetivo del chunk en tokens (default: 800).
        overlap: Solape entre chunks en tokens (default: 100).

    Returns:
        Lista de tuplas (seq, chunk_text) en orden. Si el texto es muy corto,
        devuelve una lista vacía.
    """
    # Validar y ajustar parámetros
    if overlap >= chunk_tokens:
        original_overlap = overlap
        overlap = max(1, chunk_tokens // 4)
        logger.warning(
            f"overlap ({original_overlap}) >= chunk_tokens ({chunk_tokens}). "
            f"Reduciendo overlap a {overlap} para evitar bucle infinito."
        )

    # Convertir tokens a caracteres (1 token ≈ 4 caracteres)
    chunk_size_chars = chunk_tokens * 4
    overlap_chars = overlap * 4

    # Normalizar el texto
    texto = _normalize_text(texto)

    # Si el texto es muy corto, devolver lista vacía
    if len(texto.strip()) < 50:
        return []

    # Si el texto cabe en un chunk, devolver un único chunk
    if len(texto) <= chunk_size_chars:
        return [(0, texto)]

    chunks: list[tuple[int, str]] = []
    seq = 0
    current_pos = 0

    while current_pos < len(texto):
        # Calcular el final teórico del chunk
        chunk_end = current_pos + chunk_size_chars

        # Si es el último chunk, tomar el resto del texto
        if chunk_end >= len(texto):
            chunk_text_content = texto[current_pos:].strip()
            if len(chunk_text_content) >= 50:
                chunks.append((seq, chunk_text_content))
            break

        # Buscar un punto de corte natural
        cut_pos = _find_natural_cut(texto, current_pos, chunk_end)

        # Si el corte no avanza (protección contra bucle infinito),
        # forzar avance al límite duro
        if cut_pos <= current_pos:
            cut_pos = chunk_end
            logger.debug(
                f"No se encontró corte natural, usando límite duro en pos {cut_pos}"
            )

        # Extraer el chunk
        chunk_text_content = texto[current_pos:cut_pos].strip()

        # Descartar chunks muy pequeños
        if len(chunk_text_content) >= 50:
            chunks.append((seq, chunk_text_content))
            seq += 1

        # Avanzar con solape
        # El siguiente chunk empieza overlap_chars caracteres antes del final
        # de este chunk
        current_pos = cut_pos - overlap_chars

        # Protección adicional: si el solape nos devuelve atrás, avanzar
        # al menos un carácter
        if current_pos <= chunks[-1 if chunks else -1][0]:
            current_pos = cut_pos

    return chunks


def _normalize_text(texto: str) -> str:
    """Normaliza espacios en blanco en el texto.

    Colapsa 3+ saltos de línea consecutivos a 2 y elimina espacios al final
    de cada línea. Preserva la estructura general de párrafos.

    Args:
        texto: El texto a normalizar.

    Returns:
        Texto normalizado.
    """
    # Colapsar 3+ saltos de línea a 2
    texto = re.sub(r"\n{3,}", "\n\n", texto)

    # Eliminar espacios al final de línea
    lines = texto.split("\n")
    lines = [line.rstrip() for line in lines]
    texto = "\n".join(lines)

    return texto


def _find_natural_cut(
    texto: str,
    start_pos: int,
    end_pos: int,
) -> int:
    """Encuentra un punto de corte natural en el texto.

    Busca en este orden de preferencia:
    1. Párrafos completos ("\n\n") dentro del último 25% del rango
    2. Final de oración (".", "?", "!") seguido de espacio
    3. Límite duro (end_pos)

    Args:
        texto: El texto completo.
        start_pos: Inicio del rango de búsqueda.
        end_pos: Final teórico del chunk.

    Returns:
        Posición donde cortar (exclusiva: el chunk es texto[start_pos:cut_pos]).
    """
    # Ventana de búsqueda: último 25% del chunk
    search_start = max(start_pos, end_pos - (end_pos - start_pos) // 4)

    # 1. Buscar párrafo completo ("\n\n")
    for i in range(end_pos, search_start, -1):
        if i + 1 < len(texto) and texto[i : i + 2] == "\n\n":
            return i + 2

    # 2. Buscar fin de oración
    # Buscamos patrones como ". ", ".\n", "? ", "?\n", "! ", "!\n"
    for i in range(end_pos, search_start - 1, -1):
        if i < len(texto):
            # Verificar si hay un signo de puntuación seguido de espacio o newline
            if i > 0 and texto[i - 1] in ".?!":
                if i < len(texto) and texto[i] in " \n":
                    # Omitir espacios en blanco tras la puntuación
                    j = i
                    while j < len(texto) and texto[j] in " \t":
                        j += 1
                    return j

    # 3. Límite duro
    return end_pos
