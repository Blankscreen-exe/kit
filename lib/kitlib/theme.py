"""kit's look, "Concrete": raw grey slabs, hard light edges and a safety-yellow accent, monospace throughout.

The one place its colours live. Web pages get them through `web_css()` / `inject()`, Textual apps through
kitlib.tui, and terminal output through the "accent" style in kitlib.term.
"""

from __future__ import annotations

from pathlib import Path

BG = "#1a1a1a"         # the page / screen
PANEL = "#242424"      # cards and panels on it
RAISED = "#2e2e2e"     # hover, stripes, a level above a panel
EDGE = "#e8e8e8"       # borders - hard and light
TEXT = "#f2f2f2"
MUTED = "#a8a8a8"
DIM = "#6a6a6a"
ACCENT = "#ffd400"     # safety yellow
ON_ACCENT = "#111111"  # text on a yellow block
GOOD = "#35e07f"
BAD = "#ff4d4d"
SHADOW = "#e8e8e8"     # the hard offset shadow under windows and buttons

MARK = "■"
ACCENT_RGB = (0xFF, 0xD4, 0x00)

MONO = 'ui-monospace, "Cascadia Mono", "Cascadia Code", Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace'

_CSS_FILE = Path(__file__).with_name("kit.css")
MARKER = "<!--kit-theme-->"


def web_css() -> str:
    """The shared stylesheet: the palette as CSS variables, then kit.css's components built on them."""
    tokens = {
        "bg": BG, "panel": PANEL, "raised": RAISED, "edge": EDGE, "text": TEXT, "muted": MUTED, "dim": DIM,
        "accent": ACCENT, "on-accent": ON_ACCENT, "good": GOOD, "bad": BAD, "shadow": SHADOW, "mono": MONO,
    }
    root = ":root {\n" + "".join(f"  --{name}: {value};\n" for name, value in tokens.items()) + "}\n"
    return root + _CSS_FILE.read_text(encoding="utf-8")


def inject(html: str) -> str:
    """Put the shared stylesheet where a page has <!--kit-theme--> (in its <head>, before its own styles)."""
    return html.replace(MARKER, f"<style>\n{web_css()}</style>", 1)
