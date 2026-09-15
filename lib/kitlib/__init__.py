"""Helpers shared by kit and its Python tools.

kit puts lib/ on PYTHONPATH when it runs a tool, so a tool can simply:

    from kitlib import KIT_HOME, style, error, warn, die
"""

import os
from pathlib import Path

from kitlib.term import color_enabled, die, error, style, warn

KIT_HOME = Path(os.environ.get("KIT_HOME") or Path(__file__).resolve().parents[2])

__all__ = ["KIT_HOME", "color_enabled", "die", "error", "style", "warn"]
