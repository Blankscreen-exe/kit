"""Render text as an ASCII-art banner using figlet."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from kitlib import KIT_HOME, die

VENDORED = KIT_HOME / "vendor" / "figlet-win32"


def locate_figlet() -> tuple[str, Path | None]:
    """(figlet executable, font directory to pass with -d, or None for figlet's own default)."""
    vendored = VENDORED / "figlet.exe"
    if sys.platform.startswith("win") and vendored.is_file():
        return str(vendored), VENDORED / "fonts"
    system = shutil.which("figlet")
    if system:
        return system, None
    die("figlet not found - install it (e.g. 'sudo apt install figlet') or put it on PATH")


def list_fonts(exe: str, font_dir: Path | None) -> int:
    if font_dir is None:
        font_dir = Path(subprocess.run([exe, "-I2"], capture_output=True, text=True).stdout.strip())
    fonts = sorted((p.stem for p in font_dir.glob("*.flf")), key=str.lower)
    if not fonts:
        die(f"no .flf fonts found in {font_dir}")
    print("\n".join(fonts))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="kit banner", description="Render text as an ASCII-art banner.")
    parser.add_argument("text", nargs="*", help="text to render (or pipe it in)")
    parser.add_argument("-f", "--font", default="standard", help="font name (default: standard)")
    parser.add_argument("-w", "--width", type=int, default=shutil.get_terminal_size().columns, help="max output width")
    parser.add_argument("--fonts", action="store_true", help="list available fonts and exit")
    args = parser.parse_args()

    exe, font_dir = locate_figlet()
    if args.fonts:
        return list_fonts(exe, font_dir)
    if not args.text and sys.stdin.isatty():
        parser.error("give some text to render, e.g.: kit banner hello")

    command = [exe]
    if font_dir is not None:
        command += ["-d", str(font_dir)]
    command += ["-f", args.font, "-w", str(args.width)]
    if args.text:
        command.append(" ".join(args.text))  # otherwise figlet reads stdin
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
