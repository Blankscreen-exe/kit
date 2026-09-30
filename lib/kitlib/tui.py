"""kit's look for Textual apps: the Concrete theme, the styles every app shares, and square big digits.

Only apps built on Textual import this. An app subclasses KitApp instead of App and gets:

* the "kit-concrete" theme switched on, so Textual's own widgets (footer, cursor, scrollbars, input
  selection) already use kit's colours;
* BASE_CSS in front of its own CSS: square heavy borders, yellow-block buttons, dialogs, tables, tabs;
* BigDigits - a clock drawn in solid square blocks, which drops to a 3-row version in short windows.

The colours themselves come from kitlib.theme; nothing here defines one.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App
from textual.events import Resize
from textual.reactive import reactive
from textual.theme import Theme
from textual.widget import Widget

from kitlib.theme import ACCENT, BAD, BG, DIM, EDGE, GOOD, MARK, MUTED, ON_ACCENT, PANEL, RAISED, TEXT

THEME = Theme(
    name="kit-concrete",
    primary=ACCENT, secondary=EDGE, accent=ACCENT, warning=ACCENT, error=BAD, success=GOOD,
    foreground=TEXT, background=BG, surface=BG, panel=PANEL, boost=RAISED, dark=True,
    variables={
        "border": ACCENT, "border-blurred": EDGE,
        "block-cursor-background": ACCENT, "block-cursor-foreground": ON_ACCENT, "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": RAISED, "block-cursor-blurred-foreground": TEXT,
        "block-hover-background": RAISED,
        "footer-background": BG, "footer-foreground": MUTED, "footer-key-background": BG,
        "footer-key-foreground": ACCENT, "footer-description-foreground": MUTED, "footer-item-background": BG,
        "input-cursor-background": ACCENT, "input-cursor-foreground": ON_ACCENT,
        "input-selection-background": f"{ACCENT} 35%",
        "scrollbar": DIM, "scrollbar-hover": MUTED, "scrollbar-active": ACCENT,
        "scrollbar-background": BG, "scrollbar-background-hover": BG, "scrollbar-background-active": BG,
        "scrollbar-corner-color": BG,
    },
)

BASE_CSS = f"""
Screen {{ background: {BG}; color: {TEXT}; }}

/* slabs: a heavy light frame, its title a yellow block */
.slab {{
    border: heavy {EDGE}; background: {BG};
    border-title-color: {ON_ACCENT}; border-title-background: {ACCENT}; border-title-style: bold;
    border-subtitle-color: {MUTED};
}}
.slab:focus, .slab:focus-within {{ border: heavy {ACCENT}; }}

Button {{
    min-width: 7; width: auto; height: 3; padding: 0;  /* Textual pads the label itself (line-pad) */
    border: heavy {EDGE}; background: {BG}; color: {TEXT}; text-style: bold;
}}
Button:hover {{ background: {RAISED}; border: heavy {ACCENT}; color: {TEXT}; text-style: bold; }}
Button:focus {{ text-style: bold; }}
Button.-active {{ background: {ACCENT}; color: {ON_ACCENT}; border: heavy {ACCENT}; }}
Button.primary {{ background: {ACCENT}; color: {ON_ACCENT}; border: heavy {ACCENT}; }}
Button.primary:hover {{ background: {ACCENT}; color: {ON_ACCENT}; border: heavy {EDGE}; }}
Button.danger {{ background: {BAD}; color: {ON_ACCENT}; border: heavy {BAD}; }}

Input {{ border: heavy {EDGE}; background: {BG}; color: {TEXT}; padding: 0 1; }}
Input:focus {{ border: heavy {ACCENT}; background: {BG}; }}
Input > .input--placeholder {{ color: {DIM}; }}

ProgressBar Bar > .bar--bar {{ color: {ACCENT}; background: {RAISED}; }}
ProgressBar Bar > .bar--complete {{ color: {ACCENT}; }}
ProgressBar PercentageStatus {{ color: {MUTED}; text-style: bold; }}

DataTable {{ background: {BG}; }}
DataTable > .datatable--header {{ background: {BG}; color: {MUTED}; text-style: bold; }}
DataTable > .datatable--even-row, DataTable > .datatable--odd-row {{ background: {BG}; }}
DataTable > .datatable--hover {{ background: {RAISED}; }}
/* a translucent band, not a solid yellow block: cells keep their own colours, and yellow text stays readable */
DataTable > .datatable--cursor {{ background: {ACCENT} 25%; color: {TEXT}; text-style: bold; }}
DataTable:blur > .datatable--cursor {{ background: {RAISED}; }}

Tabs {{ background: {BG}; }}
Tab {{ color: {MUTED}; text-style: bold; }}
Tab:hover {{ color: {TEXT}; }}
Tab.-active {{ color: {ON_ACCENT}; background: {ACCENT}; }}
Tab.-active:hover {{ color: {ON_ACCENT}; }}
Underline > .underline--bar {{ color: {ACCENT}; background: {EDGE}; }}

Footer {{ background: {BG}; }}
Footer FooterKey .footer-key--key {{ text-style: bold; }}
Footer FooterKey:hover {{ background: {RAISED}; }}

ModalScreen {{ align: center middle; background: rgba(0, 0, 0, 0.7); }}
.dialog {{
    border: heavy {ACCENT}; background: {PANEL}; padding: 1 2;
    border-title-color: {ON_ACCENT}; border-title-background: {ACCENT}; border-title-style: bold;
    border-subtitle-color: {MUTED};
}}
/* a button's background shows around its border lines, so it has to match whatever it sits on */
.dialog Button {{ background: {PANEL}; }}
.dialog Button:hover {{ background: {RAISED}; }}
.dialog Button.primary, .dialog Button.-active {{ background: {ACCENT}; }}
.dialog Button.danger {{ background: {BAD}; }}
.dialog Input, .dialog Input:focus {{ background: {PANEL}; }}

Toast {{ background: {PANEL}; border-left: thick {ACCENT}; }}
Toast.-error {{ border-left: thick {BAD}; }}
.toast--title {{ text-style: bold; color: {ACCENT}; }}
"""


def brand(name: str) -> Text:
    """The app's name as kit writes it in a corner: '■ KIT COUNTDOWN'."""
    return Text.assemble((f"{MARK} ", f"bold {ACCENT}"), (name.upper(), f"bold {TEXT}"))


def title(text: str) -> str:
    """A border title, padded and in capitals: ' COUNTDOWN '."""
    return f" {text.upper()} "


class KitApp(App):
    """App with kit's theme on and BASE_CSS in front of the subclass's own CSS (so the app's rules win)."""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.CSS.startswith(BASE_CSS):  # a subclass of a KitApp subclass already has it
            cls.CSS = BASE_CSS + cls.CSS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.register_theme(THEME)
        self.theme = THEME.name

    def on_resize(self, event: Resize) -> None:
        # a short window swaps BigDigits for their 3-row version, which they only notice on a redraw
        for digits in self.query(BigDigits):
            digits.refresh(layout=True)


# --- square big digits ---------------------------------------------------------------------------
#
# A 3x5 pixel font. Drawn tall, each pixel is two cells wide and one high - square on a terminal, whose
# cells are about twice as tall as wide. Drawn short, two pixel rows share a cell through half blocks
# (▀ ▄ █), one cell wide - square again, at a third of the height.

GLYPHS = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "-": ("...", "...", "###", "...", "..."),
    ":": (".", "#", ".", "#", "."),
    " ": ("...", "...", "...", "...", "..."),
}
HALF_BLOCKS = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}


def pixel_rows(text: str) -> list[str]:
    """The text as 5 rows of '#' and '.', one pixel gap between characters."""
    glyphs = [GLYPHS.get(char, GLYPHS[" "]) for char in text]
    return [".".join(glyph[row] for glyph in glyphs) for row in range(5)]


def draw_tall(text: str) -> list[str]:
    return [row.replace("#", "██").replace(".", "  ") for row in pixel_rows(text)]


def draw_short(text: str) -> list[str]:
    rows = pixel_rows(text) + ["." * len(pixel_rows(text)[0])]  # pad to an even 6 pixel rows
    return ["".join(HALF_BLOCKS[(top[i] == "#", bottom[i] == "#")] for i in range(len(top)))
            for top, bottom in zip(rows[0::2], rows[1::2])]


class BigDigits(Widget):
    """A number drawn in square blocks: 5 rows tall, or 3 rows when the window is shorter than `short_below`."""

    DEFAULT_CSS = "BigDigits { width: auto; height: auto; }"
    value: reactive[str] = reactive("", layout=True)

    def __init__(self, value: str = "", *, short_below: int = 24, **kwargs) -> None:
        super().__init__(**kwargs)
        self.short_below = short_below
        self.set_reactive(BigDigits.value, value)

    @property
    def short(self) -> bool:
        return self.app.size.height < self.short_below

    def update(self, value: str) -> None:
        self.value = value

    def lines(self) -> list[str]:
        return draw_short(self.value) if self.short else draw_tall(self.value)

    def get_content_width(self, container, viewport) -> int:
        return max((len(line) for line in self.lines()), default=0)

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self.lines())

    def render(self) -> Text:
        return Text("\n".join(self.lines()), no_wrap=True, end="")
