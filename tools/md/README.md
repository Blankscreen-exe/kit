# md

Convert a Markdown file into a styled PDF or HTML page with coloured headings, highlighted code and tables.

## Usage

```
kit md <file.md> [-o OUTPUT] [--html] [--open] [--accent COLOR] [--paper SIZE] [--browser PATH]
```

- PDF is the default, written next to the input (`notes.md` -> `notes.pdf`).
- `--html` (or `-o something.html`) writes a standalone HTML page instead, for viewing in a browser.
- PDFs are printed by headless Microsoft Edge, Google Chrome or Chromium, found automatically.
  Pass `--browser PATH`, or set `kit.browser` (or `KIT_BROWSER`), to pick one.
- `--accent` colours the main headings, links, table headers and code-block edges (default `#1f6feb`).
  Lower-level headings each get their own colour.
- `--paper`: `A4` (default), `Letter`, `Legal`, `A3` or `A5`.
- `--open` opens the result when it's done.

Supports CommonMark plus tables, strikethrough, task lists, footnotes and syntax-highlighted code blocks.
YAML front matter at the top of the file is skipped.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `md.accent` | `#1f6feb` | Colour of main headings, links and table headers |
| `md.paper` | `A4` | Page size for PDFs: `A4`, `Letter`, `Legal`, `A3` or `A5` |
| `kit.browser` | *(found automatically)* | Browser that prints PDFs. `$KIT_BROWSER` overrides it. |

```
kit config set md.paper Letter
kit config set md.accent "#c2410c"
```

## Examples

```
kit md notes.md                        # notes.pdf
kit md notes.md --html --open          # notes.html, opened in your browser
kit md README.md -o out/readme.pdf     # choose where it goes
kit md report.md --accent "#c2410c"    # orange headings (quote it: # starts a comment in shells)
```
