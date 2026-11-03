#!/usr/bin/env python3
"""Envoltorio del replay aislado de pliegos históricos."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.drilldown.replay import main

if __name__ == "__main__":
    raise SystemExit(main())
