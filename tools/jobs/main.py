"""Launch the job tracker (its `jobs` command), from PATH or a checkout. Nothing is imported."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from kitlib import die
from kitlib.settings import tool_settings


def command() -> list[str]:
    installed = shutil.which("jobs")
    if installed:
        return [installed]
    project = str(tool_settings().get("project") or "").strip()
    if not project:
        die("the job tracker is not on PATH. Put its bin/ folder on PATH, "
            "or point kit at a checkout: kit config set jobs.project <folder>")
    folder = Path(project).expanduser()
    script = folder / "jobs.py"
    if not script.is_file():
        die(f"no job tracker at {folder} (setting jobs.project)")
    # Standard library only, so kit's own Python runs it.
    return [sys.executable, str(script)]


sys.exit(subprocess.call(command() + sys.argv[1:]))
