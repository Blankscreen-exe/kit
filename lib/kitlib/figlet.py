"""figlet for tools: the build bundled in vendor/figlet-win32 on Windows, the system figlet elsewhere."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from kitlib import KIT_HOME

VENDORED = KIT_HOME / "vendor" / "figlet-win32"


class FigletError(Exception):
    pass


def locate_figlet() -> tuple[str, Path | None]:
    """(figlet executable, font directory to pass with -d, or None for figlet's own default)."""
    vendored = VENDORED / "figlet.exe"
    if sys.platform.startswith("win") and vendored.is_file():
        return str(vendored), VENDORED / "fonts"
    system = shutil.which("figlet")
    if system:
        return system, None
    raise FigletError("figlet not found - install it (e.g. 'sudo apt install figlet') or put it on PATH")


def figlet_command(font: str, width: int) -> list[str]:
    """The figlet command line up to (not including) the text to render."""
    exe, font_dir = locate_figlet()
    command = [exe]
    if font_dir is not None:
        command += ["-d", str(font_dir)]
    return command + ["-f", font, "-w", str(width)]


def render_figlet(text: str, font: str = "standard", width: int = 200) -> list[str]:
    """Render text and return the banner's lines, without trailing blank lines."""
    result = subprocess.run([*figlet_command(font, width), text], capture_output=True, text=True)
    if result.returncode != 0:
        raise FigletError(result.stderr.strip() or f"figlet exited with code {result.returncode}")
    lines = result.stdout.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
