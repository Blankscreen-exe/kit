"""QR codes for tools: draw them in the terminal or save them as PNG/SVG (uses segno)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

import segno
import segno.helpers

from kitlib.term import color_enabled

BLACK_ON_WHITE = "\x1b[30;107m"
RESET = "\x1b[0m"
# (top module drawn, bottom module drawn) -> character covering both
HALF_BLOCKS = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


def make_qr(data: str, error: str = "m") -> segno.QRCode:
    return segno.make_qr(data, error=error)


def make_wifi_qr(ssid: str, password: str | None = None, security: str | None = None, hidden: bool = False) -> segno.QRCode:
    """A code phones scan to join a Wi-Fi network. security: WPA, WEP or nopass (default: WPA if a password is given)."""
    security = security or ("WPA" if password else "nopass")
    return segno.helpers.make_wifi(
        ssid=ssid,
        password=password if security != "nopass" else None,
        security=security,
        hidden=hidden,
    )


def _can_encode(stream: TextIO, text: str) -> bool:
    try:
        text.encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def qr_lines(data: str | segno.QRCode, border: int = 2, invert: bool = False, stream: TextIO | None = None) -> list[str]:
    """Terminal rendering of a QR code, one string per line.

    With colour it is drawn black on white whatever the terminal's own colours, so phones can scan it.
    Without colour the light modules are drawn as blocks, which suits dark terminals; invert=True suits light ones.
    Uses half-block characters (two module rows per line) and falls back to "##" when the stream can't encode them.
    """
    stream = stream or sys.stdout
    code = data if isinstance(data, segno.QRCode) else make_qr(data)
    colour = color_enabled(stream)

    # "ink" marks the modules that get a glyph; everything else is background.
    def ink(dark: bool) -> bool:
        return (dark if colour else not dark) != invert

    rows = [[ink(bool(module)) for module in row] for row in code.matrix_iter(scale=1, border=border)]

    if _can_encode(stream, "▀▄█"):
        if len(rows) % 2:
            rows.append([ink(False)] * len(rows[0]))  # one more quiet-zone row to pair the last one
        lines = [
            "".join(HALF_BLOCKS[(top, bottom)] for top, bottom in zip(rows[i], rows[i + 1]))
            for i in range(0, len(rows), 2)
        ]
    else:
        lines = ["".join("##" if drawn else "  " for drawn in row) for row in rows]

    if colour:
        lines = [BLACK_ON_WHITE + line + RESET for line in lines]
    return lines


def save_qr(data: str | segno.QRCode, path: str | Path, scale: int = 10, border: int = 4) -> Path:
    """Save as .png or .svg (chosen by the file extension)."""
    code = data if isinstance(data, segno.QRCode) else make_qr(data)
    path = Path(path)
    if path.suffix.lower() not in (".png", ".svg"):
        raise ValueError(f"unsupported image type '{path.suffix or '(none)'}' - use .png or .svg")
    path.parent.mkdir(parents=True, exist_ok=True)
    code.save(str(path), scale=scale, border=border)
    return path
