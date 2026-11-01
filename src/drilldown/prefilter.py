"""
Pre-filter module: regex-based screening against client capability dictionary signals.

This module provides text filtering based on inclusion and exclusion signals derived
from a client's catalog (e.g., clients/multicomputos_sv.yaml). Signals are comma-separated
lists of keywords/phrases that match desired or undesired proceso content.

Usage:
    from src.drilldown.prefilter import passes_prefilter

    result = passes_prefilter(
        "Se requiere un servidor rack para el data center",
        inclusion_signals=["servidor, server, rack"],
        exclusion_signals=["alimentos, bebidas"]
    )
    # result = True (has "servidor" and "rack", no exclusions)
"""

import logging
import re
import unicodedata

from src.llm import parseo

logger = logging.getLogger(__name__)


_RE_ESCAPE_RESIDUAL = re.compile(r"\\u[0-9a-fA-F]{0,4}")


def normalizar_texto(texto: str) -> str:
    """Forma canónica para comparar: la MISMA para el pliego y para las señales.

    Repara el mojibake medido (`PºbLICAS`, `espec\\u00ifico`) con la función del
    módulo de LLM, quita tildes (NFKD sin marcas combinantes), pasa a minúsculas
    y colapsa espacios. Pura e idempotente.
    """
    texto = parseo.normalizar_texto_modelo(texto or "")
    texto = _RE_ESCAPE_RESIDUAL.sub("", texto)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


def _normalize_signals(signals: list[str]) -> list[str]:
    """
    Flatten and normalize a list of comma-separated signal strings.

    Each signal may contain multiple comma-separated tokens. This function:
    - Splits each signal on commas
    - Strips whitespace and converts to lowercase
    - Discards empty tokens
    - Returns a flat list of normalized tokens

    Args:
        signals: List of signal strings, each potentially containing comma-separated tokens
                Example: ["servidor, server, rack", "almacenamiento, storage"]

    Returns:
        Flat list of lowercase, normalized tokens.
        Example: ["servidor", "server", "rack", "almacenamiento", "storage"]
    """
    normalized = []
    for signal in signals:
        # Split by comma and process each token
        tokens = signal.split(",")
        for token in tokens:
            # Strip whitespace and convert to lowercase
            token_clean = normalizar_texto(token)
            # Discard empty tokens
            if token_clean:
                normalized.append(token_clean)

    logger.debug(f"Normalized {len(signals)} signal(s) into {len(normalized)} token(s)")
    return normalized


def passes_prefilter(
    text: str,
    inclusion_signals: list[str],
    exclusion_signals: list[str]
) -> bool:
    """
    Determine if text passes inclusion/exclusion pre-filter.

    A texto passes the filter if:
    1. At least one inclusion signal is present (if inclusion signals are configured), AND
    2. No exclusion signals are present

    If no inclusion signals are configured, the filter passes by default (no filtering
    by absence of config).

    Token matching:
    - Single-word tokens use word boundaries: r"\b<token>\b"
    - Multi-word tokens use substring matching (literal)

    Args:
        text: The proceso text/description to filter
        inclusion_signals: List of signals (comma-separated strings); processo must match at least one
        exclusion_signals: List of signals (comma-separated strings); processo must match none

    Returns:
        True if processo passes filter; False otherwise.
    """
    # Misma normalización que las señales (tildes, mojibake, mayúsculas)
    text_lower = normalizar_texto(text)

    # Normalize signals
    incl_tokens = _normalize_signals(inclusion_signals)
    excl_tokens = _normalize_signals(exclusion_signals)

    # Check exclusions first: if any exclusion token found, reject immediately
    has_exclusion = False
    for token in excl_tokens:
        if _match_token(text_lower, token):
            logger.debug(
                f"Exclusion signal '{token}' found in text; rejecting"
            )
            has_exclusion = True
            break

    if has_exclusion:
        return False

    # Check inclusions: if no inclusion signals configured, accept by default
    if not incl_tokens:
        logger.debug("No inclusion signals configured; passing filter (default accept)")
        return True

    # At least one inclusion signal must be present
    has_inclusion = False
    for token in incl_tokens:
        if _match_token(text_lower, token):
            logger.debug(f"Inclusion signal '{token}' found in text")
            has_inclusion = True
            break

    if not has_inclusion:
        logger.debug(f"No inclusion signals matched (checked {len(incl_tokens)} token(s))")
        return False

    logger.debug("Text passed pre-filter (has inclusion, no exclusion)")
    return True


def _match_token(text: str, token: str) -> bool:
    """
    Match a token against text using reasonable word boundaries.

    - Single-word tokens use word boundaries: r"\b<token>\b"
    - Multi-word tokens (containing spaces) use substring matching

    Args:
        text: Lowercase text to search
        token: Lowercase token to find (already normalized)

    Returns:
        True if token found; False otherwise.
    """
    # Check if token contains spaces (multi-word)
    if " " in token:
        # Multi-word: use substring matching
        return token in text
    else:
        # Single-word: use word boundaries
        pattern = r"\b" + re.escape(token) + r"\b"
        return bool(re.search(pattern, text))
