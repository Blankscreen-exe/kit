"""Render text as an ASCII-art banner using figlet."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from kitlib import die, style
from kitlib.figlet import KIT_FONTS, FigletError, available_fonts, figlet_command
from kitlib.settings import tool_settings


def list_fonts() -> int:
    fonts = available_fonts()
    if not fonts:
        die("no fonts found - add .flf or .tlf files to tools/banner/fonts")
    if not sys.stdout.isatty():
        print("\n".join(fonts))  # plain names for scripts
        return 0
    for source, title in (("kit", f"KIT FONTS  {KIT_FONTS}"), ("figlet", "FIGLET FONTS")):
        names = [name for name, where in fonts.items() if where == source]
        if names:
            print(style(title, "bold"))
            for name in names:
                print(f"  {name}")
            print()
    print(style("try one: kit banner -f <font> hello", "dim"))
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
        command.append(" ".join(args.text))
        text_in = None
    else:
        text_in = sys.stdin.buffer.read()  # figlet renders each piped line
    result = subprocess.run(command, input=text_in, capture_output=True)
    if result.returncode != 0:
        if args.font.lower() not in {name.lower() for name in available_fonts()}:
            die(f"no font called '{args.font}' - see them with: kit banner --fonts")
        message = result.stderr.decode("utf-8", "replace").strip()
        die(message or f"figlet exited with code {result.returncode}")

    # Fonts can hold any Unicode (block and box characters). figlet writes UTF-8, which a Windows console
    # on an old code page would garble, so print text to a terminal and pass the bytes through to a pipe.
    if sys.stdout.isatty():
        sys.stdout.write(result.stdout.decode("utf-8", "replace"))
    else:
        sys.stdout.flush()
        sys.stdout.buffer.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
