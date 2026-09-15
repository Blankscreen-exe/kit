"""Terminal output: ANSI styling that switches itself off when piped or when NO_COLOR is set."""

from __future__ import annotations

import os
import sys
from typing import NoReturn, TextIO

_CODES = {"bold": 1, "dim": 2, "red": 31, "green": 32, "yellow": 33, "blue": 34, "magenta": 35, "cyan": 36}
_vt_enabled = False


def _enable_windows_vt() -> None:
    """Turn on ANSI escape handling in the classic Windows console (no-op elsewhere)."""
    global _vt_enabled
    if _vt_enabled or os.name != "nt":
        return
    _vt_enabled = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def color_enabled(stream: TextIO | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        _enable_windows_vt()
        return True
    try:
        tty = (stream or sys.stdout).isatty()
    except (AttributeError, ValueError):
        return False
    if tty:
        _enable_windows_vt()
    return tty


def style(text: object, *styles: str, stream: TextIO | None = None) -> str:
    """Wrap text in ANSI codes, e.g. style("done", "bold", "green"). Plain text when colour is off."""
    if not styles or not color_enabled(stream):
        return str(text)
    codes = ";".join(str(_CODES[s]) for s in styles)
    return f"\033[{codes}m{text}\033[0m"


def error(message: str) -> None:
    print(style("error:", "bold", "red", stream=sys.stderr), message, file=sys.stderr)


def warn(message: str) -> None:
    print(style("warning:", "bold", "yellow", stream=sys.stderr), message, file=sys.stderr)


def die(message: str, code: int = 1) -> NoReturn:
    error(message)
    raise SystemExit(code)
