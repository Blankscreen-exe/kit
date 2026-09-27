"""figlet for tools: the build bundled in vendor/figlet-win32 on Windows, the system figlet elsewhere.

Fonts in tools/banner/fonts work on every platform, next to figlet's own. They can be FIGlet fonts
(.flf) or TOIlet fonts (.tlf): figlet refuses the tlf2a header, but the rest of the format is the same,
so kit hands figlet a copy with the header swapped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from kitlib import KIT_HOME

VENDORED = KIT_HOME / "vendor" / "figlet-win32"
KIT_FONTS = KIT_HOME / "tools" / "banner" / "fonts"
FONT_SUFFIXES = (".flf", ".tlf")


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


# --- fonts ---------------------------------------------------------------------------

def kit_fonts() -> dict[str, Path]:
    """Fonts in tools/banner/fonts, by lower-cased name (a .flf wins over a .tlf of the same name)."""
    found: dict[str, Path] = {}
    if KIT_FONTS.is_dir():
        for path in sorted(KIT_FONTS.iterdir(), key=lambda p: FONT_SUFFIXES.index(p.suffix.lower())
                           if p.suffix.lower() in FONT_SUFFIXES else 99):
            if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                found.setdefault(path.stem.lower(), path)
    return found


def figlet_font_dir() -> Path | None:
    exe, font_dir = locate_figlet()
    if font_dir is None:
        try:
            output = subprocess.run([exe, "-I2"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        font_dir = Path(output) if output else None
    return font_dir


def available_fonts() -> dict[str, str]:
    """Every usable font name -> where it comes from ("kit" or "figlet"), kit fonts first on a clash."""
    fonts: dict[str, str] = {}
    for path in kit_fonts().values():
        fonts[path.stem] = "kit"
    taken = {name.lower() for name in fonts}
    font_dir = figlet_font_dir()
    if font_dir and font_dir.is_dir():
        for path in font_dir.glob("*.flf"):
            if path.stem.lower() not in taken and is_figlet_font(path):
                fonts[path.stem] = "figlet"
    return dict(sorted(fonts.items(), key=lambda item: item[0].lower()))


def is_figlet_font(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"flf2a"
    except OSError:
        return False


def cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "kit" / "figlet-fonts"


def figlet_ready(path: Path) -> Path:
    """The font itself if figlet can read it, else a cached copy with the TOIlet header made FIGlet's."""
    if path.suffix.lower() == ".flf" and is_figlet_font(path):
        return path
    target = cache_dir() / f"{path.stem}.flf"
    try:
        if target.is_file() and target.stat().st_mtime >= path.stat().st_mtime:
            return target
        data = path.read_bytes()
    except OSError as exc:
        raise FigletError(f"can't read font {path}: {exc}") from exc
    if data.startswith(b"tlf2a"):
        data = b"flf2a" + data[5:]
    elif not data.startswith(b"flf2a"):
        raise FigletError(f"{path.name} isn't a FIGlet or TOIlet font")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_bytes(data)
        os.replace(temp, target)
    except OSError as exc:
        raise FigletError(f"can't prepare font {path.name} in {target.parent}: {exc}") from exc
    return target


def needs_full_width(exe: str, font_file: Path) -> bool:
    """The bundled Windows figlet (2.2, 1997) predates UTF-8: it treats each byte of a multi-byte character
    as its own column, so overlapping letters ("smushing") cuts characters in half. Fonts with non-ASCII
    characters are drawn at full width there instead (-W), which renders them the way figlet 2.2.5 does."""
    if Path(exe).parent != VENDORED:
        return False
    try:
        return not font_file.read_bytes().isascii()
    except OSError:
        return False


def figlet_command(font: str, width: int) -> list[str]:
    """The figlet command line up to (not including) the text to render."""
    exe, font_dir = locate_figlet()
    custom = kit_fonts().get(font.lower())
    if custom is not None:
        # -d swaps figlet's font folder for this one run; figlet adds .flf to the name itself
        ready = figlet_ready(custom)
        full = ["-W"] if needs_full_width(exe, ready) else []
        return [exe, *full, "-d", str(ready.parent), "-f", ready.stem, "-w", str(width)]
    command = [exe]
    if font_dir is not None:
        command += ["-d", str(font_dir)]
    return command + ["-f", font, "-w", str(width)]


def render_figlet(text: str, font: str = "standard", width: int = 200) -> list[str]:
    """Render text and return the banner's lines, without trailing blank lines."""
    result = subprocess.run([*figlet_command(font, width), text], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise FigletError(result.stderr.strip() or f"figlet exited with code {result.returncode}")
    lines = result.stdout.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
