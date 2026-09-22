# kit

A personal toolbox for the terminal. One command, `kit`, lists every tool, shows its docs and runs it,
from PowerShell, cmd, or a Linux/macOS shell. `kit hub` does the same from a dashboard in your browser.

```text
$ kit
kit - personal toolbox

  DEV
    dev                            Developer utilities: UUIDs, hashes, base64, JWT decoding, timestamps, JSON, URL encoding and secrets.
    docker-view     dv             A local web dashboard for Docker containers, images, volumes and networks, with action buttons and the matching docker commands.

  DOCS
    cheat           cheatsheet     Open a command cheatsheet for PowerShell, bash, cmd or macOS in your browser, from SS64 or tldr pages.
    md              md2pdf         Convert a Markdown file into a styled PDF or HTML page with coloured headings, highlighted code and tables.
    merge-pdf                      Merge images into a single PDF after arranging them on a local page with thumbnails.

  MEDIA
    media           ff             Convert, trim, compress and resize videos, make GIFs and pull out audio, with short ffmpeg commands.

  NETWORK
    hosts                          View and edit the hosts file safely: list, add, block, disable and remove entries, with a backup before every change.
    internet-speed                 Test your connection's latency, jitter, download and upload speed against Cloudflare, and time TCP connections to any host.
    port            ports          See what's using network ports, stop it, and find free ports.
    serve                          Share a folder, or an app running on this computer, with other devices on your network, with a QR code for your phone.
    ssh                            List, add, remove and connect to the SSH hosts saved in your ~/.ssh/config.

  PRODUCTIVITY
    countdown       timer          A big countdown timer in the terminal that you can click or drive with keys, with an alert when time is up.
    pad             scratch, note  A plain scratch pad in the terminal that saves as you type, for drafting messages and parking links.
    pomodoro        pomo           A pomodoro focus timer in the terminal that you can drive with the mouse or keyboard, and that keeps a history of your sessions.

  SYSTEM
    clean                          Find and safely free disk space: temp files, the trash, package-manager caches and Docker leftovers.
    env                            See and permanently change environment variables and PATH, on Windows and Linux.
    fetch           neofetch       Show system information next to colourful ASCII art, in the style of neofetch.
    pathfix                        Find and clean up duplicate, missing and redundant folders in your PATH.
    top             procs          Find suspicious programs: a live process list that flags odd locations, fake system names, missing signatures, shady command lines, backdoor ports and autostart entries.

  TEXT
    banner                         Render text as a big ASCII-art banner using figlet.

  TIME
    time            t              Time arithmetic for tracking hours: span between clock times, duration differences and totals.

  UTILS
    inbrowser       ib             Open inbrowser.app, a big set of tools that run inside your browser, offline.
    qr                             Show a QR code in the terminal for any text or URL, or save it as a PNG or SVG.

run:       kit <tool> [args]      docs: kit help <tool>
built-ins: list, help, new, doctor, path, config, update, hub, share
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

Then open a new terminal and run `kit`. The shell block also defines a small `kit` function: it runs kit as usual, and lets
`kit env set|unset|path` update the terminal you're in as well as the saved settings.

To undo everything, use `.\install.ps1 -Uninstall` or `./install.sh --uninstall`.

### What the installer changes

| | Windows | Linux / macOS |
|---|---|---|
| Put `kit` on PATH | appends `bin\` to your **user PATH** | writes a two-line `kit` launcher into `~/.local/bin` |
| `KIT_HOME` | user environment variable | exported from `~/.bashrc` / `~/.zshrc` |
| Tab completion + `kit` shell function | commented block in your PowerShell profile | same block in `~/.bashrc` |
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
| `kit hub` | Open the kit dashboard in your browser |
| `kit share start <tool> [--name N] [-- args]` | Launch a web tool in the background, tracked by kit |
| `kit share list` \| `stop <name>\|--all` \| `logs <name> [-f]` | See, stop or read the output of what's currently shared |
| `kit config ...` | See and change settings for kit and every tool |
| `kit update` | Update kit from GitHub, install packages and run doctor |
| `kit new <name> [--lang py\|ps1\|sh]` | Create a new tool folder from a template |
| `kit doctor` | Check the install, the uv environment, the settings file and every tool folder |
| `kit path [tool]` | Print a folder, e.g. `code (kit path)` or `cd (kit path time)` |

## Settings

All settings for kit and its tools live in one commented TOML file **outside the repo**, so your personal
choices never get pushed to GitHub:

- Windows: `%APPDATA%\kit\config.toml`
- Linux/macOS: `~/.config/kit/config.toml` (or `$XDG_CONFIG_HOME/kit/config.toml`)
- `KIT_CONFIG` points kit at a different file.

```toml
[kit]
update_check = true

[fetch]
art = "biohazard"
color = "magenta"

[pomodoro]
focus = 50
```

```sh
kit config                          # every setting: value, where it comes from, help
kit config list fetch               # one section
kit config set fetch.color magenta  # checked against the tool's allowed values
kit config get pomodoro.focus
kit config unset fetch.color        # back to the default
kit config reset fetch              # clear a whole section
kit config edit                     # open the file in your editor
kit config path
```

When the same setting is given in several places, this order wins, highest first:
**command-line flag → the setting's environment variable → settings file → default**.
Editing by hand is fine: `kit config set` keeps your comments. `kit doctor` reports a broken file, unknown keys and
invalid values.

kit's own settings, in `[kit]`:

| Setting | Default | What it does |
|---|---|---|
| `kit.update_check` | `true` | Check GitHub for updates once a day and print a one-line notice |
| `kit.hub_port` | `9800` | Port `kit hub` tries first |
| `kit.hub_open` | `true` | Open the hub in your browser when it starts |
| `kit.browser` | *(found automatically)* | Edge/Chrome/Chromium used for PDFs and app windows. `$KIT_BROWSER` overrides it. |

Each tool's settings are listed in its README (`kit help <tool>`) and on the Settings tab of `kit hub`.

## Updating

```sh
kit update           # pull the latest kit, install packages (uv sync), run doctor
kit update --check   # just see whether there's anything new
```

`kit update` only fast-forwards. It refuses if you have uncommitted changes to kit's files, or local commits that
aren't on GitHub, and tells you how to sort that out. Once a day kit checks for updates in the background and
prints `kit: an update is available (N new commits) - run: kit update` after a command. Turn that off with
`kit config set kit.update_check false`.

## kit hub

`kit hub` opens a local dashboard in your browser (`--no-open` just prints the address, `--port N` picks the port).

- **Tools**: every tool with its docs. Run command-line tools with arguments and watch their output live
  (Stop ends the whole process). Web tools (docker-view, serve, merge-pdf) get a Launch button and an Open link;
  terminal apps (pomodoro, countdown) open in a new terminal window. Input is closed, so tools that ask
  for confirmation need `--yes`.
- **Settings**: every setting for kit and each tool, with its default, where the current value comes from
  (file / environment variable / default), Save and Reset.
- **Updates**: check GitHub for new commits and update in place (restart the hub afterwards).
- **Doctor**: run `kit doctor` and see the results.

It only listens on 127.0.0.1 and needs the one-time token in the printed address, so other web pages and
programs can't use it to run commands.

`kit hub --lan` also binds your network, so another device can open the same dashboard without typing a
token: anyone there can see what's running and open its links, but running, stopping or changing anything
still only works from the machine `kit hub` is running on, token or not.

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
title becomes the summary in `kit list`. Stick to the usual sections: a one-line summary, then Usage, Settings
(if it has any) and Examples.

### tool.json (optional)

```json
{
  "summary": "Overrides the summary taken from README.md",
  "category": "git",
  "aliases": ["gc"],
  "entry": "run.py",
  "platforms": ["windows"],
  "settings": {
    "remote": {"type": "string", "default": "origin", "help": "Remote to clean against"},
    "days": {"type": "int", "default": 30, "min": 1, "help": "Only branches older than this"}
  },
  "interactive": false,
  "web": false
}
```

| Field | Default | Meaning |
|---|---|---|
| `summary` | first line of README | One-liner shown in `kit list` |
| `category` | `misc` | Group heading in `kit list` |
| `aliases` | none | Short names, e.g. `kit gc` |
| `entry` | auto-detected | A file name, or per platform: `{"windows": "run.ps1", "linux": "run.sh"}` |
| `platforms` | all | Limit the tool to some of `windows`, `linux`, `macos` |
| `settings` | none | Settings the tool reads from the settings file (see below) |
| `interactive` | `false` | A full-screen terminal app; `kit hub` opens it in a terminal window instead of running it in the page |
| `web` | `false` | Starts a local web page; `kit hub` gives it a Launch button and an Open link |

Each entry in `settings` has a `type` (`string`, `int`, `float`, `bool` or `choice`), and optionally `default`,
`help`, `choices` (for `choice`), `min`/`max` (for numbers) and `env` (an environment variable that overrides the
file). In a Python tool, read them with:

```python
from kitlib.settings import tool_settings

settings = tool_settings()  # defaults, then the settings file, then env vars
parser.add_argument("--days", type=int, default=settings["days"])  # a flag still wins
```

`kit config` and `kit hub` pick the new settings up automatically, and validate values against the declaration.

### Tools outside this repo

Set `KIT_PATH` to one or more extra folders (separated by `;` on Windows, `:` elsewhere). Their sub-folders
are picked up exactly like `tools/`. This is handy for private or work-only tools.

## Layout

```text
kit.py            entry point
core/             registry (finds tools), runner, CLI, `kit new` templates, `kit config`, `kit update`, hub/
lib/kitlib/       helpers Python tools can import (output, settings, figlet, QR codes, browser, clipboard)
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
| `KIT_CONFIG` | Use a different settings file |
| `KIT_PYTHON` | Skip uv and run kit with this Python interpreter instead |
| `KIT_PATH` | Extra folders to load tools from |
| `KIT_BROWSER` | Browser for PDFs and app windows (overrides `kit.browser`) |
| `KIT_FETCH_ART` | Default art for `kit fetch` (overrides `fetch.art`) |
| `KIT_SSH_CONFIG` | SSH config file `kit ssh` reads and edits (overrides `ssh.config`) |
| `KIT_POMODORO_DATA` | Where `kit pomodoro` keeps its history (default `%APPDATA%\kit\pomodoro.json` / `~/.local/share/kit/pomodoro.json`) |
| `KIT_BACKUP_DIR` | Where `kit ssh`, `kit pathfix`, `kit env` and `kit hosts` save backups before changing files |
| `KIT_PAD_DIR` | Where `kit pad` keeps its pads (default `%APPDATA%\kit\pad` / `~/.local/share/kit/pad`) |
| `KIT_TOP_DATA` | Where `kit top` keeps its list of trusted programs |
| `KIT_INBROWSER_DATA` | Where `kit inbrowser` caches the list of inbrowser.app tools |
| `KIT_FFMPEG` | ffmpeg `kit media` uses: the program or its folder (overrides `media.ffmpeg`) |
| `KIT_STATE_DIR` | Where kit keeps its update-check state |
| `NO_COLOR` | Turn off coloured output |
