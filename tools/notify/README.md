# notify

Send a desktop notification to another PC on your LAN or tailnet.

## Usage

```
kit notify serve [--lan] [--port N] [--rotate] [--discovery-port N]
kit notify send [<url>] "message" [--title TEXT] [--discovery-port N]
```

- `kit notify serve` listens for notifications and pops them up on this machine. It prints its own
  address, ending in `?t=...` - that's what the other machine sends to.
  Local-only by default; `--lan` also binds this network **and** makes it discoverable (see below).
- `kit notify send <url> "message"` sends one, where `<url>` is exactly what `serve` printed on the
  other machine.
- `kit notify send "message"` - **no address** - finds every discoverable machine on this network
  by itself and sends to all of them. Nothing to copy or save, once set up (below).
- Run `serve` on every machine you want to be able to notify, and `send` from any of them.
- The token stays the same across restarts (kept on disk, not made fresh every time), so save the
  address once and it keeps working. `--rotate` deliberately replaces it - do that if it ever leaks.

### LAN discovery

Set the same passphrase on every machine that should find each other - once, ever:

```
kit config set notify.passphrase <make one up, same value on every machine>
```

With that set, `--lan` means two things: reachable on this network, and *discoverable* on it -
`kit notify send` with no address broadcasts a signed "who's out there" query over UDP
(`notify.discovery_port`, default 8898), and a machine only answers if it can prove it knows the
same passphrase (an HMAC over a timestamp - the passphrase itself is never sent, only proof of it,
and a stale or reused proof past 30 seconds is rejected). Get the passphrase wrong, or don't set
one, and you get silence - not an error, so there's no way to tell "wrong passphrase" from
"nothing's listening" by probing.

Without a passphrase set, `--lan` still makes the machine directly reachable at its printed
address, same as always - it just isn't discoverable, and `serve` says so.

### Reaching a machine that isn't on the same LAN

Discovery only finds machines on the same LAN segment - it doesn't cross into a tailnet or reach a
different network. For that, launch `serve` through `kit share` instead, which does the same job
over your tailnet without you managing a second process:

```
kit share start notify --tailscale -- serve
```

Send it the address that prints (not discovery - that's LAN-only), from the other machine.

### Firewall

The first time `kit notify serve --lan` runs, Windows Firewall may ask whether Python may accept
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
kit notify serve --lan                                        # on the receiving PC
kit notify send "build's done"                                # finds it automatically, same LAN
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
- There's no address book: you always pass the exact address `serve` printed.
- `serve` confirms delivery as soon as it's received a message, not once it's been shown or
  clicked - showing it (especially the clickable Windows balloon, which waits for that click)
  happens in the background, so a slow or unattended notification on one machine never holds up
  `send` or a broadcast to everyone else.
- A broadcast (`send` with no address) reaches every discovered device at the same time, not one
  after another - the whole thing takes as long as the single slowest one, not their sum.
