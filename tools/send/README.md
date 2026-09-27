# send

Send a file straight to someone's browser, peer to peer, with a link they just click.

## Usage

```
kit send                                     # open the sharing window and add files there
kit send <file> [<file>...] [--once | --max N] [--expire 2h] [--page URL]
kit send <file> [--lan] [--port N] [--no-open] [--no-qr] [--no-window]
```

`kit send` opens a small **sharing window** and everything happens there: add files, copy links, show a
QR code, watch progress, stop sharing. Naming files on the command line is a shortcut that shares them
straight away - the window still opens, and the terminal still lists the links.

When someone opens a link, the file goes **straight from your computer to their browser**. It is never
uploaded to any server, and nothing in the middle can read it. Both sides hash the file as it flows, so
a file that doesn't match is reported and not saved.

**The window has to stay open** while sending, the same way a video call needs its tab open. Closing it,
or pressing Ctrl+C in the terminal, ends sharing.

### Adding files in the window

| | |
|---|---|
| **Choose files…** | opens your system's own file dialog (Windows dialog, zenity or kdialog on Linux, Finder on macOS) |
| **Browse…** | kit's own file browser: click through folders, or paste a path. Works the same everywhere, and over a remote session |
| **Drop** | drag files onto the window |

A dropped file is held **by the window**, not by kit - the browser hands over the file's contents but
never tells the page where it lives on disk. Such a link is marked *held by this window* and stops
working if you reload the window; add the file again (or use Choose files) and you get a fresh link.

Each file gets its own link, with its own expiry and download limit. Both can be changed until the
first download, after which they're fixed and shown as facts.

### Making links that work outside your own computer

The recipient's page has to be somewhere they can reach. Out of the box kit serves it from your own
machine, so those links only work **on this computer** - enough to try it out. For real sending,
publish the receiver page once and point kit at it:

```
kit config set send.page https://you.github.io/kit/send/
```

The page is static (`receiver.html`, `receiver.js`, `sw.js`, `peerjs.min.js` in this folder) and can be
published anywhere - GitHub Pages is free. It never learns anything about your transfers: the peer id
and the link's token live after the `#`, which browsers never send to a web server.

`--lan` also serves the page to your own network, so another computer or a phone on the same Wi-Fi can
open it. A plain network address isn't a "secure context", which costs two things on that page:
browsers hold the file in memory instead of streaming it to disk, so keep to files under about 300 MB,
and they withhold the hashing the page uses to check a file after it arrives - it says so, and checks
the size instead. Both limits come from the address, not from kit: a published (https) page has
neither.

### Limits and expiry

| | |
|---|---|
| `--expire 2h` | how long the link works (default `2h`; `90m`, `30s`, `1h30m` all work) |
| `--once` | the link stops after one completed download |
| `--max 5` | the link stops after five |

The window offers the same choices per link (30 minutes to 24 hours; once, a few, or no limit), and
**Remove** closes a link early. When files were named on the command line, kit stops by itself once
they're all done; when you're working in the window, it keeps running until you stop it.

### History

The window remembers what you shared - name, size, how many times it went out, and when - in
`%APPDATA%\kit\send-history.json` (`~/.local/share/kit/send-history.json` on Linux/macOS, or wherever
`KIT_SEND_DATA` points). Links' secrets are never written there. **Clear** empties it.

## How it works, and what that means for you

- The **sharing window does the sending**, not Python. Browsers have a well-tested WebRTC stack; using
  it means no extra dependencies and a connection that works first time (24/24 in testing, against 4 of
  6 when Python did the sending).
- A **free public matchmaker** (the PeerJS cloud, the same one peerino uses) introduces the two
  browsers. It sees that a connection happened, never the file.
- Expect around **10 MB/s** - roughly 90 seconds per gigabyte. That's the data channel's ceiling, not
  your network's. For big files on your own Wi-Fi, `kit serve` is far faster.
- Some strict networks block direct connections entirely. If the recipient's page can't connect, try a
  different network - there is no relay to fall back on.

### Saving on the other end

The page picks the best method the recipient's browser has, and says which before starting:

| Browser | How it saves | Size limit |
|---|---|---|
| Chrome, Edge, Opera (desktop) | straight to disk, you pick where | none |
| Firefox, Safari 16+, others | a streamed download | none |
| Anything older | held in memory | offered below 300 MB only |

## Settings

| Setting | Default | What it does |
|---|---|---|
| `send.page` | *(kit's own copy)* | Receiver page the links point at. Set this once you've published it. |
| `send.expire` | `2h` | How long a link works, and what the window offers by default |
| `send.port` | `8770` | Port to try first (the next free one is used if it's taken) |
| `send.once` | `false` | Stop each link after one completed download |
| `send.window` | `true` | Open the sharing window as an app window instead of a browser tab |

```
kit config set send.page https://you.github.io/kit/send/
kit config set send.expire 30m
```

## Examples

```
kit send                                 # add files in the window
kit send report.pdf                      # a link that lasts two hours
kit send report.pdf --once               # one download, then the link dies
kit send *.jpg --expire 30m              # one link per file
kit send video.mp4 --lan                 # another device on this Wi-Fi can load the page too
kit send big.zip --no-open               # don't open the window for me (I'll open the printed address)
```
