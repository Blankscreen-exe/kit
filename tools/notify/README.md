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
- `kit notify send "message"` - **no address** - finds every `--lan` machine on this network by
  itself and sends to all of them. Nothing to copy or save.
- Run `serve` on every machine you want to be able to notify, and `send` from any of them.
- The token stays the same across restarts (kept on disk, not made fresh every time), so save the
  address once and it keeps working. `--rotate` deliberately replaces it - do that if it ever leaks.

### LAN discovery

`--lan` means two things: reachable on this network, and *discoverable* on it - `kit notify send`
with no address broadcasts a "who's out there" query over UDP (`notify.discovery_port`, default
8898) and sends to whatever answers. A machine only answers if it was started with `--lan`; one
that wasn't is exactly as invisible to discovery as it already is to being reached directly - being
discoverable isn't a separate risk from being `--lan` in the first place, it's the same one.

One real consequence of that: a discoverable machine hands out its token to anyone who asks, in the
clear, over UDP. That's fine on a home network you already trust enough to run `--lan` on, and no
different from what `--lan` already meant - but it does mean the token stops being a secret on that
network the moment discovery is on, so don't rely on it as a barrier against other people on a
shared or public network the way you might with `--tailscale`'s address instead.

### Reaching a machine that isn't on the same LAN

Discovery only finds machines on the same LAN segment - it doesn't cross into a tailnet or reach a
different network. For that, launch `serve` through `kit share` instead, which does the same job
over your tailnet without you managing a second process:

```
kit share start notify --tailscale -- serve
```

Send it the address that prints (not discovery - that's LAN-only), from the other machine.

## Examples

```
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

## Notes

- Linux uses `notify-send` (part of most desktops' notification daemon). macOS uses `osascript`.
  Windows uses a WinForms tray balloon, needing nothing extra installed. If none of those work,
  the message is printed instead.
- There's no address book: you always pass the exact address `serve` printed.
