# hosts

View and edit the hosts file safely: list, add, block, disable and remove entries, with a backup before every change.

## Usage

```
kit hosts [list] [FILTER] [--all]
kit hosts add IP NAME [NAME...] [--comment TEXT] [--replace]
kit hosts block DOMAIN... [--www] [--replace]
kit hosts unblock DOMAIN... [--www]
kit hosts remove NAME|IP...
kit hosts disable NAME|IP...
kit hosts enable NAME|IP...
kit hosts edit
kit hosts flush
kit hosts backups
kit hosts restore [BACKUP]
kit hosts path
```

Every command also takes:

- `--file PATH` to work on another file instead of the system hosts file
- `-y` / `--yes` to skip the question
- `-n` / `--dry-run` to only show what would change
- `--no-flush` to leave the DNS cache alone

The hosts file lives at `C:\Windows\System32\drivers\etc\hosts` on Windows and `/etc/hosts` on Linux/macOS.

**Listing**

- `list` shows each entry's line number, whether it's `on` or `off` (commented out), its IP, names and comment.
  `--all` also shows comments, blank lines and anything that isn't a valid entry.
- The type column tells entries apart:
  - `block`: `0.0.0.0` / `::`
  - `local`: loopback addresses, or this computer's own name
  - `system`: the IPv6 lines in the default Linux file
  - `redirect`: points a name at another machine. These are highlighted, because malware likes to add them.
- It warns about lines that aren't valid entries, and about NUL bytes that some programs leave in the file.

**Changing**

- Before writing, every change prints the lines it removes (`-`) and adds (`+`) and asks you to confirm.
  The original is then saved to `%LOCALAPPDATA%\kit\backups` or `~/.local/state/kit/backups`
  (or `$KIT_BACKUP_DIR`).
- Lines you don't touch are kept exactly as they are, including line endings, encoding and odd bytes.
- `add` refuses to point a name somewhere else when another entry already has it. `--replace` moves it.
- `block` adds `0.0.0.0 domain  # kit: blocked`, and `--www` adds `www.domain` to the same line.
  Blocking a domain again turns its disabled line back on instead of adding a new one.
- `unblock` removes blocked names.
- `remove` takes a name (removed from every line that has it) or an IP (removes all of that IP's lines).
- `disable` comments lines out so you can `enable` them later.
  If a line has several names and you pick only some of them, the line is split in two, and kit says so.
- `restore` puts back the newest backup, or the one you name (see `backups`).

**Admin rights**

- Changing the system hosts file needs admin rights.
  - Windows: kit shows the UAC prompt and copies the new file into place.
  - Linux/macOS: kit uses `sudo`, which may ask for your password.
- `edit` opens the file in Notepad as admin (Windows), or with `sudoedit` / `$EDITOR` (Linux/macOS).
  Afterwards it shows what you changed and keeps a backup.
- After a change, kit flushes the DNS cache:
  - Windows: `ipconfig /flushdns`
  - Linux: `resolvectl` or `nscd`, when one is running
  - macOS: `dscacheutil` and `mDNSResponder`

  `flush` does just that step.

**Good to know**

- Windows Defender may flag or undo entries that redirect some well-known domains (for example Microsoft's),
  and treat the change as a hosts-file hijack.
- Browsers keep their own DNS cache. If a change doesn't show up, restart the browser or clear it,
  e.g. at `chrome://net-internals/#dns` (Chrome) or `edge://net-internals/#dns` (Edge).

## Settings

| Setting | Default | What it does |
|---|---|---|
| `hosts.flush` | `true` | Flush the DNS cache after every change to the system hosts file (`--no-flush` skips it once) |

```
kit config set hosts.flush false
```

## Examples

```
kit hosts                                  # what's in there
kit hosts list example --all               # entries and comments mentioning "example"
kit hosts add 192.168.1.20 nas.home        # reach the NAS by name
kit hosts add 127.0.0.1 myapp.test api.myapp.test -c "local dev"
kit hosts block facebook.com --www         # block a site
kit hosts unblock facebook.com --www
kit hosts disable myapp.test               # switch it off for now
kit hosts enable myapp.test
kit hosts remove 192.168.1.20
kit hosts block ads.example.com -n         # preview only
kit hosts restore                          # undo the last change
kit hosts --file ./hosts.test add 10.0.0.1 box.test -y
```
