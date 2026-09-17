# env

See and permanently change environment variables and PATH, on Windows and Linux.

## Usage

```
kit env [list] [FILTER] [--current] [--show-secrets] [--full]
kit env get NAME [--user | --machine]
kit env set NAME VALUE [--machine] [-y]
kit env unset NAME [--machine] [-y]
kit env path [--machine]
kit env path add FOLDER [--front] [--machine] [-y]
kit env path remove FOLDER [--machine] [-y]
kit env backups
kit env restore [BACKUP] [-y]
```

- `list` shows the variables that are saved permanently, as **user** and **machine** variables.
  `--current` shows this terminal's variables instead.
- Values of names containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL` or `API`
  show as `****` unless you add `--show-secrets`. Long values are shortened unless you add `--full`.
- `get` prints just the value, so it's handy in scripts. It checks your user variables first, then the
  machine ones, and exits with 1 when the variable isn't set.
- `set`, `unset` and `path add/remove` show what will change and ask first (`-y` skips the question).
  A backup is saved before every change, and `kit env restore` puts the newest one back.
- `path add` puts the folder at the end of PATH. Add `--front` to put it first, so its programs win over
  others with the same name. The folder is stored as an absolute path.

**Windows**

- User variables live in `HKCU\Environment`, machine variables in `HKLM\...\Session Manager\Environment`.
  Values are shown and stored unexpanded (`%USERPROFILE%\go`). A value containing `%VARIABLES%` is saved as
  an expandable string, and `list` marks it `expands`.
- After a change, kit tells other programs about it, so new terminals see it straight away.
- `--machine` needs admin rights: use a terminal opened with *Run as administrator*, or put `sudo` in front if
  *sudo for Windows* is turned on.
- A user variable wins over a machine variable with the same name. PATH is the exception: Windows joins the
  machine PATH and the user PATH together.
- Backups are JSON snapshots of the whole key, in `%LOCALAPPDATA%\kit\backups`.

**Linux / macOS**

- User variables go in a marked block in `~/.bashrc` (and `~/.zshrc` if you have one):

  ```sh
  # >>> kit env >>>
  export GOPATH=/home/me/go
  ...
  # <<< kit env <<<
  ```

  The block is placed before any `kit pathfix` block, so pathfix still removes duplicate PATH entries.
  `list` only shows what's in this block and in `/etc/environment`. Use `--current` to see everything.
- A PATH folder is only added when it's missing. `path remove` only removes folders that kit env added.
  For any other folder, it tells you which startup files mention it.
- `--machine` edits `/etc/environment`, which is read at login and never expanded (`$HOME` stays literal).
  If you're not root, the file is written through `sudo`, which may ask for your password. macOS has no such file.
- Backups of the changed files go to `~/.local/state/kit/backups`. Restoring one of `~/.bashrc` only puts back
  the kit env block, so other changes you made to the file since then are kept.

**This terminal**

A program can't change the variables of the shell that started it. So after a change, kit env prints the
command that applies the change to the current terminal. New terminals pick it up automatically.

If kit's shell integration is installed (the installer sets it up), `kit` is a small shell function in
PowerShell and bash. For `kit env set`, `unset` and `path`, it applies the change to the current terminal
for you. Every other command runs exactly as before.

## Examples

```
kit env                                   # saved user and machine variables
kit env list java                         # just the ones with "java" in the name
kit env --current                         # everything this terminal has
kit env get JAVA_HOME
kit env set JAVA_HOME "C:\Java\jdk-21"
kit env set EDITOR nvim
kit env unset OLD_TOOL_HOME
kit env path                              # your PATH, with missing folders marked
kit env path add ~/.cargo/bin --front
kit env path add "%USERPROFILE%\tools"    # Windows: kept as %USERPROFILE%\tools
kit env path remove ~/old/bin
kit env set HTTP_PROXY http://proxy:3128 --machine   # admin terminal on Windows
kit env backups
kit env restore                           # undo the last change
```
