# send

Send a file straight to someone's browser, peer to peer, with a link they just click.

## Usage

```
kit send <file> [<file>...] [--once | --max N] [--expire 2h] [--page URL]
kit send <file> [--lan] [--port N] [--no-open] [--no-qr] [--no-window]
```

- `kit send holiday.mp4` prints a link and opens a small **sharing window**. Send the link to anyone.
  When they open it, the file goes **straight from your computer to their browser** - it is never
  uploaded to any server, and nothing in the middle can read it.
- Each file gets its own link. The window shows progress, who's connected and a Copy button.
- **The window has to stay open** while sending, the same way a video call needs the tab open. Closing
  it, or pressing Ctrl+C in the terminal, ends sharing.
- Every transfer is checked: both sides hash the file as it flows, and a file that doesn't match is
  reported and not saved.

### Making links that work outside your own computer

The recipient's page has to be somewhere they can reach. Out of the box kit serves it from your own
machine, so those links only work **on this computer** - enough to try it out. For real sending,
publish the receiver page once and point kit at it:

```
kit config set send.page https://you.github.io/kit/send/r
```

The page is static (`receiver.html`, `receiver.js`, `sw.js`, `peerjs.min.js` in this folder) and can be
published anywhere - GitHub Pages is free. It never learns anything about your transfers: the peer id
and the link's token live after the `#`, which browsers never send to a web server.

`--lan` also serves the page to your own network, so a phone on the same Wi-Fi can scan the QR code.

### Limits and expiry

| | |
|---|---|
| `--expire 2h` | how long the link works (default `2h`; `90m`, `30s`, `1h30m` all work) |
| `--once` | the link stops after one completed download |
| `--max 5` | the link stops after five |

kit stops sharing by itself when every link is finished or expired.

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
| `send.expire` | `2h` | How long a link works |
| `send.port` | `8770` | Port to try first (the next free one is used if it's taken) |
| `send.once` | `false` | Stop each link after one completed download |
| `send.window` | `true` | Open the sharing window as an app window instead of a browser tab |

```
kit config set send.page https://you.github.io/kit/send/r
kit config set send.expire 30m
```

## Examples

```
kit send report.pdf                      # a link that lasts two hours
kit send report.pdf --once               # one download, then the link dies
kit send *.jpg --expire 30m              # one link per file
kit send video.mp4 --lan                 # phones on this Wi-Fi can load the page too
kit send big.zip --no-open               # don't open the window for me (I'll open the printed address)
```
