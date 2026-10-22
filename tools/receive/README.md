# receive

Receive files from someone's browser: send them a link and a code, and what they upload lands in a folder here.

## Usage

```
kit receive [folder] [--lan | --internet] [--once] [--max-size SIZE] [--expire 1h] [--no-code] [--port N] [--no-qr]
```

kit prints a link (with a QR code) and a six-digit **code**. Send the link however you like, and tell the person
the code separately - a call, or a different app. They open the link, type the code, then drop files on the page
or choose them, and press **Send**. Each file shows up in the folder when its last byte arrives, and the terminal
says who sent what. Nothing needs installing on their side: any browser works, phones too.

Files go to `kit-received` in your Downloads folder, or the folder you name (`receive.folder` sets it for good).

**kit has to keep running** until the files arrive. Ctrl+C stops it.

## Who the link works for

| | |
|---|---|
| `--lan` *(default)* | devices on the same network - `http://192.168.x.x:8771/u/...` |
| `--internet` | anyone, anywhere - `https://<words>.trycloudflare.com/u/...`, through a free Cloudflare tunnel |

`--internet` works the same way as [kit send](../send/README.md)'s: no account, no open port, and the address
takes about 15 seconds to set up (kit checks it works before showing it). The first time, kit downloads
Cloudflare's `cloudflared` (about 55 MB, checked against its published SHA-256). Speed is set by the sender's
upload - a 4 GB movie takes about half an hour at 20 Mbit/s.

## Big files, and carrying on

The page sends a file in pieces (a few MB to 48 MB each, sized to the connection), so any size works - Cloudflare's
free tunnels refuse anything over 100 MB in one go, and a piece is all that's lost when a connection drops.

- **A dropped connection** - the page says so and keeps trying, then carries on where it stopped.
- **A closed tab, or a restarted browser** - they open the link again (the code again, unless it's the same tab),
  choose the **same file**, and press Send: it carries on from where it got to.
- **kit stopped before a file was finished** - what arrived is kept as `<name>.<id>.kit-part` in the folder.
  Run `kit receive` on the same folder again and have them send the same file with the new link and code: it
  carries on from there. Otherwise, delete the `.kit-part` file.

Before anything is sent, kit checks the files fit: under `--max-size`, and with room on the disk (it always
leaves a little free). A file that doesn't fit is refused straight away, not an hour in.

## When the link closes

| | |
|---|---|
| `--expire 1h` | the link takes new files for an hour (default `1h`; `30m`, `2h30m` work too). Files already on their way are let finish - a long upload doesn't need a long expiry |
| `--once` | the link closes once the first set of files has all arrived |
| 5 wrong codes | the link closes for good - someone may be guessing. Run `kit receive` again for a new link and code |
| Ctrl+C | stops it at once |

kit stops by itself once the link has closed and nothing is still arriving (an upload that has gone quiet for two
minutes isn't waited for).

## Safety

- **The link** carries a random secret (`/u/<22 characters>`), and the address answers nothing without it. There
  is no way to list or download what was received through it - it only takes files in.
- **The code** is asked for before anything is accepted, and checked before kit reads a byte of a file. It isn't
  part of the link, so it doesn't end up in chat previews or browser history. Five wrong tries, from anyone,
  close the link. `--no-code` (or `receive.code = false`) turns it off - then anyone who has the link can send
  files until it closes; fine for sending to yourself, not for a link you post somewhere.
- **File names** are cut down to a bare name: no folders (`../../x` arrives as `x`), no characters Windows or
  Linux refuse, no device names like `CON`. A file never replaces another: a second `report.pdf` is saved as
  `report (2).pdf`.
- **What's received can still be harmful to open.** Receiving it does nothing; opening it is the risk. On Windows
  kit marks every file as downloaded from the internet, as a browser would, so Windows asks before running a
  program and Office opens documents in Protected View. The terminal also points out programs, scripts and disk
  images. On Linux, received files are never executable.
- **On the way:** `--internet` is https from the sender to Cloudflare, then cloudflared's own encrypted tunnel to
  this machine - Cloudflare can see the file pass through. `--lan` is plain http on your network.
- kit listens on `127.0.0.1` only with `--internet` (only the tunnel can reach it), and on every network with
  `--lan`. The first time on a network, Windows Firewall asks whether Python may accept connections: allow it on
  **Private networks**. On Linux with ufw: `sudo ufw allow 8771/tcp`.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `receive.mode` | `lan` | Who the link works for: `lan` or `internet` |
| `receive.folder` | *(Downloads\kit-received)* | Where received files go |
| `receive.expire` | `1h` | How long the link takes new files |
| `receive.max_size` | *(none)* | Largest file accepted, e.g. `2GB` (empty: whatever fits on the disk) |
| `receive.code` | `true` | Ask for a one-time code before an upload |
| `receive.once` | `false` | Close the link after the first set of files has arrived |
| `receive.port` | `8771` | Port the link uses on the LAN (the next free one is used if it's taken) |
| `receive.cloudflared` | *(PATH, else downloaded)* | The cloudflared program `--internet` uses (`KIT_CLOUDFLARED`) |

```
kit config set receive.mode internet
kit config set receive.folder D:\incoming
```

## Examples

```
kit receive --internet                 # a link and a code for someone anywhere
kit receive --internet --once          # ...that closes once their files are in
kit receive D:\incoming --max-size 2GB # into another folder, nothing over 2 GB
kit receive --expire 15m               # someone on this network, for the next 15 minutes
kit receive --no-code                  # from your own phone, on this network
```
