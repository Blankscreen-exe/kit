# kit

A personal toolbox for the terminal. One command, `kit`, lists every tool, shows its docs and runs it,
from PowerShell, cmd, or a Linux/macOS shell.

```text
$ kit
kit - personal toolbox

  DOCS
    md      md2pdf  Convert a Markdown file into a styled PDF or HTML page with coloured headings, highlighted code and tables.

  TEXT
    banner          Render text as a big ASCII-art banner using figlet.

  TIME
    time    t       Time arithmetic for tracking hours: span between clock times, duration differences and totals.

run:       kit <tool> [args]      docs: kit help <tool>
built-ins: list, help, new, doctor, path
```

## Install

Needs [**uv**](https://docs.astral.sh/uv/). It runs kit and its tools in one shared Python environment
and fetches a suitable Python if you don't have one.

**Windows (PowerShell)**

```powershell
.\install.ps1 -DryRun    # preview the changes
.\install.ps1            # apply them
```

**Linux / macOS**

```sh
./install.sh --dry-run
./install.sh
```

Then open a new terminal and run `kit`. To undo everything, use `.\install.ps1 -Uninstall` or `./install.sh --uninstall`.

### What the installer changes

| | Windows | Linux / macOS |
|---|---|---|
| Put `kit` on PATH | appends `bin\` to your **user PATH** | writes a two-line `kit` launcher into `~/.local/bin` |
| `KIT_HOME` | user environment variable | exported from `~/.bashrc` / `~/.zshrc` |
| Tab completion | commented block in your PowerShell profile | same block in `~/.bashrc` |
| Python packages | `uv sync` creates `.venv\` in the repo | `uv sync` creates `.venv/` in the repo |
| Backups | `%LOCALAPPDATA%\kit\backups\` | `<file>.kit-backup` |

The shell block sits between `# >>> kit >>>` and `# <<< kit <<<` markers. Re-running the installer updates
the block in place, and uninstalling removes only that block. Uninstalling leaves `.venv` alone.

## Commands

| Command | What it does |
|---|---|
| `kit` or `kit list [text]` | List tools, optionally filtered |
| `kit help <tool>` | Show the tool's README plus where it lives |
| `kit <tool> [args]` | Run a tool; aliases work too (`kit t sum 1:30 2h`) |
| `kit new <name> [--lang py\|ps1\|sh]` | Create a new tool folder from a template |
| `kit doctor` | Check the install, the uv environment and every tool folder |
| `kit path [tool]` | Print a folder, e.g. `code (kit path)` or `cd (kit path time)` |

## Adding a tool

Every sub-folder of `tools/` is a tool, and the folder name is the command. There's no list to update.

```text
tools/
  my-tool/
    main.py       required: the entry point (or main.ps1, main.sh, ...)
    README.md     recommended: the docs; its first line of text is the summary in `kit list`
    tool.json     optional: extra settings
```

`kit new my-tool` creates that layout for you. Delete the folder and the tool is gone.

### Entry point

kit looks for the first of these that exists, depending on the platform:

| Platform | Looked for, in order | Run with |
|---|---|---|
| Windows | `main.py`, `main.ps1`, `main.cmd`, `main.bat`, `main.exe` | kit's Python / PowerShell / cmd / directly |
| Linux, macOS | `main.py`, `main.sh`, `main` | kit's Python / bash / directly |

A tool that has only `main.ps1` shows as Windows-only, and one with only `main.sh` as Linux/macOS-only.

Tools receive `KIT_HOME`, `KIT_TOOL` and `KIT_TOOL_DIR` in their environment. Python tools can
`from kitlib import KIT_HOME, style, error, warn, die` for coloured output and error handling.

### Python packages

The repo is a single uv project, and every Python tool runs in its one environment. If a tool needs a package,
add it from the repo root and commit `pyproject.toml` and `uv.lock`:

```sh
uv add requests
```

Put a `# tools/<name>` comment above a tool's packages in `pyproject.toml` so it's clear which tool needs what.

### Docs

`README.md` is the tool's documentation. `kit help <tool>` prints it, and the first line of prose after the
title becomes the summary in `kit list`. Stick to the usual sections: a one-line summary, then Usage and Examples.

### tool.json (optional)

```json
{
  "summary": "Overrides the summary taken from README.md",
  "category": "git",
  "aliases": ["gc"],
  "entry": "run.py",
  "platforms": ["windows"]
}
```

| Field | Default | Meaning |
|---|---|---|
| `summary` | first line of README | One-liner shown in `kit list` |
| `category` | `misc` | Group heading in `kit list` |
| `aliases` | none | Short names, e.g. `kit gc` |
| `entry` | auto-detected | A file name, or per platform: `{"windows": "run.ps1", "linux": "run.sh"}` |
| `platforms` | all | Limit the tool to some of `windows`, `linux`, `macos` |

### Tools outside this repo

Set `KIT_PATH` to one or more extra folders (separated by `;` on Windows, `:` elsewhere). Their sub-folders
are picked up exactly like `tools/`. This is handy for private or work-only tools.

## Layout

```text
kit.py            entry point
core/             registry (finds tools), runner, CLI, `kit new` templates
lib/kitlib/       helpers Python tools can import
tools/            one folder per tool
vendor/           bundled third-party binaries (figlet for Windows)
bin/              launchers: kit.ps1 (PowerShell), kit.cmd (cmd), kit (sh)
shell/            tab completion for PowerShell and bash
pyproject.toml    the Python packages tools use (uv.lock pins exact versions)
install.ps1       Windows installer / uninstaller
install.sh        Linux/macOS installer / uninstaller
```

## Environment variables

| Variable | Purpose |
|---|---|
| `KIT_PYTHON` | Skip uv and run kit with this Python interpreter instead |
| `KIT_PATH` | Extra folders to load tools from |
| `KIT_BROWSER` | Browser the `md` tool uses to print PDFs |
| `NO_COLOR` | Turn off coloured output |
