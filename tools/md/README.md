# md

Convert a Markdown file into a styled PDF or HTML page with coloured headings, highlighted code and tables.

## Usage

```
kit md <file.md> [-o OUTPUT] [-s STYLE] [-a ACCENT] [-y] [--html] [--open] [--paper SIZE] [--browser PATH]
kit md --list-styles
```

Run with just the file and it asks three questions:

```
> Output file location (leave blank for same folder as target file):
   1  default   Colourful headings, blue table headers and code edges (default)
   2  academic  Serif type and justified text, like a printed paper
   ...
> Select the PDF style (leave blank for "default"):
   0     none    the style's own colours (default)
   1  ██ blue    #1f6feb
   2  ██ teal    #0f766e
   ...
> Select the accent colour (leave blank for "none"):
```

- Output location: blank writes next to the input (`notes.md` -> `notes.pdf`). A folder (an existing
  one, one ending in `/` or `\`, or a name without an extension) puts `notes.pdf` in it, created if needed.
  A file name is used as is; ending it in `.html` writes an HTML page instead of a PDF.
- Style: type its name or its number.
- Accent colour: `none` (the default) keeps the style exactly as it is. Pick a colour (name, number, or a
  hex value like `#c2410c`) to theme the style with it: every heading level gets its own shade, and code
  blocks, links, quotes, table headers and rules are tinted to match. Works with every style.
- A question is skipped when you pass its answer as a flag (`-o`, `-s`, `-a`). `-y` skips them all and uses the
  defaults. Nothing is asked when input isn't a terminal (scripts, pipes, kit hub).
- `--html` writes a standalone HTML page instead, for viewing in a browser. Styles apply to it too.
- PDFs are printed by headless Microsoft Edge, Google Chrome or Chromium, found automatically.
  Pass `--browser PATH`, or set `kit.browser` (or `KIT_BROWSER`), to pick one.
- `--paper`: `A4` (default), `Letter`, `Legal`, `A3` or `A5`.
- `--open` opens the result when it's done.

Supports CommonMark plus tables, strikethrough, task lists, footnotes and syntax-highlighted code blocks.
YAML front matter at the top of the file is skipped.

## Styles

| Style | Look |
|---|---|
| `default` | Colourful headings, blue table headers and code edges |
| `academic` | Serif type, justified text, booktabs tables |
| `dark` | Dark background for reading on screen (heavy on ink if printed) |
| `github` | Plain, like a README on GitHub |
| `minimal` | Black and white, no backgrounds, cheap to print (an accent colours text and lines only) |

### Adding a style

Every `.css` file in `tools/md/styles/` is a style named after the file, so adding one is a matter of
dropping a file in. The easiest start is a copy: `styles/default.css` -> `styles/mystyle.css`. It is
then listed in the prompt, accepted by `-s mystyle` and usable as `md.style`.

- The comment at the top supplies the details shown in the list:
  ```css
  /*
   * description: One line shown next to the name
   * pygments: monokai
   * scheme: dark
   */
  ```
  `pygments` is the colour theme for code blocks: any Pygments style name
  (`python -c "from pygments.styles import get_all_styles; print(*get_all_styles())"`). Default: `default`.
  `scheme: dark` is for dark pages, so accent shades are made lighter instead of darker. Default: `light`.
- Accent colours: write each colour as `var(--accent-..., <your own colour>)`. Your colour is used
  normally; when an accent is picked, the matching shade replaces it. Available shades:

  | Variable | Meant for |
  |---|---|
  | `--accent` | links, code-block edges, small highlights |
  | `--accent-h1` ... `--accent-h5` | heading text, one per level (`h5` also covers `h6`) |
  | `--accent-strong` | solid fills with white text on them, e.g. table headers |
  | `--accent-soft` | light backgrounds: quotes, inline code, header rows |
  | `--accent-softer` | the lightest backgrounds: code blocks, striped table rows |
  | `--accent-border` | rules, borders and heading underlines |

  For example `h2 { color: var(--accent-h2, #0f766e); }`. Anything you don't wrap stays fixed.
- `$paper` (the page size) is filled in; write `$$` for a literal `$`.
- `styles/_base.css` is loaded before every style and holds the shared layout (page size and margins,
  page breaks, task-list checkboxes). Override anything from it in your style, e.g. `@page { margin: 25mm; }`.
  Files starting with `_` aren't styles.
- For a coloured page in PDFs, set `@page { background: ... }` as well as the body's, or the margins stay white.
- Preview quickly with `kit md notes.md -s mystyle -a teal --html --open`.

### Adding an accent colour

Add a line to `ACCENTS` near the top of `main.py`: a name and a hex value. Choose a shade dark enough
to read as text on white; dark styles lighten it automatically.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `md.style` | `default` | Style offered first in the prompt, and used with `-y` or when nothing can be asked |
| `md.accent` | `none` | Accent colour offered first and used with `-y`: `none`, a name from the list or a hex value |
| `md.paper` | `A4` | Page size for PDFs: `A4`, `Letter`, `Legal`, `A3` or `A5` |
| `kit.browser` | *(found automatically)* | Browser that prints PDFs. `$KIT_BROWSER` overrides it. |

```
kit config set md.style academic
kit config set md.paper Letter
kit config set md.accent orange
```

## Examples

```
kit md notes.md                           # asks where to put it, which style and which colour
kit md notes.md -y                        # notes.pdf next to notes.md, default style, no questions
kit md notes.md -s dark --html --open     # notes.html in the dark style, opened in your browser
kit md README.md -o out/ -s github        # out/README.pdf
kit md report.md -s academic -a navy -y   # academic paper with navy headings and rules
kit md report.md -a "#c2410c" -y          # any hex works too (quote it: # starts a comment in shells)
```
