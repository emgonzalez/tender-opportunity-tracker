"""
Helpers compartidos para configurar Playwright Chromium.

Centraliza la decisión de qué flags pasar al browser para que todos los
launch points (login, session check, extractor, etc.) se comporten igual
y para poder activar el modo bajo memoria desde un solo lugar.
"""
from __future__ import annotations

from typing import Iterable

_BASE_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
)

# Flags adicionales para reducir el footprint de memoria de Chromium.
# Aplican cuando config.low_memory_mode == True (VMs con <= 2 GB RAM).
_LOW_MEMORY_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",                  # usa /tmp en vez de /dev/shm
    "--disable-extensions",
    "--disable-plugins",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--js-flags=--max-old-space-size=384",      # cap del heap de V8 a 384 MB
)


def build_chromium_args(config, extra: Iterable[str] | None = None) -> list[str]:
    """
    Construye la lista de args para `chromium.launch(args=...)`.

    Si `config.low_memory_mode` está activo, incluye flags adicionales
    que reducen significativamente el consumo de RAM de Chromium.
    """
    args: list[str] = list(_BASE_ARGS)
    if getattr(config, "low_memory_mode", False):
        args.extend(_LOW_MEMORY_ARGS)
    if extra:
        args.extend(extra)
    return args
