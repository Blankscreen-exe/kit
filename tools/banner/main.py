"""Render text as an ASCII-art banner using figlet."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from kitlib import die
from kitlib.figlet import FigletError, figlet_command, locate_figlet
from kitlib.settings import tool_settings


def list_fonts() -> int:
    exe, font_dir = locate_figlet()
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
    parser.add_argument("-f", "--font", default=tool_settings().get("font", "standard"),
                        help="font name (setting: banner.font, default: standard)")
    parser.add_argument("-w", "--width", type=int, default=shutil.get_terminal_size().columns, help="max output width")
    parser.add_argument("--fonts", action="store_true", help="list available fonts and exit")
    args = parser.parse_args()

    try:
        if args.fonts:
            return list_fonts()
        command = figlet_command(args.font, args.width)
    except FigletError as exc:
        die(str(exc))
    if not args.text and sys.stdin.isatty():
        parser.error("give some text to render, e.g.: kit banner hello")

    if args.text:
        command.append(" ".join(args.text))  # otherwise figlet reads stdin
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
