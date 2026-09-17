# banner

Render text as a big ASCII-art banner using figlet.

## Usage

```
kit banner [-f FONT] [-w WIDTH] <text...>
kit banner --fonts
```

On Windows this uses the FIGlet build bundled in `vendor/figlet-win32`. On Linux/macOS it uses
the system `figlet` (`sudo apt install figlet` or `brew install figlet`). Text can also be piped in.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `banner.font` | `standard` | Font used when you don't pass `-f` |

```
kit config set banner.font small
```

## Examples

```
kit banner hello world
kit banner -f Pagga Da PowerShell      # blocky shaded font
kit banner --fonts                     # list available fonts
"deploy done" | kit banner -f small    # piped input
```
