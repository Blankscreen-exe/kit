# notify

Send a desktop notification to another PC on your LAN or tailnet.

## Usage

```
kit notify [--ui | --no-ui]
kit notify serve [--port N] [--rotate] [--discovery-port N]
kit notify send [<url>] "message" [--title TEXT] [--to NAMES] [--discovery-port N]
kit notify devices [--discovery-port N]
```

- `kit notify` on its own opens a window (see below). Where there's no desktop to show one on - an
  SSH session, or Linux without a display - it prints this help instead.
- `--ui` opens the window even where kit would print the help, `--no-ui` prints the help even on a desktop.
- `kit notify serve` listens for notifications, pops them up on this machine and prints each one in the
  terminal as it arrives (time, sender, title, message) - on a server with no desktop, that printed list
  is how you read them. It prints its own address, ending in `?t=...` - that's what the other machine
  sends to.
  It's always reachable on this network, and discoverable too once a passphrase is set (see below).
- `kit notify send <url> "message"` sends one, where `<url>` is exactly what `serve` printed on the
  other machine.
- `kit notify send "message"` - **no address** - finds every discoverable machine on this network
  by itself and sends to all of them. Nothing to copy or save, once set up (below).
- `--to laptop,desk` sends only to the machines named (by name as `devices` shows it, or by address)
  instead of all of them - the window's checklist, from a terminal. A name that isn't found stops it,
  listing what was.
- `kit notify devices` lists the machines discovery finds, with their addresses, marking this one.
- Run `serve` on every machine you want to be able to notify, and `send` from any of them.
- The token stays the same across restarts (kept on disk, not made fresh every time), so save the
  address once and it keeps working. `--rotate` deliberately replaces it - do that if it ever leaks.

### The window

`kit notify` does what `serve` does - listens on this network, pops up what arrives - and also opens
a small window (an app window in Edge/Chrome/Chromium, or a browser tab without one) with:

- a scrollable list of everything received (who from, when) and sent (to whom, with a ✓ or ✗ per
  device - hover a ✗ for why it didn't arrive);
- a message box, and a **To** checklist of the devices discovery finds: *All devices*, or any mix of
  them. It looks again every 30 seconds by itself, or straight away with *Look again*; a machine that
  stops answering drops off.

The list lives only as long as the window: closing it stops kit notify and forgets the list, and
Ctrl+C in the terminal does the same. Only one of `serve` or the window can run on a machine at a
time - the second one refuses to start. The device list needs `notify.passphrase` set (below);
without it the window still receives, but has nobody to send to.

### LAN discovery

Set the same passphrase on every machine that should find each other - once, ever:

```
kit config set notify.passphrase <make one up, same value on every machine>
```

With that set, a running `serve` or window is *discoverable* on this network -
`kit notify send` with no address broadcasts a signed "who's out there" query over UDP
(`notify.discovery_port`, default 8898), and a machine only answers if it can prove it knows the
same passphrase (an HMAC over a timestamp - the passphrase itself is never sent, only proof of it,
and a stale or reused proof past 30 seconds is rejected). Get the passphrase wrong, or don't set
one, and you get silence - not an error, so there's no way to tell "wrong passphrase" from
"nothing's listening" by probing.

Without a passphrase set, the machine is still directly reachable at its printed address - it
just isn't discoverable, and `serve` says so.

### Reaching a machine that isn't on the same LAN

Discovery only finds machines on the same LAN segment - it doesn't cross into a tailnet or reach a
different network. For that, launch `serve` through `kit share` instead, which does the same job
over your tailnet without you managing a second process:

```
kit share start notify --tailscale -- serve
```

Send it the address that prints (not discovery - that's LAN-only), from the other machine.

### Firewall

The first time `kit notify` or `kit notify serve` runs, Windows Firewall may ask whether Python may accept
connections, or simply block it silently if that prompt gets missed or dismissed. If another
machine can't find or reach you: allow Python on **Private networks** under *Windows Security >
Firewall & network protection > Allow an app through firewall*, or add rules directly (PowerShell,
as Administrator):

```powershell
New-NetFirewallRule -DisplayName "kit notify" -Direction Inbound -Protocol TCP -LocalPort 8899 -Action Allow
New-NetFirewallRule -DisplayName "kit notify discovery" -Direction Inbound -Protocol UDP -LocalPort 8898 -Action Allow
```

Also check the network is set to **Private**, not Public, under *Settings > Network & Internet* -
Public profiles block a lot more by default, including broadcast traffic that discovery relies on.
On Linux with ufw: `sudo ufw allow 8899/tcp` and `sudo ufw allow 8898/udp`.

## Examples

```
kit config set notify.passphrase house-of-blue-lights          # once, same value everywhere
kit notify                                                    # the window: send and receive
kit notify serve                                              # or just receive, from a terminal
kit notify send "build's done"                                # finds it automatically, same LAN
kit notify send "backup finished" --to laptop                 # only the machine called laptop
kit notify devices                                            # who's out there
kit notify send http://192.168.1.28:8899/?t=AbCd1234 "build's done"  # or, a specific address
kit share start notify --tailscale -- serve                    # across your tailnet instead
```

## Settings

| Setting | Default | What it does |
|---|---|---|
| `notify.port` | `8899` | Port `kit notify serve` listens on |
| `notify.discovery_port` | `8898` | UDP port used to find `kit notify` servers on the LAN |
| `notify.passphrase` | *(empty)* | Shared secret gating LAN discovery - same value on every machine, or discovery stays off |

## Notes

- Linux uses `notify-send` (part of most desktops' notification daemon). macOS uses `osascript`.
  Windows uses a WinForms tray balloon, needing nothing extra installed. If none of those work,
  the message is printed instead.
- There's no address book: from the command line you always pass the exact address `serve`
  printed, or let discovery find everyone.
- Each message carries the sender's hostname, so the popup reads "kit notify - from laptop-b" (when
  no `--title` was given) and the window lists who it came from. Messages from an older kit notify
  show the sender's IP address instead.
- `--lan` on `serve` is still accepted but does nothing - listening on the network is always on now.
- `serve` confirms delivery as soon as it's received a message, not once it's been shown or
  clicked - showing it (especially the clickable Windows balloon, which waits for that click)
  happens in the background, so a slow or unattended notification on one machine never holds up
  `send` or a broadcast to everyone else.
- A broadcast (`send` with no address) reaches every discovered device at the same time, not one
  after another - the whole thing takes as long as the single slowest one, not their sum.
