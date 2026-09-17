# cheat

Open a command cheatsheet for PowerShell, bash, cmd or macOS in your browser, from SS64 or tldr pages.

## Usage

```
kit cheat [topic] [command] [--tldr | --ss64] [--window | --no-window] [--print] [--no-check]
kit cheat --list
```

- With no arguments it opens the full command list for your system: PowerShell on Windows, bash on Linux,
  macOS commands on a Mac.
- Topics: `ps` (also `powershell`, `pwsh`), `bash` (`linux`, `sh`), `cmd` (`bat`, `nt`), `mac` (`macos`, `osx`).
- Name a command to go straight to its page. PowerShell aliases work too: `kit cheat ps gci` opens `Get-ChildItem`.
- The page is checked online first (3-second limit). If the site has no page for that command, a search opens
  instead. If you're offline, the link is opened anyway without the check. `--no-check` skips the check.
- Sites:
  - [SS64](https://ss64.com) (default) is a full reference with every option, for PowerShell, bash, cmd and macOS.
  - [tldr pages](https://tldr.inbrowser.app) (`--tldr`) shows short, practical examples for each command.
- `--window` opens the page in its own app window without tabs, using Edge/Chrome/Chromium. It falls back
  to a normal tab.
- `--print` only prints the link, which is useful in scripts and in `kit hub`. On a Linux machine without a
  desktop, the link is always printed instead of opened.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `cheat.site` | `ss64` | Site to use: `ss64` or `tldr` (`--tldr` / `--ss64`) |
| `cheat.topic` | `auto` | Topic when you don't name one: `auto`, `ps`, `bash`, `cmd` or `mac` |
| `cheat.window` | `false` | Open pages in their own app window (`--window` / `--no-window`) |

```
kit config set cheat.site tldr
```

## Examples

```
kit cheat                  # the PowerShell (Windows) or bash (Linux) command list
kit cheat bash             # bash commands, on any system
kit cheat ps gci           # Get-ChildItem
kit cheat grep             # grep in your default topic
kit cheat cmd robocopy     # robocopy for cmd
kit cheat --tldr tar       # short tar examples
kit cheat bash find -p     # just print the link
```
