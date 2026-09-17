"""Show a QR code in the terminal for any text or URL, or save it as PNG/SVG."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kitlib import KIT_HOME, die, style
from kitlib.settings import tool_settings

try:
    import segno

    from kitlib.qr import make_qr, make_wifi_qr, qr_lines, save_qr
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(prog="kit qr", description="Show a QR code in the terminal, or save it as PNG/SVG.")
    parser.add_argument("text", nargs="*", help="text or URL to encode (or pipe it in)")
    parser.add_argument("--png", type=Path, metavar="FILE", help="save as a PNG image")
    parser.add_argument("--svg", type=Path, metavar="FILE", help="save as an SVG image")
    parser.add_argument("--show", action="store_true", help="draw in the terminal even when saving a file")
    parser.add_argument("--scale", type=int, default=10, help="pixels per module in saved images (default: 10)")
    parser.add_argument("--invert", action="store_true",
                        help="swap dark and light, for light terminals without colour (setting: qr.invert)")
    parser.add_argument("--no-invert", dest="invert", action="store_false", help="don't swap dark and light")
    parser.add_argument("--error", default="M", choices=["L", "M", "Q", "H"], type=str.upper, help="error correction level (default: M)")
    parser.add_argument("--wifi", metavar="SSID", help="make a Wi-Fi join code for this network name")
    parser.add_argument("--password", help="Wi-Fi password (with --wifi)")
    parser.add_argument("--security", choices=["WPA", "WEP", "nopass"], help="Wi-Fi security (default: WPA with a password, else nopass)")
    parser.add_argument("--hidden", action="store_true", help="the Wi-Fi network is hidden (with --wifi)")
    parser.set_defaults(invert=tool_settings().get("invert", False))
    args = parser.parse_args()

    if args.wifi is not None:
        if args.text:
            parser.error("give either text or --wifi, not both")
        if args.password and args.security == "nopass":
            parser.error("--security nopass can't have a --password")
        code = make_wifi_qr(args.wifi, args.password, args.security, args.hidden)
        caption = f"Wi-Fi network: {args.wifi}"  # never echo the password
    else:
        if args.password or args.security or args.hidden:
            parser.error("--password, --security and --hidden only apply with --wifi")
        text = " ".join(args.text)
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().lstrip("﻿").rstrip("\r\n")  # PowerShell pipes add a BOM
        if not text:
            parser.error("give some text or a URL, e.g.: kit qr https://example.com")
        try:
            code = make_qr(text, args.error.lower())
        except segno.DataOverflowError:
            die("too much data for one QR code (the limit is roughly 2,900 characters of text)")
        caption = text if len(text) <= 80 else text[:77] + "..."

    saved = []
    for path, kind in ((args.png, ".png"), (args.svg, ".svg")):
        if path is None:
            continue
        if path.suffix.lower() != kind:
            die(f"--{kind[1:]} needs a file name ending in {kind}: {path}")
        saved.append(save_qr(code, path, scale=args.scale).resolve())

    if not saved or args.show:
        print()
        print("\n".join("  " + line for line in qr_lines(code, invert=args.invert)))
        print(style(f"  {caption}", "dim"))
        print()
    for path in saved:
        print(f"{style('saved', 'bold', 'green')} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
