"""Convert a Markdown file into a styled PDF (via headless Edge/Chrome) or HTML page."""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from string import Template
from urllib.parse import unquote

from kitlib import KIT_HOME, color_enabled, die, style, warn
from kitlib.browser import configured_browser, find_browser
from kitlib.settings import tool_settings

try:
    from markdown_it import MarkdownIt
    from mdit_py_plugins.anchors import anchors_plugin
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.front_matter import front_matter_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin
    from pygments import highlight as pygments_highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound
except ImportError as exc:
    die(f"missing Python package '{exc.name}' - run 'uv sync' in {KIT_HOME} (or re-run the installer)")

PAPER_SIZES = ["A4", "Letter", "Legal", "A3", "A5"]
HEX_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
# Accent colours offered in the prompt and accepted by name in --accent / md.accent. Add one with a
# name and a hex value; pick shades dark enough to read as text on white (dark styles lighten them).
# "none" (no accent) keeps each style's own colours.
NO_ACCENT = "none"
ACCENTS = {
    "blue": "#1f6feb",
    "teal": "#0f766e",
    "green": "#15803d",
    "purple": "#7c3aed",
    "pink": "#db2777",
    "red": "#dc2626",
    "orange": "#c2410c",
    "amber": "#b45309",
    "navy": "#1e3a8a",
    "slate": "#475569",
}
# src values left alone when rewriting image paths: URLs with a scheme, absolute paths, anchors
ABSOLUTE_SRC_RE = re.compile(r"^([a-zA-Z][\w+.-]*:|/|#)")

PAGE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="kit md">
<title>$title</title>
<style>
$css
</style>
</head>
<body>
<main class="document">
$body
</main>
</body>
</html>
""")

# One CSS file per PDF style: drop a new .css file in here and it is offered everywhere. See README.md.
STYLES_DIR = Path(__file__).resolve().parent / "styles"
BASE_CSS = STYLES_DIR / "_base.css"
DEFAULT_STYLE = "default"
# "key: value" lines inside a style file's leading /* ... */ comment
STYLE_FIELD_RE = re.compile(r"^[\s*]*(\w+)\s*:\s*(.+?)\s*$", re.M)


@dataclass
class PdfStyle:
    name: str
    path: Path
    description: str = ""
    pygments: str = "default"  # Pygments theme for code blocks
    scheme: str = "light"  # "dark" for a dark page: accent shades are made lighter instead of darker


def load_styles() -> dict[str, PdfStyle]:
    """Every styles/*.css except _-prefixed files, keyed by lower-case name, 'default' first."""
    styles: dict[str, PdfStyle] = {}
    for path in sorted(STYLES_DIR.glob("*.css"), key=lambda p: (p.stem.lower() != DEFAULT_STYLE, p.stem.lower())):
        if path.name.startswith("_"):
            continue
        entry = PdfStyle(path.stem.lower(), path)
        header = re.match(r"\s*/\*(.*?)\*/", path.read_text(encoding="utf-8-sig"), re.S)
        for key, value in STYLE_FIELD_RE.findall(header.group(1) if header else ""):
            if key in ("description", "pygments", "scheme"):
                setattr(entry, key, value.lower() if key == "scheme" else value)
        styles[entry.name] = entry
    if not styles:
        die(f"no PDF styles found in {STYLES_DIR}")
    return styles


def mix(colour: str, other: str, amount: float) -> str:
    """colour moved `amount` (0-1) of the way towards other; both #rrggbb."""
    a, b = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (colour, other))
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def accent_vars(accent: str, scheme: str) -> str:
    """The --accent-* custom properties styles theme themselves with (see README.md, "Adding a style").

    Headings get one shade per level, alternating a little either side of the accent so neighbouring
    levels look different without any of them washing out or turning black. "ink" is the direction
    away from the page colour: darker on light pages, lighter on dark ones.
    """
    if scheme == "dark":
        page, ink, grey = "#0d1117", "#ffffff", "#9198a1"
        base = mix(accent, ink, 0.25)  # dark accents (navy, slate) would vanish on a dark page
        strong = mix(accent, "#000000", 0.35)
        soft, softer, border = mix(accent, page, 0.82), mix(accent, page, 0.9), mix(accent, page, 0.55)
    else:
        page, ink, grey = "#ffffff", "#000000", "#59636e"
        base = strong = accent
        soft, softer, border = mix(accent, page, 0.9), mix(accent, page, 0.95), mix(accent, page, 0.65)
    shades = {
        "accent": base,
        "accent-h1": base,
        "accent-h2": mix(base, ink, 0.15),
        "accent-h3": mix(base, page, 0.2),
        "accent-h4": mix(base, ink, 0.3),
        "accent-h5": mix(base, grey, 0.5),
        "accent-strong": strong,
        "accent-soft": soft,
        "accent-softer": softer,
        "accent-border": border,
    }
    return ":root {\n" + "".join(f"  --{name}: {value};\n" for name, value in shades.items()) + "}"


def style_css(chosen: PdfStyle, accent: str, paper: str) -> str:
    """Pygments rules, the shared base, the style itself, then the accent shades (if one was picked)."""
    try:
        parts = [HtmlFormatter(style=chosen.pygments).get_style_defs("pre")]
    except ClassNotFound:
        warn(f"style '{chosen.name}': unknown pygments theme '{chosen.pygments}' - using 'default'")
        parts = [HtmlFormatter().get_style_defs("pre")]
    for path in (BASE_CSS, chosen.path):
        try:
            parts.append(Template(path.read_text(encoding="utf-8-sig")).substitute(paper=paper))
        except (KeyError, ValueError) as exc:
            die(f"{path}: bad placeholder {exc} - only $paper exists (write $$ for a literal $)")
    if accent:
        parts.append(accent_vars(accent, chosen.scheme))
    return "\n".join(parts)


def build_markdown(image_base: Path | None) -> MarkdownIt:
    """CommonMark + tables, strikethrough, task lists, footnotes, heading ids and highlighted code.

    image_base: when set, relative image paths are made absolute against it, so images still
    load when the output lives somewhere other than the Markdown file.
    """
    formatter = HtmlFormatter(nowrap=True)

    def highlight(code: str, lang: str, _attrs: str) -> str:
        try:
            lexer = get_lexer_by_name(lang) if lang else None
        except ClassNotFound:
            lexer = None
        return pygments_highlight(code, lexer, formatter) if lexer else ""  # "" = plain, escaped

    md = (
        MarkdownIt("commonmark", {"html": True, "typographer": True, "highlight": highlight})
        .enable(["table", "strikethrough", "replacements", "smartquotes"])
        .use(front_matter_plugin)
        .use(footnote_plugin)
        .use(tasklists_plugin)
        .use(anchors_plugin, max_level=4)
    )

    if image_base is not None:
        def render_image(self, tokens, idx, options, env):
            token = tokens[idx]
            src = str(token.attrGet("src") or "")
            if src and not ABSOLUTE_SRC_RE.match(src):
                token.attrSet("src", (image_base / unquote(src)).resolve().as_uri())
            return self.image(tokens, idx, options, env)

        md.add_render_rule("image", render_image)
    return md


def document_title(tokens: list) -> str:
    """Plain text of the first level-1 heading, if there is one."""
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h1" and i + 1 < len(tokens):
            children = tokens[i + 1].children or []
            return "".join(c.content for c in children if c.type in ("text", "code_inline")).strip()
    return ""


def render_page(source: Path, chosen: PdfStyle, accent: str, paper: str, image_base: Path | None) -> str:
    md = build_markdown(image_base)
    env: dict = {}
    tokens = md.parse(source.read_text(encoding="utf-8-sig"), env)
    body = md.renderer.render(tokens, md.options, env)
    title = document_title(tokens) or source.stem
    return PAGE.substitute(title=html.escape(title), css=style_css(chosen, accent, paper), body=body)


def pick_browser(explicit: str | None) -> str:
    browser = find_browser(explicit)
    if browser:
        return browser
    choice = explicit or configured_browser()
    if choice:
        die(f"browser not found: {choice}")
    die("no Edge, Chrome or Chromium found for PDF output - install one, pass --browser PATH, or use --html")


def write_pdf(page: str, output: Path, browser: str) -> None:
    try:
        output.unlink(missing_ok=True)
    except OSError:
        die(f"can't overwrite {output} - is it open in another program?")

    with tempfile.TemporaryDirectory(prefix="kit-md-", ignore_cleanup_errors=True) as tmp:
        html_file = Path(tmp) / "document.html"
        html_file.write_text(page, encoding="utf-8")
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={Path(tmp) / 'profile'}",  # separate profile: works even while the browser is open
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",  # older name of the flag above
            f"--print-to-pdf={output}",
            html_file.as_uri(),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            die("the browser took too long to print the PDF")

    if not output.is_file() or output.stat().st_size == 0:
        detail = " | ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
        die(f"PDF export failed using {browser}" + (f": {detail}" if detail else ""))


def open_file(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ask(question: str) -> str:
    try:
        return input(f"{style('>', 'bold', 'accent')} {question} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        die("cancelled", 130)


def resolve_output(text: str, source: Path, html_hint: bool) -> Path:
    """Turn -o / the prompt answer into the output file.

    Blank: next to the input. An existing folder, a path ending in a slash, or one without an
    extension: that folder, input's name. Anything else: that exact file.
    """
    text = text.strip().strip('"').strip("'")  # "Copy as path" on Windows adds quotes
    if not text:
        return source.with_suffix(".html" if html_hint else ".pdf")
    path = Path(text).expanduser()
    if text.endswith(("/", "\\")) or path.is_dir() or not path.suffix:
        return path / (source.stem + (".html" if html_hint else ".pdf"))
    return path


def find_style(name: str, styles: dict[str, PdfStyle]) -> PdfStyle | None:
    """By name (any case) or by its number in the list."""
    name = name.strip().lower()
    if name.isdigit() and 1 <= int(name) <= len(styles):
        return list(styles.values())[int(name) - 1]
    return styles.get(name)


def print_styles(styles: dict[str, PdfStyle], default: str) -> None:
    width = max(len(n) for n in styles)
    for i, entry in enumerate(styles.values(), 1):
        mark = style(" (default)", "muted") if entry.name == default else ""
        print(f"  {style(f'{i:>2}', 'muted')}  {style(entry.name.ljust(width), 'bold')}  {entry.description}{mark}")


def ask_style(styles: dict[str, PdfStyle], default: str, kind: str) -> PdfStyle:
    print_styles(styles, default)
    while True:
        answer = ask(f'Select the {kind} style (leave blank for "{default}"):')
        chosen = find_style(answer or default, styles)
        if chosen:
            return chosen
        print(f"  no style '{answer}' - type a name or a number from the list")


def parse_accent(text: str) -> str | None:
    """A preset name (any case) or a hex value with or without '#' as #rrggbb; "" for none; None if neither."""
    text = text.strip().strip("\"'")
    if text.lower() in ("", NO_ACCENT):
        return ""
    if text.lower() in ACCENTS:
        return ACCENTS[text.lower()]
    match = HEX_RE.fullmatch(text)
    if not match:
        return None
    digits = match.group(1)
    return "#" + (digits if len(digits) == 6 else "".join(c * 2 for c in digits)).lower()


def accent_label(accent: str) -> str:
    """The preset name for a colour, or the colour itself."""
    if not accent:
        return NO_ACCENT
    return next((name for name, value in ACCENTS.items() if value.lower() == accent.lower()), accent)


def swatch(hex_colour: str) -> str:
    """A small block in the colour itself, when the terminal shows colour."""
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", hex_colour)
    if not match or not color_enabled():
        return ""
    red, green, blue = (int(match.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    return f"\033[48;2;{red};{green};{blue}m  \033[0m "  # coloured background on spaces: ASCII-safe everywhere


def ask_accent(default: str) -> str:
    width = max(len(n) for n in ACCENTS)
    mark = style(" (default)", "muted") if not default else ""
    blank = "   " if color_enabled() else ""  # lines the name up with the swatches below
    print(f"  {style(' 0', 'muted')}  {blank}{style(NO_ACCENT.ljust(width), 'bold')}  "
          f"{style('the style' + chr(39) + 's own colours', 'muted')}{mark}")
    for i, (name, value) in enumerate(ACCENTS.items(), 1):
        mark = style(" (default)", "muted") if value.lower() == default.lower() else ""
        print(f"  {style(f'{i:>2}', 'muted')}  {swatch(value)}{style(name.ljust(width), 'bold')}  {style(value, 'muted')}{mark}")
    while True:
        answer = ask(f'Select the accent colour (leave blank for "{accent_label(default)}"):')
        if not answer:
            return default
        if answer.isdigit() and 0 <= int(answer) <= len(ACCENTS):
            return list(ACCENTS.values())[int(answer) - 1] if int(answer) else ""
        accent = parse_accent(answer)
        if accent is not None:
            return accent
        print(f"  no colour '{answer}' - type a name or a number from the list (or a hex value like #1f6feb)")


def main() -> int:
    conf = tool_settings()
    styles = load_styles()
    parser = argparse.ArgumentParser(
        prog="kit md",
        description="Convert a Markdown file into a styled PDF or HTML page. "
                    "Asks for the output location, style and accent colour unless given as flags (or with -y).")
    parser.add_argument("input", type=Path, nargs="?", help="Markdown file to convert")
    parser.add_argument("-o", "--output",
                        help="output file or folder (default: next to the input, same name)")
    parser.add_argument("-s", "--style",
                        help=f"look of the page: {', '.join(styles)} (setting: md.style, default: {DEFAULT_STYLE})")
    parser.add_argument("--list-styles", action="store_true", help="show the available styles and exit")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="don't ask anything: use the defaults for whatever isn't given as a flag")
    parser.add_argument("--html", action="store_true", help="write an HTML page instead of a PDF (implied by -o *.html)")
    parser.add_argument("--open", action="store_true", help="open the result when done")
    parser.add_argument("-a", "--accent",
                        help=f"theme colour for headings, links, code blocks and tables: {', '.join(ACCENTS)}, any hex value, "
                             f"or {NO_ACCENT} for the style's own colours (setting: md.accent, default: {NO_ACCENT})")
    parser.add_argument("--paper", default=conf.get("paper", "A4"), choices=PAPER_SIZES,
                        help="page size for PDF output (setting: md.paper, default: A4)")
    parser.add_argument("--browser",
                        help="Edge/Chrome/Chromium executable for PDF output (default: the kit.browser setting / $KIT_BROWSER, else auto-detect)")
    args = parser.parse_args()

    default_style = str(conf.get("style", DEFAULT_STYLE)).strip().lower() or DEFAULT_STYLE
    if default_style not in styles:
        warn(f"md.style is '{default_style}', which isn't a style - using '{DEFAULT_STYLE}'")
        default_style = DEFAULT_STYLE if DEFAULT_STYLE in styles else next(iter(styles))
    if args.list_styles:
        print_styles(styles, default_style)
        print(style(f"  add your own: drop a .css file in {STYLES_DIR}", "muted"))
        return 0
    if args.input is None:
        parser.error("the Markdown file to convert is required")

    source = args.input.expanduser().resolve()
    if not source.is_file():
        die(f"file not found: {args.input}")
    accent = None
    if args.accent is not None:
        accent = parse_accent(args.accent)
        if accent is None:
            die(f"not a colour: '{args.accent}' (use {', '.join(ACCENTS)}, {NO_ACCENT}, or hex like #1f6feb)")
    default_accent = parse_accent(str(conf.get("accent", NO_ACCENT)))
    if default_accent is None:
        warn(f"md.accent is '{conf.get('accent')}', which isn't a colour - using '{NO_ACCENT}'")
        default_accent = ""
    chosen = None
    if args.style is not None:
        chosen = find_style(args.style, styles)
        if not chosen:
            die(f"no style '{args.style}' - available: {', '.join(styles)}")

    # Ask only for what wasn't given, and only when someone is at the keyboard (not in scripts or kit hub).
    interactive = not args.yes and sys.stdin.isatty() and sys.stdout.isatty()
    answer = args.output or ""
    if args.output is None and interactive:
        answer = ask("Output file location (leave blank for same folder as target file):")
    as_html = args.html or Path(answer.strip().strip("\"'")).suffix.lower() in (".html", ".htm")
    if chosen is None:
        chosen = ask_style(styles, default_style, "page" if as_html else "PDF") if interactive else styles[default_style]
    if accent is None:
        accent = ask_accent(default_accent) if interactive else default_accent

    output = resolve_output(answer, source, as_html).resolve()
    if output == source:
        die("the output would overwrite the input file - pick another name with -o")
    output.parent.mkdir(parents=True, exist_ok=True)

    if as_html:
        # Rewrite image paths only if the page won't sit next to the Markdown file.
        image_base = None if output.parent == source.parent else source.parent
        output.write_text(render_page(source, chosen, accent, args.paper, image_base), encoding="utf-8")
    else:
        browser = pick_browser(args.browser)
        write_pdf(render_page(source, chosen, accent, args.paper, source.parent), output, browser)

    print(f"{style('wrote', 'bold', 'good')} {output}")
    if args.open:
        open_file(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
