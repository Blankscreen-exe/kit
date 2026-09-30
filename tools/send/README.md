# send

Send files to someone's browser with a link, over your network or the internet.

## Usage

```
kit send                                     # open the sharing window and add files there
kit send <file> [<file>...] [--once | --max N] [--expire 2h]
kit send <file> [--lan | --internet | --public [--address HOST]] [--port N]
kit send <file> [--ui | --no-ui] [--no-open] [--no-qr] [--no-window]
kit send <file> --notify [NAMES]
```

kit serves the files itself. Whoever opens a link gets a small page with a **Download** button; from a
terminal the same link works with `curl -OJ LINK` or `wget --content-disposition LINK`. Downloads can
be paused and resumed, and any size works.

**kit has to keep running** while people download. Stop sharing (in the window) or Ctrl+C (in the
terminal) ends it.

## Who the links work for

| | |
|---|---|
| `--lan` *(default)* | devices on the same network - `http://192.168.x.x:8770/d/...` |
| `--internet` | anyone, anywhere - `https://<words>.trycloudflare.com/d/...`, through a free Cloudflare tunnel |
| `--public` | anyone, through this machine's own public address - `http://<public ip>:8770/d/...` |

**`--internet`** works behind any router or firewall that allows ordinary outgoing traffic: kit runs
Cloudflare's `cloudflared`, which dials out and gets a random public https address. No account is
needed. cloudflared is rarely installed, so the first time kit downloads the official release for this
machine (about 55 MB), checks it against the SHA-256 GitHub publishes, and keeps it in kit's data folder.
The address takes about 15 seconds to set up, and kit checks it works before showing a link. The file
passes through Cloudflare on the way (over https), and speed is limited by this machine's upload.

**`--public`** is for a server that already has a reachable address and an open port - no tunnel, no
third party. The port (`--port`, default 8770) has to be open in the firewall, and forwarded by the
router if there is one; kit can't check that from the inside. `--address example.com` puts a name in
the links instead of the looked-up IP.

## The window, or the terminal

`kit send` opens a small **sharing window**, and everything can be done there: add files, copy links,
show a QR code, switch between **This network** and **Internet**, change a link's expiry and download
limit, watch progress, remove a link, stop sharing. The window only controls kit - it can be closed
without stopping anything.

| Adding files | |
|---|---|
| **Choose files…** | your system's own file dialog (Windows dialog, zenity or kdialog on Linux, Finder on macOS) |
| **Browse…** | kit's own file browser: click through folders, or paste a path |
| **Drop** | drag files onto the window. The browser never says where a dropped file lives, so the window hands kit a copy, kept in a temp folder until kit stops |

**In a terminal only** - an SSH session on a server, say - kit doesn't open a window. Name the files,
and it prints each link with a QR code, shows progress as people download, and exits once every link is
used up or expired:

```
$ ssh my-server
$ kit send backup.tar.gz --internet --once
kit send  links reach anyone on the internet, through a Cloudflare tunnel
kit send  1 file, link lasts 2h 00m, one download each

  backup.tar.gz  1.2 GB
  https://quiet-river-sample-tone.trycloudflare.com/d/2JNqkudKvnUbGXJAIdUCFQ
```

kit picks the terminal by itself in an SSH session, or on Linux with no desktop (`DISPLAY` and
`WAYLAND_DISPLAY` both unset). `--no-ui` asks for it anywhere; `--ui` asks for the window anyway.
`--no-open` runs the window without opening it and prints its address - on a server, open that address
from your own PC through an SSH tunnel (`ssh -L PORT:127.0.0.1:PORT my-server`). The older `--headless`
and `--open` still work, meaning `--no-ui` and `--ui`.

## Telling your other machines

kit can send a link straight to your other machines, through [kit notify](../notify/README.md): it pops
up there, and clicking the popup opens the download page (on Windows; elsewhere the link is in the
message). Every machine that should get it runs `kit notify serve` or the notify window, and they all
share the same `notify.passphrase` - the one-time setup notify describes.

- **In the window:** **Notify…** on a link lists the machines kit finds on this network, all ticked;
  untick any, then **Send link**. Each machine gets a ✓ or a ✗ (hover it for why).
- **From a terminal:** `--notify` sends every link to every machine found, `--notify laptop,desk` only to
  those, by name or address. Put it after the files (`kit send photo.jpg --notify`); `--notify photo.jpg`
  works too - a file given there is shared, not taken for a machine name.

This machine is never on the list. Machines are found on this network only, even for an `--internet`
link - to reach one elsewhere, send it the link yourself.

## Limits and expiry

| | |
|---|---|
| `--expire 2h` | how long the link works (default `2h`; `90m`, `30s`, `1h30m` all work) |
| `--once` | the link stops after one completed download |
| `--max 5` | the link stops after five |

A download counts once its last byte has gone out, so a download that's interrupted and resumed counts
once. A link with a limit can't be *started* by more people than it has downloads left: a second person
opening a `--once` link while the first is still downloading is told to try again in a moment.

The window offers the same choices per link (30 minutes to 24 hours; once, a few, or no limit). They can
be changed until the first download, after which they're fixed. When files were named on the command
line, kit stops by itself once they're all done; when you're working in the window, it keeps running
until you stop it.

## Safety

- Each link carries a random secret (`/d/<22 characters>`). Without it there is nothing to find: the
  address kit shares answers only download links, never the window or its file browser.
- The window and everything that adds or browses files listen on `127.0.0.1` only, behind a token.
- With `--lan` or `--public` the links are plain `http`, so someone on the path could see the file go
  by. `--internet` is `https` to Cloudflare, and Cloudflare to kit is cloudflared's own encrypted tunnel.

## History

The window remembers what you shared - name, size, how many times it went out, and when - in
`%APPDATA%\kit\send-history.json` (`~/.local/share/kit/send-history.json` on Linux, or wherever
`KIT_SEND_DATA` points). Links' secrets are never written there. **Clear** empties it.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `send.mode` | `lan` | Who links work for: `lan` or `internet` |
| `send.expire` | `2h` | How long a link works, and what the window offers by default |
| `send.port` | `8770` | Port links use on the LAN or with `--public` (the next free one is used if it's taken) |
| `send.once` | `false` | Stop each link after one completed download |
| `send.window` | `true` | Open the sharing window as an app window instead of a browser tab |
| `send.cloudflared` | *(PATH, else downloaded)* | The cloudflared program `--internet` uses (`KIT_CLOUDFLARED`) |

```
kit config set send.mode internet
kit config set send.expire 30m
```

## Examples

```
kit send                                 # add files in the window
kit send report.pdf                      # a link for this network that lasts two hours
kit send report.pdf --internet --once    # a link that works anywhere, for one download
kit send *.jpg --expire 30m              # one link per file
kit send big.iso --public --port 8080    # a server with port 8080 open to the internet
kit send notes.txt --no-ui               # no window, even on a desktop
kit send slides.pdf --notify             # and pop the link up on your other machines
kit send movie.mp4 --notify laptop       # ...or just on the one called laptop
```
