"""Where kit keeps files that live outside the repo: state, logs and job registries."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def state_dir() -> Path:
    override = os.environ.get("KIT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "kit"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kit"
