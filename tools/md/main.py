"""Convert a Markdown file into a styled PDF (via headless Edge/Chrome) or HTML page."""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template
from urllib.parse import unquote

from kitlib import KIT_HOME, die, style
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
COLOR_RE = re.compile(r"#?[0-9a-fA-F]{3,8}|[a-zA-Z]+")
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

CSS = Template("""\
:root {
  --accent: $accent;
  --h2: #0f766e;
  --h3: #7c3aed;
  --h4: #b45309;
  --text: #1f2328;
  --muted: #59636e;
  --border: #d1d9e0;
  --code-bg: #f6f8fa;
  --quote-bg: #f3f6fb;
}
@page { size: $paper; margin: 18mm 16mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0;
  background: #fff;
  color: var(--text);
  font: 11pt/1.6 "Segoe UI", "Helvetica Neue", Arial, "Noto Sans", sans-serif;
}
.document { max-width: 820px; margin: 0 auto; padding: 48px 32px; }
@media print { .document { max-width: none; padding: 0; } }
.document > :first-child { margin-top: 0; }

h1, h2, h3, h4, h5, h6 {
  line-height: 1.25;
  margin: 1.6em 0 0.6em;
  font-weight: 700;
  break-after: avoid;
}
h1 { color: var(--accent); font-size: 2em; padding-bottom: 0.3em; border-bottom: 3px solid var(--accent); }
h2 { color: var(--h2); font-size: 1.5em; padding-bottom: 0.25em; border-bottom: 1px solid var(--border); }
h3 { color: var(--h3); font-size: 1.25em; }
h4 { color: var(--h4); font-size: 1.05em; }
h5, h6 { color: var(--muted); font-size: 1em; }

p, ul, ol, table, pre, blockquote, dl { margin: 0 0 1em; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
strong { font-weight: 650; }
ul, ol { padding-left: 1.6em; }
li + li { margin-top: 0.25em; }
li > ul, li > ol { margin: 0.25em 0 0; }
.task-list-item { list-style: none; }
.task-list-item-checkbox { margin: 0 0.5em 0 -1.4em; vertical-align: middle; }

code {
  font-family: "Cascadia Code", Consolas, "SFMono-Regular", Menlo, monospace;
  font-size: 0.9em;
  background: var(--code-bg);
  padding: 0.15em 0.35em;
  border-radius: 4px;
}
pre {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 6px;
  padding: 12px 14px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: 0.85em; }

blockquote {
  margin-left: 0;
  padding: 0.6em 1em;
  color: var(--muted);
  background: var(--quote-bg);
  border-left: 4px solid var(--h2);
  border-radius: 0 6px 6px 0;
}
blockquote > :last-child { margin-bottom: 0; }

table { border-collapse: collapse; width: 100%; font-size: 0.95em; break-inside: avoid; }
th, td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: var(--accent); color: #fff; }
tbody tr:nth-child(even) td { background: var(--code-bg); }

hr { border: 0; border-top: 2px solid var(--border); margin: 2em 0; }
img { max-width: 100%; }
.footnotes { font-size: 0.9em; color: var(--muted); }
""")


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


def render_page(source: Path, accent: str, paper: str, image_base: Path | None) -> str:
    md = build_markdown(image_base)
    env: dict = {}
    tokens = md.parse(source.read_text(encoding="utf-8-sig"), env)
    body = md.renderer.render(tokens, md.options, env)
    # Pygments rules first, so the page's own pre/code styling wins where they overlap.
    css = HtmlFormatter().get_style_defs("pre") + "\n" + CSS.substitute(accent=accent, paper=paper)
    title = document_title(tokens) or source.stem
    return PAGE.substitute(title=html.escape(title), css=css, body=body)


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


def main() -> int:
    conf = tool_settings()
    parser = argparse.ArgumentParser(prog="kit md", description="Convert a Markdown file into a styled PDF or HTML page.")
    parser.add_argument("input", type=Path, help="Markdown file to convert")
    parser.add_argument("-o", "--output", type=Path, help="output file (default: next to the input, same name)")
    parser.add_argument("--html", action="store_true", help="write an HTML page instead of a PDF (implied by -o *.html)")
    parser.add_argument("--open", action="store_true", help="open the result when done")
    parser.add_argument("--accent", default=conf.get("accent", "#1f6feb"),
                        help="colour of main headings, links and table headers (setting: md.accent, default: #1f6feb)")
    parser.add_argument("--paper", default=conf.get("paper", "A4"), choices=PAPER_SIZES,
                        help="page size for PDF output (setting: md.paper, default: A4)")
    parser.add_argument("--browser",
                        help="Edge/Chrome/Chromium executable for PDF output (default: the kit.browser setting / $KIT_BROWSER, else auto-detect)")
    args = parser.parse_args()

    source = args.input.expanduser().resolve()
    if not source.is_file():
        die(f"file not found: {args.input}")
    if not COLOR_RE.fullmatch(args.accent):
        die(f"not a colour: '{args.accent}' (use hex like #1f6feb or a name like teal)")
    accent = args.accent if args.accent.startswith("#") or not re.fullmatch(r"[0-9a-fA-F]{3,8}", args.accent) else f"#{args.accent}"

    as_html = args.html or (args.output is not None and args.output.suffix.lower() in (".html", ".htm"))
    output = (args.output.expanduser() if args.output else source.with_suffix(".html" if as_html else ".pdf")).resolve()
    if output == source:
        die("the output would overwrite the input file - pick another name with -o")
    output.parent.mkdir(parents=True, exist_ok=True)

    if as_html:
        # Rewrite image paths only if the page won't sit next to the Markdown file.
        image_base = None if output.parent == source.parent else source.parent
        output.write_text(render_page(source, accent, args.paper, image_base), encoding="utf-8")
    else:
        browser = pick_browser(args.browser)
        write_pdf(render_page(source, accent, args.paper, source.parent), output, browser)

    print(f"{style('wrote', 'bold', 'green')} {output}")
    if args.open:
        open_file(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
