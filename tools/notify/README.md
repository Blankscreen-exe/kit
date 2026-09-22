# notify

Send a desktop notification to another PC on your LAN or tailnet.

## Usage

```
kit notify serve [--lan] [--port N]
kit notify send <url> "message" [--title TEXT]
```

- `kit notify serve` listens for notifications and pops them up on this machine. It prints its own
  address, ending in `?t=...` - that's what the other machine sends to.
  Local-only by default; `--lan` also binds this network.
- `kit notify send <url> "message"` sends one, where `<url>` is exactly what `serve` printed on the
  other machine.
- Run `serve` on every machine you want to be able to notify, and `send` from any of them, pointed
  at another's address.
- Each `serve` picks a fresh token every time it starts, so an old address stops working once you
  restart it - grab the new one it prints.

### Reaching another machine

`kit notify serve` alone is LAN-shareable (`--lan`) the same way any kit web tool is. For a machine
that isn't on the same LAN, launch it through `kit share` instead, which does the same thing over
your tailnet without you managing a second process:

```
kit share start notify --tailscale -- serve
```

Send it the address that prints, from the other machine.

## Examples

```
kit notify serve --lan                                        # on the receiving PC
kit notify send http://192.168.1.28:8899/?t=AbCd1234 "build's done"
kit share start notify --tailscale -- serve                    # across your tailnet instead
```

## Settings

| Setting | Default | What it does |
|---|---|---|
| `notify.port` | `8899` | Port `kit notify serve` listens on |

## Notes

- Linux uses `notify-send` (part of most desktops' notification daemon). macOS uses `osascript`.
  Windows uses a WinForms tray balloon, needing nothing extra installed. If none of those work,
  the message is printed instead.
- There's no address book: you always pass the exact address `serve` printed.
