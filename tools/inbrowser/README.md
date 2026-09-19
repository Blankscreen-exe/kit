# inbrowser

Open inbrowser.app, a big set of tools that run inside your browser, offline.

## Usage

```
kit inbrowser                      # the tool list page
kit inbrowser <words>              # open the tool that matches
kit inbrowser --list [words]       # list the tools instead of opening one
kit inbrowser --refresh            # fetch the list of tools again
```

- [inbrowser.app](https://inbrowser.app/tools/) has around 200 small tools (hashes, base64, QR codes, colour
  pickers, JSON and JWT, image and PDF jobs, timestamps, regex, diffs and more). The pages do their work in the
  browser, so what you paste in stays on your computer, and they keep working offline once loaded.
- `kit inbrowser qr code` opens the matching tool page. A single match opens straight away; several matches are
  listed so you can be more specific, and the exact slug (`kit inbrowser json-formatter`) always wins.
- The list of tools is fetched once and kept for a week in
  `%APPDATA%\kit\inbrowser-tools.json` (`~/.local/share/kit/inbrowser-tools.json` on Linux/macOS), so searching
  is instant and works offline. `--refresh` updates it, and `KIT_INBROWSER_DATA` points somewhere else.
- `--print` writes the link instead of opening it, which is handy in scripts and when there's no browser.
- `--window` opens the page in its own window without tabs or an address bar (Edge/Chrome/Chromium).

## Settings

| Setting | Default | What it does |
|---|---|---|
| `inbrowser.window` | `false` | Open pages in their own app window instead of a browser tab (`--window` / `--no-window`) |

```
kit config set inbrowser.window true
```

## Examples

```
kit inbrowser                      # browse everything
kit ib qr                          # QR code generator
kit ib json formatter
kit inbrowser --list hash          # every hashing tool
kit inbrowser uuid --print         # just the link
```
