# fetch

Show system information next to colourful ASCII art, in the style of neofetch.

## Usage

```
kit fetch [--art NAME|FILE] [--figlet TEXT] [-f FONT] [--color COLOR] [--stack] [--no-palette]
kit fetch --list-art
```

- Shows user@host, OS, host model, kernel, uptime, shell, terminal, resolution, CPU, GPU, memory, disks,
  local IP and battery (when there is one). Anything that can't be read is skipped.
- The art comes from a text file in `art/`. By default kit picks `art/<distro>.txt` (Linux, e.g. `ubuntu.txt`),
  then `art/<windows|linux|macos>.txt`, then `art/default.txt`.
- `--art` takes a name from `art/` (e.g. `--art default`) or a path to any text file.
  Make your own art the default with `kit config set fetch.art <name or path>` (or `KIT_FETCH_ART`).
- `--figlet TEXT` draws a figlet banner as the art instead; `-f` picks its font.
- `--color` sets the colour of the labels and of art without colour tokens (default: cyan).
- The art sits beside the info when the terminal is wide enough, otherwise above it. `--stack` always puts it above.

## Art files

Plain text. A colour token switches colour from that point on, and the colour carries over to the next lines:

`{red}` `{green}` `{yellow}` `{blue}` `{magenta}` `{cyan}` `{white}` `{gray}` `{black}` `{accent}` `{bold}` `{reset}`

`{accent}` is the `--color` colour. A file with no tokens at all is drawn in that colour.

```
{gray}   .--.
{gray}  |{white}o{gray}_{white}o{gray} |
{gray}  |{yellow}:_/{gray} |
{white} //   \ \
{yellow}(|     | )
```

Save your own as `art/<name>.txt` and use `kit fetch --art <name>`, or keep it anywhere and pass its path.

## Settings

Set these once with `kit config set`, or edit the settings file with `kit config edit`. A flag still wins for that run.

| Setting | Default | What it does |
|---|---|---|
| `fetch.art` | *(art for this OS)* | Art name or text file path. `$KIT_FETCH_ART` overrides it. |
| `fetch.color` | `cyan` | Colour of the labels and of art without colour tokens |
| `fetch.palette` | `true` | Show the colour swatches (`--no-palette` / `--palette`) |
| `fetch.stack` | `false` | Always put the art above the info (`--stack` / `--no-stack`) |

```
kit config set fetch.art biohazard
kit config set fetch.color magenta
```

## Examples

```
kit fetch                          # art for this OS
kit fetch --art default            # the bundled terminal-screen art
kit fetch --art D:\art\dragon.txt  # your own art file
kit fetch --figlet "$env:COMPUTERNAME" -f small --color magenta
kit fetch --list-art               # bundled art names
```
