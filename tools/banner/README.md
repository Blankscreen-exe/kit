# banner

Render text as a big ASCII-art banner using figlet.

## Usage

```
kit banner [-f FONT] [-w WIDTH] <text...>
kit banner --fonts
```

On Windows this uses the FIGlet build bundled in `vendor/figlet-win32`. On Linux/macOS it uses
the system `figlet` (`sudo apt install figlet` or `brew install figlet`). Text can also be piped in.

## Adding fonts

Drop font files into `tools/banner/fonts/` and they work straight away, on every platform, next to
figlet's own fonts. The file name is the font name (`ANSI Shadow.flf` → `-f "ANSI Shadow"`), and names
aren't case-sensitive. Commit the file and your other machines get it with `kit update`.

- Both FIGlet fonts (`.flf`) and TOIlet fonts (`.tlf`) work. figlet can't read TOIlet fonts itself, so kit
  gives it a converted copy, kept in `%LOCALAPPDATA%\kit\figlet-fonts` (`~/.cache/kit/figlet-fonts` on
  Linux).
- A font here wins over a figlet font with the same name.
- Good places to find fonts: [xero/figlet-fonts](https://github.com/xero/figlet-fonts) (hundreds of
  them) and [figlet.org's font database](http://www.figlet.org/fontdb.cgi).
- `kit fetch --figlet TEXT -f FONT` can use these fonts too.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `banner.font` | `standard` | Font used when you don't pass `-f` |

```
kit config set banner.font "ANSI Shadow"
```

## Examples

```
kit banner hello world
kit banner -f "ANSI Shadow" deploy     # a font from tools/banner/fonts
kit banner -f pagga Da PowerShell      # blocky shaded TOIlet font
kit banner --fonts                     # list available fonts
"deploy done" | kit banner -f small    # piped input
```
